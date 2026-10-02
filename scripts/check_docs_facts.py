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
    expected = {
        "评论总数": 59033,
        "景点总数": 837,
        "可评价景点(≥100条)": 57,
        "规则层已完成": 10228,
        "真实API已完成": 31,
        "sentiment(deepseek)": 10259,
        "aspect": 60,
        "comment_semantic": 10259,
        "spot_fact_package": 0,
        "spot_report": 0,
        "数据库表数": 17,
    }
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
