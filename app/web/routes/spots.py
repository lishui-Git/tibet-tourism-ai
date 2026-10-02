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

from app.web.data_access import CALIBER_THRESHOLD, fetch_one, parse_int_arg, parse_pagination
from app.web.response import CODE_DB_ERROR, CODE_NOT_FOUND, fail, ok
from app.web.routes._auth_helpers import admin_required, current_user
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


@bp.post("/<int:spot_id>/report/regenerate")
@admin_required
def spots_report_regenerate(spot_id: int):
    """重新生成评价（接口 19，**管理员**）：**异步提交**，不在请求内调用模型。

    设计（§6.2 第 19 项）标注为"异步提交"，`景点智能评价流程图.md` 把它列为触发方式之一。
    实现取向（重要）：
        · 本接口只做三件事——校验景点与门槛、登记一条 **pending 任务**、返回提交结果；
        · **真正生成由离线批处理执行**（`python -m app.llm --stage report --spot-ids <id> --force`），
          因此 Web 端永远不会自己产生模型费用（成本闸门 `--yes`/`--offline` 全在离线侧）；
        · 若该景点评论量 <100（BR-02/BR-03），不生成评价，返回正常业务结果 `available=false`。

    这样既补齐了接口，又不会让"点一下按钮"变成不可控的在线消费。
    """
    from app.batch.task_registry import request_task
    from app.db import DatabaseError, connection

    spot = fetch_one(
        "SELECT spot_id, spot_name, review_count, has_full_evaluation FROM spot WHERE spot_id=%s",
        (spot_id,),
    )
    if not spot:
        return fail(CODE_NOT_FOUND, f"景点 {spot_id} 不存在")

    review_count = int(spot["review_count"] or 0)
    if review_count < 100:
        # BR-02/BR-03：低于门槛本就不生成评价，属正常业务响应（HTTP 200、code=0）
        return ok(
            {
                "spot_id": spot_id,
                "spot_name": spot["spot_name"],
                "submitted": False,
                "available": False,
                "reason": "REVIEW_COUNT_BELOW_THRESHOLD",
                "message_text": "该景点评论量不足 100 条，不生成智能评价（BR-02/BR-03）。",
                "review_count": review_count,
                "caliber_note": CALIBER_THRESHOLD,
            }
        )

    operator = current_user()
    try:
        with connection() as conn:
            task_id = request_task(
                conn,
                task_type="spot_report",
                task_name=f"（请求）重新生成景点评价 spot_id={spot_id} {spot['spot_name']}",
                total_count=1,
                operator_id=operator.user_id if operator else None,
            )
    except DatabaseError as exc:
        return fail(CODE_DB_ERROR, f"提交失败（数据库）：{exc}")

    # 注意：`ok()` 自身返回 `(response, http_status)` 二元组，
    # 要返回 202 必须取它的第一个元素再配新的状态码，否则会变成嵌套元组（Flask 会报错）。
    response, _ = ok(
        {
            "spot_id": spot_id,
            "spot_name": spot["spot_name"],
            "submitted": True,
            "task_id": task_id,
            "task_status": "pending",
            "review_count": review_count,
            "message_text": (
                "重新生成请求已登记（pending）。评价由离线任务生成，"
                "执行方式：python -m app.llm --stage report --spot-ids "
                f"{spot_id} --force"
            ),
            "caliber_note": (
                CALIBER_THRESHOLD
                + " 本接口只登记请求、不调用模型；实际生成与费用发生在离线批处理（需显式 --yes）。"
            ),
        }
    )
    return response, 202


__all__ = ["bp"]
