# -*- coding: utf-8 -*-
"""景点评价写库失败兜底测试（C-BAT-07；对应保险机制第 10 条）。

## 修复前的真实缺陷（本轮审计发现）
`run_spot_report` 的写库语句**位于 try/except 之外**，且整轮 57 个景点**共用一个事务**
（`connection()` 只在正常退出时提交一次）。因此只要有一次写库失败：

    ① 异常直接抛出 → 已经写过的景点**全部回滚**；
    ② `finish_task` 永不执行 → 任务行永远停在 `running`；
    ③ `task_log` 里**没有任何失败痕迹**；
    ④ 那份**已付费**的评价彻底丢失，下次运行会**再调用一次模型**。

## 修复后的行为（本测试固定）
    ① 写失败时只重试写库（不重新调用模型）；
    ② 仍失败 → 结果落盘 `logs/recovery/spot_report_*.jsonl` 待补；
    ③ 记 `failed` + 写 `task_log`（ERROR 级），**批次继续**；
    ④ 每个景点写成功后立刻提交（断点粒度 = 1 个景点）。

## 零 API 消费
全程使用 `MockClient`；且测试自己快照 + 还原该景点的 `spot_report` 与事实包，
结束时断言核心表行数回到基线。
"""

from __future__ import annotations

import json
import sys
from typing import Sequence

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.batch.fact_package import run_fact_package
from app.batch.spot_report import run_spot_report
from app.batch.write_recovery import RECOVERY_DIR, load_recovery
from app.db import connection, query_all, query_one
from app.llm.mock import MockClient
from app.llm.client import LlmError  # noqa: F401  （保留导入以便将来注入调用失败）

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


class WriteFailCursor:
    """代理游标：**只让 `spot_report` 的那条 upsert 失败**，模拟"写评价时写不进去"。

    刻意不拦所有 INSERT/UPDATE：任务登记（`analysis_task`）与日志（`task_log`）
    必须能正常写入，否则测不出"失败被记录、批次继续"这些真正要验证的行为
    （首版拦了全部写操作，结果连 `register_task` 都被拦掉，测试自己先崩了）。
    """

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *args, **kwargs):
        if "INSERT INTO spot_report" in sql:
            from app.db import DatabaseError

            raise DatabaseError("注入的写库失败（模拟磁盘满/锁等待超时）")
        return self._real.execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        return self._real.executemany(sql, *args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._real, item)

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


def counts() -> dict[str, int]:
    row = query_one(
        "SELECT (SELECT COUNT(*) FROM spot_report) AS reports, "
        "       (SELECT COUNT(*) FROM spot_fact_package) AS pkgs, "
        "       (SELECT COUNT(*) FROM analysis_task) AS tasks"
    )
    return {k: int(v) for k, v in row.items()}


