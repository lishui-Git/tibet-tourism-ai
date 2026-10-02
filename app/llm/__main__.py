# -*- coding: utf-8 -*-
"""阶段四统一入口：C-BAT-05 / C-BAT-06 / C-BAT-07。

用法（在项目根目录执行）：
    .\\.venv\\Scripts\\python.exe -m app.llm --stage semantic --limit 8 --mock
    .\\.venv\\Scripts\\python.exe -m app.llm --stage facts --limit 3
    .\\.venv\\Scripts\\python.exe -m app.llm --stage report --limit 3 --mock
    .\\.venv\\Scripts\\python.exe -m app.llm --stage all --limit 5 --mock
    .\\.venv\\Scripts\\python.exe -m app.llm --check

安全闸门（写进代码，而不是靠记忆）：
    · `--mock` 且 `--limit > MOCK_MAX_LIMIT`（默认 50）→ **拒绝执行**，避免假数据混入全量结果；
    · `--mock` 的结果在 `analysis_task.task_name` 中标注 `[mock]`，便于事后甄别；
    · 未配置 API Key 时真实调用会在客户端层抛出明确错误（NR-R-05：降级而非崩溃）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Sequence

# Windows 控制台默认 GBK，输出中文/符号会抛 UnicodeEncodeError（阶段一已踩过），统一改 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

MOCK_MAX_LIMIT = 50        # mock 模式允许的最大样本量
CONFIRM_THRESHOLD = 50     # 超过该规模的真实/模拟批处理必须显式 --yes（防误触发 4.7 万次调用）


def _print(title: str, payload: dict[str, Any]) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    _print_cost_line(payload)


def _print_cost_line(payload: dict[str, Any]) -> None:
    """把"本次实际用量与实际费用"单独打一行，便于操作者一眼看到（保险机制第 11 条）。

    为什么在 JSON 之外再打一行：JSON 适合事后解析，但**人**在跑完 4.7 万次调用后
    最需要的是一个能直接读的结论——花了多少 token、多少钱。
    两个生成组件返回的费用字段已刻意保持同形（见 `app/llm/README.md` §7.2），
    因此这里可以统一处理，不需要为组件写特例。
    """
    cost = payload.get("cost")
    if not isinstance(cost, dict):
        return
    prompt = cost.get("prompt_tokens")
    completion = cost.get("completion_tokens")
    total = cost.get("total_tokens")
    if prompt is None and completion is None and total is None:
        return
    if total is None:
        total = int(prompt or 0) + int(completion or 0)
    money = cost.get("cost_total_cny")
    per_call = cost.get("avg_tokens_per_call")
    parts = [
        f"输入 {int(prompt or 0):,} + 输出 {int(completion or 0):,} = 合计 {int(total or 0):,} tokens",
    ]
    if per_call:
        parts.append(f"单次均 {per_call} tokens")
    if money is not None:
        parts.append(f"实际费用 ¥{money}")
    else:
        parts.append("费用未计算（无单价配置）")
    print(f"\n[本次实际用量与费用] {'；'.join(parts)}")
    print("  （金额按实际输入/输出 token × 配置单价计算；最终以 DeepSeek 账单为准）")


def _guard_mock(args: argparse.Namespace) -> None:
    """mock 模式的安全闸门：只允许小样本。"""
    if not args.mock:
        return
    if args.limit and args.limit > MOCK_MAX_LIMIT:
        raise SystemExit(
            f"[拒绝执行] --mock 仅用于小样本联调（--limit ≤ {MOCK_MAX_LIMIT}）。"
            f"当前 --limit={args.limit}。真实全量请去掉 --mock 并填写 API Key。"
        )
    if not args.limit and args.stage in {"semantic", "all"}:
        raise SystemExit(
            f"[拒绝执行] --mock 必须显式指定 --limit（≤ {MOCK_MAX_LIMIT}），"
            f"否则可能在 5 万条数据上写入假结果。"
        )


def _guard_scale(args: argparse.Namespace) -> None:
    """规模闸门：大批量执行必须显式确认。

    理由：C-BAT-05 全量是 4.7 万次 API 调用（真实费用 + 长时间运行），
    不该因为"少写一个参数"就被触发；C-BAT-07 全量 57 个景点同理。
    """
    if args.yes or args.dry_run or args.stage == "check":
        return
    # `--only-ids` / `--spot-ids` 是**显式枚举**的目标集合，规模由用户自己写死，
    # 不存在"不小心全量"的风险，因此不需要二次确认（否则失败补跑会被卡住）。
    if args.only_ids or args.spot_ids:
        return
    if args.stage in {"semantic", "all"} and not args.limit:
        raise SystemExit(
            "[需要确认] C-BAT-05 未指定 --limit，将对全库约 4.7 万条评论发起调用。\n"
            "  如确认全量执行，请追加 --yes；只想试跑请加 --limit N（N ≤ 50 时可用 --mock 零消耗验证）。"
        )
    if args.limit and args.limit > CONFIRM_THRESHOLD and not args.yes:
        raise SystemExit(
            f"[需要确认] 本次计划处理 {args.limit} 条（> {CONFIRM_THRESHOLD}）。\n"
            f"  如确认执行，请追加 --yes；否则请把 --limit 调小。"
        )


def _build_client(args: argparse.Namespace):
    """按参数构造客户端；真实客户端在缺 Key 时会抛出明确错误。

    【保险机制】`--offline`（或环境变量 `APP_LLM_OFFLINE=1`）会**硬阻断**真实调用：
    即使命令行写了 --yes、--limit 很大，也不可能产生任何 API 消费。
    用途：开发/演示/调试阶段把"不烧钱"变成代码级保证，而不是靠记性。
    """
    if args.mock:
        from app.llm.mock import MockClient

        return MockClient(fail_every=args.mock_fail_every)

    if args.offline or os.environ.get("APP_LLM_OFFLINE", "").strip() in {"1", "true", "yes", "on"}:
        raise SystemExit(
            "[已阻断真实调用] 当前处于离线模式（--offline 或 APP_LLM_OFFLINE=1）。\n"
            "  本模式不会发起任何 DeepSeek 请求，因此不会产生费用。\n"
            "  如确需真实调用，请去掉 --offline 并确认已获得授权。"
        )

    from app.llm.client import DeepSeekClient

    client = DeepSeekClient()
    if not client.is_configured:
        raise SystemExit(
            "[无法调用] 未检测到 APP_DEEPSEEK_API_KEY。\n"
            "  请把 Key 填入项目根目录的 .env（键名 APP_DEEPSEEK_API_KEY=），\n"
            "  或先用 --mock --limit N 验证链路（不产生真实调用）。"
        )
    return client


def _run_semantic(args: argparse.Namespace) -> dict[str, Any]:
    from app.batch.semantic_analysis import run_semantic_analysis

    client = None if args.dry_run else _build_client(args)
    only_ids = [int(x) for x in args.only_ids.split(",")] if args.only_ids else None
    return run_semantic_analysis(
        client=client,
        limit=args.limit,
        concurrency=args.concurrency,
        dry_run=args.dry_run,
        mock=args.mock,
        only_comment_ids=only_ids,
        bound_auxiliary=args.mock,  # mock 联调时把规则层/复用层也限制在小样本内
    )


def _run_facts(args: argparse.Namespace) -> dict[str, Any]:
    from app.batch.fact_package import FACT_PACKAGE_VERSION, run_fact_package

    return run_fact_package(
        spot_ids=[int(x) for x in args.spot_ids.split(",")] if args.spot_ids else None,
        limit=args.limit,
        version=args.version or FACT_PACKAGE_VERSION,
        only_missing=args.only_missing,
        dry_run=args.dry_run,   # 承诺"只统计不写库"必须落到本阶段（原先被忽略，实测抓到）
    )


def _run_report(args: argparse.Namespace) -> dict[str, Any]:
    from app.batch.spot_report import run_spot_report

    client = None if args.dry_run else _build_client(args)
    return run_spot_report(
        client=client,
        limit=args.limit,
        spot_ids=[int(x) for x in args.spot_ids.split(",")] if args.spot_ids else None,
        version=args.version,
        force=args.force,
        dry_run=args.dry_run,
        mock=args.mock,
    )


def _run_check() -> dict[str, Any]:
    """阶段四自检：核对三张结果表的行数、幂等键重复、外键完整性与状态分布。"""
    from app.db import query_all, query_one

    def scalar(sql: str, params: Sequence[Any] = ()) -> int:
        row = query_one(sql, params) or {}
        return int(list(row.values())[0] or 0)

    checks: dict[str, Any] = {
        "sentiment_deepseek": scalar("SELECT COUNT(*) FROM sentiment WHERE method='deepseek'"),
        "sentiment_mllib": scalar("SELECT COUNT(*) FROM sentiment WHERE method='mllib'"),
        "sentiment_deepseek_duplicates": scalar(
            "SELECT COUNT(*) FROM (SELECT comment_id FROM sentiment WHERE method='deepseek' "
            "GROUP BY comment_id HAVING COUNT(*)>1) t"
        ),
        "aspect_rows": scalar("SELECT COUNT(*) FROM aspect WHERE method='deepseek'"),
        "aspect_duplicate_keys": scalar(
            "SELECT COUNT(*) FROM (SELECT comment_id, aspect_name FROM aspect WHERE method='deepseek' "
            "GROUP BY comment_id, aspect_name HAVING COUNT(*)>1) t"
        ),
        "comment_semantic_rows": scalar("SELECT COUNT(*) FROM comment_semantic"),
        "spot_fact_package_rows": scalar("SELECT COUNT(*) FROM spot_fact_package"),
        "spot_report_rows": scalar("SELECT COUNT(*) FROM spot_report"),
        "spot_report_need_review": scalar("SELECT COUNT(*) FROM spot_report WHERE need_review=1"),
        "illegal_polarity": scalar(
            "SELECT COUNT(*) FROM sentiment WHERE method='deepseek' "
            "AND polarity NOT IN ('positive','neutral','negative')"
        ),
        "orphan_spot_id": scalar(
            "SELECT COUNT(*) FROM sentiment se LEFT JOIN spot s ON s.spot_id=se.spot_id "
            "WHERE se.method='deepseek' AND s.spot_id IS NULL"
        ),
        "failed_logs_open": scalar(
            "SELECT COUNT(*) FROM task_log WHERE level='ERROR' AND stage='semantic' AND resolved=0"
        ),
    }
    checks["polarity_distribution"] = query_all(
        "SELECT polarity, COUNT(*) AS n FROM sentiment WHERE method='deepseek' GROUP BY polarity ORDER BY n DESC"
    )
    checks["source_distribution"] = query_all(
        "SELECT source, COUNT(*) AS n FROM comment_semantic GROUP BY source ORDER BY n DESC"
    )
    checks["recent_tasks"] = query_all(
        "SELECT task_id, task_type, task_name, status, total_count, success_count, fail_count, skip_count, "
        "cost_seconds FROM analysis_task WHERE task_type IN ('semantic','fact_package','spot_report') "
        "ORDER BY task_id DESC LIMIT 8"
    )
    checks["ok"] = (
        checks["sentiment_deepseek_duplicates"] == 0
        and checks["aspect_duplicate_keys"] == 0
        and checks["illegal_polarity"] == 0
        and checks["orphan_spot_id"] == 0
    )
    return checks


def _guard_offline(args: argparse.Namespace) -> None:
    """离线模式下，除 preflight/check/dry-run/mock 外的真实调用阶段一律拒绝。

    这道闸门与 `_build_client` 里的阻断是**双重保险**：
    即使将来有人改了客户端构造逻辑，这里也会先拦住。

    **为什么允许 `--mock` 通过**：mock 用假客户端，根本不会发起请求；
    而 `--mock` 受 `_guard_mock` 的 `--limit ≤ 50` 约束，所以放行它是安全的，
    也方便在离线状态下做链路联调。

    **为什么 `--only-ids` 不能放行**：`--only-ids` 是**定向真实补跑**，
    它绕过规模闸门（这是设计意图——失败项要能单独重试），但正因为它绕过规模闸门，
    就更不能同时绕过离线闸门；否则 `--offline --only-ids` 会变成
    "既跳过费用确认、又允许真实调用"的组合。因此这里保持拒绝。
    """
    offline = args.offline or os.environ.get("APP_LLM_OFFLINE", "").strip() in {"1", "true", "yes", "on"}
    if not offline:
        return
    if args.mock or args.dry_run or args.stage in {"check", "preflight"}:
        return
    # 提示要能直接照着做：很多人会在真实跑之前顺手加 --offline"以防万一"，
    # 结果被这道闸门拦住却不知道下一步该怎么做（实测踩到过），因此把替代做法写清楚。
    hint = ""
    if args.only_ids or args.spot_ids:
        hint = (
            "\n  你用了 --only-ids/--spot-ids（定向补跑，会真实调用）。"
            "\n  · 想先看看会调用多少条：去掉 --offline 后加 --dry-run（零调用）"
            "\n  · 想在离线状态下演练链路：加 --mock --limit N（N ≤ 50，假客户端）"
            "\n  · 确实要补跑这几条：去掉 --offline（单条约 ¥0.002，无需 --yes）"
        )
    raise SystemExit(
        f"[已阻断] 离线模式下不允许执行 --stage {args.stage} 的真实调用。\n"
        "  允许的组合：--stage preflight / --stage check / --dry-run / --mock。"
        + hint
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.llm",
        description="阶段四：DeepSeek 语义分析（C-BAT-05 评论语义 / C-BAT-06 事实包 / C-BAT-07 景点评价）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--stage",
        choices=["semantic", "facts", "report", "all", "check", "preflight"],
        default="check",
        help="要执行的阶段：semantic=C-BAT-05；facts=C-BAT-06；report=C-BAT-07；all=05→06→07；"
             "check=结果健康自检；preflight=全量运行前预检（只读、零调用、含费用估算）",
    )
    parser.add_argument("--limit", type=int, default=None, help="小样本条数（确定性取前 N 条/前 N 个景点）")
    parser.add_argument("--mock", action="store_true", help=f"使用假客户端（仅小样本联调，--limit ≤ {MOCK_MAX_LIMIT}）")
    parser.add_argument("--mock-fail-every", type=int, default=0, help="mock 专用：每 N 次调用注入一次失败")
    parser.add_argument("--dry-run", action="store_true", help="只统计与切分，不调用模型、不写库")
    parser.add_argument("--concurrency", type=int, default=None, help="并发数（默认取 .env 的 APP_DEEPSEEK_MAX_CONCURRENCY）")
    parser.add_argument("--spot-ids", type=str, default=None, help="只处理指定景点，逗号分隔（补跑用）")
    parser.add_argument("--only-ids", type=str, default=None, help="C-BAT-05：只处理指定 comment_id，逗号分隔（失败补跑用）")
    parser.add_argument("--version", type=str, default=None, help="事实包版本（C-BAT-06/07，默认 v1）")
    parser.add_argument("--force", action="store_true", help="C-BAT-07：即使已有同版本评价也重新生成")
    parser.add_argument("--only-missing", action="store_true", help="C-BAT-06：只补缺失的事实包")
    parser.add_argument(
        "--yes",
        action="store_true",
        help=f"确认执行大规模批处理（--limit > {CONFIRM_THRESHOLD} 或全量必须显式确认）",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="离线模式：硬阻断一切真实 API 调用（开发/演示/调试时保证零消费）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _guard_mock(args)
    _guard_scale(args)
    _guard_offline(args)

    if args.stage == "preflight":
        # 全量运行前预检：只读数据库、零 API 调用（见 app/llm/preflight.py）
        from app.llm.preflight import print_report

        print_report()
        return 0

    if args.stage == "check":
        _print("阶段四自检", _run_check())
        return 0

    if args.dry_run:
        print("[dry-run] 只统计不写库；真实调用与写库均不会发生")

    if args.stage == "semantic":
        _print("C-BAT-05 评论语义分析", _run_semantic(args))
    elif args.stage == "facts":
        _print("C-BAT-06 景点事实包", _run_facts(args))
    elif args.stage == "report":
        _print("C-BAT-07 景点智能评价", _run_report(args))
    elif args.stage == "all":
        _print("C-BAT-05 评论语义分析", _run_semantic(args))
        _print("C-BAT-06 景点事实包", _run_facts(args))
        _print("C-BAT-07 景点智能评价", _run_report(args))

    _print("阶段四自检", _run_check())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
