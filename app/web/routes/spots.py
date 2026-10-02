# -*- coding: utf-8 -*-
"""M2 景点分析 + M3 景点智能评价接口（C4 组件 `C-API-03`～`C-API-09`）。

对应页面：景点分析页（M2）、景点智能评价页（M3）。详细设计 §6.2 第 10–19 项。

**在线只读原则（BR-12 / CC-1 / CC-2）**：
本文件所有接口都只读取已经落库的分析结果（Spark 统计 + DeepSeek 离线语义 + 离线评价），
**任何接口都不会调用 DeepSeek**。评价类结果由离线任务生成，页面刷新不产生模型费用。

数据不足是**正常业务响应**（HTTP 200、code=0、`available=false`），不是错误（BR-03）。
"""

from __future__ import annotations

from flask import Blueprint, request

from app.web.data_access import parse_int_arg, parse_pagination
from app.web.routes._helpers import respond, respond_one
from app.web.services import (
    list_spots,
    ranking_spots,
    spot_aspects,
    spot_detail,
    spot_report,
    spot_reviews,
    spot_sentiment,
    spot_topics,
    spot_trend,
)

bp = Blueprint("spots", __name__, url_prefix="/api/spots")


@bp.get("")
def spots_list():
    """景点列表（接口 10）：`keyword`、`page`、`page_size`。"""
    keyword = request.args.get("keyword") or None

    # 参数校验必须在 respond() **内部**执行，否则 ParamError 会逃逸成 500
    def action():
        page, page_size = parse_pagination(request.args)
        return list_spots(keyword, page, page_size)

    return respond(action)


@bp.get("/ranking")
def spots_ranking():
    """景点排行（接口 11）：`by=reviews|rating|positive_rate`。"""
    by = request.args.get("by", "reviews")

    def action():
        limit = parse_int_arg(request.args, "limit", required=False, default=20)
        return ranking_spots(by, limit or 20)

    return respond(action)


@bp.get("/<int:spot_id>")
def spots_detail(spot_id: int):
    """景点详情与统计指标（接口 12）。"""
    return respond_one(lambda: spot_detail(spot_id), f"景点 {spot_id} 不存在")


@bp.get("/<int:spot_id>/trend")
def spots_trend(spot_id: int):
    """景点时间趋势（接口 13）。"""
    granularity = request.args.get("granularity", "year")
    return respond_one(lambda: spot_trend(spot_id, granularity), f"景点 {spot_id} 不存在")


@bp.get("/<int:spot_id>/sentiment")
def spots_sentiment(spot_id: int):
    """情感占比（接口 14）：`method=deepseek|mllib|dict`，默认 deepseek。"""
    method = request.args.get("method", "deepseek")
    return respond_one(lambda: spot_sentiment(spot_id, method), f"景点 {spot_id} 不存在")


@bp.get("/<int:spot_id>/aspects")
def spots_aspects(spot_id: int):
    """方面分析（接口 15）：含 BR-04 样本门槛判断。"""
    method = request.args.get("method", "deepseek")
    return respond_one(lambda: spot_aspects(spot_id, method), f"景点 {spot_id} 不存在")


@bp.get("/<int:spot_id>/topics")
def spots_topics(spot_id: int):
    """LDA 主题（接口 16）。"""
    return respond_one(lambda: spot_topics(spot_id), f"景点 {spot_id} 不存在")


@bp.get("/<int:spot_id>/reviews")
def spots_reviews(spot_id: int):
    """代表性正负面评论（接口 17）：`limit` 默认 5。"""
    def action():
        limit = parse_int_arg(request.args, "limit", required=False, default=5)
        return spot_reviews(spot_id, limit or 5)

    return respond_one(action, f"景点 {spot_id} 不存在")


@bp.get("/<int:spot_id>/report")
def spots_report(spot_id: int):
    """景点智能评价（接口 18）：含事实依据回显。

    `available=false`（评论量不足 / 评价未生成）是**正常业务响应**，HTTP 200、code=0。
    """
    return respond_one(lambda: spot_report(spot_id), f"景点 {spot_id} 不存在")


__all__ = ["bp"]
