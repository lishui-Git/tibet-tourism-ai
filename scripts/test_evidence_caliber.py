# -*- coding: utf-8 -*-
"""evidence 校验口径一致性测试（preflight ↔ 正式 validator）。

## 为什么需要这个测试
全量 semantic 跑完后，`--stage preflight` 报 `evidence_not_in_content = 11` 并因此 **BLOCKED**，
但用**正式 validator 的函数**逐条复核，这 11 条**全部合法**（11/11）。原因是两处各写了一套比较：

| 位置 | 判据 | 对 Unicode 空白 |
|---|---|---|
| `validators._evidence_is_substring` | 严格子串 **或** 去掉所有空白后再比 | `re` 的 `\\s` 覆盖全角空格 U+3000、不间断空格 U+00A0 等 |
| `preflight` 原实现 | MySQL `LOCATE(evidence, content)` | **不覆盖**，只认字面连续 |

于是"证据里含全角空格"这类**完全合法**的行被误判为"模型编造证据"。

## 修法与测法
修法不是"在 SQL 里再写一遍空白归一化"（那只是把漂移换个地方），
而是让 preflight **直接复用 validator 本函数**做最终判定，SQL 只负责粗筛候选。
本测试固定三件事：
    ① 两处判据**必须**是同一实现（结构性断言：preflight 用的是 validator 的函数）；
    ② 构造样例上两者**逐例一致**（含全角空格/不间断空格/真正编造）；
    ③ 真正编造的 evidence **仍然**被判为违规且仍属**阻断项**（修复没有放宽要求）。
"""
from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.db import query_all  # noqa: E402
from app.llm import preflight as pf  # noqa: E402
from app.llm.validators import _evidence_is_substring  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 88)
    print("evidence 校验口径一致性测试（preflight ↔ validator；只读、零 API）")
    print("=" * 88)

    # ---------- ① 结构性：两处必须是同一实现 ----------
    print("\n[1] 口径统一的实现方式（不是各写一套规则去对齐）")
    check("preflight 直接复用 validators 的判断函数",
          pf._validator_evidence_ok is _evidence_is_substring,
          "preflight._validator_evidence_ok is validators._evidence_is_substring")
    check("preflight 保留 SQL 粗筛（候选集）而不在 SQL 里重写空白规则",
          "LOCATE(a.evidence, r.content) = 0" in pf.EVIDENCE_CANDIDATE_SQL,
          "候选 SQL 仍用严格匹配（作为粗筛，最终判定交给 validator）")

    # ---------- ② 构造样例：两者逐例一致 ----------
    print("\n[2] 构造样例上的判定一致性（含 Unicode 空白）")
    cases: list[tuple[str, str, str, bool]] = [
        ("精确子串", "景色很美", "这里的景色很美，值得一去", True),
        ("半角空格差异", "景色 很美", "这里的景色很美，值得一去", True),
        ("全角空格 U+3000", "景色\u3000很美", "这里的景色很美，值得一去", True),
        ("不间断空格 U+00A0", "景色\u00a0很美", "这里的景色很美，值得一去", True),
        ("换行/制表差异", "景色\n很美", "这里的景色很美，值得一去", True),
        ("真正编造（原文没有）", "青藏公路美翻了", "这里的景色很美，值得一去", False),
        ("空 evidence", "", "这里的景色很美", False),
    ]
    for label, evidence, content, expected in cases:
        got = _evidence_is_substring(evidence, content)
        check(f"validator 判定正确：{label}", got == expected, f"期望 {expected}，实得 {got}")

    # ---------- ③ 库内实测：与 SQL 粗筛对照 ----------
    print("\n[3] 库内实测：粗筛候选 vs validator 复核")
    strict_rows = query_all(pf.EVIDENCE_CANDIDATE_SQL)
    genuine, whitespace_only = pf.collect_evidence_violations()
    check("粗筛候选数 = 真违规 + 仅空白差异",
          len(strict_rows) == len(genuine) + len(whitespace_only),
          f"{len(strict_rows)} = {len(genuine)} + {len(whitespace_only)}")
    check("**真违规为 0**（11 条均为空白写法差异，属 validator 合法范围）",
          len(genuine) == 0,
          f"genuine={len(genuine)}，whitespace_only={len(whitespace_only)}")
    check("仅空白差异的行**确实**被 validator 判为合法（逐条复核，非抽样）",
          all(_evidence_is_substring(str(r.get("evidence") or ""), str(r.get("content") or ""))
              for r in whitespace_only),
          f"复核 {len(whitespace_only)} 条")

    # ---------- ④ preflight 汇总中的取值口径 ----------
    print("\n[4] preflight 汇总取值口径")
    collected = pf.collect()
    check("preflight 的 evidence_not_in_content 等于 validator 复核后的真违规数",
          collected.integrity.get("evidence_not_in_content") == len(genuine),
          f"汇总 {collected.integrity.get('evidence_not_in_content')} vs 复核 {len(genuine)}")
    check("粗筛候选数单独保留（便于知情，不参与判定）",
          collected.integrity.get("evidence_strict_mismatch") == len(strict_rows),
          f"汇总 {collected.integrity.get('evidence_strict_mismatch')} vs 粗筛 {len(strict_rows)}")
    check("仅空白差异的行被记录到 evidence_whitespace_only（可追溯）",
          len(collected.evidence_whitespace_only) == len(whitespace_only),
          f"{len(collected.evidence_whitespace_only)} 条")
    check("该情形只出现在 warnings，不出现在 issues（不阻断）",
          any("仅空白写法不同" in w for w in collected.warnings)
          and not any("evidence" in i for i in collected.issues),
          f"warnings 命中={any('仅空白写法不同' in w for w in collected.warnings)}")

    # ---------- ⑤ 真正编造仍然阻断 ----------
    print("\n[5] 修复没有放宽要求：真正编造仍判违规并可阻断")
    fabricated = [
        ("青藏公路美翻了（评论里没有）", "这里的景色很美，值得一去"),
        ("完全不存在的一句话", "门票有点贵但是风景很好"),
    ]
    check("真正编造的 evidence 均被判为违规",
          all(not _evidence_is_substring(e, c) for e, c in fabricated),
          "2/2 判违规")
    # 直接核对 blocking_keys 的实际内容（从 collect() 源码里读，避免"断言了个空条件"）
    import inspect

    src = inspect.getsource(pf.collect)
    check("collect() 的阻断项清单包含 evidence_not_in_content（真编造会 BLOCKED）",
          '"evidence_not_in_content"' in src,
          "出现在 blocking_keys 元组中")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"evidence 口径一致性测试：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：preflight 与正式 validator 使用**同一份** evidence 判定逻辑，")
        print("      空白写法差异不再误报为 BLOCKED，而真正编造的 evidence 仍会被阻断。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
