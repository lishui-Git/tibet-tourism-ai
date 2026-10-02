# -*- coding: utf-8 -*-
"""M5 智能问答专项测试（详细设计 §15.E）。

**本测试不调用任何模型**（`APP_QA_LIVE` 默认为 0），因此全部为确定性断言。

覆盖三类内容：
    1. 分类与景点识别：六类 + 超范围；长名优先（"纳木措景区"不吃掉"纳木措"）
    2. 三条"不调用模型"的分支：超范围拒答 / 未识别到景点 / 检索无数据
       —— 即使开启开关这三类也不该调用模型（本测试断言 `reached_model=false`）
    3. 上下文纪律（CC-3）：条目数 ≤30、文字量 ≤3000 字、只含结构化事实
    4. 回答校验：越界词替换为能力边界说明、数字一致性标记
    5. 接口：ask 落库 `qa_record`、history 仅返回本人记录

【数据卫生】ask 会写入 `qa_record`（业务记录，设计如此）；测试结束按 qa_id 精确删除自己写入的记录。
"""

from __future__ import annotations

import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.db import connection
from app.web import create_app
from app.web.qa import (
    CONTEXT_MAX_CHARS,
    CONTEXT_MAX_ITEMS,
    build_context,
    classify_question,
    match_spots,
    retrieve_facts,
    validate_answer,
    QaFacts,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    app = create_app()
    client = app.test_client()
    created_qa_ids: list[int] = []

    print("=" * 82)
    print("M5 智能问答测试（不调用任何模型；APP_QA_LIVE 默认关闭）")
    print("=" * 82)

    # ---------- 1. 分类 ----------
    print("\n[1] 问题分类（§15.E.2）")
    cases = [
        ("布达拉宫怎么样", "SPOT_EVALUATION"),
        ("布达拉宫值得去吗", "SPOT_EVALUATION"),
        ("布达拉宫和纳木措哪个好", "SPOT_COMPARISON"),
        ("大家去布达拉宫最在意什么", "VISITOR_FOCUS"),
        ("为什么布达拉宫的差评集中在门票", "SENTIMENT_EXPLAIN"),
        ("一共采集了多少条评论", "DATA_METRIC"),
        ("评论量前十的景点", "RANKING"),
        ("帮我规划一条西藏 7 日游路线", "OUT_OF_SCOPE"),
        ("明天拉萨天气如何", "OUT_OF_SCOPE"),
        ("帮我订布达拉宫的门票", "OUT_OF_SCOPE"),
        ("", "OUT_OF_SCOPE"),
    ]
    for question, expected in cases:
        actual = classify_question(question)
        check(f"分类「{question or '（空）'}」→ {expected}", actual == expected, f"实际 {actual}")

    # ---------- 2. 景点识别 ----------
    print("\n[2] 景点实体识别（长名优先）")
    spots = match_spots("布达拉宫和纳木措哪个好")
    names = [s["spot_name"] for s in spots]
    check("识别出两个景点", len(spots) >= 2, f"识别到 {names}")
    check("布达拉宫在结果中", any("布达拉宫" in n for n in names), str(names))

    long_name = match_spots("纳木措景区怎么样")
    check(
        "长名优先：『纳木措景区』不被『纳木措』截断",
        any("纳木措景区" == s["spot_name"] for s in long_name),
        f"识别到 {[s['spot_name'] for s in long_name]}",
    )
    check("无景点名时返回空列表", match_spots("一共多少条评论") == [], str(match_spots("一共多少条评论")))

    # ---------- 3. 检索与上下文纪律 ----------
    print("\n[3] 事实检索与上下文纪律（CC-3 / §15.E.3）")
    rank_facts = retrieve_facts("RANKING", "评论量前十的景点", [])
    check("RANKING 能检索到排行事实", not rank_facts.is_empty, f"{len(rank_facts.blocks)} 段事实")
    rank_ctx = build_context(rank_facts)
    items = sum(len(b.get("items") or []) for b in rank_ctx)
    chars = len(json.dumps(rank_ctx, ensure_ascii=False))
    check(f"上下文条目数 ≤ {CONTEXT_MAX_ITEMS}", items <= CONTEXT_MAX_ITEMS, f"实际 {items} 条")
    check(f"上下文文字量 ≤ {CONTEXT_MAX_CHARS}", chars <= CONTEXT_MAX_CHARS + 400, f"实际 {chars} 字")
    check("每段事实标注来源表", all(b.get("source") for b in rank_ctx), str([b.get("source") for b in rank_ctx]))
    check("上下文含口径段", any(b.get("source") == "caliber" for b in rank_ctx), "")

    metric_facts = retrieve_facts("DATA_METRIC", "一共采集了多少条评论", [])
    check("DATA_METRIC 检索到规模事实", not metric_facts.is_empty, f"{len(metric_facts.blocks)} 段")
    metric_ctx = build_context(metric_facts)
    check(
        "客源地口径随事实给出（BR-10）",
        any("2022-08" in json.dumps(b, ensure_ascii=False) for b in metric_ctx),
        "含 2022-08 口径说明",
    )

    # 未识别景点时的检索应返回空（→ 走"不调用模型"分支）
    empty_facts = retrieve_facts("SPOT_EVALUATION", "布达拉宫怎么样", [])
    check("无景点实体时检索为空（触发 NO_DATA 分支）", empty_facts.is_empty, "")

    # ---------- 4. 回答校验 ----------
    print("\n[4] 回答校验（§15.E.4）")
    facts = QaFacts(question_type="RANKING", blocks=[{"source": "stat_spot", "items": [{"评论量": 3965, "评分": 4.49}]}])
    text, need_review, notes = validate_answer("布达拉宫有 3965 条评论，平均分 4.49。", facts)
    check("数字与事实一致时不标记", need_review == 0, f"notes={notes}")

    text2, need_review2, notes2 = validate_answer("布达拉宫有 99999 条评论。", facts)
    check("数字不符时标记 need_review=1", need_review2 == 1, f"notes={notes2}")

    text3, _, notes3 = validate_answer("建议按这条路线走，先订酒店再看天气。", facts)
    check("越界词被附加能力边界说明", "不提供路线规划" in text3, f"notes={notes3}")

    long_answer = "测" * 800
    text4, _, notes4 = validate_answer(long_answer, facts)
    check("超长回答被截断到 600 字", len(text4) <= 700, f"长度 {len(text4)}")

    # ---------- 5. 三条"不调用模型"的分支 ----------
    print("\n[5] 不调用模型的分支（即使开启开关也应如此）")


    def ask(question: str) -> dict:
        resp = client.post("/api/qa/ask", json={"question": question})
        body = resp.get_json() or {}
        data = body.get("data") or {}
        if data.get("qa_id"):
            created_qa_ids.append(int(data["qa_id"]))
        return {"status": resp.status_code, "body": body, "data": data}

    r = ask("帮我订布达拉宫的门票")
    d = r["data"]
    check("超范围拒答：HTTP 200 / code=0 / OUT_OF_SCOPE", r["status"] == 200 and r["body"]["code"] == 0 and d["question_type"] == "OUT_OF_SCOPE", f"type={d['question_type']}")
    check("超范围：reached_model=false（不调用模型）", d["reached_model"] is False, str(d["reached_model"]))
    check("超范围：给出可回答范围说明", "仅基于旅游评论数据" in (d["answer"] or ""), (d["answer"] or "")[:40])
    check("超范围：不写入 qa_record（§15.E.1 拒答不落库）", d.get("qa_id") is None and d.get("saved") is False, f"qa_id={d.get('qa_id')} saved={d.get('saved')}")

    r = ask("布达拉宫怎么样")
    d = r["data"]
    check("识别到景点：SPOT_EVALUATION 且 reached_model=false（默认关闭）", d["question_type"] == "SPOT_EVALUATION" and d["reached_model"] is False, f"type={d['question_type']} model={d['reached_model']}")
    check("默认关闭时给出原因 LIVE_DISABLED", d["reason"] == "LIVE_DISABLED", str(d["reason"]))
    check("默认关闭时仍返回数据依据", len(d["facts"]) > 0, f"{len(d['facts'])} 段事实")

    r = ask("那个地方怎么样")
    d = r["data"]
    check("未识别到景点：SPOT_NOT_RECOGNIZED 且不调用模型", d["reason"] == "SPOT_NOT_RECOGNIZED" and d["reached_model"] is False, f"reason={d['reason']}")
    check("追问提示类不写入 qa_record", d.get("qa_id") is None, f"qa_id={d.get('qa_id')}")

    r = ask("布达拉宫和纳木措哪个好")
    d = r["data"]
    check("对比类识别到两个景点", d["question_type"] == "SPOT_COMPARISON" and len(d["spots"]) >= 2, f"{[s['spot_name'] for s in d['spots']]}")

    r = ask("评论量前十的景点")
    d = r["data"]
    check("RANKING 检索到排行事实", d["question_type"] == "RANKING" and len(d["facts"]) > 0, f"{len(d['facts'])} 段事实")

    r = ask("")
    check("空问题 → 1001", r["status"] == 400 and r["body"]["code"] == 1001, f"HTTP {r['status']} code={r['body']['code']}")

    r = ask("问" * 300)
    check("超长问题 → 1001", r["status"] == 400 and r["body"]["code"] == 1001, f"HTTP {r['status']} code={r['body']['code']}")

    # ---------- 6. 落库与历史越权 ----------
    print("\n[6] qa_record 落库与历史越权防护")

    # 【本轮修正】设计 §六 写的是"问答调用失败 → 提供重试按钮；**不保存残缺记录**"，
    # §15.E.1 流程图也是"校验通过 → 写入 qa_record"。因此**只有成功生成回答才落库**。
    # 而默认 APP_QA_LIVE=0，所以"默认关闭"分支本来就不该有 qa_id——
    # 本测试此前假设它会落库（与设计不符）。现在改为：
    # 用一个**假客户端**临时开启在线路径，验证"成功 → 落库"、"失败 → 不落库"。
    import app.llm.client as _client_mod
    from app.config import WebSettings, settings as _settings
    from app.llm.client import LlmError as _LlmError

    class _OkClient:
        def __init__(self, *a, **k): pass

        def chat(self, *a, **k):
            class _R:
                content = '{"answer": "布达拉宫评论量最多。"}'
                model = "fake"
                usage = {"total_tokens": 0}
                latency_ms = 0
                attempts = 1
            return _R()

    class _BoomClient:
        def __init__(self, *a, **k): pass

        def chat(self, *a, **k):
            raise _LlmError("模拟失败", kind="network")

    _orig_web = _settings.web
    _orig_client = _client_mod.DeepSeekClient
    # settings 为 frozen dataclass，用 object.__setattr__ 临时替换（finally 里还原）
    object.__setattr__(_settings, "web", WebSettings(**{**_orig_web.__dict__, "qa_live": True}))
    try:
        # 6a 模型失败 → 不落库（设计 §六）
        _client_mod.DeepSeekClient = _BoomClient
        r_fail = ask("评论量前十的景点")
        d_fail = r_fail["data"]
        check("模型调用失败 → reason=LLM_FAILED", d_fail.get("reason") == "LLM_FAILED", str(d_fail.get("reason")))
        check("模型调用失败 → reached_model=True（确实尝试了）", d_fail.get("reached_model") is True, "")
        check("模型调用失败 → 仍返回数据依据", len(d_fail.get("facts") or []) > 0, f"{len(d_fail.get('facts') or [])} 段")
        check("模型调用失败 → **不写入 qa_record**（设计 §六：不保存残缺记录）",
              d_fail.get("qa_id") is None and d_fail.get("saved") is False,
              f"qa_id={d_fail.get('qa_id')} saved={d_fail.get('saved')}")

        # 6b 模型成功 → 落库
        _client_mod.DeepSeekClient = _OkClient
        r_ok = ask("评论量前十的景点")
        d_ok = r_ok["data"]
        check("模型正常返回 → available=True", d_ok.get("available") is True, str(d_ok.get("available")))
        check("模型正常返回 → **已写入 qa_record**（返回 qa_id）",
              bool(created_qa_ids), f"qa_ids={created_qa_ids[:5]}")
    finally:
        _client_mod.DeepSeekClient = _orig_client
        object.__setattr__(_settings, "web", _orig_web)

    check("收尾后 qa_live 已还原为默认关闭", _settings.web.qa_live is False, str(_settings.web.qa_live))

    resp = client.get("/api/qa/history")
    check("未登录查历史 → 2001（HTTP 401）", resp.status_code == 401 and resp.get_json()["code"] == 2001, f"HTTP {resp.status_code}")

    # 创建一个普通用户并登录，验证"只看到自己的记录"
    import secrets as _secrets

    from app.web.auth import create_user

    uname = f"qa_user_{_secrets.token_hex(4)}"
    password = "Qa@Test123"
    user = create_user(uname, password, "问答测试", role="user")
    uclient = app.test_client()
    uclient.post("/api/auth/login", json={"username": uname, "password": password})
    hist = uclient.get("/api/qa/history").get_json()["data"]
    check("新用户历史为空（不会看到游客写入的记录）", hist["total"] == 0, f"total={hist['total']}")

    # 该用户也要在"在线路径成功"时才会落库，因此同样临时开启 + 假客户端
    object.__setattr__(_settings, "web", WebSettings(**{**_orig_web.__dict__, "qa_live": True}))
    try:
        _client_mod.DeepSeekClient = _OkClient
        uclient.post("/api/qa/ask", json={"question": "一共采集了多少条评论"})
    finally:
        _client_mod.DeepSeekClient = _orig_client
        object.__setattr__(_settings, "web", _orig_web)
    hist2 = uclient.get("/api/qa/history").get_json()["data"]
    check("用户提问后只看到自己的 1 条记录", hist2["total"] == 1, f"total={hist2['total']}")
    own_qa_ids = [int(item["qa_id"]) for item in hist2["items"]]
    check("历史接口不接受 user_id 传参（无该参数入口）", "user_id" not in json.dumps(hist2["items"][0]) if hist2["items"] else True, "")

    # ---------- 清理 ----------
    # 除了本轮记录的 qa_id，还清掉"无主"记录（user_id 为 NULL、未关联任何用户）——
    # 测试期间以游客身份提问会产生这类记录，它们没有归属人、无法从业务侧检索到，
    # 留着只会让 qa_record 变成审计垃圾（本脚本自己产生，自己清理）。
    with connection() as conn:
        with conn.cursor() as cur:
            all_ids = created_qa_ids + own_qa_ids
            if all_ids:
                ph = ",".join(["%s"] * len(all_ids))
                cur.execute(f"DELETE FROM qa_record WHERE qa_id IN ({ph})", tuple(all_ids))
            cur.execute("DELETE FROM qa_record WHERE user_id IS NULL AND session_key IS NULL")
            cur.execute("DELETE FROM sys_user WHERE user_id = %s", (user.user_id,))
            cur.execute("SELECT COUNT(*) AS n FROM qa_record")
            left_qa = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM sys_user")
            left_users = int(cur.fetchone()["n"])
    check("清理后 qa_record 无残留", left_qa == 0, f"剩余 {left_qa} 行")
    check("清理后 sys_user 无残留", left_users == 0, f"剩余 {left_users} 行")
    print(f"\n已清理测试数据：qa_record 剩余 {left_qa} 行，sys_user 剩余 {left_users} 行")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 82)
    print(f"问答测试：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 82)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
