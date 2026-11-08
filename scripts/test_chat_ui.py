# -*- coding: utf-8 -*-
"""聊天式 AI 问答页结构与状态测试（零模型调用）。

覆盖：
    · 聊天头、消息区、底部输入区、头像/气泡、发送按钮；
    · 加载中不确定进度、错误重试、超时/取消、防重复发送；
    · 移动端断点与气泡最大宽度；
    · `/api/qa/ask` 的真实离线返回（支持问题 / 超范围拒答）。

注意：本测试不调用模型；浏览器手工验证见交付报告。
"""

from __future__ import annotations

import re
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

    print("=" * 82)
    print("聊天式 AI 问答页结构测试（不调用模型）")
    print("=" * 82)

    html = c.get("/smart?tab=qa").get_data(as_text=True)
    js = c.get("/static/js/qa.js").get_data(as_text=True)
    css = c.get("/static/css/app.css").get_data(as_text=True)

    print("\n[1] 聊天结构")
    check("存在聊天壳 .qa-chat", 'class="qa-chat"' in html, "")
    check("顶部显示“西藏旅游 AI 助手”", "西藏旅游 AI 助手" in html, "")
    check("顶部有数据来源状态", "基于已入库的景点评论与统计数据回答" in html, "")
    check("存在可滚动消息区 #qa-scroll", 'id="qa-scroll"' in html, "")
    check("存在消息容器 #qa-body", 'id="qa-body"' in html and 'role="log"' in html, "")
    check("存在底部输入区 .qa-composer", 'class="qa-composer"' in html, "")
    check("输入区使用 textarea", 'id="qa-question"' in html and "<textarea" in html, "")
    check("输入长度限制 200", 'maxlength="200"' in html, "")
    check("发送按钮初始禁用", bool(re.search(r'id="btn-ask"[^>]*disabled', html)), "")
    check("提示 Enter 发送 / Shift+Enter 换行", "Enter 发送" in html and "Shift+Enter" in html, "")

    print("\n[2] 消息气泡与头像")
    check("JS 追加用户/AI 消息", "appendQaBubble" in js and "qa-msg-avatar" in js, "")
    check("头像按角色区分", "isUser ? '我' : 'AI'" in js, "")
    check("CSS 有用户气泡样式", ".qa-msg.user .qa-bubble" in css, "")
    check("CSS 有助手气泡样式", ".qa-msg.assistant" in css, "")
    check("回答保留换行", "white-space: pre-wrap" in css, "")

    print("\n[3] 加载、超时、错误与防重复")
    check("加载气泡文案", "正在分析你的问题" in js, "")
    check("不确定进度条（无百分比文案）", "qa-progress" in js and "qa-loading" in css, "")
    check("请求超时已设置", "45000" in js and "AbortController" in js, "")
    check("失败显示重试按钮", "重试这条问题" in js and "qa-retry" in js, "")
    check("重试不重复追加用户消息", "opts && opts.retry" in js, "")
    check("请求中禁用输入与按钮", "input.disabled = true" in js and "qaAsking" in js, "")
    check("完成后恢复输入与按钮", "input.disabled = false" in js and "updateQaSendState" in js, "")
    check("页面离开中止未完成请求", "pagehide" in js and "qaController.abort" in js, "")
    check("只在接近底部时自动滚动", "isQaNearBottom" in js and "scrollQaToLast" in js, "")

    print("\n[4] 输入框自适应与移动端")
    check("输入框自动增高且限制 120px", "autoGrowQa" in js and "120" in js, "")
    check("移动端有聊天断点", "@media (max-width: 620px)" in css and "calc(100dvh - 430px)" in css, "")
    check("窄屏气泡不超出屏幕", "calc(100% - 32px)" in css, "")
    check("窄屏隐藏次要状态徽标", ".qa-badge { display: none; }" in css, "")

    print("\n[5] 真实接口的离线行为（不调用模型）")
    d = answer_question("布达拉宫的游客评价如何？")
    check("支持问题分类正确", classify_question("布达拉宫的游客评价如何？") == "SPOT_EVALUATION", "")
    check("支持问题有数据依据", bool(d.get("facts")) and d.get("sample_size", 0) > 0,
          f"facts={len(d.get('facts') or [])} sample={d.get('sample_size')}")
    check("离线模式不触达模型", d.get("reached_model") is False, str(d.get("reason")))
    check("离线模式不写问答记录", should_save_record(d) is False, str(d.get("reason")))

    out = answer_question("帮我规划一条西藏7日游路线")
    check("超范围问题被拒答", out.get("question_type") == "OUT_OF_SCOPE", "")
    check("超范围问题不触达模型", out.get("reached_model") is False, "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 82)
    print(f"聊天式问答页测试：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 82)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
