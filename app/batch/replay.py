# -*- coding: utf-8 -*-
"""把"已付费但没写进库"的结果补写回去（零模型调用）。

## 为什么需要它
`write_recovery` 在写库连续失败时会把**已经付过费**的结果落盘到
`logs/recovery/semantic_*.jsonl` / `spot_report_*.jsonl`，避免白花钱。
但此前只做了"落盘"，**没有做"读回来补写"**——
`load_recovery()` 与 `payload_to_record()` 都写好了、docstring 也写着"补写用"，
却没有任何生产入口调用它们（只有测试在用）。也就是说：
**恢复文件是只写不读的，出事后得手工处理。**

本模块补上这条链路：`python -m app.llm --stage replay`。

## 设计要点
1. **绝不重新调用模型**：补写只做"payload → 数据库"，用的是当初已经拿到的结果。
2. **补写成功才删文件**：只把**仍失败**的条目写回文件；全部成功则删除该文件。
   宁可重复补写（upsert 幂等），也不允许"没写进去却把文件删了"。
3. **默认先看后做**：`dry_run=True` 时只报告"有几个文件、多少条待补"，
   不写库、不改文件——让操作者在动手前知道工作量。
4. **走同一条安全写入路径**：复用 `write_records_safely` / `write_with_retry`，
   因此补写自身若再失败，仍会按原机制重试并保留凭证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from app.batch.write_recovery import RECOVERY_DIR, load_recovery, recovery_path
from app.db import connection


@dataclass
class ReplayStats:
    """补写统计（如实汇报：读了多少、成了多少、还剩多少）。"""

    files: list[str] = field(default_factory=list)
    entries_total: int = 0
    written: int = 0
    still_failed: int = 0
    skipped: int = 0
    files_removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "recovery_dir": str(RECOVERY_DIR),
            "files": self.files,
            "entries_total": self.entries_total,
            "written": self.written,
            "still_failed": self.still_failed,
            "skipped": self.skipped,
            "files_removed": self.files_removed,
            "errors": self.errors[:10],
        }


def _semantic_entries() -> list[dict[str, Any]]:
    return load_recovery("semantic")


def _report_entries() -> list[dict[str, Any]]:
    return load_recovery("spot_report")


def replay_semantic(*, dry_run: bool = False) -> ReplayStats:
    """补写评论级语义结果（C-BAT-05）。"""
    from app.batch.semantic_analysis import (
        SemanticStats,
        payload_to_record,
        write_records_safely,
    )

    stats = ReplayStats()
    files = sorted(p.name for p in RECOVERY_DIR.glob("semantic_*.jsonl")) if RECOVERY_DIR.exists() else []
    stats.files = files
    entries = _semantic_entries()
    stats.entries_total = len(entries)
    if dry_run or not entries:
        return stats

    stats_obj = SemanticStats()
    records: list[dict[str, Any]] = []
    for entry in entries:
        try:
            records.append(payload_to_record(entry["payload"]))
        except Exception as exc:  # noqa: BLE001
            stats.skipped += 1
            stats.errors.append(f"comment_id={entry.get('payload', {}).get('comment_id')}: {exc}")
    if not records:
        return stats

    with connection() as conn:
        write_records_safely(conn, records, stats_obj)
        conn.commit()
    stats.written = len(records)

    # 兜底：safe writer 若又落盘，会生成新文件；这里通过"补写后是否仍能读到同一 comment_id"
    # 判断成功与否，只把仍失败的条目写回（避免误删已付费结果）。
    remaining = _reconcile("semantic", entries, stats)
    return stats


def replay_spot_report(*, dry_run: bool = False) -> ReplayStats:
    """补写景点级评价结果（C-BAT-07）。"""
    from app.batch.spot_report import REPORT_PROMPT_VERSION, REPORT_UPSERT_SQL, report_upsert_params

    stats = ReplayStats()
    files = (
        sorted(p.name for p in RECOVERY_DIR.glob("spot_report_*.jsonl")) if RECOVERY_DIR.exists() else []
    )
    stats.files = files
    entries = _report_entries()
    stats.entries_total = len(entries)
    if dry_run or not entries:
        return stats

    with connection() as conn:
        with conn.cursor() as cur:
            for entry in entries:
                payload = entry.get("payload") or {}
                try:
                    # task_id=None：补写不属于任何一次运行任务，不编造任务号
                    cur.execute(
                        REPORT_UPSERT_SQL,
                        report_upsert_params(payload, REPORT_PROMPT_VERSION, None),
                    )
                    stats.written += 1
                except Exception as exc:  # noqa: BLE001
                    stats.still_failed += 1
                    stats.errors.append(f"spot_id={payload.get('spot_id')}: {exc}")
        conn.commit()

    _reconcile("spot_report", entries, stats)
    return stats


def _reconcile(prefix: str, entries: Sequence[dict[str, Any]], stats: ReplayStats) -> None:
    """核对补写结果：只保留**确实没写进库**的条目；全部成功则删除文件。

    为什么不能"补写完就无脑删文件"：如果 `write_records_safely` 也失败了，
    它会重新落盘——此时若我们先删旧文件，就等于把已付费结果弄丢了。
    因此这里以"数据库里到底有没有"为准，重写恢复文件。
    """
    from app.batch.write_recovery import persist_recovery

    def _exists(entry: dict[str, Any]) -> bool:
        payload = entry.get("payload") or {}
        with connection() as conn:
            with conn.cursor() as cur:
                if prefix == "semantic":
                    cid = payload.get("comment_id")
                    cur.execute(
                        "SELECT 1 FROM sentiment WHERE comment_id=%s AND method='deepseek' LIMIT 1",
                        (cid,),
                    )
                else:
                    cur.execute(
                        "SELECT 1 FROM spot_report WHERE spot_id=%s LIMIT 1",
                        (payload.get("spot_id"),),
                    )
                return cur.fetchone() is not None

    missing: list[dict[str, Any]] = []
    for entry in entries:
        try:
            if not _exists(entry):
                missing.append(entry)
        except Exception as exc:  # noqa: BLE001
            missing.append(entry)
            stats.errors.append(f"核对失败：{exc}")

    if not missing:
        for path in RECOVERY_DIR.glob(f"{prefix}_*.jsonl"):
            path.unlink()
            stats.files_removed.append(path.name)
        if RECOVERY_DIR.exists() and not any(RECOVERY_DIR.iterdir()):
            RECOVERY_DIR.rmdir()
        return

    stats.still_failed = len(missing)
    key = "comment_id" if prefix == "semantic" else "spot_id"
    by_shard: dict[int, list[dict[str, Any]]] = {}
    for entry in missing:
        shard = int((entry.get("payload") or {}).get(key) or 0) % 100
        by_shard.setdefault(shard, []).append(entry)
    for shard, items in by_shard.items():
        persist_recovery(prefix, items, shard)


def replay_all(*, dry_run: bool = False) -> dict[str, Any]:
    """补写全部恢复文件，返回两个组件的统计。"""
    return {
        "dry_run": dry_run,
        "semantic": replay_semantic(dry_run=dry_run).as_dict(),
        "spot_report": replay_spot_report(dry_run=dry_run).as_dict(),
        "note": "补写只把已付费结果写回数据库，**不产生任何模型调用**。",
    }


__all__ = [
    "ReplayStats",
    "replay_all",
    "replay_semantic",
    "replay_spot_report",
]
