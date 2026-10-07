# -*- coding: utf-8 -*-
"""文档事实核对：问答手册里的数字与命令，必须和系统实际状态一致（零 API 消费）。

## 为什么需要它
`docs/答辩演示手册.md` 里有两类**会被当场核对**的内容：
    1. **第五节/第六节的命令**——演示时照着敲；写错一个选项名，现场就卡住；
    2. **第七节的事实基线表**——"59,033 条""31 条真实结果""17 张表"……
       如果手册说 31 条而库里是 30 条，评委一查就对不上。

本脚本把这两类都变成断言：
    · 逐条**解析**手册里的命令（只解析参数，**绝不执行**，尤其不执行付费命令）；
    · 逐项**查询数据库**核对手册基线表的数字；
    · 核对 API 路由数与页面路由数。
"""

from __future__ import annotations

import io
import os
import re
import shlex
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DOC = ROOT / "docs" / "答辩演示手册.md"
INDEX = ROOT / "开发上下文索引.md"

# 「明确未做的事」里已被划掉的条目（`~~...~~`）不该再被读成"还没做"。
# 本清单是**人工确认过已完成**的功能，用于防止索引里出现自相矛盾的过期声明。
INDEX_MUST_NOT_CLAIM_MISSING = (
    "26 个接口）未实现",
    "5 个业务页面未做",
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def check_commands(doc: str) -> None:
    """解析手册里的每条命令（只解析，不执行）。"""
    from app.llm.__main__ import build_parser

    parser = build_parser()
    lines = [
        line.strip().lstrip("> ").strip()
        for line in doc.splitlines()
        if ".venv\\Scripts\\python.exe" in line
    ]
    check("手册中确实列有可执行命令", len(lines) >= 8, f"{len(lines)} 条")

    bad: list[str] = []
    parsed = 0
    for line in lines:
        tail = line.split("python.exe", 1)[1].strip().strip("`")
        tail = re.sub(r"[（(].*$", "", tail).strip().rstrip("`。;；")
        if not tail:
            continue
        first = tail.split()[0]
        if first.startswith("scripts\\") or first == "run.py":
            if not (ROOT / first).exists():
                bad.append(f"脚本不存在：{first}")
            continue
        if not tail.startswith("-m app.llm"):
            continue
        argv = shlex.split(tail)[2:]
        try:
            parser.parse_args(argv)
            parsed += 1
        except SystemExit:
            bad.append(" ".join(argv))
    check(f"手册中的 app.llm 命令全部可被 CLI 解析（{parsed} 条）", not bad,
          "；".join(bad) if bad else "")
    # 特别是本轮新增的 replay 必须写在手册里（否则出事后没人知道有这条命令）
    check("手册写明了 --stage replay 的用法（含 --dry-run 先看）",
          "--stage replay --dry-run" in doc and "--stage replay" in doc, "")


def check_baseline(doc: str) -> None:
    """核对手册第七节的事实基线表与数据库实际状态。"""
    from app.db import query_one

    def n(sql: str) -> int:
        return int((query_one(sql) or {}).get("n", 0))

    actual = {
        "评论总数": n("SELECT COUNT(*) AS n FROM review"),
        "景点总数": n("SELECT COUNT(*) AS n FROM spot"),
        "可评价景点(≥100条)": n("SELECT COUNT(*) AS n FROM spot WHERE has_full_evaluation = 1"),
        "规则层已完成": n("SELECT COUNT(*) AS n FROM comment_semantic WHERE source = 'rule'"),
        "真实API已完成": n(
            "SELECT COUNT(*) AS n FROM sentiment WHERE method='deepseek' "
            "AND (JSON_EXTRACT(raw_json,'$.mode') IS NULL OR JSON_UNQUOTE(JSON_EXTRACT(raw_json,'$.mode'))='real') "
            "AND comment_id IN (SELECT comment_id FROM comment_semantic WHERE source='deepseek')"
        ),
        "sentiment(deepseek)": n("SELECT COUNT(*) AS n FROM sentiment WHERE method='deepseek'"),
        "aspect": n("SELECT COUNT(*) AS n FROM aspect"),
        "comment_semantic": n("SELECT COUNT(*) AS n FROM comment_semantic"),
        "spot_fact_package": n("SELECT COUNT(*) AS n FROM spot_fact_package"),
        "spot_report": n("SELECT COUNT(*) AS n FROM spot_report"),
        "数据库表数": n(
            "SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE TABLE_SCHEMA='tibet_review'"
        ),
    }

    # 手册里逐项写明的数字（改动手册时这里要一起改，否则本测试会失败——这正是目的）
    #
    # 【2026-10-07 更新】全量 semantic 已实际跑完（47,702 次付费调用），因此下面这 4 项
    # 从"全量前的小样本基线"更新为"全量完成后的真实终值"：
    #     真实API已完成      31     → 47,731
    #     sentiment(deepseek) 10,259 → 59,030（= 规则 10,228 + 真实 API 47,731 + 复用 1,071）
    #     aspect              60     → 91,280
    #     comment_semantic    10,259 → 59,030
    # 其余项（评论总数 / 景点总数 / 可评价景点 / 规则层 / 表数）**未变**，保持原值。
    # 注意：通用索引与历史日报里出现的 10,259 / 31 属**阶段历史记录**，按"只追加不覆盖"保留，不改。
    expected = {
        "评论总数": 59033,
        "景点总数": 837,
        "可评价景点(≥100条)": 57,
        "规则层已完成": 10228,
        "真实API已完成": 47731,
        "sentiment(deepseek)": 59030,
        "aspect": 91280,
        "comment_semantic": 59030,
        "spot_fact_package": 0,
        "spot_report": 0,
        "数据库表数": 17,
    }
    # 【状态无关断言】景点级结果只有两种合法状态：
    #   · **未生成** → 0 行（Stage 5 之前）；
    #   · **已全量生成** → 等于"可评价景点数"（Stage 5 之后）。
    # "部分生成"（既不是 0 也不是全量）说明生成中断或数据被破坏，必须报错。
    # 这样本测试在 Stage 5 前后**都**成立，不必每次生成完就来改基线。
    eligible = actual["可评价景点(≥100条)"]
    for key in ("spot_fact_package", "spot_report"):
        got = actual[key]
        check(
            f"基线「{key}」∈ {{0（未生成）, {eligible}（已全量生成）}}",
            got in (0, eligible),
            f"实际 {got:,}；可评价景点 {eligible}（部分生成属异常）",
        )
        expected.pop(key, None)

    for key, want in expected.items():
        check(f"基线「{key}」= {want:,}", actual[key] == want, f"实际 {actual[key]:,}")

    # 手册文字里也必须出现这些数字（避免"库对了但手册没更新"）
    for text in ("59,033", "837", "10,228", "47,702", "1,071", "10,259", "17 张"):
        check(f"手册文字含『{text}』", text in doc, "")


def check_routes(doc: str) -> None:
    """核对接口数与页面路由数。"""
    from app.web import create_app

    app = create_app()
    api = sorted(
        r.rule for r in app.url_map.iter_rules() if str(r.rule).startswith("/api/")
    )
    pages = sorted(
        r.rule for r in app.url_map.iter_rules()
        if not str(r.rule).startswith("/api/") and not str(r.rule).startswith("/static")
    )
    print(f"      · API 路由 {len(api)} 个；页面/自检路由 {len(pages)} 个")
    check("API 路由数 ≥ 27（手册口径）", len(api) >= 27, f"实际 {len(api)}")
    check("页面与自检路由数 ≥ 9（手册口径）", len(pages) >= 9, f"实际 {len(pages)}")
    # 手册必须提到这两类规模
    check("手册写明了接口/页面规模", "27 个 API 路由" in doc and "9 个页面" in doc, "")


def check_index_freshness() -> None:
    """`开发上下文索引.md` 不得再声称"接口/页面未实现"，且成本口径必须与预检一致。

    为什么单独查它：这份索引是**给未来（或换人接手时）快速了解进度**用的。
    实测发现它一度仍写着"26 个接口未实现，目前只有 2 个自检口""5 个业务页面未做"，
    还留着 `32 行`/`63 行`/`10,260` 等旧数字与 `¥60–73` 这个与预检冲突的成本区间——
    这类**过期声明比没有文档更危险**：读者会据此误判进度。
    """
    if not INDEX.exists():
        check("开发上下文索引.md 存在", False, str(INDEX))
        return
    text = io.open(INDEX, encoding="utf-8").read()

    for phrase in INDEX_MUST_NOT_CLAIM_MISSING:
        # 允许"划掉式"记录（~~旧说法~~ → 已完成），只要不在正文里当成现状陈述
        offending = [
            line for line in text.splitlines()
            if phrase in line and not line.strip().startswith("- ~~")
        ]
        check(f"索引不再声称『{phrase}』为未完成",
              not offending, offending[0].strip()[:70] if offending else "")

    # 成本口径：索引里不该再出现与预检冲突的旧区间
    for stale in ("¥60–73", "¥60-73", "75.05", "97.67"):
        check(f"索引不含过期成本数字『{stale}』", stale not in text, "")

    # 索引应与预检一致地写出当前口径
    from app.llm.preflight import collect

    pf = collect()
    total_min = pf.cost["total_cost_min_cny"]
    total_max = pf.cost["total_cost_max_cny"]
    # 【状态无关】索引的金额必须与预检**当前口径**一致 —— 但只在"确实还有实质待处理工作"时才有意义。
    # 两阶段都跑完后，待处理只剩补跑个别评论，预算退化为 ¥0.0–0.02；
    # 此时强求索引写出 "0.0" 这种字面值既无意义又易碎，改为要求索引**写明阶段状态**。
    # 这样 Stage 5 之前 / 之后都成立。
    if total_max >= 0.05:
        check(f"索引写出的全量成本与预检一致（¥{total_min}–{total_max}）",
              f"{total_min}" in text and f"{total_max}" in text, "")
    else:
        check("预检已无实质待处理工作（预算 < ¥0.05）时，索引写明阶段状态即可",
              "已完成" in text,
              f"当前预检 ¥{total_min}–{total_max}、剩余调用 {pf.workload['total_api_calls']} 次")


def main() -> int:
    print("=" * 88)
    print("文档事实核对：手册里的命令与基线数字必须与系统一致（零 API 消费）")
    print("=" * 88)
    doc = io.open(DOC, encoding="utf-8").read()

    print("\n[1] 手册命令可解析性（只解析，不执行）")
    check_commands(doc)

    print("\n[2] 手册第七节事实基线 vs 数据库实际")
    check_baseline(doc)

    print("\n[3] 接口与页面路由规模")
    check_routes(doc)

    print("\n[4] 开发上下文索引 不得含过期声明")
    check_index_freshness()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"文档事实核对：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：手册里照着敲的命令都能跑、写明的数字都查得到——演示前不必担心文档与系统脱节。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
