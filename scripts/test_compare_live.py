# -*- coding: utf-8 -*-
"""M4 景点对比「在线解读」专项测试（**全部用假客户端，零 API 消费**）。

## 为什么需要它
项目此前有 `test_qa.py`（问答在线链路）与 `test_failure_fallback.py`（对比解读**失败**仍给数据），
但**没有**覆盖对比解读**成功**那条路径：JSON 结构、条数上限、违禁判定词、
数字一致性、修复轮，以及"解读不落库"。

## 覆盖内容（对应验收清单 A–N 中与 Compare 相关的部分）
    [1] 关闭状态（默认配置）：不构造客户端、返回 LIVE_DISABLED、指标照常
    [2] 开启 + 合法 JSON：结构/条数/长度、prompt 版本、token 用量、无告警
    [3] 违禁判定词（"更值得"）→ need_review=1，且**内容不被删除**
    [4] 编造数字 → need_review=1（数字一致性核对生效，且千分位/百分数不误报）
    [5] 首次非法 JSON → 触发 1 次修复轮 → 第二次合法则成功
    [6] 两次都非法 / 空结果 → 降级 LLM_FAILED，指标与方面照常返回
    [7] 模型异常（网络/超时）→ 降级 LLM_FAILED，指标照常返回
    [8] 解读**不写任何数据库**（调用前后逐表行数一致）
    [9] Prompt 约束静态自检（QA 与 Compare 的硬性约束都在模板里）
    [10] 上下文纪律：送去模型的是结构化"对比事实"，不含原始评论正文

【数据卫生】本测试只读数据库；不做任何写入，收尾复核行数回到基线。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.llm.client as client_mod
from app.config import WebSettings, settings
from app.db import query_one
from app.llm.client import LlmError
from app.llm.prompts import COMPARE_PROMPT_VERSION, COMPARE_SYSTEM_PROMPT, QA_SYSTEM_PROMPT
from app.web.compare import compare_spots

RESULTS: list[tuple[str, bool, str]] = []

# 固定的真实景点组合（布达拉宫 564 / 纳木措景区 196）
SPOT_A, SPOT_B = 564, 196

# 需要复核行数的表（解读不应写入任何一张）
COUNTED_TABLES = (
    "review", "spot", "comment_semantic", "aspect", "spot_fact_package", "spot_report",
    "qa_record", "analysis_task", "task_log", "sys_user",
)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def row_counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for table in COUNTED_TABLES:
        out[table] = int(query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"])
    return out


class FakeClient:
    """按顺序吐出预设内容；调用计数挂在**每个脚本化的子类**上（便于断言修复轮）。

    注意：`compare.py` 内部是 `DeepSeekClient()` **构造**客户端，
    因此注入的必须是"类"而不是实例（与 test_qa.py / test_failure_fallback.py 的做法一致）。
    """

    responses: list = []
    calls = 0
    seen_prompts: list = []

    def __init__(self, *a, **k):
        pass

    def chat(self, messages, *, temperature, max_tokens):
        cls = type(self)
        cls.calls += 1
        cls.seen_prompts.append(json.dumps(messages, ensure_ascii=False))
        item = cls.responses[min(cls.calls - 1, len(cls.responses) - 1)]
        if isinstance(item, Exception):
            raise item

        class _R:
            content = item
            model = "fake-compare"
            usage = {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200}
            latency_ms = 5
            attempts = 1

        return _R()


def fake_client(responses) -> type:
    """生成一个"脚本化"的假客户端类（每个用例独立计数，互不干扰）。"""
    return type(
        "ScriptedFakeClient",
        (FakeClient,),
        {"responses": list(responses), "calls": 0, "seen_prompts": []},
    )


class ExplodingClient:
    """一旦被构造就报错：用于证明"关闭状态根本不会触碰模型"。"""

    def __init__(self, *a, **k):
        raise AssertionError("关闭状态下不应构造 DeepSeekClient")


def run_compare(live: bool, fake_cls):
    """在给定的 live 开关与假客户端（类）下跑一次对比；finally 保证还原。"""
    orig_web, orig_client = settings.web, client_mod.DeepSeekClient
    object.__setattr__(settings, "web", WebSettings(**{**orig_web.__dict__, "compare_live": live}))
    if fake_cls is not None:
        client_mod.DeepSeekClient = fake_cls
    try:
        return compare_spots(SPOT_A, SPOT_B)
    finally:
        client_mod.DeepSeekClient = orig_client
        object.__setattr__(settings, "web", orig_web)


def main() -> int:
    print("=" * 88)
    print("M4 景点对比「在线解读」专项测试（假客户端，零 API 消费）")
    print("=" * 88)

    base_counts = row_counts()
    print(f"\n基线行数：review={base_counts['review']}、aspect={base_counts['aspect']}、"
          f"spot_report={base_counts['spot_report']}、qa_record={base_counts['qa_record']}")

    # ---------------- [1] 关闭状态 ----------------
    print("\n[1] 关闭状态（默认配置）：不触碰模型、指标照常返回")
    check("默认 compare_live 为 False", settings.web.compare_live is False, str(settings.web.compare_live))
    off = run_compare(False, ExplodingClient)
    interp = off["interpretation"]
    check("关闭时 reason=LIVE_DISABLED", interp.get("reason") == "LIVE_DISABLED", str(interp.get("reason")))
    check("关闭时 available=False 且 differences 为空",
          interp.get("available") is False and interp.get("differences") == [], "")
    check("关闭时**不构造客户端**（注入即炸的客户端未被触发）", True, "未抛异常")
    check("关闭时指标对比照常返回", len(off.get("indicator_compare") or []) >= 5,
          f"{len(off.get('indicator_compare') or [])} 项")
    facts = off["facts"]
    check("关闭时方面对比照常返回", len(facts.get("aspects") or []) > 0,
          f"{len(facts.get('aspects') or [])} 个方面")
    check("关闭时样本量信息照常返回", bool(off.get("sample_size")), str(off.get("sample_size")))

    # 用真实事实里的数字构造"模型回答"，这样数字一致性核对应当是干净的
    fa, fb, diff = facts["spot_a"], facts["spot_b"], facts["diff"]
    good_payload = {
        "differences": [
            f"{fa['spot_name']}与{fb['spot_name']}的评论量分别为 {fa['review_count']} 条与 {fb['review_count']} 条",
            f"两处样本量相差 {diff['sample_ratio']} 倍，平均评分相差 {diff['avg_score_delta']} 分",
        ],
        "possible_reasons": ["评论量差异可能影响结论的稳定性"],
        "reliability_note": "",
    }
    good_json = json.dumps(good_payload, ensure_ascii=False)

    # ---------------- [2] 开启 + 合法 JSON ----------------
    print("\n[2] 开启状态 + 合法 JSON：结构、版本、用量、无告警")
    fake = fake_client([good_json])
    on = run_compare(True, fake)
    it = on["interpretation"]
    check("available=True", it.get("available") is True, str(it.get("available")))
    check("differences 非空且条数 <=5", 1 <= len(it.get("differences") or []) <= 5,
          f"{len(it.get('differences') or [])} 条")
    check("possible_reasons 条数 <=4", len(it.get("possible_reasons") or []) <= 4,
          f"{len(it.get('possible_reasons') or [])} 条")
    check("每条差异 <=50 字", all(len(x) <= 50 for x in (it.get("differences") or [])),
          str([len(x) for x in (it.get("differences") or [])]))
    check(f"prompt_version = {COMPARE_PROMPT_VERSION}（模板已改需递增）",
          it.get("prompt_version") == COMPARE_PROMPT_VERSION, str(it.get("prompt_version")))
    check("记录 token 用量（成本可核算）", int(it.get("token_usage") or 0) > 0, str(it.get("token_usage")))
    check("生成时间已填写", bool(it.get("generated_at")), str(it.get("generated_at")))
    check("合法内容不产生 need_review 告警", not it.get("need_review"),
          f"need_review={it.get('need_review')} note={it.get('need_review_note')}")
    check("模型只被调用 1 次（合法则不进修复轮）", fake.calls == 1, f"{fake.calls} 次")

    # ---------------- [3] 违禁判定词 ----------------
    print("\n[3] 违禁判定词（替用户做选择）→ 标记待复核，但**不删除内容**")
    verdict_payload = dict(good_payload)
    verdict_payload["differences"] = list(good_payload["differences"]) + ["因此更值得优先安排"]
    v = run_compare(True, fake_client([json.dumps(verdict_payload, ensure_ascii=False)]))
    vi = v["interpretation"]
    check("检出违禁表述 → need_review=1", bool(vi.get("need_review")), str(vi.get("need_review")))
    # 【2026-10 表达规范】提示文案改为面向游客的说法（"偏向性表述"），
    # 不再出现"判定优劣（设计禁止）"这类开发/设计语言。
    check("need_review_note 用游客能读懂的说法说明原因",
          "偏向性" in (vi.get("need_review_note") or ""),
          (vi.get("need_review_note") or "")[:60])
    check("解读内容**仍然返回**（只标记不改写）", len(vi.get("differences") or []) >= 2,
          f"{len(vi.get('differences') or [])} 条")

    # ---------------- [4] 编造数字 ----------------
    print("\n[4] 编造数字 → 数字一致性核对生效（且千分位/百分数不误报）")
    fake_num = dict(good_payload)
    fake_num["differences"] = list(good_payload["differences"]) + ["另有 987654 条评论来自其他平台"]
    n = run_compare(True, fake_client([json.dumps(fake_num, ensure_ascii=False)]))
    ni = n["interpretation"]
    note = ni.get("need_review_note") or ""
    check("未命中数字 → need_review=1", bool(ni.get("need_review")), str(ni.get("need_review")))
    # 【2026-10 表达规范】提示面向游客，只说明"有个别数字未能一一对应"，
    # **不再把未命中的原始数字（内部核对细节）打到页面上**。
    check("提示说明了原因（数字未能一一对应）", "数字" in note, note[:80])
    check("提示不打印未命中的原始数字（内部核对细节不上屏）", "987654" not in note, note[:80])
    check("千分位 / 百分数写法不被误判为编造",
          "3,965" not in note and "3,057" not in note and "84" not in note, note[:80])

    # ---------------- [5] 修复轮 ----------------
    print("\n[5] 首次非法 JSON → 修复轮一次 → 第二次合法则成功")
    repair_fake = fake_client(["{这不是合法 JSON", good_json])
    rep = run_compare(True, repair_fake)
    check("修复后成功（available=True）", rep["interpretation"].get("available") is True, "")
    check("确实发生了 2 次调用（首轮 + 修复轮）", repair_fake.calls == 2, f"{repair_fake.calls} 次")

    # ---------------- [6] 两次都非法 / 空结果 ----------------
    print("\n[6] 两次都非法 / 空结果 → 降级，但数据部分不受影响")
    bad_fake = fake_client(["not-json", "still-not-json"])
    bad = run_compare(True, bad_fake)
    bi = bad["interpretation"]
    check("降级为 LLM_FAILED", bi.get("reason") == "LLM_FAILED", str(bi.get("reason")))
    check("降级后 available=False 且无解读文本",
          bi.get("available") is False and not bi.get("differences"), "")
    check("降级后**指标对比照常返回**", len(bad.get("indicator_compare") or []) >= 5,
          f"{len(bad.get('indicator_compare') or [])} 项")
    check("降级后方面对比照常返回", len((bad.get("facts") or {}).get("aspects") or []) > 0, "")
    check("非法 JSON 已尝试修复轮（共 2 次）", bad_fake.calls == 2, f"{bad_fake.calls} 次")

    empty_fake = fake_client(["", ""])
    emp = run_compare(True, empty_fake)
    check("空结果同样降级为 LLM_FAILED", emp["interpretation"].get("reason") == "LLM_FAILED",
          str(emp["interpretation"].get("reason")))

    # ---------------- [7] 模型异常 ----------------
    print("\n[7] 模型异常（网络/超时）→ 降级，数据部分不受影响")
    boom = fake_client([LlmError("请求超时（60s）", kind="network")])
    b = run_compare(True, boom)
    check("异常被翻译为 LLM_FAILED 降级", b["interpretation"].get("reason") == "LLM_FAILED", "")
    check("异常不冒泡到接口层（页面不会整体报错）", isinstance(b, dict), type(b).__name__)
    check("降级后指标仍在", len(b.get("indicator_compare") or []) >= 5, "")

    # ---------------- [8] 不写数据库 ----------------
    print("\n[8] 解读不写任何数据库（调用前后逐表行数一致）")
    after_counts = row_counts()
    changed = {k: (base_counts[k], after_counts[k]) for k in COUNTED_TABLES if base_counts[k] != after_counts[k]}
    check(f"{len(COUNTED_TABLES)} 张表行数逐表未变", not changed, str(changed) or "全部一致")

    # ---------------- [9] Prompt 约束静态自检 ----------------
    print("\n[9] Prompt 硬性约束自检（QA 与 Compare 的约束都在模板里）")
    compare_needs = (
        "不得使用你自身的知识",
        "禁止判定",
        "推荐结论",
        "样本量悬殊",
        "不得把缺失当成 0",
        "不得为了对比而制造显著差异",
        "替用户做选择",
        "不要照抄字段名",
        "只输出 JSON",
    )
    for text in compare_needs:
        check(f"Compare 约束含「{text}」", text in COMPARE_SYSTEM_PROMPT, "")
    qa_needs = (
        "不得使用你自身的知识",
        "引用数字必须与结构化事实完全一致",
        "不得做推荐或优劣判定",
        "不得把缺失当成 0",
        "两套不同统计方式的结果不得混用",
        "不要使用\"口径\"这类内部术语",
        "数据说明",
        "不得出现任何内部名称",
        "不要输出 JSON",
    )
    for text in qa_needs:
        check(f"QA 约束含「{text}」", text in QA_SYSTEM_PROMPT, "")

    # ---------------- [10] 上下文纪律 ----------------
    print("\n[10] 上下文纪律：只送结构化对比事实，不含原始评论正文")
    prompt_text = repair_fake.seen_prompts[1] if len(repair_fake.seen_prompts) > 1 else ""
    check("送去模型的是「对比事实」", "对比事实" in prompt_text, "")
    check("送了输出 Schema", "differences" in prompt_text and "possible_reasons" in prompt_text, "")
    check("不含原始评论字段（评论正文/点赞/昵称）",
          not any(k in prompt_text for k in ("评论内容", "评论正文", "用户昵称", "点赞数")), "")
    check("上下文体积受控（< 8000 字符）", 0 < len(prompt_text) < 8000, f"{len(prompt_text)} 字符")

    # ---------------- 收尾 ----------------
    final_counts = row_counts()
    check("收尾：所有被核对的表仍与基线一致", final_counts == base_counts,
          str({k: (base_counts[k], final_counts[k]) for k in COUNTED_TABLES if base_counts[k] != final_counts[k]})
          or "一致")
    check("收尾：compare_live 已还原为默认关闭", settings.web.compare_live is False, "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"景点对比在线解读测试：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：对比解读的在线链路（生成 → 校验 → 告警 → 降级）全部落到实处，")
        print("      且解读不写任何数据库、失败时数据部分照常可用。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
