# -*- coding: utf-8 -*-
"""数据访问组件（C4 组件 `C-API-13` 的 Python 实现）。

设计约束（详细设计说明书 §2 分层职责）：
    · 本模块**只做**连接管理、参数化 SQL 执行与结果映射，**不含任何业务判断**；
    · 所有 SQL 必须使用参数化占位符（%s），禁止字符串拼接（NR-S-03 防注入）；
    · 事务边界由调用方决定：读多写少的批处理场景按批 commit，便于断点续跑（NR-R-01）。

阶段一用到的能力只有三件：连通性自检、单条/批量写入、只读查询。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

import pymysql
from pymysql.cursors import DictCursor

from app.config import settings


class DatabaseError(RuntimeError):
    """数据库层统一异常，便于 Web 层映射为错误码 5001。"""


def get_connection():
    """新建一个数据库连接（DictCursor，关闭自动提交）。

    调用方负责关闭；推荐使用下面的 `connection()` 上下文管理器。
    """
    if not settings.db.is_configured:
        raise DatabaseError(
            "数据库口令未配置：请在项目根目录的 .env 中填写 DB_PASSWORD（样例见 .env.example）"
        )
    try:
        return pymysql.connect(
            **settings.db.connect_kwargs(),
            cursorclass=DictCursor,
        )
    except pymysql.MySQLError as exc:  # 认证失败/库不存在/端口不通
        raise DatabaseError(f"连接 MySQL 失败（{settings.db.summary}）：{exc}") from exc


@contextmanager
def connection() -> Iterator[Any]:
    """连接上下文管理器。正常退出提交，异常时回滚，最后一定关闭。"""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ping() -> dict:
    """连通性自检：返回版本、字符集、库名与已建表数量。

    用于「阶段一 · MySQL 连接测试」与 `scripts/check_env.py`。
    """
    sql = """
        SELECT VERSION()                                   AS version,
               @@character_set_database                   AS charset,
               DATABASE()                                  AS database_name,
               (SELECT COUNT(*) FROM information_schema.TABLES
                 WHERE TABLE_SCHEMA = DATABASE())          AS table_count,
               (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND CONSTRAINT_TYPE = 'FOREIGN KEY')    AS foreign_key_count
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone() or {}


def query_all(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    """执行只读查询并返回全部行（list[dict]）。"""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return list(cur.fetchall())


def query_one(sql: str, params: Sequence[Any] | None = None) -> dict | None:
    """执行只读查询并返回首行或 None。"""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    """执行单条写语句，返回受影响行数。"""
    with connection() as conn:
        with conn.cursor() as cur:
            return cur.execute(sql, params or ())


def table_counts(tables: Iterable[str]) -> dict[str, int]:
    """批量统计若干张表的行数（逐表 COUNT(*)，结果精确）。"""
    result: dict[str, int] = {}
    with connection() as conn:
        with conn.cursor() as cur:
            for name in tables:
                cur.execute(f"SELECT COUNT(*) AS c FROM `{name}`")
                row = cur.fetchone() or {}
                result[name] = int(row.get("c", 0))
    return result


__all__ = [
    "DatabaseError",
    "get_connection",
    "connection",
    "ping",
    "query_all",
    "query_one",
    "execute",
    "table_counts",
]
