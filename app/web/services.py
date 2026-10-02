# -*- coding: utf-8 -*-
"""后端业务层：只读组合分析结果（C4 组件 `C-API-02` ~ `C-API-08`、`C-API-14`）。

【架构原则（答辩必答）】
    本模块**只从数据库读取已经生成好的分析结果**，不做实时模型调用：
      · 统计类指标 ← 阶段三 Spark 的 `stat_spot` / `stat_time` / `stat_ip`
      · 情感/主题   ← 阶段三 `sentiment`(mllib) 与阶段四 `sentiment`(deepseek)、`topic`
      · 评价/事实包 ← 阶段四 C-BAT-06/07 的 `spot_fact_package` / `spot_report`
    因此"用户打开页面"不会产生任何 DeepSeek 调用，结果可复现、响应稳定、成本可控。

【口径纪律（BR-01/02/03/04/10）】
    凡受口径影响的返回都带 `caliber_note` 与 `sample_size`；
    数据不足时返回"正常业务结果"（如 `available=false`），而不是报错。

本模块不包含任何写操作；所有 SQL 均为只读且参数化。
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from app.web.data_access import (
    CALIBER_ASPECT,
    CALIBER_IP,
    CALIBER_SENTIMENT,
    CALIBER_THRESHOLD,
    CALIBER_TREND,
    ParamError,
    fetch_all,
    fetch_one,
    scalar,
)

# 情感方法白名单（`sentiment.method` 的取值）
SENTIMENT_METHODS = ("deepseek", "mllib", "dict")
# 排行依据白名单
RANKING_BY = {
    "reviews": "st.review_count",
    "rating": "st.avg_score",
    "positive_rate": "st.positive_rate",
}


# ---------------------------------------------------------------------------
# M1 数据总览（C-API-02）
# ---------------------------------------------------------------------------


def overview_summary() -> dict[str, Any]:
    """数据集规模 + 评分分布 + 情感方法覆盖情况。

    说明：`stat_overview` 表在阶段三被明确"不重建"（设计输出清单未含该表），
    因此这里的全局汇总由 `review` / `spot` / `stat_spot` **实时聚合**得到，
    与 `统计聚合（C-SPK-02）` 的字段口径保持一致。
    """
    totals = fetch_one(
        """
        SELECT (SELECT COUNT(*) FROM review)                        AS review_count,
               (SELECT COUNT(*) FROM spot)                          AS spot_count,
               (SELECT COUNT(*) FROM spot WHERE source_scope='tibet') AS spot_tibet,
               (SELECT COUNT(*) FROM spot WHERE source_scope='route') AS spot_route,
               (SELECT COUNT(*) FROM spot WHERE has_full_evaluation=1) AS spot_ge100,
               (SELECT COUNT(*) FROM review WHERE is_low_info=1)     AS low_info_count,
               (SELECT COUNT(*) FROM review WHERE is_dup_content=1)  AS dup_count,
               (SELECT COUNT(DISTINCT dup_group_id) FROM review WHERE is_dup_content=1) AS dup_group_count,
               (SELECT COUNT(*) FROM review WHERE image_count > 0)   AS image_review_count,
               (SELECT COUNT(*) FROM review WHERE ip_is_unknown=1)   AS ip_unknown_count,
               (SELECT COUNT(*) FROM review WHERE score IS NULL)     AS score_null_count,
               (SELECT COUNT(*) FROM review WHERE is_dup_content=0)  AS dedup_review_count
        """
    ) or {}

    score_dist = fetch_all(
        """
        SELECT score, COUNT(*) AS n
          FROM review
         WHERE score IS NOT NULL
         GROUP BY score
         ORDER BY score DESC
        """
    )
    scored_total = sum(int(row["n"]) for row in score_dist)

    # 情感方法覆盖：让前端能如实展示"哪些结果是全量的、哪些还在生产中"
    sentiment_cover = fetch_all(
        "SELECT method, COUNT(*) AS n FROM sentiment GROUP BY method ORDER BY n DESC"
    )
    topic_cover = fetch_one(
        "SELECT (SELECT COUNT(*) FROM topic) AS topic_count, (SELECT COUNT(*) FROM topic_word) AS topic_word_count"
    ) or {}

    return {
        "totals": totals,
        "score_distribution": [
            {
                "score": int(row["score"]),
                "count": int(row["n"]),
                "rate": round(int(row["n"]) / scored_total, 4) if scored_total else None,
            }
            for row in score_dist
        ],
        "scored_total": scored_total,
        "coverage": {
            "sentiment": sentiment_cover,
            "topic": topic_cover,
            "report": {
                "spots_ge100": int((totals or {}).get("spot_ge100") or 0),
                "reports_generated": scalar("SELECT COUNT(*) FROM spot_report"),
                "fact_packages": scalar("SELECT COUNT(*) FROM spot_fact_package"),
            },
        },
        "caliber_note": CALIBER_THRESHOLD + " " + CALIBER_TREND,
        "sample_size": int((totals or {}).get("review_count") or 0),
    }


def overview_trend(granularity: str = "year") -> dict[str, Any]:
    """评论量时间趋势（`granularity=year|month`），直接读阶段三的 `stat_time`。

    `stat_time` 口径：`scope_type='global'` 为全局，‘spot’ 为单景点；
    这里只取 global，避免把景点明细混进总览曲线。
    """
    if granularity not in ("year", "month"):
        raise ParamError("granularity 只能为 year 或 month", code=1001)

    rows = fetch_all(
        """
        SELECT period_type, period, review_count, avg_score, sentiment_avg
          FROM stat_time
         WHERE scope_type = 'global' AND period_type = %s
         ORDER BY period
        """,
        (granularity,),
    )
    total = sum(int(row["review_count"] or 0) for row in rows)
    return {
        "granularity": granularity,
        "points": [
            {
                "period": row["period"],
                "review_count": int(row["review_count"] or 0),
                "avg_score": row["avg_score"],
                # 说明：`stat_time` 没有正/负好评率列（设计输出清单里也没有），
                # 只有情感均值；`sentiment_avg` 目前为 NULL（阶段三未填，见 spark/README §9）。
                "sentiment_avg": row["sentiment_avg"],
            }
            for row in rows
        ],
        "total": total,
        "caliber_note": CALIBER_TREND + " `sentiment_avg` 由 Spark 统计任务预留，当前为 NULL（未填充）。",
        "sample_size": total,
    }


def overview_distribution() -> dict[str, Any]:
    """评论量分档 + 来源口径构成（西藏 / 进藏沿线）。"""
    buckets = fetch_all(
        """
        SELECT CASE
                 WHEN review_count >= 3000 THEN '3000+'
                 WHEN review_count >= 1000 THEN '1000-2999'
                 WHEN review_count >= 500  THEN '500-999'
                 WHEN review_count >= 100  THEN '100-499'
                 WHEN review_count >= 10   THEN '10-99'
                 ELSE '1-9'
               END AS bucket,
               COUNT(*) AS spot_count,
               SUM(review_count) AS review_count
          FROM stat_spot
         GROUP BY bucket
        """
    )
    order = ["3000+", "1000-2999", "500-999", "100-499", "10-99", "1-9"]
    buckets.sort(key=lambda row: order.index(row["bucket"]) if row["bucket"] in order else 99)

    scope = fetch_all(
        """
        SELECT s.source_scope,
               COUNT(DISTINCT s.spot_id) AS spot_count,
               COUNT(r.comment_id)       AS review_count
          FROM spot s LEFT JOIN review r ON r.spot_id = s.spot_id
         GROUP BY s.source_scope
        """
    )
    return {
        "buckets": [
            {
                "bucket": row["bucket"],
                "spot_count": int(row["spot_count"]),
                "review_count": int(row["review_count"] or 0),
            }
            for row in buckets
        ],
        "source_scope": [
            {
                "scope": row["source_scope"],
                "label": "西藏" if row["source_scope"] == "tibet" else "进藏沿线",
                "spot_count": int(row["spot_count"]),
                "review_count": int(row["review_count"] or 0),
            }
            for row in scope
        ],
        "caliber_note": CALIBER_THRESHOLD,
        "sample_size": scalar("SELECT COUNT(*) FROM review"),
    }


def overview_provinces(limit: int = 20) -> dict[str, Any]:
    """客源地分布（BR-01：仅 2022-08 之后的有效样本）。

    直接读阶段三的 `stat_ip`（`scope_type='global'`），不自建第二套口径。
    """
    if limit < 1 or limit > 100:
        raise ParamError("limit 必须在 1–100 之间", code=1002)
    rows = fetch_all(
        """
        SELECT ip_province, review_count, ratio, is_overseas
          FROM stat_ip
         WHERE scope_type = 'global'
         ORDER BY review_count DESC
         LIMIT %s
        """,
        (limit,),
    )
    province_count = scalar("SELECT COUNT(*) FROM stat_ip WHERE scope_type='global'")
    # 有效样本量直接取 `stat_ip.sample_size`（BR-01 口径：2022-08 后且排除"未知"）
    sample_row = fetch_one(
        "SELECT MAX(sample_size) AS n FROM stat_ip WHERE scope_type='global'"
    ) or {}
    return {
        "points": [
            {
                "province": row["ip_province"],
                "review_count": int(row["review_count"] or 0),
                "rate": row["ratio"],
                "is_overseas": bool(row["is_overseas"]),
            }
            for row in rows
        ],
        "province_count": province_count,
        "caliber_note": CALIBER_IP,
        "sample_size": int(sample_row.get("n") or 0),
    }


def overview_data_note() -> dict[str, Any]:
    """数据来源、质量与已知局限（FR-OV 的"数据说明"）。"""
    return {
        "dataset": "西藏及进藏沿线（川藏/滇藏/青藏线）旅游景点评论数据集",
        "source": "携程景点评论页公开评论（阶段一采集、阶段二清洗、阶段三离线分析）",
        "size": {
            "reviews": scalar("SELECT COUNT(*) FROM review"),
            "spots": scalar("SELECT COUNT(*) FROM spot"),
            "spots_tibet": scalar("SELECT COUNT(*) FROM spot WHERE source_scope='tibet'"),
            "spots_route": scalar("SELECT COUNT(*) FROM spot WHERE source_scope='route'"),
        },
        "quality": {
            "empty_content": scalar("SELECT COUNT(*) FROM review WHERE content IS NULL OR TRIM(content)=''"),
            "low_info": scalar("SELECT COUNT(*) FROM review WHERE is_low_info=1"),
            "dup_content": scalar("SELECT COUNT(*) FROM review WHERE is_dup_content=1"),
            "dup_groups": scalar("SELECT COUNT(DISTINCT dup_group_id) FROM review WHERE is_dup_content=1"),
            "score_null": scalar("SELECT COUNT(*) FROM review WHERE score IS NULL"),
            "ip_unknown": scalar("SELECT COUNT(*) FROM review WHERE ip_is_unknown=1"),
        },
        "limitations": [
            CALIBER_IP,
            CALIBER_TREND,
            CALIBER_THRESHOLD,
            CALIBER_ASPECT,
            "评论为历史快照，不含实时数据；系统不做实时分析。",
            "情感存在两种方法（deepseek / mllib），结论不可混用，需按 method 区分展示。",
        ],
        "caliber_note": "以上为数据来源与已知局限说明，供前端『数据说明』区域展示。",
        "sample_size": scalar("SELECT COUNT(*) FROM review"),
    }


# ---------------------------------------------------------------------------
# M2 景点分析（C-API-03 / 04）
# ---------------------------------------------------------------------------


def list_spots(keyword: str | None, page: int, page_size: int) -> dict[str, Any]:
    """景点列表（支持关键词模糊匹配，按评论量降序）。"""
    where = "WHERE 1=1"
    params: list[Any] = []
    if keyword:
        where += " AND s.spot_name LIKE %s"
        params.append(f"%{keyword}%")

    total = scalar(f"SELECT COUNT(*) FROM spot s {where}", tuple(params))
    rows = fetch_all(
        f"""
        SELECT s.spot_id, s.spot_name, s.source_scope, s.has_full_evaluation,
               s.address, s.review_count,
               st.avg_score, st.positive_rate, st.negative_rate
          FROM spot s LEFT JOIN stat_spot st ON st.spot_id = s.spot_id
          {where}
         ORDER BY s.review_count DESC, s.spot_id
         LIMIT %s OFFSET %s
        """,
        tuple(params) + (page_size, (page - 1) * page_size),
    )
    for row in rows:
        row["source_label"] = "西藏" if row["source_scope"] == "tibet" else "进藏沿线"
        row["evaluation_available"] = bool(row["has_full_evaluation"])

    return {
        "items": rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        "caliber_note": CALIBER_THRESHOLD,
        "sample_size": total,
    }


def ranking_spots(by: str, limit: int) -> dict[str, Any]:
    """景点排行（by=reviews|rating|positive_rate）。"""
    if by not in RANKING_BY:
        raise ParamError("by 只能为 reviews / rating / positive_rate", code=1001)
    if limit < 1 or limit > 100:
        raise ParamError("limit 必须在 1–100 之间", code=1002)

    order_column = RANKING_BY[by]
    # 评分/好评率排行必须排除样本过小的景点，否则头部会被"1 条 5 星"的景点占据
    min_reviews = 10 if by in ("rating", "positive_rate") else 1
    rows = fetch_all(
        f"""
        SELECT s.spot_id, s.spot_name, s.source_scope, s.has_full_evaluation,
               st.review_count, st.avg_score, st.positive_rate, st.negative_rate
          FROM spot s JOIN stat_spot st ON st.spot_id = s.spot_id
         WHERE st.review_count >= %s
         ORDER BY {order_column} DESC, st.review_count DESC
         LIMIT %s
        """,
        (min_reviews, limit),
    )
    note = CALIBER_THRESHOLD
    if min_reviews > 1:
        note += f" 排行按 {by} 排序时仅纳入评论量 ≥ {min_reviews} 条的景点，避免小样本失真。"
    return {
        "by": by,
        "min_reviews": min_reviews,
        "items": rows,
        "caliber_note": note,
        "sample_size": len(rows),
    }


def spot_detail(spot_id: int) -> dict[str, Any] | None:
    """景点详情：基础信息 + 统计指标 + 评价可用性判断（BR-02/BR-03）。"""
    spot = fetch_one(
        """
        SELECT s.spot_id, s.spot_name, s.source_scope, s.address, s.open_time, s.phone,
               s.introduction, s.review_count, s.has_full_evaluation, s.poi_url
          FROM spot s WHERE s.spot_id = %s
        """,
        (spot_id,),
    )
    if not spot:
        return None

    stat = fetch_one("SELECT * FROM stat_spot WHERE spot_id = %s", (spot_id,)) or {}
    review_count = int(spot["review_count"] or 0)
    available = review_count >= 100

    data: dict[str, Any] = {
        "spot": spot,
        "statistics": stat,
        "review_count": review_count,
        # BR-03：<100 条只展示基础统计并提示样本不足，不对评价性内容下结论
        "evaluation_available": available,
        "availability_note": (
            None if available
            else "该景点评论量不足 100 条，仅提供基础统计，不生成智能评价。"
        ),
        # 缺失字段不展示（FR-SA-01）：显式告诉前端哪些字段为空，不要用占位符填充
        "missing_fields": [
            field for field in ("open_time", "phone", "introduction", "poi_url")
            if not spot.get(field)
        ],
        "caliber_note": CALIBER_THRESHOLD,
        "sample_size": review_count,
    }
    return data


def spot_trend(spot_id: int, granularity: str = "year") -> dict[str, Any] | None:
    """景点时间趋势（读 `stat_time` 的 spot 维度）。"""
    if not fetch_one("SELECT 1 FROM spot WHERE spot_id=%s", (spot_id,)):
        return None
    if granularity not in ("year", "month"):
        raise ParamError("granularity 只能为 year 或 month", code=1001)
    rows = fetch_all(
        """
        SELECT period, review_count, avg_score, sentiment_avg
          FROM stat_time
         WHERE scope_type='spot' AND scope_id=%s AND period_type=%s
         ORDER BY period
        """,
        (spot_id, granularity),
    )
    total = sum(int(row["review_count"] or 0) for row in rows)
    return {
        "spot_id": spot_id,
        "granularity": granularity,
        "points": rows,
        "total": total,
        "caliber_note": CALIBER_TREND,
        "sample_size": total,
    }


def spot_sentiment(spot_id: int, method: str = "deepseek") -> dict[str, Any] | None:
    """情感占比（按 method 并列展示；FR-SA-04 支持基线对照）。"""
    if not fetch_one("SELECT 1 FROM spot WHERE spot_id=%s", (spot_id,)):
        return None
    if method not in SENTIMENT_METHODS:
        raise ParamError("method 只能为 deepseek / mllib / dict", code=1001)

    counts = fetch_all(
        """
        SELECT polarity, COUNT(*) AS n
          FROM sentiment
         WHERE spot_id = %s AND method = %s
         GROUP BY polarity
        """,
        (spot_id, method),
    )
    total = sum(int(row["n"]) for row in counts)
    by_polarity = {row["polarity"]: int(row["n"]) for row in counts}

    # 三种方法的可用性：让前端区分"没有结果"与"结果为 0"
    availability = fetch_all(
        "SELECT method, COUNT(*) AS n FROM sentiment WHERE spot_id=%s GROUP BY method",
        (spot_id,),
    )
    note = CALIBER_SENTIMENT
    if method == "deepseek" and total == 0:
        note += " 该景点暂无 deepseek 方法结果（离线语义分析尚未覆盖到此景点）。"
    return {
        "spot_id": spot_id,
        "method": method,
        "sample_size": total,
        "distribution": {
            "positive": by_polarity.get("positive", 0),
            "neutral": by_polarity.get("neutral", 0),
            "negative": by_polarity.get("negative", 0),
        },
        "rates": {
            "positive": round(by_polarity.get("positive", 0) / total, 4) if total else None,
            "neutral": round(by_polarity.get("neutral", 0) / total, 4) if total else None,
            "negative": round(by_polarity.get("negative", 0) / total, 4) if total else None,
        },
        "available_methods": {row["method"]: int(row["n"]) for row in availability},
        "caliber_note": note,
    }


def spot_aspects(spot_id: int, method: str = "deepseek") -> dict[str, Any] | None:
    """方面分析（C-API-07）：实施 BR-04 样本门槛。

    返回**全部**方面，但每个方面带 `conclusive` 标志：
    样本 <10 时不给出正负倾向（rate 为 null），只回样本量与 note——
    这是 BR-04 在接口层的落地，避免前端误用不可靠的比例。

    **口径限制（如实说明）**：方面级结果只有 `deepseek` 一种来源——
    阶段三 Spark 的 MLlib 基线只做评论级三分类（`sentiment`），不产出 `aspect` 行。
    因此这里只接受 `method='deepseek'`；传 mllib 会明确报参数错误，
    而不是返回一个看起来"该景点没有方面数据"的空列表（那会误导使用者）。
    """
    if not fetch_one("SELECT 1 FROM spot WHERE spot_id=%s", (spot_id,)):
        return None
    if method != "deepseek":
        raise ParamError(
            "方面分析仅支持 method=deepseek：MLlib 基线只产出评论级情感（sentiment），"
            "不产出方面级结果（aspect）",
            code=1001,
        )

    rows = fetch_all(
        """
        SELECT aspect_name,
               COUNT(*) AS sample_size,
               SUM(polarity='positive') AS positive_n,
               SUM(polarity='negative') AS negative_n,
               SUM(polarity='neutral')  AS neutral_n
          FROM aspect
         WHERE spot_id=%s AND method=%s
         GROUP BY aspect_name
         ORDER BY sample_size DESC, aspect_name
        """,
        (spot_id, method),
    )
    items = []
    for row in rows:
        sample = int(row["sample_size"] or 0)
        conclusive = sample >= 10
        items.append(
            {
                "aspect": row["aspect_name"],
                "sample_size": sample,
                "conclusive": conclusive,
                "positive_n": int(row["positive_n"] or 0),
                "negative_n": int(row["negative_n"] or 0),
                "neutral_n": int(row["neutral_n"] or 0),
                "positive_rate": round(int(row["positive_n"] or 0) / sample, 4) if conclusive else None,
                "negative_rate": round(int(row["negative_n"] or 0) / sample, 4) if conclusive else None,
                "note": None if conclusive else "样本不足（<10 条），不出结论",
            }
        )
    total_sample = sum(int(row["sample_size"] or 0) for row in rows)
    return {
        "spot_id": spot_id,
        "method": method,
        "items": items,
        "conclusive_count": sum(1 for item in items if item["conclusive"]),
        "caliber_note": CALIBER_ASPECT,
        "sample_size": total_sample,
    }


def spot_topics(spot_id: int) -> dict[str, Any] | None:
    """LDA 主题（C-API-06）：优先返回该景点的主题，没有则回退到 global 主题。"""
    if not fetch_one("SELECT 1 FROM spot WHERE spot_id=%s", (spot_id,)):
        return None
    topics = fetch_all(
        """
        SELECT topic_id, scope_type, scope_id, topic_index, topic_rate, sample_size, model_version
          FROM topic
         WHERE (scope_type='spot' AND scope_id=%s) OR scope_type='global'
         ORDER BY (scope_type='spot') DESC, topic_index
        """,
        (spot_id,),
    )
    if not topics:
        return {"spot_id": spot_id, "scope": None, "topics": [], "caliber_note": "暂无主题结果。", "sample_size": 0}

    scope = topics[0]["scope_type"]
    selected = [t for t in topics if t["scope_type"] == scope]
    words = fetch_all(
        """
        SELECT topic_id, rank_no, word, weight
          FROM topic_word
         WHERE topic_id IN (%s)
         ORDER BY topic_id, rank_no
        """
        % ",".join(["%s"] * len(selected)),
        tuple(t["topic_id"] for t in selected),
    )
    by_topic: dict[int, list[dict]] = {}
    for row in words:
        by_topic.setdefault(int(row["topic_id"]), []).append(
            {"rank_no": int(row["rank_no"]), "word": row["word"], "weight": row["weight"]}
        )
    for topic in selected:
        topic["words"] = by_topic.get(int(topic["topic_id"]), [])

    note = "主题模型由 Spark LDA 训练（阶段三）；单景点主题仅在具备完整评价资格且可建模评论 ≥100 条的景点上产出。"
    if scope == "global":
        note += " 该景点暂无专属主题模型，此处展示全局主题。"
    return {
        "spot_id": spot_id,
        "scope": scope,
        "topics": selected,
        "caliber_note": note,
        "sample_size": int(selected[0]["sample_size"] or 0) if selected else 0,
    }


def spot_reviews(spot_id: int, limit: int = 5) -> dict[str, Any] | None:
    """代表性正负面评论（C-API-08，FR-SA-07：优先非重复正文）。"""
    if not fetch_one("SELECT 1 FROM spot WHERE spot_id=%s", (spot_id,)):
        return None
    if limit < 1 or limit > 20:
        raise ParamError("limit 必须在 1–20 之间", code=1002)

    def pick(polarity: str) -> list[dict]:
        if polarity == "positive":
            condition, order_extra = "r.score >= 4", "0"
        else:
            condition, order_extra = "r.score <= 2", "0"
        return fetch_all(
            f"""
            SELECT r.comment_id, r.content, r.score, r.like_count, r.publish_date,
                   r.content_length, r.is_low_info, r.is_dup_content
              FROM review r
             WHERE r.spot_id = %s AND {condition}
               AND r.content IS NOT NULL AND TRIM(r.content) <> ''
             ORDER BY r.is_dup_content ASC, r.like_count DESC, r.content_length DESC
             LIMIT %s
            """,
            (spot_id, limit),
        )

    positive = pick("positive")
    negative = pick("negative")
    return {
        "spot_id": spot_id,
        "positive": positive,
        "negative": negative,
        # 负面不足时如实留少，不凑数（§15.B.3）
        "note": (
            "代表评论筛选：排除低信息量 → 优先非重复正文 → 点赞数降序；"
            "负面评论不足时如实留少，不补造。"
        ),
        "caliber_note": "代表评论仅用于展示原文依据，其数量不代表整体分布；分布请看统计与情感接口。",
        "sample_size": len(positive) + len(negative),
    }


# ---------------------------------------------------------------------------
# M3 景点智能评价（C-API-09）——只读，不在线调用模型
# ---------------------------------------------------------------------------


def spot_report(spot_id: int) -> dict[str, Any] | None:
    """景点智能评价（含事实依据回显）。

    **只读 `spot_report` / `spot_fact_package`**：评价在离线阶段由 C-BAT-07 生成并落库，
    本接口不触发任何模型调用（BR-12 / CC-1 / CC-2 的在线侧要求）。
    数据不足时返回 `available=false`（HTTP 200、code=0 的正常业务响应）。
    """
    spot = fetch_one(
        "SELECT spot_id, spot_name, review_count, has_full_evaluation FROM spot WHERE spot_id=%s",
        (spot_id,),
    )
    if not spot:
        return None

    review_count = int(spot["review_count"] or 0)
    report = fetch_one(
        """
        SELECT spot_id, fact_package_version, summary, advantages_json, issues_json,
               visitor_focus_json, model, prompt_version, need_review, token_usage, generated_at
          FROM spot_report WHERE spot_id = %s
        """,
        (spot_id,),
    )

    if review_count < 100:
        # BR-02/BR-03：低于门槛不出评价，属正常业务响应
        return {
            "spot_id": spot_id,
            "spot_name": spot["spot_name"],
            "available": False,
            "reason": "REVIEW_COUNT_BELOW_THRESHOLD",
            "message_text": "该景点评论量不足 100 条，仅提供基础统计，不生成智能评价。",
            "review_count": review_count,
            "caliber_note": CALIBER_THRESHOLD,
        }

    if not report:
        return {
            "spot_id": spot_id,
            "spot_name": spot["spot_name"],
            "available": False,
            "reason": "REPORT_NOT_GENERATED",
            "message_text": "该景点的智能评价尚未生成（离线评价任务未覆盖到此景点）。",
            "review_count": review_count,
            "caliber_note": CALIBER_THRESHOLD,
        }

    basis = _report_basis(spot_id)
    return {
        "spot_id": spot_id,
        "spot_name": spot["spot_name"],
        "available": True,
        "review_count": review_count,
        "report": {
            "summary": report["summary"],
            "advantages": _loads(report["advantages_json"]),
            "issues": _loads(report["issues_json"]),
            "visitor_focus": _loads(report["visitor_focus_json"]),
        },
        "basis": basis,
        "need_review": bool(report["need_review"]),
        "need_review_note": (
            "该评价在生成时存在与事实包不一致的数字，已标记待人工复核。" if report["need_review"] else None
        ),
        "model": report["model"],
        "prompt_version": report["prompt_version"],
        "fact_package_version": report["fact_package_version"],
        "generated_at": report["generated_at"],
        "caliber_note": CALIBER_THRESHOLD + " 智能评价为离线生成结果，页面读取不消耗模型调用。",
        "sample_size": review_count,
    }


def _report_basis(spot_id: int) -> dict[str, Any]:
    """组装的"事实依据"：优先用事实包快照（可追溯 FR-IE-07），缺失则用当前统计。"""
    package_row = fetch_one(
        "SELECT version, package_json, generated_at FROM spot_fact_package "
        "WHERE spot_id=%s ORDER BY generated_at DESC LIMIT 1",
        (spot_id,),
    )
    if package_row:
        package = package_row["package_json"]
        if isinstance(package, str):
            package = json.loads(package)
        stats = package.get("statistics") or {}
        aspects = [a for a in (package.get("aspects") or []) if a.get("sample_size")]
        aspects.sort(key=lambda a: a.get("sample_size") or 0, reverse=True)
        return {
            "source": "fact_package",
            "version": package_row["version"],
            "generated_at": package_row["generated_at"],
            "review_count": stats.get("review_count"),
            "avg_score": stats.get("avg_score"),
            "positive_rate": stats.get("positive_rate"),
            "negative_rate": stats.get("negative_rate"),
            "sentiment": package.get("sentiment"),
            "top_aspects": [
                {
                    "name": a.get("aspect"),
                    "sample_size": a.get("sample_size"),
                    "positive_rate": a.get("positive_rate"),
                    "negative_rate": a.get("negative_rate"),
                    "note": a.get("note"),
                }
                for a in aspects[:5]
            ],
            "representative_reviews": package.get("representative_reviews"),
        }

    stat = fetch_one("SELECT * FROM stat_spot WHERE spot_id=%s", (spot_id,)) or {}
    return {
        "source": "stat_spot",
        "version": stat.get("stat_version"),
        "generated_at": stat.get("updated_at"),
        "review_count": stat.get("review_count"),
        "avg_score": stat.get("avg_score"),
        "positive_rate": stat.get("positive_rate"),
        "negative_rate": stat.get("negative_rate"),
        "sentiment": None,
        "top_aspects": [],
        "representative_reviews": {"positive": [], "negative": []},
    }


def _loads(value: Any) -> Any:
    """JSON 列可能是字符串（取决于驱动），统一解析。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


