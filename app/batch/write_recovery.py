# -*- coding: utf-8 -*-
"""写库失败的通用兜底：**已付费的模型结果绝不能因为写不进去而丢失**。

## 为什么需要它（真实失效路径）
批量生成的顺序是"**先调用模型（钱已经花了）→ 再写库**"。而 `app/db.py` 的
`connection()` 只在**正常退出时提交一次**、异常时**整个事务回滚**。于是只要写库抛错：

    ① 这一批（乃至本次运行的全部）结果全部回滚丢弃；
    ② 下次运行的游标发现这些数据"还没有结果"，就会**再调用一次模型** → 重复扣费；
    ③ 更糟的是任务行会停在 `running`、`task_log` 里也没有任何失败痕迹。

本模块提供三件事，供各生成组件复用：

    `persist_recovery()`  把"已付费但写库失败"的结果落盘成 JSONL，保留补写依据；
    `load_recovery()`     读回落盘结果，支持零成本补写；
    `write_with_retry()`  只重试**写库**（绝不重新调用模型），失败后再落盘。

## 落盘位置
`logs/recovery/<prefix>_<分片>.jsonl`（`logs/` 已被 `.gitignore` 排除）。
每条记录自带 `text`（用于还原）与 `payload`（业务字段），
因此**不需要**为每种组件单独实现反序列化。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from app.config import PROJECT_ROOT
from app.db import DatabaseError

RECOVERY_DIR = PROJECT_ROOT / "logs" / "recovery"

DEFAULT_ATTEMPTS = 3      # 含首次在内最多尝试次数
DEFAULT_SLEEP = 1.0       # 退避基数（秒）


def recovery_path(prefix: str, shard_key: int) -> Path:
    """按 `key % 100` 分片，避免单文件过大（与批大小同数量级）。"""
    safe = "".join(ch for ch in prefix if ch.isalnum() or ch in "-_") or "recovery"
    return RECOVERY_DIR / f"{safe}_{int(shard_key) % 100:02d}.jsonl"


def persist_recovery(prefix: str, entries: Sequence[dict[str, Any]], shard_key: int | None = None) -> Path | None:
    """把"已付费但写库失败"的结果追加落盘。

    :param entries: 每项形如 `{"text": <可读摘要>, "payload": <可写库的业务数据>}`
    :returns: 落盘文件路径；磁盘不可写时返回 None（调用方只提示，不改变主流程）
    """
    if not entries:
        return None
    key = shard_key if shard_key is not None else 0
    path = recovery_path(prefix, key)
    try:
        RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return path
    except OSError:
        return None


def load_recovery(prefix: str) -> list[dict[str, Any]]:
    """读回某个前缀的全部落盘记录（用于零成本补写）。"""
    if not RECOVERY_DIR.exists():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(RECOVERY_DIR.glob(f"{prefix}_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    return entries


@dataclass
class RetryOutcome:
    """一次"写库重试"的结果。"""

    ok: bool
    attempts: int
    recovery_path: Path | None = None
    last_error: str = ""


def write_with_retry(
    write: Callable[[], None],
    *,
    prefix: str,
    entries: Sequence[dict[str, Any]],
    shard_key: int | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
    sleep: float = DEFAULT_SLEEP,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> RetryOutcome:
    """只重试**写库**，绝不重新调用模型；仍失败则落盘待补。

    :param write: 真正执行写库的无参可调用对象（内部应是**同一个事务**，
                  失败时会整批回滚，因此重试不会产生重复行）；
    :param entries: 落盘用内容（与 `persist_recovery` 同结构）
    """
    last_error = ""
    for attempt in range(1, max(1, attempts) + 1):
        try:
            write()
            return RetryOutcome(ok=True, attempts=attempt)
        except DatabaseError as exc:
            last_error = str(exc)
            if attempt < attempts:
                if on_retry:
                    on_retry(attempt, exc)
                time.sleep(sleep * attempt)
    path = persist_recovery(prefix, entries, shard_key)
    return RetryOutcome(ok=False, attempts=max(1, attempts), recovery_path=path, last_error=last_error)


__all__ = [
    "RECOVERY_DIR",
    "RetryOutcome",
    "recovery_path",
    "persist_recovery",
    "load_recovery",
    "write_with_retry",
]
