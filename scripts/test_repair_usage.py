# -*- coding: utf-8 -*-
"""C-BAT-05 修复轮 usage 统计测试（**零 API 消费**，用脚本化假客户端）。

## 修的是什么
评论语义分析在"首次返回校验不通过"时会做一次**修复轮**调用。修复轮是
**第二次真实且计费的 API 请求**，但原实现里它返回的 `usage` 被直接丢弃：

    stats.record_success(response)                     # 只记首次
    _build_api_record(row, result, response.usage)     # 只写首次

后果：`CallStats` 的费用汇总与 `raw_json.usage` 都**少算一次调用**，
与"运行结束后输出**实际** usage 与实际费用"的要求相矛盾（§7.2）。
修复方式见 `semantic_analysis.merge_call_results()`：把两次调用合并成一个
`ChatResult`（usage 相加、attempts 相加），并额外把逐次明细写进
`raw_json.usage_calls`。

## 测法（不产生任何真实请求）
用**脚本化假客户端**：第一次返回非法 JSON（必然触发 `ValidationError`），
第二次返回合法 JSON（修复轮成功）。然后断言：
    ① 两次调用的 usage 都被计入 `CallStats`；
    ② 总 token = 第一次 + 修复轮；
    ③ `raw_json.usage` 是两次之和，且 `raw_json.usage_calls` 保留明细；
    ④ 仍然只写"最终成功的那一次"的结果（业务语义不变）；
    ⑤ 假客户端自己记录调用次数（证明真的调了两次，而不是"合并不存在的调用"）。
"""

from __future__ import annotations

import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.batch.semantic_analysis import (
    SemanticStats,
    _build_api_record,
    call_batch,
    merge_call_results,
)
from app.llm.client import CallStats, ChatResult

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# 让 `validate_semantic` **真的抛 ValidationError**：必须不是合法 JSON。
# （只缺字段通常不会抛——校验器会"按实际保留"并记 repairs。这个坑在
#  `test_report_usage.py` 里已经踩过一次，这里沿用同样的做法。）
INVALID = "这不是 JSON {"

BODY = "布达拉宫的风景非常壮观，值得专程前往。"

# evidence 必须是正文的连续子串，否则方面会被校验拦下（这本身是另一条规则，不是本测试的目标）
VALID = json.dumps(
    {
        "polarity": "positive",
        "intensity": 4,
        "aspects": [{"aspect": "景观体验", "polarity": "positive", "evidence": "风景非常壮观"}],
        "keywords": ["布达拉宫", "壮观"],
        "summary": "称赞布达拉宫景观壮观。",
    },
    ensure_ascii=False,
)

# 两次调用的"已知用量"（故意取不同值，便于发现"只记了其中一次"这类错误）
FIRST_USAGE = {"prompt_tokens": 300, "completion_tokens": 100, "total_tokens": 400}
REPAIR_USAGE = {"prompt_tokens": 320, "completion_tokens": 120, "total_tokens": 440}
TOTAL = {"prompt_tokens": 620, "completion_tokens": 220, "total_tokens": 840}


class ScriptedClient:
    """按脚本依次返回响应的假客户端（**不发起任何网络请求**）。"""

    def __init__(self, replies: list[tuple[str, dict[str, int]]]) -> None:
        self._replies = replies
        self.calls = 0

    def chat(self, messages, temperature=0.1, max_tokens=512):  # noqa: ANN001, ARG002
        content, usage = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return ChatResult(content=content, model="scripted", usage=usage, latency_ms=5, attempts=1)


def row() -> dict:
    return {"comment_id": 900000001, "spot_id": 1, "spot_name": "布达拉宫", "content": BODY}


