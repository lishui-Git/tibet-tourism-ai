# -*- coding: utf-8 -*-
"""断点续跑实证：**游标就是结果表本身**（零 API 调用）。

## 为什么需要它
"中途 Ctrl+C 后能安全继续、只处理未完成部分"是长任务书明确要求的一条保险机制。
此前对它的信心来自**读代码**（`WHERE NOT EXISTS(...method='deepseek')`）与
"重启后计数看起来对"——但**没有一次实验能直接证明游标在起作用**。

本测试用一个可逆的小动作把它变成证据：
    ① 记录当前计划调用数与已完成数；
    ② 挑一条**当前待处理**的评论，只插入 **1 行 `sentiment`**（伪造"它做完了"）；
    ③ 重新跑 `--dry-run`（**零调用**）：
       断言 计划调用 **-1**、已完成 **+1**；
    ④ 收尾删掉那行，断言行数回到基线。

如果游标不是"以结果表为准"，第 ③ 步的数字就不会动——
**改动量恰好为 1**，是比"数字看起来对"强得多的证据。
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.batch.semantic_analysis import run_semantic_analysis
from app.db import connection, query_one

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def scalar(sql: str, params: tuple = ()) -> int:
    return int(query_one(sql, params)["n"])


def pick_pending_comment() -> tuple[int, int]:
    """挑一条**待处理**的调用层评论（有内容、正文 >10 字、尚无任何语义结果）。"""
    row = query_one(
        """
        SELECT r.comment_id, r.spot_id
          FROM review r
         WHERE TRIM(COALESCE(r.content, '')) <> ''
           AND CHAR_LENGTH(TRIM(r.content)) > 10
           AND NOT EXISTS (SELECT 1 FROM sentiment s
                            WHERE s.comment_id = r.comment_id AND s.method = 'deepseek')
           AND NOT EXISTS (SELECT 1 FROM comment_semantic cs WHERE cs.comment_id = r.comment_id)
         ORDER BY r.comment_id DESC
         LIMIT 1
        """
    )
    return int(row["comment_id"]), int(row["spot_id"])


def main() -> int:
    print("=" * 88)
    print("断点续跑实证：游标 = 结果表本身（零 API 调用）")
    print("=" * 88)

    base_total = scalar("SELECT COUNT(*) AS n FROM sentiment WHERE method='deepseek'")
    print(f"\n基线：sentiment(deepseek) = {base_total:,} 行")

    before = run_semantic_analysis(dry_run=True)
    print(f"起点：计划调用 {before['api_calls_planned']:,}；已完成 {before['already_done_in_db']:,}")
    check("起点有 >0 条待处理（否则本测试没有意义）", before["api_calls_planned"] > 0,
          f"{before['api_calls_planned']:,}")

    cid, sid = pick_pending_comment()
    print(f"\n选中待处理评论 comment_id={cid}（景点 {sid}）")
    check("该评论此刻确实没有任何 deepseek 结果",
          scalar("SELECT COUNT(*) AS n FROM sentiment WHERE comment_id=%s AND method='deepseek'",
                 (cid,)) == 0, f"comment_id={cid}")

    try:
        # 只插一行：让"游标"认为这条已处理
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sentiment (comment_id, spot_id, method, polarity, intensity, created_at) "
                    "VALUES (%s, %s, 'deepseek', 'neutral', 1, NOW())",
                    (cid, sid),
                )

        after = run_semantic_analysis(dry_run=True)
        print(f"\n插一行后：计划调用 {after['api_calls_planned']:,}；已完成 {after['already_done_in_db']:,}")

        check("计划调用恰好减少 1（游标认结果表，不是认时间戳/日志）",
              after["api_calls_planned"] == before["api_calls_planned"] - 1,
              f"{before['api_calls_planned']:,} → {after['api_calls_planned']:,}")
        check("已完成数恰好增加 1",
              after["already_done_in_db"] == before["already_done_in_db"] + 1,
              f"{before['already_done_in_db']:,} → {after['already_done_in_db']:,}")
        check("dry-run 本身不产生任何写入（续跑验证不花钱也不脏库）",
              scalar("SELECT COUNT(*) AS n FROM sentiment WHERE method='deepseek'") == base_total + 1,
              "插入的 1 行是我们自己插的，dry-run 未额外写入")
    finally:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sentiment WHERE comment_id=%s AND method='deepseek'", (cid,))

        restored = scalar("SELECT COUNT(*) AS n FROM sentiment WHERE method='deepseek'")
        check("收尾后行数回到基线（未污染真实数据）", restored == base_total,
              f"{base_total:,} → {restored:,}")

        final = run_semantic_analysis(dry_run=True)
        check("计划调用数与起点一致（证明前面的变化只是那 1 行造成的）",
              final["api_calls_planned"] == before["api_calls_planned"],
              f"{final['api_calls_planned']:,} vs {before['api_calls_planned']:,}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"断点续跑实证：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：中间中断后重跑只会处理未完成部分——已完成的评论被 SQL 游标自动跳过，")
        print("      因此 Ctrl+C 之后直接重跑同一命令即可，不会重复付费。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
