# -*- coding: utf-8 -*-
"""恢复补写测试（`--stage replay`）：已付费结果能零成本写回数据库。

## 为什么需要它
`write_recovery` 只做了"把付费结果落盘"这一半；**另一半（读回来补写）此前不存在**——
`load_recovery()` 与 `payload_to_record()` 写好了、docstring 写着"补写用"，
却没有任何生产入口调用（只有测试在用）。也就是说恢复文件是**只写不读**的，
真要出事后得手工处理。本测试验证新补的 `--stage replay` 链路真的能用：

    ① 造一个"写库失败落盘"的场景，确认恢复文件里确实有**已付费结果**；
    ② 跑 `--dry-run`：只报告、不写库、不删文件；
    ③ 跑实际补写：结果进入数据库；
    ④ 补写成功后**文件被删除**（凭证已兑现）；
    ⑤ 全程 **零模型调用**（replay 不持有客户端）。

## 数据安全
用一条真实存在的评论 id，但**先快照、后恢复**（与 `test_write_recovery.py` 同法）；
结束时逐字段还原并断言行数回到基线，绝不留痕。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.batch.replay import replay_all, replay_semantic
from app.batch.write_recovery import RECOVERY_DIR, persist_recovery, recovery_path
from app.db import connection, query_all, query_one

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def pick_comment_id() -> int:
    """挑一条**真实存在**的评论（补写必须有真外键）。"""
    row = query_one(
        "SELECT comment_id FROM review WHERE content IS NOT NULL AND TRIM(content) <> '' "
        "ORDER BY comment_id LIMIT 1"
    )
    return int(row["comment_id"])


def snapshot_comment(cid: int) -> dict:
    sent = query_one(
        "SELECT * FROM sentiment WHERE comment_id=%s AND method='deepseek'", (cid,)
    )
    sem = query_one("SELECT * FROM comment_semantic WHERE comment_id=%s", (cid,))
    aspects = query_all("SELECT * FROM aspect WHERE comment_id=%s ORDER BY id", (cid,))
    return {"sentiment": sent, "semantic": sem, "aspects": aspects}


def restore(cid: int, snap: dict) -> None:
    """恢复到快照状态（逐字段写回；不存在的则删除）。"""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM aspect WHERE comment_id=%s", (cid,))
            cur.execute("DELETE FROM comment_semantic WHERE comment_id=%s", (cid,))
            cur.execute("DELETE FROM sentiment WHERE comment_id=%s AND method='deepseek'", (cid,))
            if snap["sentiment"]:
                cols = [k for k in snap["sentiment"] if k not in ("sentiment_id",)]
                ph = ",".join(["%s"] * len(cols))
                cur.execute(
                    f"INSERT INTO sentiment ({','.join(cols)}) VALUES ({ph})",
                    tuple(snap["sentiment"][c] for c in cols),
                )
            if snap["semantic"]:
                cols = [k for k in snap["semantic"] if k not in ("semantic_id",)]
                ph = ",".join(["%s"] * len(cols))
                cur.execute(
                    f"INSERT INTO comment_semantic ({','.join(cols)}) VALUES ({ph})",
                    tuple(snap["semantic"][c] for c in cols),
                )
            for row in snap["aspects"]:
                cols = [k for k in row if k != "aspect_id"]
                ph = ",".join(["%s"] * len(cols))
                cur.execute(
                    f"INSERT INTO aspect ({','.join(cols)}) VALUES ({ph})",
                    tuple(row[c] for c in cols),
                )


def counts() -> tuple[int, int, int]:
    s = int(query_one("SELECT COUNT(*) AS n FROM sentiment WHERE method='deepseek'")["n"])
    m = int(query_one("SELECT COUNT(*) AS n FROM comment_semantic")["n"])
    a = int(query_one("SELECT COUNT(*) AS n FROM aspect")["n"])
    return s, m, a


def main() -> int:
    print("=" * 86)
    print("恢复补写测试（--stage replay，零模型调用）")
    print("=" * 86)

    cid = pick_comment_id()
    before = snapshot_comment(cid)
    base_counts = counts()
    print(f"\n样本 comment_id={cid}；基线行数 sentiment/comment_semantic/aspect = {base_counts}")

    # 从快照里造一份"已付费结果"的 payload（结构同 _build_api_record 的产物）
    if before["sentiment"]:
        source = before["sentiment"]
        polarity = source.get("polarity") or "neutral"
    else:
        polarity = "neutral"
    payload = {
        "comment_id": cid,
        "spot_id": int(
            (query_one("SELECT spot_id FROM review WHERE comment_id=%s", (cid,)) or {}).get("spot_id") or 0
        ),
        "result": {
            "polarity": polarity,
            "intensity": 2,
            "is_valid": 1,
            "aspects": [{"aspect": "风景", "polarity": "positive", "evidence": "补写测试"}],
            "keywords": ["补写测试"],
            "summary": "补写测试用摘要",
            "dropped_aspects": [],
        },
        # 注意：`source` 是**顶层**字段（与 `_build_api_record` 的产物一致）。
        # 首版我把它放进 raw 里，补写时 `comment_semantic.source` 就成了 NULL，
        # 被非空约束拦下（IntegrityError: Column 'source' cannot be null）——
        # 这说明"构造测试数据"也必须照着真实产物来，不能凭印象。
        "source": "deepseek",
        "raw": {
            "source": "deepseek",
            "mode": "real",
            "prompt_version": "p1",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    }

    try:
        # ---- ① 造恢复文件 ----
        print("\n[1] 造一个恢复文件（模拟写库失败后的落盘）")
        path = persist_recovery("semantic", [{"text": f"comment_id={cid}", "payload": payload}], cid)
        check("恢复文件已生成", bool(path and path.exists()), str(path.name if path else None))
        loaded = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[0])
        check("文件里确实保存了已付费结果（payload.result 存在）",
              "result" in loaded.get("payload", {}), f"keys={sorted(loaded.get('payload', {}).keys())}")
        # 清掉该评论现有结果，制造"需要补写"的局面
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM aspect WHERE comment_id=%s", (cid,))
                cur.execute("DELETE FROM comment_semantic WHERE comment_id=%s", (cid,))
                cur.execute("DELETE FROM sentiment WHERE comment_id=%s AND method='deepseek'", (cid,))
        check("已清空该评论结果，处于待补写状态",
              query_one("SELECT COUNT(*) AS n FROM sentiment WHERE comment_id=%s AND method='deepseek'", (cid,))["n"] == 0,
              "")

        # ---- ② dry-run 只报告 ----
        print("\n[2] --dry-run：只报告，不写库、不删文件")
        dry = replay_semantic(dry_run=True)
        check("dry-run 报告了待补条数", dry.entries_total >= 1, f"entries_total={dry.entries_total}")
        check("dry-run 未写库",
              query_one("SELECT COUNT(*) AS n FROM sentiment WHERE comment_id=%s AND method='deepseek'", (cid,))["n"] == 0,
              "")
        check("dry-run 未删文件", recovery_path("semantic", cid).exists(), "")

        # ---- ③ 实际补写 ----
        print("\n[3] 实际补写：结果进入数据库")
        res = replay_semantic(dry_run=False)
        sent_row = query_one("SELECT * FROM sentiment WHERE comment_id=%s AND method='deepseek'", (cid,))
        sem_row = query_one("SELECT * FROM comment_semantic WHERE comment_id=%s", (cid,))
        asp_rows = query_all("SELECT * FROM aspect WHERE comment_id=%s", (cid,))
        check("sentiment 已补写", sent_row is not None, f"polarity={sent_row and sent_row.get('polarity')}")
        check("comment_semantic 已补写", sem_row is not None, f"source={sem_row and sem_row.get('source')}")
        check("aspect 已补写", len(asp_rows) >= 1, f"{len(asp_rows)} 行")
        check("补写结果标记为 real（不是 mock）",
              "mock" not in json.dumps(sent_row, ensure_ascii=False, default=str),
              json.dumps(sent_row.get("raw_json") if sent_row else {}, ensure_ascii=False, default=str)[:70])

        # ---- ④ 成功后删文件 ----
        print("\n[4] 补写成功后恢复文件被删除（凭证已兑现）")
        check("恢复文件已删除", not recovery_path("semantic", cid).exists(),
              f"files_removed={res.files_removed}")
        check("报告里记录了删除动作", bool(res.files_removed), f"{res.files_removed}")

        # ---- ⑤ 零模型调用 ----
        print("\n[5] 补写不持有客户端（因此不可能调用模型）")
        import inspect
        from app.batch import replay as replay_mod

        src = inspect.getsource(replay_mod)
        check("replay 模块不导入任何 LLM 客户端",
              "ChatClient" not in src and "app.llm.client" not in src, "")
        check("replay 模块不含 requests / HTTP 调用",
              "requests" not in src and "http" not in src.lower(), "")

    finally:
        # ---- 收尾：恢复快照 + 清恢复文件 ----
        print("\n[收尾] 还原数据并清理")
        restore(cid, before)
        for p in RECOVERY_DIR.glob("semantic_*.jsonl"):
            p.unlink()
        if RECOVERY_DIR.exists() and not any(RECOVERY_DIR.iterdir()):
            RECOVERY_DIR.rmdir()
        after_counts = counts()
        check("行数回到基线（未污染真实数据）", after_counts == base_counts,
              f"{base_counts} → {after_counts}")
        check("恢复目录已清理", not RECOVERY_DIR.exists() or not any(RECOVERY_DIR.iterdir()), "")
        replay_all(dry_run=True)  # 只是确认还能正常报告

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 86)
    print(f"恢复补写测试：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：恢复文件不再是『只写不读』——已付费结果可以零成本补写回库，成功后才删凭证。")
    print("=" * 86)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