def main() -> int:
    print("=" * 88)
    print("C-BAT-05 修复轮 usage 统计测试（脚本化假客户端；零 API 消费）")
    print("=" * 88)

    # ---------- ① 合并函数本身 ----------
    print("\n[1] merge_call_results()：两次调用必须相加，且明细不丢")
    first = ChatResult(content=INVALID, model="scripted", usage=FIRST_USAGE, latency_ms=5, attempts=1)
    repair = ChatResult(content=VALID, model="scripted", usage=REPAIR_USAGE, latency_ms=7, attempts=1)
    merged, breakdown = merge_call_results(first, repair)
    check("usage 三个键都等于两次之和",
          merged.usage == TOTAL, f"{merged.usage}")
    check("总 token = 第一次 + 修复轮",
          merged.usage["total_tokens"] == FIRST_USAGE["total_tokens"] + REPAIR_USAGE["total_tokens"],
          f"{FIRST_USAGE['total_tokens']} + {REPAIR_USAGE['total_tokens']} = {merged.usage['total_tokens']}")
    check("attempts 相加（让 retry_count 能反映这次额外请求）",
          merged.attempts == 2, f"attempts={merged.attempts}")
    check("content 取**最终成功**的那一次（业务语义不变）",
          merged.content == VALID, "取修复轮内容")
    check("明细保留两次调用（first / repair）",
          [b["call"] for b in breakdown] == ["first", "repair"], str([b["call"] for b in breakdown]))
    check("明细里两次的 token 都完整",
          breakdown[0]["total_tokens"] == 400 and breakdown[1]["total_tokens"] == 440,
          f"{breakdown[0]['total_tokens']} / {breakdown[1]['total_tokens']}")

    # ---------- ② 生产路径 call_batch：CallStats 必须含修复轮 ----------
    print("\n[2] call_batch()：修复轮触发时 CallStats 必须包含两次调用")
    client = ScriptedClient([(INVALID, FIRST_USAGE), (VALID, REPAIR_USAGE)])
    stats = CallStats()
    records, failures = call_batch(client, [row()], concurrency=1, stats=stats, mode="real")

    check("假客户端确实被调用了两次（修复轮真的发生）", client.calls == 2, f"calls={client.calls}")
    check("无失败项（修复轮成功即成功）", not failures, f"failures={len(failures)}")
    check("恰好产出 1 条结果记录", len(records) == 1, f"records={len(records)}")
    check("CallStats 汇总的输入 token = 两次之和",
          stats.prompt_tokens == TOTAL["prompt_tokens"],
          f"{stats.prompt_tokens} vs 期望 {TOTAL['prompt_tokens']}")
    check("CallStats 汇总的输出 token = 两次之和",
          stats.completion_tokens == TOTAL["completion_tokens"],
          f"{stats.completion_tokens} vs 期望 {TOTAL['completion_tokens']}")
    check("CallStats 总 token = 840（不是只有首次的 400）",
          stats.total_tokens == TOTAL["total_tokens"], f"{stats.total_tokens}")
    check("retry_count = 1（修复轮这次额外请求被看见）",
          stats.retry_count == 1, f"attempts={stats.attempts} requests={stats.requests}")

    # ---------- ③ raw_json：usage 之和 + 明细 ----------
    print("\n[3] raw_json：usage 为两次之和，usage_calls 保留明细")
    raw = records[0]["raw"]
    check("raw_json.usage 是两次之和（不再是只有首次）",
          raw.get("usage") == TOTAL, f"{raw.get('usage')}")
    check("raw_json.usage_calls 存在且含两次明细",
          isinstance(raw.get("usage_calls"), list) and len(raw["usage_calls"]) == 2,
          f"{raw.get('usage_calls')}")
    check("usage_calls 里两次的调用名正确",
          [c.get("call") for c in raw.get("usage_calls", [])] == ["first", "repair"],
          str([c.get("call") for c in raw.get("usage_calls", [])]))
    check("usage_calls 明细 token 与假客户端给的一致",
          [c.get("total_tokens") for c in raw["usage_calls"]] == [400, 440],
          str([c.get("total_tokens") for c in raw["usage_calls"]]))
    check("结果本身仍是最终成功的那次（polarity 正确）",
          records[0]["result"].polarity == "positive", records[0]["result"].polarity)

    # ---------- ④ 向后兼容：无修复轮时不得凭空多出明细 ----------
    print("\n[4] 向后兼容：不发生修复轮时行为不变")
    client_ok = ScriptedClient([(VALID, FIRST_USAGE)])
    stats_ok = CallStats()
    records_ok, failures_ok = call_batch(client_ok, [row()], concurrency=1, stats=stats_ok, mode="real")
    check("只调用一次", client_ok.calls == 1, f"calls={client_ok.calls}")
    check("CallStats 只记一次用量", stats_ok.total_tokens == 400, f"{stats_ok.total_tokens}")
    check("retry_count = 0（没有修复轮）", stats_ok.retry_count == 0, f"{stats_ok.retry_count}")
    check("raw_json.usage 就是那一次的量", records_ok[0]["raw"].get("usage") == FIRST_USAGE, "")
    check("不写 usage_calls（避免给只调了一次的结果添噪音）",
          "usage_calls" not in records_ok[0]["raw"], "键不存在")

    # ---------- ⑤ 既有读取方仍兼容 ----------
    print("\n[5] 兼容性：旧读取方（只认 usage 三个键）不受影响")
    check("raw_json.usage 仍是同样的三个键",
          set(raw["usage"].keys()) == {"prompt_tokens", "completion_tokens", "total_tokens"},
          str(sorted(raw["usage"].keys())))
    check("_build_api_record 未传 usage_calls 时也能正常工作",
          "usage_calls" not in _build_api_record(row(), records[0]["result"], FIRST_USAGE, mode="real")["raw"],
          "可选参数，缺省即不写")

    # ---------- ⑥ 零成本自证 ----------
    print("\n[6] 零成本自证")
    check("全程未使用真实客户端（ScriptedClient 无任何网络导入）",
          "requests" not in type(client).__module__, type(client).__module__)
    check("假客户端调用次数可复核（2 + 1 = 3 次均为本地）",
          client.calls + client_ok.calls == 3, f"{client.calls} + {client_ok.calls}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"修复轮 usage 统计测试：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：修复轮这一次真实计费的调用，已同时进入 CallStats 与 raw_json，")
        print("      费用事后可复核；未发生修复轮时行为保持不变。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
