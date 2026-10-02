# -*- coding: utf-8 -*-
"""景点评价"实际 usage 与实际费用"测试（长任务书保险机制第 11 条的落地）。

## 修的是什么
`spot_report` 原先只把 **token 总量**塞进 `CallStats`（`usage={"total_tokens": n}`），
于是 `CallStats.prompt_tokens` 恒为 0，费用只能按 **7:3 经验比例**估算。
这不符合"运行结束后输出**实际** usage 与实际费用统计"的要求——
输入 ¥2/M、输出 ¥8/M 单价差 4 倍，比例猜错会直接算错钱。

现在 `generate_one()` 返回**真实用量**（含 prompt/completion 拆分，修复轮累加），
`_estimate_cost()` 按实际拆分 × 实际单价计算。

## 测法（零 API 消费）
用**假客户端**返回"已知用量"的响应，然后断言：
    · 汇总里的 prompt/completion token 与假客户端给出的一致；
    · 费用 = prompt/1e6×单价_in + completion/1e6×单价_out（精确到分位）；
    · 修复轮（首次校验失败、重试成功）时两次用量**都被累加**。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.batch.spot_report import _estimate_cost, generate_one
from app.config import settings
from app.llm.client import CallStats, ChatResult

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


VALID_REPORT = """{
  "summary": "综合评价：数据表现良好。",
  "advantages": ["评论量充足"],
  "issues": ["个别评论提到交通"],
  "visitor_focus": ["风景最受关注"]
}"""

# 必须让 `validate_report` **真的抛 ValidationError** 才会走修复轮。
# 注意：只缺条目、或只有 summary，都不会抛——校验器会"按实际保留"并记 repairs。
# 只有「summary 缺失」或「JSON 无法解析」才抛。首版这里给了 `{"summary": "缺字段"}`，
# 结果校验通过、修复轮根本没触发，测试自己先失败了（教训：先确认触发条件）。
INVALID_REPORT = "这不是 JSON {"


class ScriptedClient:
    """按脚本依次返回响应的假客户端（不发起任何网络请求）。"""

    def __init__(self, replies: list[tuple[str, dict[str, int]]]):
        self._replies = replies
        self.calls = 0

    def chat(self, messages, temperature=0.3, max_tokens=1200):  # noqa: ANN001, ARG002
        content, usage = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return ChatResult(content=content, model="scripted", usage=usage, latency_ms=0, attempts=1)


def expected_cost(prompt: int, completion: int) -> float:
    return round(
        prompt / 1_000_000 * settings.deepseek.price_input
        + completion / 1_000_000 * settings.deepseek.price_output,
        4,
    )


def main() -> int:
    print("=" * 84)
    print("景点评价实际 usage / 实际费用测试（假客户端，零 API 消费）")
    print("=" * 84)
    print(f"\n单价：输入 ¥{settings.deepseek.price_input}/M，输出 ¥{settings.deepseek.price_output}/M")

    package = {"spot_name": "测试景点", "review_count": 100}

    # ---------- A. 单次调用 ----------
    print("\n[A] 单次调用：汇总的 token 拆分与费用都必须是实际值")
    usage = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
    client = ScriptedClient([(VALID_REPORT, usage)])
    _, got_usage = generate_one(client, package)

    stats = CallStats()
    from app.batch.spot_report import _usage_result

    stats.record_success(_usage_result(got_usage))
    cost = _estimate_cost(stats)

    check("prompt_tokens 保留真实值（旧版恒为 0）",
          stats.prompt_tokens == 1000, f"{stats.prompt_tokens}")
    check("completion_tokens 保留真实值", stats.completion_tokens == 500, f"{stats.completion_tokens}")
    check("total_tokens 一致", stats.total_tokens == 1500, f"{stats.total_tokens}")
    check("费用按实际拆分与单价计算",
          cost["cost_total_cny"] == expected_cost(1000, 500),
          f"实际 ¥{cost['cost_total_cny']} vs 期望 ¥{expected_cost(1000, 500)}")
    check("费用字段不再是 estimated 口径（键名如实反映）",
          "cost_total_cny" in cost and "cost_total_cny_estimated" not in cost,
          f"keys={sorted(cost.keys())}")

    # ---------- B. 修复轮：两次用量都要累加 ----------
    print("\n[B] 修复轮（首次校验失败 → 重试成功）：两次用量累加")
    first = {"prompt_tokens": 800, "completion_tokens": 120, "total_tokens": 920}
    second = {"prompt_tokens": 900, "completion_tokens": 200, "total_tokens": 1100}
    client2 = ScriptedClient([(INVALID_REPORT, first), (VALID_REPORT, second)])
    result, got2 = generate_one(client2, package)
    stats2 = CallStats()
    stats2.record_success(_usage_result(got2))
    cost2 = _estimate_cost(stats2)

    check("确实调用了两次（修复轮生效）", client2.calls == 2, f"calls={client2.calls}")
    check("prompt_tokens 为两次之和", stats2.prompt_tokens == 800 + 900, f"{stats2.prompt_tokens}")
    check("completion_tokens 为两次之和", stats2.completion_tokens == 120 + 200, f"{stats2.completion_tokens}")
    check("费用按两次实际用量之和计算",
          cost2["cost_total_cny"] == expected_cost(1700, 320),
          f"实际 ¥{cost2['cost_total_cny']} vs 期望 ¥{expected_cost(1700, 320)}")
    check("修复记录已写入结果（便于事后复核）", bool(result.repairs), f"{result.repairs[:1]}")

    # ---------- C. 与 7:3 估算的差异（说明修这个是有意义的） ----------
    print("\n[C] 对比：旧口径（7:3 估算）与真实口径的金额差")
    naive = expected_cost(int(1500 * 0.7), 1500 - int(1500 * 0.7))
    real = expected_cost(1000, 500)
    print(f"  旧口径（1000/500 全按 7:3 猜）¥{naive}；真实口径 ¥{real}")
    check("两种口径确实不同（说明修复影响实际金额）", naive != real,
          f"差 ¥{round(abs(naive - real), 4)}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 84)
    print(f"实际 usage / 费用测试：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 84)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