def cleanup(run_tag: str, spot_ids: Sequence[int] | None = None) -> int:
    """清理**本次运行**产生的行：按 `created_at >= run_tag` 圈定本次的任务与日志。

    为什么要按运行标记而不是"手工收集 task_id"：`run_fact_package` 与 `run_spot_report`
    都会在内部**自行登记任务**，只删自己拿到的 id 一定会漏（同类缺陷上一轮已经踩过一次）。
    :returns: 删除的任务行数
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT task_id FROM analysis_task WHERE created_at >= %s "
                "AND task_type IN ('fact_package','spot_report')",
                (run_tag,),
            )
            task_ids = [int(r["task_id"]) for r in cur.fetchall()]
            if task_ids:
                ph = ",".join(["%s"] * len(task_ids))
                cur.execute(f"DELETE FROM task_log WHERE task_id IN ({ph})", tuple(task_ids))
                cur.execute(f"DELETE FROM analysis_task WHERE task_id IN ({ph})", tuple(task_ids))
            if spot_ids:
                ph = ",".join(["%s"] * len(spot_ids))
                cur.execute(f"DELETE FROM spot_report WHERE spot_id IN ({ph})", tuple(spot_ids))
                cur.execute(f"DELETE FROM spot_fact_package WHERE spot_id IN ({ph})", tuple(spot_ids))
    return len(task_ids)


def main() -> int:
    from datetime import datetime

    print("=" * 86)
    print("景点评价写库失败兜底测试（MockClient；零 API 消费）")
    print("=" * 86)

    spots = query_all(
        "SELECT spot_id, spot_name FROM spot WHERE has_full_evaluation=1 ORDER BY spot_id LIMIT 2"
    )
    spot_ids = [int(r["spot_id"]) for r in spots]
    print(f"\n测试景点：{[(r['spot_id'], r['spot_name']) for r in spots]}")

    # 先清掉本测试可能遗留的数据（上一次异常退出会留下事实包/任务行），保证基线干净
    stale = cleanup("1970-01-01 00:00:00", None)
    print(f"预清理历史遗留：扫描到 {stale} 条 fact_package/spot_report 任务（均为本测试类型）")

    run_tag = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    before = counts()
    print(f"基线（预清理后）：{json.dumps(before, ensure_ascii=False)}")

    # ---------- 准备事实包（零模型调用，注意：它会自行登记一条任务） ----------
    run_fact_package(spot_ids=spot_ids)
    pkg_row = query_one(
        "SELECT COUNT(*) AS n FROM spot_fact_package WHERE spot_id IN (%s, %s)",
        (spot_ids[0], spot_ids[1]),
    )
    check("事实包已就绪（2 个景点）", int(pkg_row["n"]) == 2, f"rows={pkg_row['n']}")

    # ---------- 注入写库失败 ----------
    # 用"连接代理"包住真实连接：`connection()` 本身不变，只有游标被执行时按需失败。
    # （首版自己手写 contextmanager 包住了真实连接，导致 rollback/close 被重复调用而报
    #   "Already closed"，反而掩盖了真正要测的行为。）
    import app.batch.spot_report as sr

    real_connection = sr.connection
    injected = {"used": False}

    class ProxyConnection:
        def __init__(self, real):
            self._real = real

        def cursor(self):
            return WriteFailCursor(self._real.cursor())

        def __getattr__(self, item):
            return getattr(self._real, item)

    def proxied_connection():
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            with real_connection() as real:
                injected["used"] = True
                yield ProxyConnection(real)

        return _cm()

    sr.connection = proxied_connection
    try:
        summary = run_spot_report(client=MockClient(), mock=True, spot_ids=spot_ids, force=True)
    finally:
        sr.connection = real_connection

    print(f"\n注入生效 = {injected['used']}")
    print(f"运行结果：generated={summary['generated']} failed={summary['failed']} skipped={summary['skipped']}")

    check("注入确实生效（走了会失败的写库路径）", injected["used"] is True, "")
    check("写库失败被计为 failed（未虚报成功）", summary["failed"] == 2, f"failed={summary['failed']}")
    check("generated 为 0（没有把失败当成功）", summary["generated"] == 0, f"generated={summary['generated']}")
    check("任务未异常中断（批次跑完并正常收尾）", "task_id" in summary, f"task_id={summary.get('task_id')}")

    # ---------- 失败痕迹与落盘待补 ----------
    task_id = summary.get("task_id")
    if task_id:
        logs = query_all(
            "SELECT level, stage, message, ref_key FROM task_log WHERE task_id=%s ORDER BY log_id",
            (task_id,),
        )
        errors = [r for r in logs if r["level"] == "ERROR"]
        check("task_log 记录了写库失败（ERROR 级）", len(errors) >= 2, f"ERROR 条数={len(errors)}")
        check("失败日志带上了景点编号便于定向补跑",
              all(r["ref_key"] for r in errors), f"ref_keys={[r['ref_key'] for r in errors]}")
        row = query_one("SELECT status, fail_count FROM analysis_task WHERE task_id=%s", (task_id,))
        check("任务状态如实标为 partial（而非停在 running）",
              row and row["status"] == "partial", f"status={row['status'] if row else None}")

    entries = load_recovery("spot_report")
    check("已付费结果落盘待补（spot_report 恢复文件非空）", len(entries) >= 2, f"落盘条目={len(entries)}")
    if entries:
        payload = entries[0]["payload"]
        check("落盘内容含可补写字段（spot_id/version/summary/usage）",
              "spot_id" in payload and "fact_package_version" in payload
              and "summary" in payload and "token_usage" in payload,
              json.dumps({k: payload[k] for k in ("spot_id", "fact_package_version", "token_usage")}, ensure_ascii=False))

    # ---------- 清理：按运行标记删除本次产生的任务/日志/事实包 ----------
    removed = cleanup(run_tag, spot_ids)
    if RECOVERY_DIR.exists():
        for path in RECOVERY_DIR.glob("spot_report_*.jsonl"):
            path.unlink()
        if not any(RECOVERY_DIR.iterdir()):
            RECOVERY_DIR.rmdir()

    check("清理覆盖了本次运行的全部任务（含内部自行登记的）", removed >= 2, f"删除任务 {removed} 条")
    after = counts()
    check("清理后核心表回到基线（未污染真实数据）", after == before,
          f"{json.dumps(before, ensure_ascii=False)} → {json.dumps(after, ensure_ascii=False)}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 86)
    print(f"景点评价写库兜底：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 86)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
