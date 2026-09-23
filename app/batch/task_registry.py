# -*- coding: utf-8 -*-
"""任务登记与日志（复用冻结的运维表 `analysis_task` / `task_log`）。

**为什么不新建表**：设计已把"任务状态 + 步骤日志 + 失败重试"合并进
`analysis_task` 与 `task_log` 两张表（原 `ml_model` / `llm_failure` / `clean_log`
已按精简要求删除，信息去处见 `建表脚本.sql` 第七节注释）。
阶段四的 C-BAT-05～07 全部复用这两张表记录：
    · `analysis_task.task_type`：`semantic` / `fact_package` / `spot_report`
    · `task_log.stage`：`语义抽取` / `事实包构造` / `景点评价`
    · 失败明细：`level=ERROR` + `stage=semantic` + `ref_key=comment_id` + `resolved=0`

本文件属于 `C-BAT-01`（任务编排与断点续跑）的公共能力，被三个组件共用，
避免每个组件各写一套 INSERT。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Iterator, Sequence


def chunked(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    """把序列切成固定大小的批次（批处理写库/提交用）。"""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def register_task(
    conn,
    *,
    task_type: str,
    task_name: str,
    total_count: int | None = None,
) -> int:
    """登记一个 `analysis_task` 行（status=running），返回 task_id。

    `task_log.task_id` 是 NOT NULL 外键，因此必须先登记任务再写日志。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analysis_task (task_type, task_name, status, total_count, started_at)
            VALUES (%s, %s, 'running', %s, %s)
            """,
            (task_type, task_name[:128], total_count, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        return int(cur.lastrowid)


def finish_task(
    conn,
    task_id: int,
    *,
    status: str,
    success_count: int,
    fail_count: int = 0,
    skip_count: int = 0,
    started_at: datetime | None = None,
    error_message: str | None = None,
    model_metrics: dict | None = None,
) -> None:
    """收尾：更新任务状态、计数、耗时，以及（可选）模型与实验指标。

    `model_metrics` 会写入 `analysis_task.model_metrics_json`（该表本就承接原 `ml_model` 的
    实验信息字段，阶段三的 Spark 已用过同一口径）。
    """
    finished_at = datetime.now()
    cost_seconds = int((finished_at - started_at).total_seconds()) if started_at else None
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE analysis_task
               SET status = %s,
                   success_count = %s,
                   fail_count = %s,
                   skip_count = %s,
                   finished_at = %s,
                   cost_seconds = %s,
                   error_message = %s,
                   model_metrics_json = %s
             WHERE task_id = %s
            """,
            (
                status,
                success_count,
                fail_count,
                skip_count,
                finished_at.strftime("%Y-%m-%d %H:%M:%S"),
                cost_seconds,
                (error_message or None) and error_message[:512],
                json.dumps(model_metrics, ensure_ascii=False) if model_metrics else None,
                task_id,
            ),
        )


@dataclass
class TaskRecorder:
    """`task_log` 写入器（INFO/WARN/ERROR + 四类计数 detail_json）。"""

    conn: Any
    task_id: int

    def _write(
        self,
        level: str,
        stage: str,
        message: str,
        *,
        detail: dict | None = None,
        ref_key: str | None = None,
        retry_count: int = 0,
        resolved: int = 0,
        processed_count: int | None = None,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO task_log
                    (task_id, level, stage, message, ref_key, retry_count, resolved,
                     processed_count, detail_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    self.task_id,
                    level,
                    stage[:64],
                    message[:512],
                    ref_key,
                    retry_count,
                    resolved,
                    processed_count,
                    json.dumps(detail, ensure_ascii=False) if detail else None,
                ),
            )

    def info(self, stage: str, message: str, *, detail: dict | None = None) -> None:
        self._write("INFO", stage, message, detail=detail)

    def warn(self, stage: str, message: str, *, detail: dict | None = None) -> None:
        self._write("WARN", stage, message, detail=detail)

    def error(
        self,
        stage: str,
        message: str,
        *,
        ref_key: str | None = None,
        retry_count: int = 0,
        detail: dict | None = None,
    ) -> None:
        self._write("ERROR", stage, message, ref_key=ref_key, retry_count=retry_count, detail=detail)

    def progress(self, processed: int, *, detail: dict | None = None) -> None:
        """记录进度（管理端「任务进度」展示用，FR-SY-03）。"""
        self._write("INFO", "进度", f"当前处理量 {processed}", detail=detail, processed_count=processed)


__all__ = ["chunked", "register_task", "finish_task", "TaskRecorder"]
