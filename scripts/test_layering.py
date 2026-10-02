# -*- coding: utf-8 -*-
"""分层自洽性测试（详细设计 BR-05 / BR-06 的"不重不漏"证明）。

## 为什么需要它
"有多少条评论已完成"这种问题，光看各层计数是**证明不了**的：
可能出现某类评论既不属于低信息量、也不属于重复组、又没有进入待调用集合——
它们会被**静默跳过**，跑完全量也拿不到结果，而各层计数一切正常。

本测试把 preflight 新加的分层检查固定下来：
    A. 三层划分**互斥且穷尽**：低信息量 + 重复组成员 + 其余 = 可分析评论数；
    B. 各层口径与待处理计数**互相吻合**（模型层待处理 = 总量 − 已完成）；
    C. "待处理 0"必须真的等于该层已全部完成；
    D. 异常组合为 0（非低信息量被写成规则结果、正文为空却写入了结果表）。

全程只读数据库、**不调用任何模型**。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.llm.preflight import collect

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 84)
    print("分层自洽性测试（只读；不调用任何模型）")
    print("=" * 84)

    pf = collect()
    layer, counts = pf.layering, pf.counts

    print("\n[A] 三层划分互斥且穷尽")
    rule = layer["layer_rule_total"]
    reuse = layer["layer_reuse_total"]
    call = layer["layer_call_total"]
    analyzable = layer["analyzable"]
    check("三层之和 = 可分析评论数（不重不漏）", rule + reuse + call == analyzable,
          f"{rule} + {reuse} + {call} = {rule + reuse + call} / {analyzable}")
    check("可分析评论 + 正文为空 = 评论总数",
          analyzable + counts["empty_content"] == counts["review_total"],
          f"{analyzable} + {counts['empty_content']} = {counts['review_total']}")
    check("规则层总量 = 低信息量计数（BR-05 口径一致）",
          rule == counts["low_info_total"], f"{rule} vs {counts['low_info_total']}")
    check("复用层总量 = 待复用成员数（BR-06 口径一致）",
          reuse == counts["reuse_pending"], f"{reuse} vs {counts['reuse_pending']}")

    print("\n[B] 各层口径与待处理计数互相吻合")
    check("模型层待处理 = 调用层总量 − 已完成",
          counts["call_pending"] == call - layer["layer_call_done"],
          f"pending={counts['call_pending']} vs {call} − {layer['layer_call_done']} = {call - layer['layer_call_done']}")
    check("规则层待处理 = 规则层总量 − 已完成",
          counts["rule_pending"] == rule - layer["layer_rule_done"],
          f"pending={counts['rule_pending']} vs {rule} − {layer['layer_rule_done']} = {rule - layer['layer_rule_done']}")
    check("复用层待处理 = 复用层总量 − 已完成",
          counts["reuse_pending"] == reuse - layer["layer_reuse_done"],
          f"pending={counts['reuse_pending']} vs {reuse} − {layer['layer_reuse_done']}")

    print("\n[C] 各层已完成不超过总量（不会出现负待处理）")
    for key, label in (
        ("layer_rule", "规则层"),
        ("layer_reuse", "复用层"),
        ("layer_call", "调用层"),
    ):
        done, total = layer[f"{key}_done"], layer[f"{key}_total"]
        check(f"{label}：已完成 {done} ≤ 总量 {total}", done <= total, "")

    print("\n[D] 异常组合应为 0")
    check("非低信息量评论未被写成规则层结果", layer["rule_row_on_normal"] == 0,
          f"异常 {layer['rule_row_on_normal']} 条")
    check("正文为空的评论未写入结果表", layer["empty_content_in_any_layer"] == 0,
          f"异常 {layer['empty_content_in_any_layer']} 条")

    print("\n[E] 预检结论一致")
    expected_status = "READY" if not pf.issues else "BLOCKED"
    check("status 与 issues 一致", pf.status == expected_status, f"status={pf.status} issues={len(pf.issues)}")
    check("分层检查已纳入 issues 判据", any("分层" in i for i in pf.issues) or pf.status == "READY",
          f"issues={pf.issues}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 84)
    print(f"分层自洽性：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 84)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
