# -*- coding: utf-8 -*-
"""验证 `--stage facts` 的两项承诺（答辩演示手册 §六 步骤③）。

承诺：① 评论语义 → 事实包 **0 次模型调用**；② 重复执行**幂等跳过**、不产生多余开销。

做法（全程零 API 消费）：
    · 对**单个景点**做**隔离**：把该景点"测试前"的事实包快照下来并临时清空，
      使"首次生成"在**任何**库状态下都可测（表为空 / 已跑完全量都行）；
    · 真实执行一次 `run_fact_package`（写库），再执行一次验证幂等跳过；
    · 清掉本测试登记的任务；
    · 出隔离块时把快照**原样写回**——`spot_fact_package` 的生产结果**绝不会被删除或覆盖**。

【为什么必须隔离】旧实现按 `spot_id` 无条件 `DELETE FROM spot_fact_package`。
在表为空时看不出问题，但 Stage 5 生成 57 份生产事实包后，它会把真实结果删掉
（并让"回到基线"的断言失败）。这与 Stage 4 踩过的"测试删掉生产复用行"是同一类事故。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.batch.fact_package import run_fact_package
from app.db import connection, query_one
from scripts.result_isolation import isolated_spot_results

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


def run_checks(spot_id: int, base: dict[str, int]) -> None:
    """隔离块内的检查；`base` 是"该景点已被临时清空"之后的基线。"""
    # ---------- ① 首次生成 ----------
    first = run_fact_package(spot_ids=[spot_id], only_missing=True)
    check("首次生成成功（该景点已被隔离清空，因此必然是 new）",
          first["generated"] == 1 and first["skipped"] == 0,
          f"generated={first['generated']} skipped={first['skipped']}")
    mid = counts()
    check("事实包行数 +1", mid["pkgs"] == base["pkgs"] + 1, f"{base['pkgs']} → {mid['pkgs']}")
    check("未写入任何 spot_report（事实包阶段不产出评价）",
          mid["reports"] == base["reports"], f"{mid['reports']}")
    check("评论级结果未被改动（事实包只读评论级结果）",
          mid["deepseek"] == base["deepseek"], f"deepseek={mid['deepseek']}")

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

    # ---------- ④ 只清本测试登记的任务；景点级结果由隔离层精确还原 ----------
    with connection() as conn:
        with conn.cursor() as cur:
            for task_id in {int(first.get("task_id") or 0), int(second.get("task_id") or 0)}:
                if task_id:
                    cur.execute("DELETE FROM task_log WHERE task_id=%s", (task_id,))
                    cur.execute("DELETE FROM analysis_task WHERE task_id=%s", (task_id,))
    cleaned = counts()
    check("任务登记已清理（行数回到隔离后基线）",
          cleaned["tasks"] == base["tasks"], f"{base['tasks']} → {cleaned['tasks']}")
    check("本测试只新增 1 份事实包（收尾由隔离层还原为测试前状态）",
          cleaned["pkgs"] == base["pkgs"] + 1, f"{base['pkgs']} → {cleaned['pkgs']}")


def main() -> int:
    print("=" * 84)
    print("验证 `--stage facts`：0 次模型调用 + 幂等跳过（零 API 消费）")
    print("=" * 84)

    spot = query_one(
        "SELECT spot_id, spot_name FROM spot WHERE has_full_evaluation=1 ORDER BY spot_id LIMIT 1"
    )
    spot_id = int(spot["spot_id"])
    print(f"\n测试景点：{spot['spot_name']}({spot_id})")

    before_all = counts()
    print(f"隔离前基线：{before_all}")

    with isolated_spot_results([spot_id]):
        base = counts()
        print(f"隔离后基线（该景点的事实包已临时清空）：{base}")
        run_checks(spot_id, base)

    after_all = counts()
    check("收尾后回到隔离前状态（生产事实包被**精确还原**，未被删除）",
          after_all == before_all, f"{before_all} → {after_all}")

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
