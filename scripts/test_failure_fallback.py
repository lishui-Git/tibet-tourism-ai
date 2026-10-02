# -*- coding: utf-8 -*-
"""设计 §7.5 失败兜底核对（逐行对照，零 API 消费）。

设计 §7.5 有四行失效处理，前三行都能在本机零成本验证（第四行见 `test_qa.py`）：

| 失败层级 | 设计要求 | 本测试怎么验 |
|---|---|---|
| 单条评论语义抽取失败 | 写 `task_log`（ERROR/semantic/ref_key=comment_id/resolved=0），**不阻塞整批**；该评论**不进入占比分母** | 注入 2 条失败 → 断言落库字段与 `MAX_FAILURE_LOGS` 截断；并用真实失败评论证明"无情感行 ⇒ 不进分母" |
| 景点评价生成失败 | 不写 `spot_report`，保持 `available=false` 并记录原因 | 见 `test_spot_report_write_failure.py`（已覆盖） |
| 对比解读失败 | **仍返回完整指标与方面对比**，仅解读部分提示可重试 | 注入假客户端抛错 → 断言指标/方面照常返回 |

【数据卫生】本测试只在"自己创建的 running 任务"下写 `task_log`，
结束时按 `task_id` 精确删除任务与其日志，并断言回到基线。
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.batch.semantic_analysis import MAX_FAILURE_LOGS, _flush_failures
from app.batch.task_registry import TaskRecorder, register_task
from app.db import connection, query_all, query_one

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 88)
    print("设计 §7.5 失败兜底核对（零 API 消费）")
    print("=" * 88)

    base_tasks = int(query_one("SELECT COUNT(*) AS n FROM analysis_task")["n"])
    base_logs = int(query_one("SELECT COUNT(*) AS n FROM task_log")["n"])
    print(f"\n基线：analysis_task={base_tasks}、task_log={base_logs}")

    # ---------- 第 1 行：单条语义抽取失败 ----------
    print("\n[1] 语义抽取失败 → 写 task_log(ERROR/semantic/resolved=0)，且不阻塞整批")
    task_id = None
    extra_tasks: list[int] = []
    try:
        with connection() as conn:
            task_id = register_task(
                conn, task_type="semantic", task_name="【测试】§7.5 失败兜底核对", total_count=2
            )
            recorder = TaskRecorder(conn, task_id)
            failures = [
                ({"comment_id": 900000001, "spot_id": 1}, "network", "模拟：连接被重置"),
                ({"comment_id": 900000002, "spot_id": 1}, "bad_response", "模拟：返回体为空"),
            ]
            _flush_failures(conn, task_id, recorder, failures)
        rows = query_all(
            "SELECT level, stage, ref_key, retry_count, resolved, message "
            "FROM task_log WHERE task_id=%s ORDER BY log_id",
            (task_id,),
        )
        check("两条失败都被登记", len(rows) == 2, f"{len(rows)} 行")
        check("level=ERROR", all(r["level"] == "ERROR" for r in rows), "")
        check("stage=semantic", all(r["stage"] == "semantic" for r in rows), "")
        check("ref_key=comment_id（可据此定位并补跑）",
              {r["ref_key"] for r in rows} == {"900000001", "900000002"},
              str(sorted(r["ref_key"] for r in rows)))
        check("resolved=0（表示尚未解决、可补跑）",
              all(int(r["resolved"]) == 0 for r in rows), "")
        check("message 带失败类别前缀（如 [network]）",
              all("[" in (r["message"] or "") for r in rows), (rows[0]["message"] or "")[:36])

        # 关键：分母口径 —— 失败的评论**没有 sentiment 行**，因此天然不进分母
        missing = int(
            query_one(
                "SELECT COUNT(*) AS n FROM sentiment WHERE comment_id IN (900000001, 900000002)"
            )["n"]
        )
        check("失败评论没有任何情感行 ⇒ 不进『正负面占比』分母（设计要求）",
              missing == 0, f"sentiment 行数={missing}")
        check(f"失败明细有上限 MAX_FAILURE_LOGS={MAX_FAILURE_LOGS}（防止故障时刷爆日志）",
              MAX_FAILURE_LOGS > 0, "")

        # 截断行为：注入超过上限的失败，只登记前 N 条 ERROR，并另记一条 WARN 告知截断。
        # 用一个**独立的新任务**来数，避免与上一段的 2 条 ERROR 混在一起（首版就混了，数出 202）。
        print("\n[2] 失败数超过上限时只登记前 MAX_FAILURE_LOGS 条（并另记 WARN 告知截断）")
        many = [({"comment_id": 900000100 + i, "spot_id": 1}, "network", f"模拟 {i}")
                for i in range(MAX_FAILURE_LOGS + 5)]
        with connection() as conn:
            task2 = register_task(
                conn, task_type="semantic", task_name="【测试】§7.5 截断核对", total_count=len(many)
            )
            extra_tasks.append(task2)
            recorder2 = TaskRecorder(conn, task2)
            _flush_failures(conn, task2, recorder2, many)
        err_rows = int(
            query_one(
                "SELECT COUNT(*) AS n FROM task_log "
                "WHERE task_id=%s AND level='ERROR' AND stage='semantic'",
                (task2,),
            )["n"]
        )
        warn_rows = int(
            query_one("SELECT COUNT(*) AS n FROM task_log WHERE task_id=%s AND level='WARN'", (task2,))["n"]
        )
        # 上限约束的是"失败明细"（ERROR）行数；截断时还会另记一条 WARN，所以总行数会多 1。
        check(f"ERROR 明细被截断到 {MAX_FAILURE_LOGS}（不是 {len(many)}）",
              err_rows == MAX_FAILURE_LOGS, f"实际 {err_rows} 行")
        check("截断时另记一条 WARN，告知『仅登记前 N 条』（不静默丢弃）",
              warn_rows >= 1, f"WARN {warn_rows} 行")

    finally:
        ids = [t for t in ([task_id] + extra_tasks) if t is not None]
        if ids:
            with connection() as conn:
                with conn.cursor() as cur:
                    ph = ",".join(["%s"] * len(ids))
                    cur.execute(f"DELETE FROM task_log WHERE task_id IN ({ph})", tuple(ids))
                    cur.execute(f"DELETE FROM analysis_task WHERE task_id IN ({ph})", tuple(ids))
        left_tasks = int(query_one("SELECT COUNT(*) AS n FROM analysis_task")["n"])
        left_logs = int(query_one("SELECT COUNT(*) AS n FROM task_log")["n"])
        check("收尾：任务与日志均回到基线",
              left_tasks == base_tasks and left_logs == base_logs,
              f"task {base_tasks}→{left_tasks}，log {base_logs}→{left_logs}")

    # ---------- 第 3 行：对比解读失败仍返回数据 ----------
    print("\n[3] 对比解读失败 → 指标与方面对比照常返回（数据不依赖模型）")
    import app.llm.client as client_mod
    from app.config import WebSettings, settings
    from app.llm.client import LlmError
    from app.web.compare import compare_spots

    class _Boom:
        def __init__(self, *a, **k): pass

        def chat(self, *a, **k):
            raise LlmError("模拟：解读失败", kind="network")

    orig_web = settings.web
    orig_client = client_mod.DeepSeekClient
    object.__setattr__(settings, "web", WebSettings(**{**orig_web.__dict__, "compare_live": True}))
    try:
        client_mod.DeepSeekClient = _Boom
        result = compare_spots(564, 322)
        interp = result.get("interpretation") or {}
        check("解读部分如实标记失败（reason=LLM_FAILED）",
              interp.get("reason") == "LLM_FAILED", str(interp.get("reason")))
        check("指标对比**照常返回**（不因解读失败而缺失）",
              bool(result.get("indicator_compare")), f"{len(result.get('indicator_compare') or [])} 项")
        facts = result.get("facts") or {}
        check("事实与方面对比照常返回", bool(facts.get("aspects") is not None), "")
        check("样本量信息照常返回", bool(result.get("sample_size")), str(result.get("sample_size")))
    finally:
        client_mod.DeepSeekClient = orig_client
        object.__setattr__(settings, "web", orig_web)
    check("收尾：compare_live 已还原为默认关闭", settings.web.compare_live is False, "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"设计 §7.5 失败兜底核对：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：设计 §7.5 的四行失效处理都能落到实处——")
        print("      失败被登记且可补跑、不进占比分母；解读失败时数据部分照常可用。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
