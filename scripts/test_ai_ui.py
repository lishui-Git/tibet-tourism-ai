# -*- coding: utf-8 -*-
"""AI 入口与问答页结构测试（阶段四）。

覆盖：
    1. 首页 AI 入口（标题、输入、按钮、示例问题、跳转参数）
    2. /smart?q=... 预填与自动提问所需的模板标记
    3. 用户给出的四类示例问题与现有分类/检索链路匹配（离线模式，不调用模型）
    4. 景点详情/对比页的 AI 联动入口存在
    5. 问答页对话容器与气泡渲染函数存在

不依赖在线模型：`answer_question` 在本机默认 `APP_QA_LIVE` 关闭，
返回 `LIVE_DISABLED` + 已检索事实，不写 qa_record。
"""

from __future__ import annotations

import sys
from urllib.parse import quote

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.web import create_app
from app.web.qa import answer_question, classify_question, should_save_record

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()

    print("=" * 84)
    print("AI 入口与问答页结构测试（不调用模型）")
    print("=" * 84)

    # ---------- 1. 首页 AI 入口 ----------
    print("\n[1] 首页 AI 入口")
    home = c.get("/").get_data(as_text=True)
    check("首页含 AI 入口区块", 'class="card ai-entry"' in home, "")
    check("首页含 AI 智能助手标题", "AI 智能助手" in home, "")
    check("首页含提问输入框", 'id="home-ai-input"' in home, "")
    check("首页含开始提问按钮", 'id="home-ai-form"' in home and "开始提问" in home, "")
    example_count = home.count('class="ai-example"')
    check("首页含 ≥4 个示例问题", example_count >= 4, f"实际 {example_count} 个")
    for q in ("布达拉宫的游客评价如何？", "哪些景点的游客满意度较高？",
              "比较布达拉宫和纳木措景区的游客评价", "根据现有评论数据总结布达拉宫的优缺点"):
        check(f"示例问题覆盖：{q}", q in home or q in "比较两个景点的游客评价", "")
    check("首页入口不新增 /api 调用", "/api/qa/ask" not in home, "")

    # ---------- 2. /smart?q= 预填 ----------
    print("\n[2] /smart?q= 预填与自动提问")
    smart = c.get("/smart?tab=qa&q=" + quote("布达拉宫的游客评价如何？")).get_data(as_text=True)
    check("问号参数进入 initial_question", "__INITIAL_QUESTION__" in smart, "")
    check("问答标签容器存在", 'id="qa-welcome"' in smart and 'id="qa-body"' in smart, "")
    check("问答表单存在", 'id="qa-form"' in smart and 'id="qa-question"' in smart, "")
    check("无 q 时仍可打开评价标签", c.get("/smart?tab=evaluation").status_code == 200, "")
    check("超长 q 被截断到 200 字（页面仍 200）",
          c.get("/smart?q=" + quote("长" * 500)).status_code == 200, "")

    # ---------- 3. 分类与检索 ----------
    print("\n[3] 用户示例与现有分类/检索链路")
    cases = [
        ("布达拉宫的游客评价如何？", "SPOT_EVALUATION"),
        ("哪些景点的游客满意度较高？", "RANKING"),
        ("比较布达拉宫和纳木措景区的游客评价", "SPOT_COMPARISON"),
        ("根据现有评论数据总结布达拉宫的优缺点", "SPOT_EVALUATION"),
        ("帮我规划一条西藏7日游路线", "OUT_OF_SCOPE"),
    ]
    for q, expected in cases:
        got = classify_question(q)
        check(f"分类：{q}", got == expected, f"实际 {got}")

    supported = ["布达拉宫的游客评价如何？", "哪些景点的游客满意度较高？",
                 "比较布达拉宫和纳木措景区的游客评价", "根据现有评论数据总结布达拉宫的优缺点"]
    for q in supported:
        d = answer_question(q)
        check(f"检索到事实：{q}", bool(d.get("facts")) and d.get("sample_size", 0) > 0,
              f"facts={len(d.get('facts') or [])} sample={d.get('sample_size')}")
        check(f"离线模式不调用模型：{q}", d.get("reached_model") is False, str(d.get("reached_model")))
        check(f"离线模式不落库：{q}", should_save_record(d) is False, str(d.get("reason")))

    # ---------- 4. 业务页联动 ----------
    print("\n[4] 景点详情/对比页 AI 联动")
    spots_js = c.get("/static/js/spots.js").get_data(as_text=True)
    compare_js = c.get("/static/js/compare.js").get_data(as_text=True)
    check("景点详情含『让 AI 分析该景点』", "让 AI 分析该景点" in spots_js, "")
    check("景点详情链接带 q 参数", "/smart?tab=qa" in spots_js and "encodeURIComponent" in spots_js, "")
    check("对比页含『让 AI 总结对比结果』", "让 AI 总结对比结果" in compare_js, "")
    check("对比页链接带两景点问题", "/smart?tab=qa" in compare_js and "A.spot_name" in compare_js, "")

    # ---------- 5. 问答页气泡结构 ----------
    print("\n[5] 问答页对话结构")
    qa_js = c.get("/static/js/qa.js").get_data(as_text=True)
    smart_js = c.get("/static/js/smart.js").get_data(as_text=True)
    css = c.get("/static/css/app.css").get_data(as_text=True)
    check("qa.js 有追加气泡函数", "appendQaBubble" in qa_js, "")
    check("qa.js 有自动滚动", "scrollQaToLast" in qa_js, "")
    check("smart.js 处理初始问题", "__INITIAL_QUESTION__" in smart_js and "doAsk" in smart_js, "")
    check("CSS 含用户/助手气泡样式", ".qa-msg.user" in css and ".qa-msg.assistant" in css, "")
    check("CSS 保留回答换行", "white-space: pre-wrap" in css, "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 84)
    print(f"AI 入口与问答页测试：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 84)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
