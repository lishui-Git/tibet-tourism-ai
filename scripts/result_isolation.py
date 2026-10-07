# -*- coding: utf-8 -*-
"""景点级结果表（`spot_fact_package` / `spot_report`）的**测试隔离**工具。

## 为什么需要它（Stage 4 已经踩过一次同类事故）
Stage 5 会生产出 **57 份事实包 + 57 份评价**。而在此之前，多个测试是按 `spot_id`
**无条件删除**这两张表的行，甚至在收尾时 `DELETE FROM spot_fact_package`（整表清空）：

    cur.execute("DELETE FROM spot_fact_package WHERE spot_id=%s", (spot_id,))   # ← 会删掉生产行
    cur.execute("DELETE FROM spot_fact_package")                                # ← 整表清空

在表为空时这些都"看起来没问题"，一旦有了生产数据就会**删掉/覆盖真实结果**
（Stage 4 就发生过：`verify_phase4.purge_test_traces` 把 1,071 条合法复用行删光）。

## 本模块只做一件事
**快照 → 让测试在干净状态下跑 → 精确还原**。只碰这两张表，且只碰指定 `spot_id`。
用法：

    with isolated_spot_results([5]):
        ... 测试主体（可放心跑 facts/report）...
    # 出块后自动把所有被清掉的行**原样写回**（含 generated_at / task_id / json 内容）

`spot_ids=None` 表示"整表隔离"（用于链路串联这类需要全表为空的测试）。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from app.db import connection, query_all

FACT_COLUMNS: tuple[str, ...] = (
    "spot_id", "version", "package_json", "review_count", "stat_version", "generated_at", "task_id",
)
REPORT_COLUMNS: tuple[str, ...] = (
    "spot_id", "fact_package_version", "summary", "advantages_json", "issues_json",
    "visitor_focus_json", "model", "prompt_version", "need_review", "token_usage",
    "generated_at", "task_id",
)
_JSON_COLUMNS = {
    "package_json", "advantages_json", "issues_json", "visitor_focus_json",
}


def _select(table: str, columns: Sequence[str], spot_ids: Sequence[int] | None) -> list[dict[str, Any]]:
    sql = f"SELECT {','.join(columns)} FROM {table}"
    params: tuple = ()
    if spot_ids is not None:
        ids = tuple(int(i) for i in spot_ids)
        if not ids:
            return []
        sql += " WHERE spot_id IN (%s)" % ",".join(["%s"] * len(ids))
        params = ids
    return query_all(sql, params) if params else query_all(sql)


def snapshot_all() -> dict[str, list[dict[str, Any]]]:
    """快照**整表**（用于需要"全表为空"的测试，例如链路串联）。"""
    return {
        "fact_package": _select("spot_fact_package", FACT_COLUMNS, None),
        "spot_report": _select("spot_report", REPORT_COLUMNS, None),
    }


def snapshot(spot_ids: Sequence[int]) -> dict[str, list[dict[str, Any]]]:
    """快照指定景点的行（可能为空 —— 那就表示"测试前本来就没有"）。"""
    return {
        "fact_package": _select("spot_fact_package", FACT_COLUMNS, spot_ids),
        "spot_report": _select("spot_report", REPORT_COLUMNS, spot_ids),
    }


def _delete(cur: Any, spot_ids: Sequence[int]) -> None:
    ids = tuple(int(i) for i in spot_ids)
    if not ids:
        return
    ph = ",".join(["%s"] * len(ids))
    cur.execute(f"DELETE FROM spot_report WHERE spot_id IN ({ph})", ids)
    cur.execute(f"DELETE FROM spot_fact_package WHERE spot_id IN ({ph})", ids)


def purge(spot_ids: Sequence[int] | None) -> None:
    """删除指定景点（None = 整表）的景点级结果行。"""
    with connection() as conn:
        with conn.cursor() as cur:
            if spot_ids is None:
                cur.execute("DELETE FROM spot_report")
                cur.execute("DELETE FROM spot_fact_package")
            else:
                _delete(cur, spot_ids)


def _value(row: dict[str, Any], column: str) -> Any:
    value = row.get(column)
    if column in _JSON_COLUMNS and value is not None and not isinstance(value, (str, bytes)):
        return json.dumps(value, ensure_ascii=False)
    return value


def restore(snap: dict[str, list[dict[str, Any]]]) -> None:
    """把快照**原样写回**：先删这些 spot_id 的当前行，再插入快照行。

    这样无论测试把它们改成什么（重生成、mock 覆盖、失败注入），最终都回到测试前状态。
    """
    fact = snap.get("fact_package") or []
    report = snap.get("spot_report") or []
    ids = {int(r["spot_id"]) for r in fact} | {int(r["spot_id"]) for r in report}
    with connection() as conn:
        with conn.cursor() as cur:
            # 先删掉这些 spot 的当前行，再把快照原样写回。
            # （快照为空时 ids 也为空，说明"测试前本就没有"——那由调用方的 purge 负责清干净。）
            if ids:
                _delete(cur, tuple(ids))
            for row in fact:
                cur.execute(
                    "INSERT INTO spot_fact_package "
                    "(spot_id, version, package_json, review_count, stat_version, generated_at, task_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    tuple(_value(row, c) for c in FACT_COLUMNS),
                )
            for row in report:
                cur.execute(
                    "INSERT INTO spot_report "
                    "(spot_id, fact_package_version, summary, advantages_json, issues_json, "
                    " visitor_focus_json, model, prompt_version, need_review, token_usage, "
                    " generated_at, task_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    tuple(_value(row, c) for c in REPORT_COLUMNS),
                )


@contextmanager
def isolated_spot_results(spot_ids: Sequence[int] | None) -> Iterator[dict[str, list[dict[str, Any]]]]:
    """快照 → 清空 → yield → **精确还原**（异常也会还原）。

    :param spot_ids: 要隔离的景点；`None` 表示整表隔离。
    """
    if spot_ids is None:
        snap = snapshot_all()
        ids: Sequence[int] | None = None
    else:
        ids = tuple(int(i) for i in spot_ids)
        snap = snapshot(ids)
    purge(ids)
    try:
        yield snap
    finally:
        # 整表隔离时，还原要覆盖"测试期间**新写入**的任意 spot"，
        # 因此先把两张表整个清掉，再写回快照。
        if ids is None:
            purge(None)
        else:
            # 指定景点隔离：除了快照里的 spot，还要清掉测试可能写入的这些 spot 的行。
            purge(ids)
        restore(snap)


__all__ = [
    "isolated_spot_results",
    "snapshot",
    "snapshot_all",
    "purge",
    "restore",
    "FACT_COLUMNS",
    "REPORT_COLUMNS",
]
