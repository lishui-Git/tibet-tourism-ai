# -*- coding: utf-8 -*-
"""验证 `--stage facts` 的两项承诺（答辩演示手册 §六 步骤③）。

承诺：① 评论语义 → 事实包 **0 次模型调用**；② 重复执行**幂等跳过**、不产生多余开销。

做法（全程零 API 消费）：
    · 统计核心表基线；
    · 对**单个景点**跑一次 `run_fact_package`（真实执行、写库）；
    · 再跑一次，验证幂等跳过；
    · 精确清理本次写入，并证明行数回到基线。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.batch.fact_package import run_fact_package
from app.db import connection, query_one

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def counts() -> dict[str, int]:
    row = query_one(
        "SELECT (SELECT COUNT(*) FROM spot_fact_package) AS pkgs, "
        "       (SELECT COUNT(*) FROM spot_report) AS reports, "
        "       (SELECT COUNT(*) FROM analysis_task) AS tasks, "
        "       (SELECT COUNT(*) FROM sentiment WHERE method='deepseek') AS deepseek"
    )
    return {k: int(v) for k, v in row.items()}


def main() -> int:
    print("=" * 84)
    print("验证 `--stage facts`：0 次模型调用 + 幂等跳过（零 API 消费）")
    print("=" * 84)

    before = counts()
    print(f"\n基线：{before}")

    spot = query_one(
        "SELECT spot_id, spot_name FROM spot WHERE has_full_evaluation=1 ORDER BY spot_id LIMIT 1"
    )
    spot_id = int(spot["spot_id"])
    print(f"测试景点：{spot['spot_name']}({spot_id})")

    # ---------- ① 首次生成 ----------
    first = run_fact_package(spot_ids=[spot_id], only_missing=True)
    check("首次生成成功", first["generated"] == 1, f"generated={first['generated']} skipped={first['skipped']}")
    mid = counts()
    check("事实包行数 +1", mid["pkgs"] == before["pkgs"] + 1, f"{before['pkgs']} → {mid['pkgs']}")
    check("未写入任何 spot_report（事实包阶段不产出评价）", mid["reports"] == before["reports"], f"{mid['reports']}")
    check("评论级结果未被改动（事实包只读评论级结果）", mid["deepseek"] == before["deepseek"],
          f"deepseek={mid['deepseek']}")

    # ---------- ② 重复执行：幂等跳过 ----------
    second = run_fact_package(spot_ids=[spot_id], only_missing=True)
    check("重复执行幂等跳过（generated=0 / skipped=1）",
          second["generated"] == 0 and second["skipped"] == 1,
          f"generated={second['generated']} skipped={second['skipped']}")
    after = counts()
    check("重复执行后事实包行数不变", after["pkgs"] == mid["pkgs"], f"{mid['pkgs']} → {after['pkgs']}")

    # ---------- ③ 证明"0 次模型调用" ----------
    print("\n[静态证明] 事实包不调用模型")
    import app.batch.fact_package as fp
    src = open(fp.__file__, encoding="utf-8").read()
    check("源码中没有导入任何 LLM 客户端",
          "DeepSeekClient" not in src and "app.llm.client" not in src and "requests" not in src,
          "未出现 DeepSeekClient / app.llm.client / requests")
    check("源码中只从 app.llm 取常量（ASPECT_CANDIDATES）",
          src.count("from app.llm") == 1 and "ASPECT_CANDIDATES" in src,
          "仅 import 候选方面常量")

    # ---------- 清理 ----------
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM spot_fact_package WHERE spot_id=%s", (spot_id,))
            for task_id in {int(first.get("task_id") or 0), int(second.get("task_id") or 0)}:
                if task_id:
                    cur.execute("DELETE FROM task_log WHERE task_id=%s", (task_id,))
                    cur.execute("DELETE FROM analysis_task WHERE task_id=%s", (task_id,))
    cleaned = counts()
    check("清理后回到基线（核心表逐项一致）", cleaned == before,
          f"{before} → {cleaned}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 84)
    print(f"事实包验证：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 84)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
