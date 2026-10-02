# -*- coding: utf-8 -*-
"""运行安全闸门测试（preflight / --mock / --offline / 规模确认）。

## 为什么单独测"闸门"
成本安全的实现不只在业务代码里，还在**命令行闸门**上：
没有这些闸门，一次手误就能发起 4.7 万次真实调用。闸门逻辑属于"平时不生效、
关键时刻才起作用"的代码，容易被后续改动无声破坏，因此把实际行为固定下来。

## 覆盖
    A. 规模闸门：`--stage semantic` 无 `--limit`/`--yes` → 拒绝执行；`--yes` 放行
    B. 定向补跑：`--only-ids` / `--spot-ids` 绕过规模闸门（失败项要能单独重试）
    C. mock 闸门：`--mock` 必须配 `--limit ≤ 50`
    D. 离线闸门：`--offline` 阻断真实调用；放行 preflight/check/--dry-run/--mock
    E. 客户端构造：离线时**绝不**构造真实客户端（第二道保险）
    F. mock 结果自带标记：`raw_json.mode='mock'`，可被 preflight 检出

**本测试不发起任何真实调用**：只调用闸门函数与纯构造逻辑。
"""

from __future__ import annotations

import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.batch.semantic_analysis import _build_api_record
from app.llm.__main__ import _build_client, _guard_mock, _guard_offline, _guard_scale, build_parser
from app.llm.validators import SemanticResult

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def guard_blocks(func, args) -> tuple[bool, str]:
    """执行闸门函数：被 SystemExit 拦下返回 (True, 提示语)，否则 (False, '')。"""
    try:
        func(args)
        return False, ""
    except SystemExit as exc:
        return True, str(exc)