# ---------------------------------------------------------------------------
# M6 系统管理（C-API-14）——任务与日志（只读）
# ---------------------------------------------------------------------------


def admin_tasks(limit: int = 20, task_type: str | None = None) -> dict[str, Any]:
    """批处理任务列表与进度（FR-SY-03）。"""
    if limit < 1 or limit > 100:
        raise ParamError("limit 必须在 1–100 之间", code=1002)
    params: list[Any] = []
    where = "WHERE 1=1"
    if task_type:
        where += " AND task_type = %s"
        params.append(task_type)

    rows = fetch_all(
        f"""
        SELECT task_id, task_type, task_name, status, total_count, success_count,
               fail_count, skip_count, started_at, finished_at, cost_seconds,
               model_type, model_version, random_seed, error_message, created_at
          FROM analysis_task {where}
         ORDER BY task_id DESC
         LIMIT %s
        """,
        tuple(params) + (limit,),
    )
    summary = fetch_all(
        "SELECT task_type, status, COUNT(*) AS n FROM analysis_task GROUP BY task_type, status ORDER BY task_type"
    )
    return {
        "items": rows,
        "summary": summary,
        "total": scalar("SELECT COUNT(*) FROM analysis_task"),
        "caliber_note": "任务状态来自运维表 analysis_task；success_count 为该任务实际写入的行数。",
        "sample_size": len(rows),
    }


