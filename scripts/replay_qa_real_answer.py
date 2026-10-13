# -*- coding: utf-8 -*-
"""真实回答回放：数字一致性核对（**零 API 调用**）。

把 2026-10 受控验收中**真实模型返回的回答原文**，喂给修正后的 `validate_answer`，
核对"人读写法"不再被误判、而真编造仍然拦得住。

覆盖两轮实测暴露的问题：
    · 引用离线评价摘要里的日期（2016-04-30）被误判；
    · 星级区间标注（"4-5 星"）里的 5 被误判；
    · **千分位（3,965）被切成 3 与 965**；
    · **百分数写法（87.8% vs 事实里的 0.8782）被误判**。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.web.data_access import CALIBER_SENTIMENT, CALIBER_THRESHOLD  # noqa: E402
from app.web.qa import QaFacts, validate_answer  # noqa: E402

# 真实调用（deepseek-flash，prompt p3）返回的回答原文，逐字保留
REAL_ANSWER = (
    "布达拉宫共有 3,965 条评论，平均评分 4.49 分，好评率（4-5 星）87.8%，"
    "差评率（1-2 星）6.7%，整体评价偏正面。\n\n"
    "从评论内容看，风景是被提到最多的亮点，正面率 97.0%；住宿餐饮正面率 89.0%，"
    "服务正面率 75.0%。另一方面，门票和性价比的负面率都是 58.0%，设施负面率 55.0%，"
    "人流拥挤负面率 53.0%，这几项反映出的不满相对集中。\n\n"
    "需要留意两点：一是情感分析给出的正面、中性、负面占比分别为 60.0%、27.0%、13.0%，"
    "这与上面按星级算出的好评率、差评率是两套不同口径，不能混在一起比较；"
    "二是相关数据存在一致性提示，已由系统核对，阅读时建议结合样本量综合判断。"
)

# 与真实调用同构的事实（离线评价摘要作为字符串事实给出，含日期区间）
FACTS = QaFacts(
    question_type="SPOT_EVALUATION",
    blocks=[
        {
            "source": "stat_spot、spot_report",
            "title": "布达拉宫 的统计与评价",
            "items": [
                {"指标": "评论量", "值": 3965},
                {"指标": "平均评分", "值": 4.49},
                {"指标": "好评率（4-5 星占比）", "值": 0.8782},
                {"指标": "差评率（1-2 星占比）", "值": 0.0671},
                {"指标": "已生成的智能评价（离线）",
                 "值": "评论时间范围为2016-04-30至2026-09-16，风景正面率0.97，门票负面率0.58"},
                {"指标": "数据说明", "值": "部分统计数据存在一致性提示，相关内容已由系统核对"},
                {"方面": "风景", "样本量": 886, "正面占比": 0.97, "负面占比": 0.02},
                {"方面": "门票", "样本量": 452, "正面占比": 0.24, "负面占比": 0.58},
                {"方面": "服务", "样本量": 1919, "正面占比": 0.75, "负面占比": 0.23},
                {"方面": "住宿餐饮", "样本量": 36, "正面占比": 0.89, "负面占比": 0.08},
                {"方面": "设施", "样本量": 123, "正面占比": 0.24, "负面占比": 0.55},
                {"方面": "人流拥挤", "样本量": 190, "正面占比": 0.28, "负面占比": 0.53},
            ],
        }
    ],
    caliber_note=CALIBER_THRESHOLD + " " + CALIBER_SENTIMENT,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 84)
    print("真实回答回放：数字一致性核对（零 API 调用）")
    print("=" * 84)

    text, need_review, notes = validate_answer(REAL_ANSWER, FACTS)
    print(f"\n真实回答长度：{len(REAL_ANSWER)} 字")
    print(f"复核结果：need_review={need_review}  notes={notes or '(无)'}")

    check("整段真实回答不再产生任何数字类告警", need_review == 0, f"notes={notes}")
    check("千分位（3,965）被正确识别为 3965", "3965" not in " ".join(notes) and "965" not in " ".join(notes), "")
    check("百分数写法（87.8% / 58.0%）与事实里的比率对应", "87.8" not in " ".join(notes), "")
    check("星级区间（4-5 星 / 1-2 星）不再被误判", "5" not in " ".join(notes), "")

    # 反向验证：真编造仍然拦得住
    fabricated = REAL_ANSWER + "\n另有 987654 条评论来自其他平台，好评率 99.9%。"
    _t2, n2, notes2 = validate_answer(fabricated, FACTS)
    check("仍能拦住真正的编造数字（987654）", n2 == 1 and "987654" in " ".join(notes2), f"notes={notes2}")

    # 越界词替换仍生效
    t3, _n3, notes3 = validate_answer("建议按这条路线走，先订酒店再看天气。", FACTS)
    check("越界承诺词仍被替换为能力边界说明", "不提供路线规划" in t3, f"notes={notes3}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 84)
    print(f"真实回答回放核对：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 84)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
