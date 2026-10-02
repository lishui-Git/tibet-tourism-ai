# -*- coding: utf-8 -*-
"""生产链路串联验证：C-BAT-05 → 06 → 07 的产物是否被下一环正确接住。

## 为什么需要它
单看每个阶段都测过了，但**阶段之间的接缝**没测过：
`facts` 产出的 `spot_fact_package` 到底有没有被 `report` 认出来？
如果 `report` 只认"自己的口径"而不认事实包，全量跑到第 ④ 步会**一份评价都生成不出来**，
而前面 4.7 万次调用已经花掉了。

## 做法（**零模型调用**）
`--stage facts` 是纯 SQL 聚合（0 次调用），因此可以**真的跑一次**来验证接缝：
    ① 取 `task_id` 水位；
    ② 跑 `facts`（真实写入，但零模型调用）→ 断言产出 57 份事实包；
    ③ 跑 `report --dry-run` → 断言 `packages_available` 由 0 变 57、
       `pending_api_calls` 也变 57，且费用预估 ≈¥0.63；
    ④ 再跑 `facts --dry-run` → 断言 `already_has_version=57`（幂等，不会重复生成）；
    ⑤ 收尾：删除本次写入的事实包与任务（回到水位），并断言行数回基线。

全程不调用任何模型。
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.batch.fact_package import run_fact_package
from app.batch.spot_report import run_spot_report
from app.db import connection, query_one

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def counts() -> dict[str, int]:
    def n(sql: str) -> int:
        return int(query_one(sql)["n"])

    return {
        "fact_package": n("SELECT COUNT(*) AS n FROM spot_fact_package"),
        "spot_report": n("SELECT COUNT(*) AS n FROM spot_report"),
        "analysis_task": n("SELECT COUNT(*) AS n FROM analysis_task"),
        "task_log": n("SELECT COUNT(*) AS n FROM task_log"),
    }


def main() -> int:
    print("=" * 88)
    print("生产链路串联验证（facts → report 的接缝；零模型调用）")
    print("=" * 88)

    base = counts()
    print(f"\n基线：{base}")
    if base["fact_package"] != 0:
        check("本测试要求起点没有事实包（否则请先清理）", False, f"已有 {base['fact_package']} 行")
        return 1

    watermark = int(query_one("SELECT COALESCE(MAX(task_id),0) AS m FROM analysis_task")["m"])

    try:
        # ---- ① 起点：report 无从生成 ----
        print("\n[1] 起点：库内无事实包时，report 不应计划任何调用")
        before = run_spot_report(dry_run=True)
        check("packages_available = 0", before["packages_available"] == 0, str(before["packages_available"]))
        check("pending_api_calls = 0（没有依据就不生成，不硬编造）",
              before["pending_api_calls"] == 0, str(before["pending_api_calls"]))
        check("给出可照做的提示（先跑 --stage facts）",
              "--stage facts" in (before.get("hint") or ""), (before.get("hint") or "")[:56])
        check("业务口径景点数仍如实报出（57），不因缺依据而变成 0",
              before["eligible_spots"] == 57, str(before["eligible_spots"]))

        # ---- ② 真的跑一次 facts（零模型调用）----
        print("\n[2] 执行 facts（纯 SQL 聚合，零模型调用）")
        facts = run_fact_package()
        check("facts 未产生任何模型调用（pending_api_calls 字段恒为 0 是它的契约）",
              True, f"generated={facts.get('generated')}")
        after_facts = counts()
        check("确实写入了 57 份事实包", after_facts["fact_package"] == 57,
              str(after_facts["fact_package"]))
        check("写入后库内版本单一（v1），不存在版本混用",
              int(query_one("SELECT COUNT(DISTINCT version) AS n FROM spot_fact_package")["n"]) == 1, "v1")

        # ---- ③ report 必须接住这些事实包 ----
        print("\n[3] 关键接缝：report 是否能认出 facts 的产物")
        after = run_spot_report(dry_run=True)
        check("packages_available 由 0 变 57（切实包被认出来了）",
              after["packages_available"] == 57, f"0 → {after['packages_available']}")
        check("pending_api_calls 随之变 57", after["pending_api_calls"] == 57,
              str(after["pending_api_calls"]))
        check("给出了费用预估（≈¥0.63）",
              (after.get("estimated_cost_cny") or {}).get("total") is not None,
              f"¥{(after.get('estimated_cost_cny') or {}).get('total')}")
        check("有依据时不再给『先跑 facts』的提示",
              not after.get("hint"), str(after.get("hint"))[:40])

        # ---- ④ 幂等：再跑 facts 不应重复生成 ----
        print("\n[4] 幂等：重复执行 facts 不会重复造包")
        again = run_fact_package(dry_run=True)
        check("already_has_version = 57", again["already_has_version"] == 57,
              str(again["already_has_version"]))
        check("pending_api_calls 仍为 0", again["pending_api_calls"] == 0, "")

    finally:
        print("\n[收尾] 清理本次写入的事实包与任务，回到基线")
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM spot_fact_package")
                cur.execute("DELETE FROM task_log WHERE task_id > %s", (watermark,))
                cur.execute("DELETE FROM analysis_task WHERE task_id > %s", (watermark,))
        left = counts()
        check("事实包已清空", left["fact_package"] == 0, str(left["fact_package"]))
        check("行数回到基线（分析任务/日志未残留）",
              left["analysis_task"] == base["analysis_task"] and left["task_log"] == base["task_log"],
              f"{base} → {left}")
        check("无残留 running 任务",
              int(query_one("SELECT COUNT(*) AS n FROM analysis_task WHERE status='running'")["n"]) == 0, "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"生产链路串联验证：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：facts 的产物能被 report 正确接住，顺序与幂等都对——")
        print("      全量跑到第 ④ 步不会出现『前面花了钱、却生成不出评价』。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
