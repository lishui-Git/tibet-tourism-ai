# -*- coding: utf-8 -*-
"""M1 数据总览接口（C4 组件 `C-API-02`，详细设计 §6.2 第 5–9 项）。

对应页面：数据总览页（M1）。全部为**只读**查询，数据来自：
    · 数据集规模 / 评分分布 ← `review` / `spot` 实时聚合
    · 时间趋势             ← 阶段三 Spark 的 `stat_time`
    · 分档与来源构成       ← `stat_spot` / `spot`
    · 客源地分布           ← 阶段三 Spark 的 `stat_ip`（BR-01 口径）
受口径影响的返回都带 `caliber_note`（§6.1 / BR-10）。
"""

from __future__ import annotations

from flask import Blueprint, request

from app.web.data_access import ParamError
from app.web.routes._helpers import respond
from app.web.services import (
    overview_data_note,
    overview_distribution,
    overview_provinces,
    overview_summary,
    overview_trend,
)

bp = Blueprint("overview", __name__, url_prefix="/api/overview")


@bp.get("/summary")
def summary():
    """数据集规模与评分分布（接口 5）。"""
    return respond(overview_summary)


@bp.get("/trend")
def trend():
    """评论量时间趋势（接口 6）；`granularity=year|month`，默认 year。"""
    granularity = request.args.get("granularity", "year")
    return respond(lambda: overview_trend(granularity))


@bp.get("/distribution")
def distribution():
    """评论量分档与来源口径构成（接口 7）。"""
    return respond(overview_distribution)


@bp.get("/provinces")
def provinces():
    """客源地分布（接口 8）；仅 2022-08 之后的有效样本（BR-01）。"""
    def action():
        raw = request.args.get("limit", "20")
        try:
            limit = int(raw)
        except (TypeError, ValueError) as exc:
            raise ParamError("limit 必须为整数", code=1001) from exc
        return overview_provinces(limit)

    return respond(action)


@bp.get("/data-note")
def data_note():
    """数据来源、质量与已知局限说明（接口 9）。"""
    return respond(overview_data_note)


__all__ = ["bp"]
