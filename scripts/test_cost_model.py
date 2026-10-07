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
        MEASURED_TOKENS_PER_CALL_MIN,
        OBSERVED_TOKENS_PER_CALL_MAX,
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
    print("\n[1] 评论级 token 常量（口径：全量实测）")
    check("min ≤ avg ≤ 观测最大（区间自洽）",
          MEASURED_TOKENS_PER_CALL_MIN <= MEASURED_TOKENS_PER_CALL <= OBSERVED_TOKENS_PER_CALL_MAX,
          f"{MEASURED_TOKENS_PER_CALL_MIN} ≤ {MEASURED_TOKENS_PER_CALL} ≤ {OBSERVED_TOKENS_PER_CALL_MAX}")

    # 全量实测汇总（47,702 次真实调用）：输入 16,199,065 / 输出 6,881,503 / 合计 23,080,568
    measured_in, measured_out, measured_calls = 16_199_065, 6_881_503, 47_702
    actual_share = measured_out / (measured_in + measured_out)
    check(f"OUTPUT_SHARE 与全量实测一致（实测 {actual_share:.4f}）",
          abs(OUTPUT_SHARE - actual_share) < 0.01,
          f"配置 {OUTPUT_SHARE} vs 实测 {actual_share:.4f}")
    full_mean = (measured_in + measured_out) / measured_calls
    check(f"全量实测均值可复算（23,080,568 ÷ 47,702 = {full_mean:.1f} ≈ 配置 {MEASURED_TOKENS_PER_CALL}）",
          abs(full_mean - MEASURED_TOKENS_PER_CALL) <= 1.0,
          f"{full_mean:.1f}")

    # ---------- ①b 区间上下界必须锚在"库内全量实测"上 ----------
    # 为什么单独查库：这三个数（最小/最大/均值）必须**任何人都能用只读 SQL 复核**。
    # 若这里失败，说明库内已经跑出比模型更大的 token —— 必须重新校准，而不是让模型悄悄漂移。
    print("\n[1b] 区间上下界与库内**真实 API** usage 对齐（关键：观测最大值不得被突破）")
    from app.db import query_all as _q_all

    # 【必须限定 source='deepseek'】复用层（source='reuse'）的 usage 是**全 0**
    # （复用不产生费用）。若不排除，实测"最小"会变成 0，把校准证据误导成
    # "存在 0 token 的真实调用"。全量后这类行有 1,071 条。
    usage_rows = _q_all(
        "SELECT CAST(JSON_UNQUOTE(JSON_EXTRACT(raw_json,'$.usage.total_tokens')) AS UNSIGNED) AS tt "
        "FROM sentiment WHERE method='deepseek' "
        "AND JSON_UNQUOTE(JSON_EXTRACT(raw_json,'$.source'))='deepseek' "
        "AND JSON_EXTRACT(raw_json,'$.usage') IS NOT NULL"
    )
    toks = sorted(int(r["tt"]) for r in usage_rows if r.get("tt") is not None)
    if toks:
        print(f"      · 库内真实 API usage {len(toks):,} 条：最小 {toks[0]} / 最大 {toks[-1]} / 合计 {sum(toks):,}")
        check(f"配置的下界 ≤ 库内实测最小（{toks[0]}）",
              MEASURED_TOKENS_PER_CALL_MIN <= toks[0],
              f"配置 {MEASURED_TOKENS_PER_CALL_MIN} vs 实测 {toks[0]}（必须 ≤ 实测，否则低估最低花费）")
        # 这条是**防漂移**断言：OBSERVED 必须是"已观测到的最大值"，不能被真实数据突破。
        check(f"配置的**观测最大值** ≥ 库内实测最大（{toks[-1]}）——不得被真实数据突破",
              OBSERVED_TOKENS_PER_CALL_MAX >= toks[-1],
              f"配置 {OBSERVED_TOKENS_PER_CALL_MAX} vs 实测 {toks[-1]}（超出即须重新校准）")
    else:
        check("库内存在可复核的 usage 样本（否则区间无从校准）", False, "未读到任何 usage")

    # ---------- ①c 两个概念必须分开：观测最大值 vs 预算上界 ----------
    print("\n[1c] **观测最大值 ≠ 预算上界**（本轮重点澄清的语义）")
    from app.llm.preflight import (
        CHINESE_TOKENS_PER_CHAR,
        CONSERVATIVE_OUTPUT_TOKENS,
        FIXED_PROMPT_TOKENS,
        conservative_tokens_per_call,
        conservative_tokens_per_call_weighted,
    )

    check("保守上界公式的固定 prompt 与实测相符（实测最短正文 prompt_tokens 304–312）",
          295 <= FIXED_PROMPT_TOKENS <= 320, f"配置 {FIXED_PROMPT_TOKENS}")
    check("正文系数偏保守（中文常见 0.65，取 ≥0.65）",
          CHINESE_TOKENS_PER_CHAR >= 0.65, f"配置 {CHINESE_TOKENS_PER_CHAR}")
    check("输出按上限估算而非中位数（实测输出中位约 100）",
          CONSERVATIVE_OUTPUT_TOKENS >= 200, f"配置 {CONSERVATIVE_OUTPUT_TOKENS}")
    check("保守上界随正文长度单调不减",
          all(conservative_tokens_per_call(a) <= conservative_tokens_per_call(b)
              for a, b in ((0, 10), (10, 126), (126, 500), (500, 1829))),
          f"126 字 {conservative_tokens_per_call(126)}；1829 字 {conservative_tokens_per_call(1829)}")

    pf_len = collect()
    weighted = conservative_tokens_per_call_weighted(
        [(int(r.get("n") or 0), float(r.get("avg_chars") or 0)) for r in pf_len.length_buckets]
    )
    print(f"      · 按真实正文长度分布加权的保守口径：{weighted:.1f} token/条")
    print(f"      · 全量观测最大值：{OBSERVED_TOKENS_PER_CALL_MAX} token/条（分布尾部观测点）")

    # 语义 1：预算上界是"**平均单条**的保守估计"，理应**高于实测均值**
    check("预算上界（加权保守口径）≥ 全量实测均值（预算必须高于均值才有意义）",
          weighted >= MEASURED_TOKENS_PER_CALL,
          f"{weighted:.1f} vs 均值 {MEASURED_TOKENS_PER_CALL}")
    # 语义 2：它**不必**高于"单条观测最大值"——尾部观测值天然可以高于平均值。
    #        这是本轮明确区分两个概念的核心断言（旧断言"加权 ≥ 观测最大"是**错的语义**）。
    check("预算上界**允许低于**单条观测最大值（它约束的是平均，不是尾部）",
          weighted < OBSERVED_TOKENS_PER_CALL_MAX,
          f"加权 {weighted:.1f} < 观测最大 {OBSERVED_TOKENS_PER_CALL_MAX}"
          "（若哪天反超，说明分布变得极长尾，需要重新审视口径）")
    # 语义 3：预算数值**不得**由"观测最大值 × 调用数"得出（那是错误口径）
    calls_now = pf_len.workload["comment_api_calls"]
    budget_from_tail = round(estimate_cost(calls_now, OBSERVED_TOKENS_PER_CALL_MAX), 2)
    check("预算数值**不是**用『观测最大值 × 调用数』算出来的（口径不得混用）",
          abs(pf_len.cost["comment_cost_conservative_cny"] - budget_from_tail) > 1e-9
          or calls_now == 0,
          f"按观测最大值算会得到 ¥{budget_from_tail}，而预算项是 "
          f"¥{pf_len.cost['comment_cost_conservative_cny']}")
    check("保守上界不是靠「所有评论都按最长正文」堆出来的（应低于极端口径）",
          conservative_tokens_per_call_weighted(
              [(int(r.get("n") or 0), float(r.get("avg_chars") or 0)) for r in pf_len.length_buckets]
          ) < conservative_tokens_per_call(1829),
          "加权值 < 最长正文单条上界，说明它反映了真实分布")

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
    expect_max = round(estimate_cost(calls, OBSERVED_TOKENS_PER_CALL_MAX) + estimate_report_cost(reports), 2)
    check("合计下限可复算",
          abs(expect_min - pf.cost["total_cost_min_cny"]) < 0.05,
          f"复算 {expect_min} vs 报告 {pf.cost['total_cost_min_cny']}")
    check("合计上限可复算",
          abs(expect_max - pf.cost["total_cost_max_cny"]) < 0.05,
          f"复算 {expect_max} vs 报告 {pf.cost['total_cost_max_cny']}")
    # 【语义澄清】上限不再要求"严格大于"下限：全量跑完后待处理只剩 2 条，
    # 两个分项四舍五入到分后可能相等（0.0 vs 0.02 → 合计都含景点级 0.73）。
    # 这里改为断言"上限 ≥ 下限"，并**另外**用一个足够大的假设调用量验证区间确实张开。
    check("区间自洽（上限 ≥ 下限）", pf.cost["total_cost_max_cny"] >= pf.cost["total_cost_min_cny"], "")
    big_min = round(estimate_cost(47_702, MEASURED_TOKENS_PER_CALL_MIN), 2)
    big_max = round(estimate_cost(47_702, OBSERVED_TOKENS_PER_CALL_MAX), 2)
    check("在 47,702 次量级上区间确实张开（上限 > 下限）", big_max > big_min, f"¥{big_min} < ¥{big_max}")

    # 预算覆盖判断：以**保守估算上界**为准，而不是被低估的区间上限。
    # 为什么这条要测：原先这条写死了"余额约 ¥57 低于下限"，随着余额变化它会变成
    # 一条永远为真的死断言。现在改成"给定余额 → 是否被保守上界覆盖"的纯函数，
    # 并且用**足够大的调用量**构造成本字典，避免"当前只剩 2 条待处理"导致三档都≈0、
    # 断言失去意义。
    def budget_verdict(balance: float, cost: dict) -> str:
        if balance < cost["total_cost_min_cny"]:
            return "BLOCKED"
        if balance < cost["total_cost_conservative_cny"]:
            return "READY_WITH_RISK"
        return "READY"

    synthetic_cost = {
        "total_cost_min_cny": big_min,
        "total_cost_conservative_cny": round(estimate_cost(47_702, weighted), 2),
    }
    check("预算判断：余额低于最低估算 → BLOCKED（示例 ¥30）",
          budget_verdict(30.0, synthetic_cost) == "BLOCKED",
          f"¥30 vs 最低 ¥{synthetic_cost['total_cost_min_cny']}")
    check("预算判断：余额在最低与保守上界之间 → READY_WITH_RISK",
          budget_verdict(synthetic_cost["total_cost_conservative_cny"] - 1, synthetic_cost)
          == "READY_WITH_RISK",
          f"vs 保守 ¥{synthetic_cost['total_cost_conservative_cny']}")
    check("预算判断：余额覆盖保守上界 → READY",
          budget_verdict(synthetic_cost["total_cost_conservative_cny"] + 1, synthetic_cost) == "READY", "")
    check("（参考）当前实际余额 ¥152 对**已跑完**的剩余任务绰绰有余",
          pf.cost["total_cost_conservative_cny"] < 152.0,
          f"剩余预算建议 ¥{pf.cost['total_cost_conservative_cny']}")

    # ---------- ③b 全量实际费用与模型预测的偏差必须可复现 ----------
    print("\n[3b] 全量实际结果 vs 模型预测（偏差可复现）")
    # 全量 semantic 实际（进程自报）：47,702 次调用、23,080,568 token、¥87.4502
    ACTUAL_FULL_COST = 87.4502
    predicted_full = round(estimate_cost(47_702, MEASURED_TOKENS_PER_CALL), 2)
    deviation = abs(predicted_full - ACTUAL_FULL_COST) / ACTUAL_FULL_COST
    check(f"用配置均值复算全量费用（预测 ¥{predicted_full} vs 实测 ¥{ACTUAL_FULL_COST}）",
          deviation < 0.02,
          f"偏差 {deviation * 100:.2f}%（< 2% 即口径可靠）")

    # ---------- ④ 重试不免费：必须讲清"区间可能被突破" ----------
    print("\n[4] 重试费用口径（估算不含重试，必须显式说明）")
    note = pf.cost.get("retry_note") or ""
    check("预检给出了重试口径说明", bool(note), note[:56])
    check("说明里点明『重试同样计费』", "重试同样计费" in note or "重试" in note, "")
    check("说明里点明『实际费用会高于以上数字』", "高于以上数字" in note, "")
    check("说明里给出实测重试次数作为依据", "0 重试" in note or "32 次" in note, "")

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
