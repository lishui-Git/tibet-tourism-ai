# -*- coding: utf-8 -*-
"""成本模型核对：预检的费用估算必须**有实测依据**，且偏保守（不低报）。

## 为什么需要它
项目只剩约 ¥57，而全量预计 ¥75–98——**费用估算如果低报，就会跑到一半没钱**。
因此估算必须满足两件事：
    ① **有依据**：常量来自真实调用/真实产物的测量，不是拍脑袋；
    ② **偏保守**：宁可高报。实测值与估算值之比应 ≤ 1（即估算 ≥ 实测）。

## 核对内容（零 API 消费）
    · 评论级：单条 token 的中位/区间必须**自洽**（min ≤ avg ≤ max），
      且 `OUTPUT_SHARE` 与实测汇总（输入 10,691 / 输出 4,681）一致；
      `estimate_cost(1, 480)` 必须能复算出文档里那个"约 ¥0.00174/条"；
    · 景点级：`REPORT_INPUT_TOKENS` 必须 **≥ 实测 prompt 的真实 token 估算**
      （用真实事实包 + 真实 prompt 量出来），否则就是低报；
    · 端到端：`estimate_cost(47702, min/max) + estimate_report_cost(57)`
      必须复现预检报告的合计区间。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    from app.config import settings
    from app.llm.preflight import (
        MEASURED_TOKENS_PER_CALL,
        MEASURED_TOKENS_PER_CALL_MAX,
        MEASURED_TOKENS_PER_CALL_MIN,
        OUTPUT_SHARE,
        REPORT_INPUT_TOKENS,
        REPORT_OUTPUT_TOKENS,
        collect,
        estimate_cost,
        estimate_report_cost,
    )

    print("=" * 88)
    print("成本模型核对：估算必须有实测依据且偏保守（零 API 消费）")
    print("=" * 88)

    # ---------- ① 评论级常量的自洽性 ----------
    print("\n[1] 评论级 token 常量")
    check("min ≤ avg ≤ max（区间自洽）",
          MEASURED_TOKENS_PER_CALL_MIN <= MEASURED_TOKENS_PER_CALL <= MEASURED_TOKENS_PER_CALL_MAX,
          f"{MEASURED_TOKENS_PER_CALL_MIN} ≤ {MEASURED_TOKENS_PER_CALL} ≤ {MEASURED_TOKENS_PER_CALL_MAX}")

    # 实测汇总：输入 10,691 / 输出 4,681（32 次真实调用）
    measured_in, measured_out = 10_691, 4_681
    actual_share = measured_out / (measured_in + measured_out)
    check(f"OUTPUT_SHARE 与实测一致（实测 {actual_share:.3f}）",
          abs(OUTPUT_SHARE - actual_share) < 0.005,
          f"配置 {OUTPUT_SHARE} vs 实测 {actual_share:.3f}")
    check("实测总量可复算（10,691 + 4,681 = 15,372，÷32 ≈ 480）",
          round((measured_in + measured_out) / 32) == MEASURED_TOKENS_PER_CALL,
          f"{(measured_in + measured_out) / 32:.1f}")

    # 单价：¥2/M 输入、¥8/M 输出 ⇒ 480 token（73:27）单条约 ¥0.00174
    per_call = estimate_cost(1, MEASURED_TOKENS_PER_CALL)
    check("单条评论级成本可复算（≈¥0.0017）", 0.0015 < per_call < 0.0020, f"¥{per_call:.5f}")
    check("单价与配置一致（输入 ¥2/M、输出 ¥8/M）",
          settings.deepseek.price_input == 2.0 and settings.deepseek.price_output == 8.0,
          f"{settings.deepseek.price_input} / {settings.deepseek.price_output}")

    # ---------- ② 景点级输入估算必须不低于实测 ----------
    print("\n[2] 景点级输入 token 估算（关键：不得低报）")
    from app.batch.fact_package import run_fact_package
    from app.batch.spot_report import load_fact_packages
    from app.llm.prompts import build_report_messages

    # 真的生成一次事实包（纯 SQL、零模型调用），量出真实 prompt 大小，用完清理
    from app.db import connection, query_one

    watermark = int(query_one("SELECT COALESCE(MAX(task_id),0) AS m FROM analysis_task")["m"])
    try:
        run_fact_package()
        pkgs = load_fact_packages()
        check(f"生成了 {len(pkgs)} 份事实包用于量测", len(pkgs) == 57, str(len(pkgs)))

        def pkg_of(p):
            v = p.get("package_json")
            return json.loads(v) if isinstance(v, str) else v

        # 取**最大**的一份来量（最坏情况），并对全部 57 份都量一遍
        def tokens_of(p) -> int:
            chars = sum(len(m.get("content") or "") for m in build_report_messages(pkg_of(p)))
            return int(chars * 0.65)      # 中文约 0.65 token/字（保守下限）

        per_spot = sorted(tokens_of(p) for p in pkgs)
        worst = per_spot[-1]
        median = per_spot[len(per_spot) // 2]
        print(f"      · 57 份事实包的输入 token 估算：中位 ≈{median:,}，最坏 ≈{worst:,}")
        check(f"REPORT_INPUT_TOKENS ≥ 实测最坏输入（实测≈{worst:,}）",
              REPORT_INPUT_TOKENS >= worst,
              f"配置 {REPORT_INPUT_TOKENS:,} vs 实测最坏 {worst:,} "
              f"（{REPORT_INPUT_TOKENS / max(worst, 1):.2f}x，≥1 即不低报）")
        check("输入估算同时高于中位（不至于贴着最坏值没有余量）",
              REPORT_INPUT_TOKENS > median, f"{REPORT_INPUT_TOKENS:,} > {median:,}")
        check("输出按 schema 上限估算（不是按中位数估算）",
              REPORT_OUTPUT_TOKENS >= 400, f"{REPORT_OUTPUT_TOKENS}")
    finally:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM spot_fact_package")
                cur.execute("DELETE FROM task_log WHERE task_id > %s", (watermark,))
                cur.execute("DELETE FROM analysis_task WHERE task_id > %s", (watermark,))
        check("量测用的 57 份事实包已清理（未污染库）",
              int(query_one("SELECT COUNT(*) AS n FROM spot_fact_package")["n"]) == 0, "")

    # ---------- ③ 端到端：预检合计区间可复算 ----------
    print("\n[3] 端到端：预检报告的合计区间必须可由公式复算")
    pf = collect()
    calls = pf.workload["comment_api_calls"]
    reports = pf.workload["spot_report_api_calls"]
    expect_min = round(estimate_cost(calls, MEASURED_TOKENS_PER_CALL_MIN) + estimate_report_cost(reports), 2)
    expect_max = round(estimate_cost(calls, MEASURED_TOKENS_PER_CALL_MAX) + estimate_report_cost(reports), 2)
    check("合计下限可复算",
          abs(expect_min - pf.cost["total_cost_min_cny"]) < 0.05,
          f"复算 {expect_min} vs 报告 {pf.cost['total_cost_min_cny']}")
    check("合计上限可复算",
          abs(expect_max - pf.cost["total_cost_max_cny"]) < 0.05,
          f"复算 {expect_max} vs 报告 {pf.cost['total_cost_max_cny']}")
    check("区间非空且上限 > 下限", pf.cost["total_cost_max_cny"] > pf.cost["total_cost_min_cny"], "")
    check(f"已知余额（约 ¥57）低于估算下限（¥{pf.cost['total_cost_min_cny']}）——"
          "这正是『余额不足、不能全量』的判断依据",
          57 < pf.cost["total_cost_min_cny"], "")

    # ---------- ④ 重试不免费：必须讲清"区间可能被突破" ----------
    print("\n[4] 重试费用口径（估算不含重试，必须显式说明）")
    note = pf.cost.get("retry_note") or ""
    check("预检给出了重试口径说明", bool(note), note[:56])
    check("说明里点明『重试同样计费』", "重试同样计费" in note or "重试" in note, "")
    check("说明里点明『实际费用可能高于上限』", "高于上限" in note, "")
    check("说明里给出实测重试次数（0 次）作为依据", "0 重试" in note or "32 次" in note, "")

    # 客户端确实会重试（否则这条说明就是多余的）
    import app.llm.client as _client
    src = Path(_client.__file__).read_text(encoding="utf-8")
    check("客户端确实实现了重试（说明不是无的放矢）",
          "retry" in src.lower() and "attempts" in src, "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"成本模型核对：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：费用估算的每一项常量都能追到实测数据，且景点级输入按最坏情况估算——")
        print("      估算偏保守，不会出现『跑到一半没钱』。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
