# -*- coding: utf-8 -*-
"""CC-3 / FR-QA-10 核对：问答上下文**只传结构化事实**且总量受控。

设计要求（§15.E.3 / CC-3）：
    · 只传结构化事实，**绝不传原始评论全集**；
    · 上下文总量 ≤ **3,000 字**、条目 ≤ **30**；
    · 每段标注来源表，口径单列一段。

为什么值得单独测：这是"问答不是聊天机器人"这条设计主张的技术落点——
一旦把原始评论塞进上下文，成本、越界风险与"模型自己知道答案"的口子会同时打开。

## 实测的两种触发方式
    ① **条目数上限**：给 60 条小条目 → 恰好截到 30 条；
    ② **字符数上限**：给超长条目 → 触发截断并**留下说明**（不静默丢弃）。

> 已知细节（如实记录）：口径段是在事实之后**无条件追加**的，
> 因此"事实刚好填满 3,000 字"时总量会略超（真实口径 46–100 字量级，余量约 18%）。
> 实际服务中上下文为 299–755 字，远低于上限，故未改动实现；此处记录以免误判。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.web.qa import (
    CONTEXT_MAX_CHARS,
    CONTEXT_MAX_ITEMS,
    QaFacts,
    build_context,
    classify_question,
    match_spots,
    retrieve_facts,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def chars_of(blocks: list[dict]) -> int:
    return sum(len(json.dumps(b, ensure_ascii=False)) for b in blocks)


def items_of(blocks: list[dict]) -> int:
    return sum(len(b.get("items") or []) for b in blocks)


def main() -> int:
    print("=" * 88)
    print(f"CC-3 核对：上下文 ≤{CONTEXT_MAX_CHARS} 字、≤{CONTEXT_MAX_ITEMS} 条、不含原始评论")
    print("=" * 88)

    # ---------- ① 条目数上限 ----------
    print("\n[1] 条目数上限：给 60 条小条目，应恰好截到 30 条")
    f_many = QaFacts(
        question_type="RANKING",
        blocks=[{"source": "review", "title": "小条目", "items": [{"值": i} for i in range(60)]}],
    )
    b_many = build_context(f_many)
    check(f"条目数被截到上限 {CONTEXT_MAX_ITEMS}（不是 60）",
          items_of(b_many) == CONTEXT_MAX_ITEMS, f"实际 {items_of(b_many)}")
    check("截断后仍含来源标注", all(b.get("source") for b in b_many), "")

    # ---------- ② 字符数上限 ----------
    print("\n[2] 字符数上限：给超长条目，应触发截断且留下说明")
    f_big = QaFacts(
        question_type="RANKING",
        blocks=[{"source": "review", "title": "超长条目",
                 "items": [{"值": "z" * 300} for _ in range(60)]}],
    )
    b_big = build_context(f_big)
    c_big = chars_of(b_big)
    has_note = any("上限" in str(b.get("note") or "") for b in b_big)
    check(f"总字符不超过 {CONTEXT_MAX_CHARS}", c_big <= CONTEXT_MAX_CHARS, f"{c_big} 字")
    check("截断时给出说明（不静默丢弃）", has_note,
          next((str(b.get("note")) for b in b_big if b.get("note")), "(无 note)"))

    # ---------- ③ 真实问答路径 ----------
    print("\n[3] 真实问答路径：四种问题类型都在限内")
    real_sizes: list[tuple[str, int, int]] = []
    for q in ("评论量前十的景点", "布达拉宫怎么样", "一共采集了多少条评论", "布达拉宫和纳木措哪个好"):
        qt = classify_question(q)
        spots = match_spots(q)
        blocks = build_context(retrieve_facts(qt, q, spots))
        i, c = items_of(blocks), chars_of(blocks)
        real_sizes.append((q, i, c))
        check(f"『{q}』在限内（条目 {i}、字符 {c}）",
              i <= CONTEXT_MAX_ITEMS and c <= CONTEXT_MAX_CHARS, "")
    worst = max(c for _, _, c in real_sizes)
    check(f"真实上下文留有充足余量（最坏 {worst} 字，上限 {CONTEXT_MAX_CHARS}）",
          worst < CONTEXT_MAX_CHARS * 0.9,
          f"最坏占用 {round(worst / CONTEXT_MAX_CHARS * 100)}%")

    # ---------- ④ 绝不包含原始评论正文 ----------
    print("\n[4] 绝不传原始评论全集（CC-3 的核心）")
    from app.db import query_one

    row = query_one(
        "SELECT content FROM review WHERE CHAR_LENGTH(content) > 500 "
        "ORDER BY CHAR_LENGTH(content) DESC LIMIT 1"
    )
    raw = (row or {}).get("content") or ""
    frag = raw[:40].replace("\n", "").strip()
    blob = json.dumps(b_big, ensure_ascii=False)
    for q in ("布达拉宫怎么样", "评论量前十的景点"):
        qt = classify_question(q)
        blob += json.dumps(build_context(retrieve_facts(qt, q, match_spots(q))), ensure_ascii=False)
    check(f"最长评论（{len(raw)} 字）的内容未出现在上下文里",
          bool(frag) and frag not in blob, f"探针 = {frag[:24]}…")

    # 上下文只应含结构化键（source/title/items/note），没有 content 字段
    keys = {k for b in b_big for k in b.keys()}
    check("上下文段只有结构化字段（source/title/items/note）",
          keys <= {"source", "title", "items", "note"}, str(sorted(keys)))
    item_keys = {k for b in b_big for it in (b.get("items") or []) for k in (it or {}).keys()}
    check("条目字段不含正文类字段（content/评论内容）",
          not ({"content", "评论内容", "评论正文"} & item_keys), str(sorted(item_keys))[:80])

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"CC-3 核对：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：问答上下文只含结构化事实，且总量受 3,000 字 / 30 条约束——")
        print("      『不传原始评论全集』这条设计约束有实测支撑。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
