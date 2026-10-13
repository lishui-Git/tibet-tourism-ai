# -*- coding: utf-8 -*-
"""用户可见文案的"分层"核对（**零 API 消费**）。

## 为什么要这个测试
QA / Compare 的自然语言能力接通后，出现了一类新问题：**技术正确，但把开发者/接口/
测试语言直接展示给了游客**（数据库表名、字段名、原始小数、内部校验流程、能力码……）。

这个测试固定一条纪律：**用户层只出现自然语言、可读数字与必要提示；系统层留在接口与
控制台里**。它检查的是"后端交出去的用户可见字符串"，以及"前端是否有翻译层"，
不检查模型的自由文本（那由真实调用验收 + `test_compare_live.py` 的渲染纪律覆盖）。

覆盖：
    [1] 能力范围提示（OUT_OF_SCOPE）是人话，且不出现内部码/字段名
    [2] 数据质量提示（景点评价 / 对比解读）是人话，不出现 need_review 等内部字段
    [3] 口径说明（CALIBER_*）不出现实现名与接口参数名
    [4] QA / Compare Prompt 含"面向游客的表达规范"（百分比、千分位、不得出现内部名称）
    [5] 前端有翻译层：数据库来源标识不会原样出现在页面上
    [6] 依据里的数字按人读的方式格式化（千分位 / 百分比 / 评分）
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import query_one  # noqa: E402
from app.llm.prompts import (  # noqa: E402
    COMPARE_SYSTEM_PROMPT,
    QA_SYSTEM_PROMPT,
)
from app.web import create_app  # noqa: E402
from app.web.data_access import (  # noqa: E402
    CALIBER_ASPECT,
    CALIBER_IP,
    CALIBER_SENTIMENT,
    CALIBER_THRESHOLD,
    CALIBER_TREND,
)
from app.web.qa import OUT_OF_SCOPE_ANSWER  # noqa: E402
from app.web.services import spot_report  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

# 不允许出现在**用户可见文案**里的内部标识（表名 / 字段名 / 实现名 / 状态码 / 参数名）
FORBIDDEN = (
    "stat_spot", "stat_time", "stat_ip", "stat_overview",
    "spot_report", "spot_fact_package", "comment_semantic",
    "need_review", "reliability_warning", "prompt_version", "token_usage",
    "deepseek", "mllib", "spark", "lda",
    "method=", "LIVE_DISABLED", "REPORT_NOT_GENERATED", "OUT_OF_SCOPE",
    "schema", "baseline",
    # 用户层不出现的方法学/系统术语（2026-10 表达规范）
    "口径", "基线", "校验", "标注为", "标记为",
)

FRIENDLY_SOURCE_HINT = "游客评论"   # 来源必须被翻译成中文说明


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def leaks(text: str) -> list[str]:
    low = (text or "").lower()
    return sorted({w for w in FORBIDDEN if w.lower() in low})


def main() -> int:
    print("=" * 88)
    print("用户可见文案分层核对（零 API 消费）")
    print("=" * 88)

    # ---------------- [1] 能力范围提示 ----------------
    print("\n[1] 能力范围提示（用户问「帮我规划路线」时的拒答文案）")
    check("不出现任何内部标识", not leaks(OUT_OF_SCOPE_ANSWER), str(leaks(OUT_OF_SCOPE_ANSWER)))
    check("保留了既有实践要求的表述（仅基于旅游评论数据）",
          "仅基于旅游评论数据" in OUT_OF_SCOPE_ANSWER, OUT_OF_SCOPE_ANSWER[:40])
    check("保留了既有实践要求的表述（不支持…实时信息）",
          "不支持" in OUT_OF_SCOPE_ANSWER, "")
    check("说明了系统能做什么（景点评价/关注点/差异）",
          all(k in OUT_OF_SCOPE_ANSWER for k in ("景点", "关注", "差异")), "")

    # ---------------- [2] 数据质量提示 ----------------
    print("\n[2] 数据质量提示（质检标记必须保留，但要说人话）")
    row = query_one(
        "SELECT spot_id FROM spot_report WHERE need_review=1 ORDER BY spot_id LIMIT 1"
    )
    check("存在带质量标记的报告（用于核对文案）", bool(row), str(row))
    if row:
        data = spot_report(int(row["spot_id"])) or {}
        note = data.get("need_review_note") or ""
        check("质量标记仍然存在（未删、未伪造 PASS）", bool(data.get("need_review")) and bool(note), "")
        check("提示文案不出现内部标识", not leaks(note), str(leaks(note)))
        check("提示文案不描述内部核对流程（无「校验/事实清单/保守策略/复核」）",
              not any(w in note for w in ("校验", "事实清单", "保守", "复核", "一致性核对")), note[:60])
        check("提示文案给出了可执行建议（结合样本量）", "样本量" in note, note[:60])
        check("提示文案不否定数据本身（无「评价有误/不可信」）",
              not any(w in note for w in ("有误", "不可信", "错误")), "")

    # ---------------- [3] 口径说明 ----------------
    print("\n[3] 口径说明（会随数据一并展示）")
    for name, text in (
        ("CALIBER_IP", CALIBER_IP), ("CALIBER_TREND", CALIBER_TREND),
        ("CALIBER_SENTIMENT", CALIBER_SENTIMENT), ("CALIBER_ASPECT", CALIBER_ASPECT),
        ("CALIBER_THRESHOLD", CALIBER_THRESHOLD),
    ):
        check(f"{name} 不出现内部标识", not leaks(text), str(leaks(text)))
    check("情感结果用日常说法说明两套统计方式（按星级统计 / 大模型判读）",
          "按游客星级统计" in CALIBER_SENTIMENT and "大模型" in CALIBER_SENTIMENT, CALIBER_SENTIMENT[:40])
    check("情感结果说明不使用「口径 / 基线」这类内部术语",
          "口径" not in CALIBER_SENTIMENT and "基线" not in CALIBER_SENTIMENT, "")

    # ---------------- [4] Prompt 表达规范 ----------------
    print("\n[4] Prompt 是否要求「面向游客的表达」")
    qa_rules = ("表达规范", "百分比", "千分位", "不得出现任何内部名称", "先给结论", "不要输出 0.8782")
    for text in qa_rules:
        check(f"QA 表达规范含「{text}」", text in QA_SYSTEM_PROMPT, "")
    compare_rules = ("表达规范", "百分比", "千分位", "不得出现任何内部名称",
                     "不要输出原始小数", "论文式说法")
    for text in compare_rules:
        check(f"Compare 表达规范含「{text}」", text in COMPARE_SYSTEM_PROMPT, "")
    check("Compare 要求「不要照抄字段名」", "不要照抄字段名" in COMPARE_SYSTEM_PROMPT, "")

    # ---------------- [5] 前端翻译层 ----------------
    print("\n[5] 前端翻译层：数据库来源标识不得原样上屏")
    qa_js = (ROOT / "app" / "web" / "static" / "js" / "qa.js").read_text(encoding="utf-8")
    check("qa.js 有来源翻译函数", "function friendlySource(" in qa_js, "")
    check("qa.js 有来源词典（内部标识 → 中文说明）", "SOURCE_WORDS" in qa_js, "")
    check("qa.js 有兜底说明（未知来源不透出原始标识）", "SOURCE_FALLBACK" in qa_js, "")
    check("渲染时使用翻译后的来源", "friendlySource(block.source)" in qa_js, "")
    check("页面不再直接输出原始 source",
          "${escapeHtml(block.source" not in qa_js and "block.source}</" not in qa_js, "")

    # ---------------- [6] 数字显示格式 ----------------
    print("\n[6] 依据里的数字按人读的方式格式化")
    check("qa.js 有数字格式化函数", "function formatBasisValue(" in qa_js, "")
    check("比率按百分比呈现（1 位小数）", "fmtPct(v, 1)" in qa_js, "")
    check("评分保留两位小数", "fmtNum(v, 2)" in qa_js, "")
    check("整数用千分位", "fmtInt(v)" in qa_js, "")
    compare_js = (ROOT / "app" / "web" / "static" / "js" / "compare.js").read_text(encoding="utf-8")
    check("Compare 解读区改为游客向标题（主要差异 / 可能的原因）",
          "主要差异" in compare_js and "可能的原因" in compare_js, "")
    check("Compare 不再把内部提示当告警块渲染（稳定性说明降级为提示文字）",
          "比较的稳定程度" in compare_js, "")

    # 后端返回的"用户可见提示"同样必须是人话：用一个假客户端触发 need_review 分支实测
    import json as _json

    import app.llm.client as _client_mod
    from app.config import WebSettings, settings as _settings
    from app.web.compare import compare_spots

    class _VerdictClient:
        """故意返回带"判定优劣"字样的解读 → 触发对比解读的 need_review 分支。"""

        def __init__(self, *a, **k):
            pass

        def chat(self, *a, **k):
            class _R:
                content = _json.dumps(
                    {
                        "differences": ["A 的评论量为 3057 条，B 为 1576 条", "风景评价差别不大"],
                        "possible_reasons": ["游客关注点不同"],
                        "reliability_note": "",
                    },
                    ensure_ascii=False,
                ).replace("差别不大", "更好")
                model = "fake"
                usage = {"total_tokens": 10}
                latency_ms = 1
                attempts = 1

            return _R()

    orig_web, orig_client = _settings.web, _client_mod.DeepSeekClient
    object.__setattr__(_settings, "web", WebSettings(**{**orig_web.__dict__, "compare_live": True}))
    _client_mod.DeepSeekClient = _VerdictClient
    try:
        interp = (compare_spots(196, 322) or {}).get("interpretation") or {}
    finally:
        _client_mod.DeepSeekClient = orig_client
        object.__setattr__(_settings, "web", orig_web)

    note = interp.get("need_review_note") or ""
    check("触发了对比解读的质量提示（分支可达）", bool(interp.get("need_review")) and bool(note), note[:50])
    check("该提示不出现内部标识", not leaks(note), str(leaks(note)))
    check("该提示不描述内部校验流程（无「判定优劣/设计禁止/未在对比事实/保守策略」）",
          not any(w in note for w in ("判定优劣", "设计禁止", "未在对比事实", "保守策略")), note[:60])

    # ---------------- [7] 导航视觉与 QA 呈现细节 ----------------
    print("\n[7] 导航视觉与 QA 呈现细节（2026-10 收尾项）")
    css = (ROOT / "app" / "web" / "static" / "css" / "app.css").read_text(encoding="utf-8")
    check("导航项有键盘焦点样式（.mainnav a:focus-visible）",
          ".mainnav a:focus-visible" in css, "")
    check("当前栏目为藏蓝青实心胶囊（白字 + 主色渐变）",
          ".mainnav a.active" in css and "var(--c-primary-dark)" in css and "color: #fff" in css, "")
    check("当前栏目下方有经幡短色带（复用 --flag-* 变量）",
          ".mainnav a.active::after" in css and "var(--flag-blue)" in css
          and "var(--flag-yellow)" in css, "")
    check("导航底部收口为渐变色带（3px）",
          ".topbar::after" in css and "height: 3px" in css, "")
    check("导航仍保留窄屏横向滚动（不挤压品牌区）",
          "overflow-x: auto" in css and "min-width: 132px" in css, "")
    check("QA 回答排在信息卡之前（结论先行）",
          qa_js.index("qa-answer") < qa_js.index("${head}"), "")
    check("QA 依据表的比率按指标名判定格式（labelHint）", "labelHint" in qa_js, "")
    check("QA 依据表列序为标签在前（LABEL_ORDER）", "LABEL_ORDER" in qa_js, "")
    check("QA 依据表的长文本折叠为「查看原文」", "查看原文" in qa_js, "")
    # 只看**展示用字符串**：APP_QA_LIVE 这类开关名允许留在代码注释里（用户看不到）
    check("QA 未开启提示已改为口语化措辞（不再自称「在线生成能力」）",
          "这次只给出了统计数据" in qa_js and "本次没有生成成段回答" not in qa_js, "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"用户可见文案分层核对：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：用户层只出现自然语言、可读数字与必要提示；")
        print("      数据库表名/字段名/实现名/内部状态码都留在接口与控制台里。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
