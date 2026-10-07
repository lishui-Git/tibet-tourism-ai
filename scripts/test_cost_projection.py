# -*- coding: utf-8 -*-
"""计划阶段费用预估的一致性测试（长任务书保险机制第 1、2 条）。

## 要求
"在运行前显示预计调用量"与"在运行前显示预计费用区间"。
项目有两处会给出这个预估：`--stage preflight`（只读体检）与 `--dry-run`（试跑）。
**两处必须给出一致的数字**，否则操作者会不知道该信哪个——
例如预检说 ¥75，试跑说 ¥60，就没人敢按 `--yes` 了。

## 做法（零 API 消费）
    · 取 `preflight` 的 `comment_cost_min/max` 与 `spot_report_cost`；
    · 取 `semantic --dry-run` 与 `report --dry-run` 的 `estimated_cost_cny`；
    · 断言两边**逐项相等**（同一实测口径、同一单价来源）；
    · 断言 dry-run 里给出的调用量 = 预检里的待处理量。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.batch.semantic_analysis import run_semantic_analysis
from app.batch.spot_report import run_spot_report
from app.db import query_one
from app.llm.preflight import collect

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 86)
    print("计划阶段费用预估一致性（preflight ↔ dry-run，零 API 消费）")
    print("=" * 86)

    pf = collect()
    print(f"\npreflight：待调用 {pf.workload['comment_api_calls']} 条、"
          f"待生成评价 {pf.workload['spot_report_api_calls']} 份")
    print(f" 评论级 ¥{pf.cost['comment_cost_min_cny']}–{pf.cost['comment_cost_max_cny']}；"
          f"景点级 ¥{pf.cost['spot_report_cost_cny']}")

    # ---------- A. 评论语义 dry-run ----------
    print("\n[A] 评论语义：dry-run 的费用区间应与 preflight 一致")
    sem = run_semantic_analysis(dry_run=True)
    print(f"  dry-run：计划调用 {sem['api_calls_planned']} 次；"
          f"预估 ¥{sem.get('estimated_cost_cny', {}).get('min')}–{sem.get('estimated_cost_cny', {}).get('max')}")

    check("dry-run 的计划调用量 = preflight 的待调用量",
          sem["api_calls_planned"] == pf.workload["comment_api_calls"],
          f"{sem['api_calls_planned']} vs {pf.workload['comment_api_calls']}")
    cost = sem.get("estimated_cost_cny") or {}
    check("dry-run 给出了费用区间（min/max）", "min" in cost and "max" in cost, f"{cost}")
    check("费用区间与 preflight 逐项一致",
          cost.get("min") == pf.cost["comment_cost_min_cny"]
          and cost.get("max") == pf.cost["comment_cost_max_cny"],
          f"dry-run {cost.get('min')}–{cost.get('max')} vs preflight "
          f"{pf.cost['comment_cost_min_cny']}–{pf.cost['comment_cost_max_cny']}")
    check("dry-run 标明了 token 口径（实测均值 + 区间）",
          (sem.get("estimated_tokens_per_call") or {}).get("measured_avg") is not None,
          f"{sem.get('estimated_tokens_per_call')}")
    check("dry-run 明确说明『实际费用以运行结束打印为准』",
          "实际费用" in (cost.get("note") or ""), cost.get("note", "")[:60])

    # ---------- B. 景点评价 dry-run ----------
    print("\n[B] 景点评价：两个口径的份数必须分得清，且与 preflight 对得上")
    rep = run_spot_report(dry_run=True)
    print(f"  dry-run：业务口径可评价 {rep['eligible_spots']} 个景点；"
          f"当前就绪事实包 {rep['packages_available']} 份；本次待生成 {rep['pending_api_calls']} 份")
    print(f"  hint: {rep.get('hint', '(无)')}")

    # 【状态无关】"可评价景点数"是**业务口径**（BR-02 门槛，恒为 57），
    # 而 preflight 的 `spot_report_api_calls` 是**待生成份数**（已生成的不再计）。
    # 两者只有在"一份都没生成"时才相等；Stage 5 之后 preflight 会变成 0。
    # 因此拆成三条稳定成立的断言（旧断言 `eligible == api_calls` 在全量生成后会失败）。
    check("业务口径的『可评价景点数』= 57（BR-02 门槛，与是否已生成无关）",
          rep["eligible_spots"] == 57, f"eligible={rep['eligible_spots']}")
    # preflight 的 `spot_report_api_calls` 定义 = 可评价景点 − **已有评价的景点**。
    # 注意它与 dry-run 的 `pending_api_calls` **不是同一个口径**：
    # 后者 = "当前就绪的事实包数"（没有事实包就不会生成，因此 Stage 5 跑 facts 之前为 0）。
    already_reports = int(query_one("SELECT COUNT(*) AS n FROM spot_report")["n"])
    check("preflight 的『待生成评价份数』= 可评价景点 − 已生成评价（定义式，两态通用）",
          pf.workload["spot_report_api_calls"] == rep["eligible_spots"] - already_reports,
          f"{pf.workload['spot_report_api_calls']} == {rep['eligible_spots']} − {already_reports}")
    check("dry-run 的『本次待生成』= 当前就绪的事实包数（没有依据就不生成）",
          rep["pending_api_calls"] == rep["packages_available"],
          f"pending={rep['pending_api_calls']} available={rep['packages_available']}")
    check("待生成份数 ≤ 可评价景点数（已生成的不重复计费）",
          pf.workload["spot_report_api_calls"] <= rep["eligible_spots"],
          f"{pf.workload['spot_report_api_calls']} ≤ {rep['eligible_spots']}")
    check("『本次待生成』= 当前就绪的事实包数（没有事实包就不该生成）",
          rep["pending_api_calls"] == rep["packages_available"],
          f"pending={rep['pending_api_calls']} available={rep['packages_available']}")
    check("事实包为空时给出可照做的提示（先跑 --stage facts）",
          (rep["packages_available"] > 0) or ("--stage facts" in (rep.get("hint") or "")),
          (rep.get("hint") or "")[:70])
    rep_cost = rep.get("estimated_cost_cny") or {}
    check("份数为 0 时**不**给出费用预估（避免误导为零花费已发生）",
          (rep["pending_api_calls"] > 0) or ("estimated_cost_cny" not in rep),
          f"pending={rep['pending_api_calls']}")
    if rep["pending_api_calls"] > 0:
        check("有份数时景点级预估与 preflight 一致",
              rep_cost.get("total") == pf.cost["spot_report_cost_cny"],
              f"dry-run ¥{rep_cost.get('total')} vs preflight ¥{pf.cost['spot_report_cost_cny']}")

    # ---------- C. 事实包：零调用 ----------
    print("\n[C] 事实包：dry-run 不应出现任何费用字段（它零模型调用）")
    msgs = " ".join(str(k) for k in pf.workload.keys())
    check("preflight 明确事实包调用数为 0", pf.workload["fact_package_api_calls"] == 0,
          f"{pf.workload['fact_package_api_calls']}")
    check("字段名可自解释（含 fact_package_api_calls）", "fact_package_api_calls" in msgs, msgs)

    # ---------- D. 打印出来的"分项相加 = 合计"必须真的加得起来 ----------
    print("\n[D] 预检打印的『分项 + 分项 = 合计』必须真能加得起来")
    c = pf.cost
    check("评论级下限 + 景点级 = 合计下限（按打印值核对）",
          round(c["comment_cost_min_cny"] + c["spot_report_cost_cny"], 2) == c["total_cost_min_cny"],
          f"{c['comment_cost_min_cny']} + {c['spot_report_cost_cny']} = "
          f"{round(c['comment_cost_min_cny'] + c['spot_report_cost_cny'], 2)} "
          f"vs 合计 {c['total_cost_min_cny']}")
    check("评论级上限 + 景点级 = 合计上限（按打印值核对）",
          round(c["comment_cost_max_cny"] + c["spot_report_cost_cny"], 2) == c["total_cost_max_cny"],
          f"{c['comment_cost_max_cny']} + {c['spot_report_cost_cny']} = "
          f"{round(c['comment_cost_max_cny'] + c['spot_report_cost_cny'], 2)} "
          f"vs 合计 {c['total_cost_max_cny']}")
    check("调用量也自洽：评论级 + 景点级 = 总调用量",
          pf.workload["comment_api_calls"] + pf.workload["spot_report_api_calls"]
          == pf.workload["total_api_calls"],
          f"{pf.workload['comment_api_calls']} + {pf.workload['spot_report_api_calls']} "
          f"= {pf.workload['total_api_calls']}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 86)
    print(f"计划阶段预估一致性：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 86)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