def admin_task_detail(task_id: int) -> dict[str, Any] | None:
    """单个任务详情 + 日志明细（task_log）。"""
    task = fetch_one("SELECT * FROM analysis_task WHERE task_id=%s", (task_id,))
    if not task:
        return None
    logs = fetch_all(
        """
        SELECT log_id, level, stage, message, ref_key, retry_count, resolved,
               processed_count, detail_json, created_at
          FROM task_log WHERE task_id=%s ORDER BY log_id
        """,
        (task_id,),
    )
    for log in logs:
        log["detail"] = _loads(log.get("detail_json"))
    return {
        "task": task,
        "logs": logs,
        "log_count": len(logs),
        "error_count": sum(1 for log in logs if log["level"] == "ERROR"),
        "caliber_note": "日志来自统一日志表 task_log（合并原 clean_log 与 llm_failure）。",
        "sample_size": len(logs),
    }


def admin_caliber() -> dict[str, Any]:
    """系统数据口径配置（BR-10 的集中回显）。"""
    return {
        "calibers": [
            {"key": "ip_valid_since", "value": "2022-08-01", "note": CALIBER_IP},
            {"key": "min_review_count", "value": 100, "note": CALIBER_THRESHOLD},
            {"key": "aspect_min_sample", "value": 10, "note": CALIBER_ASPECT},
            {"key": "low_info_max_length", "value": 10,
             "note": "正文 ≤10 字标记低信息量；统计类计入全量，文本语义类走规则判定，不调用模型。"},
            {"key": "sentiment_methods", "value": list(SENTIMENT_METHODS), "note": CALIBER_SENTIMENT},
            {"key": "trend_note", "value": None, "note": CALIBER_TREND},
        ],
        "caliber_note": "以上为系统当前使用的数据口径；受口径影响的接口都会在 data 中回显对应说明。",
        "sample_size": scalar("SELECT COUNT(*) FROM review"),
    }


__all__ = [
    "overview_summary",
    "overview_trend",
    "overview_distribution",
    "overview_provinces",
    "overview_data_note",
    "list_spots",
    "ranking_spots",
    "spot_detail",
    "spot_trend",
    "spot_sentiment",
    "spot_aspects",
    "spot_topics",
    "spot_reviews",
    "spot_report",
    "admin_tasks",
    "admin_task_detail",
    "admin_caliber",
]