def main() -> int:
    parser = build_parser()

    def args_of(*argv: str):
        return parser.parse_args(["--stage", "semantic", *argv])

    print("=" * 84)
    print("运行安全闸门测试（不发起任何真实调用）")
    print("=" * 84)

    # ---------- A. 规模闸门 ----------
    print("\n[A] 规模闸门（无 --limit/--yes 时拒绝全量）")
    blocked, msg = guard_blocks(_guard_scale, args_of())
    check("无 --limit/--yes → 阻断", blocked, msg.splitlines()[0] if msg else "")
    check("阻断提示里给出了下一步做法（--yes 或 --limit）",
          "--yes" in msg and "--limit" in msg, "")
    blocked, _ = guard_blocks(_guard_scale, args_of("--yes"))
    check("加 --yes → 放行", not blocked, "")
    blocked, _ = guard_blocks(_guard_scale, args_of("--limit", "8"))
    check("加 --limit 8 → 放行（小样本）", not blocked, "")

    # ---------- B. 定向补跑绕过规模闸门 ----------
    print("\n[B] 定向补跑（--only-ids / --spot-ids 绕过规模闸门）")
    blocked, _ = guard_blocks(_guard_scale, args_of("--only-ids", "73603914"))
    check("--only-ids → 放行（失败项必须能单独重试）", not blocked, "")
    blocked, _ = guard_blocks(_guard_scale, args_of("--spot-ids", "5"))
    check("--spot-ids → 放行", not blocked, "")

    # ---------- C. mock 闸门 ----------
    print("\n[C] mock 闸门（必须配小样本）")
    blocked, msg = guard_blocks(_guard_mock, args_of("--mock"))
    check("--mock 无 --limit → 阻断", blocked, msg.splitlines()[0] if msg else "")
    blocked, _ = guard_blocks(_guard_mock, args_of("--mock", "--limit", "50"))
    check("--mock --limit 50 → 放行（上限）", not blocked, "")
    blocked, _ = guard_blocks(_guard_mock, args_of("--mock", "--limit", "51"))
    check("--mock --limit 51 → 阻断（超过上限）", blocked, "")

    # ---------- D. 离线闸门 ----------
    print("\n[D] 离线闸门（--offline 阻断真实调用）")
    blocked, msg = guard_blocks(_guard_offline, args_of("--offline"))
    check("--offline + semantic → 阻断", blocked, msg.splitlines()[0] if msg else "")
    blocked, msg = guard_blocks(_guard_offline, args_of("--offline", "--only-ids", "73603914"))
    check("--offline + --only-ids → 也阻断（不得同时绕过两道闸门）", blocked, "")
    check("阻断提示对 --only-ids 给出可照做的替代做法",
          "--dry-run" in msg and "--mock" in msg, "")
    blocked, _ = guard_blocks(_guard_offline, args_of("--offline", "--dry-run"))
    check("--offline + --dry-run → 放行（零调用）", not blocked, "")
    blocked, _ = guard_blocks(_guard_offline, args_of("--offline", "--mock", "--limit", "1"))
    check("--offline + --mock → 放行（假客户端，不可能真实调用）", not blocked, "")

    offline_args = parser.parse_args(["--stage", "preflight", "--offline"])
    blocked, _ = guard_blocks(_guard_offline, offline_args)
    check("--offline + preflight → 放行（只读）", not blocked, "")

    # 环境变量形式：APP_LLM_OFFLINE=1
    os.environ["APP_LLM_OFFLINE"] = "1"
    try:
        blocked, _ = guard_blocks(_guard_offline, args_of())
        check("环境变量 APP_LLM_OFFLINE=1 同样阻断", blocked, "")
    finally:
        os.environ.pop("APP_LLM_OFFLINE", None)

    # ---------- E. 第二道保险：离线时不构造真实客户端 ----------
    print("\n[E] 客户端构造（离线时绝不构造真实客户端）")
    blocked, _ = guard_blocks(_build_client, args_of("--offline"))
    check("--offline → 构造真实客户端被阻断（第二道保险）", blocked, "")
    mock_client = _build_client(args_of("--mock", "--limit", "1"))
    check("--mock → 返回 MockClient（不会真实调用）",
          type(mock_client).__name__ == "MockClient", type(mock_client).__name__)

    # ---------- F. mock 结果自带标记 ----------
    print("\n[F] mock 结果必须自带标记（否则与真实结果无法区分）")
    result = SemanticResult(
        polarity="neutral", intensity=3, is_valid=1, aspects=[], keywords="a", summary="b"
    )
    row = {"comment_id": 1, "spot_id": 1}
    mock_raw = _build_api_record(row, result, {"total_tokens": 10}, mode="mock")["raw"]
    real_raw = _build_api_record(row, result, {"total_tokens": 10})["raw"]
    check("mock 结果写入 mode='mock'", mock_raw.get("mode") == "mock", str(mock_raw.get("mode")))
    check("默认为真实调用 mode='real'", real_raw.get("mode") == "real", str(real_raw.get("mode")))
    check("mock 与真实结果在 raw_json 上可区分", mock_raw.get("mode") != real_raw.get("mode"), "")

    # ---------- F2. 复用行标记 + preflight 的 mock 检出覆盖面 ----------
    print("\n[F2] 复用行可辨识 + preflight 必须同时查 sentiment 与 spot_report 的 mock 行")
    from app.batch.semantic_analysis import _build_reuse_raw
    from app.llm.preflight import INTEGRITY_SQL

    reuse_raw = _build_reuse_raw(12345)
    check("复用行写入 mode='reuse'（防止 source 被重跑覆盖后无法辨识）",
          reuse_raw.get("mode") == "reuse", str(reuse_raw.get("mode")))
    check("复用行仍保留 reused_from_comment_id（可追溯来源）",
          reuse_raw.get("reused_from_comment_id") == 12345, "")
    check("复用行 usage 记为 0（成本核算可区分『免费复用』与『已付费调用』）",
          (reuse_raw.get("usage") or {}).get("total_tokens") == 0, str(reuse_raw.get("usage")))

    check("preflight 检查 sentiment 的 mock 行", "mock_rows_in_results" in INTEGRITY_SQL, "")
    check("preflight 也检查 spot_report 的 mock 评价（该表无 raw_json，须按 model 查）",
          "spot_report" in INTEGRITY_SQL.get("mock_spot_report_rows", "")
          and "model = 'mock'" in INTEGRITY_SQL["mock_spot_report_rows"], "")
    check("可见指标 test_trace_rows_in_results 覆盖 mock 与 reuse 两种标记",
          "mock" in INTEGRITY_SQL.get("test_trace_rows_in_results", "")
          and "reuse" in INTEGRITY_SQL.get("test_trace_rows_in_results", ""), "")

    # ---------- F3. 跨表一致性：半成品必须被检出且值得阻断 ----------
    print("\n[F3] 跨表一致性检查（防『提交粒度被破坏』留下半成品）")
    from app.llm.preflight import collect

    pf = collect()
    # 正向：三张表必须同进同出（有情感就该有语义），否则游标会认为"已做完"、永不补写
    for key in ("semantic_without_sentiment", "sentiment_without_semantic",
                "orphan_aspect", "orphan_aspect_review"):
        check(f"{key} 在干净库上为 0（不误报）", pf.integrity.get(key) == 0,
              str(pf.integrity.get(key)))
    check("这些一致性检查确实在 SQL 里定义了",
          all(k in INTEGRITY_SQL for k in
              ("semantic_without_sentiment", "sentiment_without_semantic",
               "orphan_aspect", "orphan_aspect_review")), "")
    check("aspect 孤儿检查按 method='deepseek' 限定（不误伤 mllib 基线）",
          "method='deepseek'" in INTEGRITY_SQL["orphan_aspect"], "")
    # 反向：确认它们真的接入了**阻断判据清单**（而不是只查不管）。
    # 阻断逻辑内嵌在 `collect()` 里，因此检查其源码文本。
    import inspect

    from app.llm.preflight import collect as _collect

    src = inspect.getsource(_collect)
    for key in ("sentiment_without_semantic", "orphan_aspect", "orphan_aspect_review"):
        check(f"{key} 已接入阻断判据（不是只查不管）", key in src, "")

    # ---------- G. 不相关选项必须被提示（而不是静默忽略） ----------
    print("\n[G] 阶段不相关的选项要明确提示（避免生产时误判为『参数坏了』）")
    import io
    from contextlib import redirect_stdout

    from app.llm.__main__ import _warn_irrelevant_flags

    def capture(*argv: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            _warn_irrelevant_flags(parser.parse_args(list(argv)))
        return buf.getvalue()

    out_facts = capture("--stage", "facts", "--mock", "--limit", "1")
    check("--stage facts + --mock → 提示 --mock 不起作用",
          "不起作用" in out_facts and "--mock" in out_facts, out_facts.strip()[:80])

    out_sem = capture("--stage", "semantic", "--force")
    check("--stage semantic + --force → 提示 --force 不起作用",
          "不起作用" in out_sem and "--force" in out_sem, out_sem.strip()[:80])

    out_ok = capture("--stage", "semantic", "--limit", "5")
    check("选项相关时不打扰（无提示）", out_ok.strip() == "", repr(out_ok[:40]))

    out_rep = capture("--stage", "report", "--concurrency", "3")
    check("--stage report + --concurrency → 提示不起作用",
          "不起作用" in out_rep and "--concurrency" in out_rep, out_rep.strip()[:80])

    # ---------- H. 并发运行闸门（防两个实例重复扣费） ----------
    print("\n[H] 并发运行闸门：同类型任务仍在 running 时，真实运行必须被拒绝")
    from datetime import datetime, timedelta

    from app.db import connection, query_one
    from app.llm.__main__ import STALE_RUNNING_HOURS, _guard_concurrent_run

    def parse(*argv: str):
        return parser.parse_args(list(argv))

    def concurrent_blocks(*argv: str) -> bool:
        try:
            _guard_concurrent_run(parse(*argv))
            return False
        except SystemExit:
            return True

    created: list[int] = []

    def make_running(task_type: str, minutes_ago: int) -> int:
        started = (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO analysis_task (task_type, task_name, status, started_at) "
                    "VALUES (%s, '【闸门测试】临时任务', 'running', %s)",
                    (task_type, started),
                )
                tid = int(cur.lastrowid)
        created.append(tid)
        return tid

    try:
        check("无 running 任务时放行（不误伤正常流程）",
              not concurrent_blocks("--stage", "semantic", "--only-ids", "1"), "")

        make_running("semantic", 1)
        check("同类型任务刚启动 → 拒绝（这是防重复扣费的关键）",
              concurrent_blocks("--stage", "semantic", "--only-ids", "1"), "")
        check("--dry-run 不受影响（它不调用模型）",
              not concurrent_blocks("--stage", "semantic", "--only-ids", "1", "--dry-run"), "")
        check("不同类型任务不互相阻断（report 不受 semantic 影响）",
              not concurrent_blocks("--stage", "report"), "")
        check("--allow-concurrent 可显式放行",
              not concurrent_blocks("--stage", "semantic", "--only-ids", "1", "--allow-concurrent"), "")
        check("只读阶段（check / preflight）不受影响",
              not concurrent_blocks("--stage", "check") and not concurrent_blocks("--stage", "preflight"), "")

        # 陈旧 running（异常退出遗留）不应永久锁死
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE analysis_task SET started_at = %s WHERE task_id = %s",
                    (
                        (datetime.now() - timedelta(hours=STALE_RUNNING_HOURS + 1)).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        created[0],
                    ),
                )
        check(f"超过 {STALE_RUNNING_HOURS} 小时的 running 视为遗留，不再阻断（避免崩溃后锁死）",
              not concurrent_blocks("--stage", "semantic", "--only-ids", "1"), "")
    finally:
        if created:
            with connection() as conn:
                with conn.cursor() as cur:
                    ph = ",".join(["%s"] * len(created))
                    cur.execute(f"DELETE FROM analysis_task WHERE task_id IN ({ph})", tuple(created))
        left = int(query_one("SELECT COUNT(*) AS n FROM analysis_task WHERE status='running'")["n"])
        check("清理后没有残留的 running 任务", left == 0, f"running={left}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 84)
    print(f"安全闸门：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 84)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
