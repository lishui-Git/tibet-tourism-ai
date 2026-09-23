# -*- coding: utf-8 -*-
"""C-BAT-06 景点事实包构造（设计 §4.3 / §15.B / FR-IE-02 / BR-02 / BR-04 / FR-IE-07）。

【组件职责】
    为"评论量 ≥100 的 57 个景点"组装一份**可追溯的事实快照**，写入 `spot_fact_package`。

【本组件不调用任何模型】——这是"数据负责事实"的第一道保证：
    事实包里的每个数字都由 SQL 聚合得到（或读取 Spark 已验证的结果），
    模型在 C-BAT-07 只能引用这些数字，不能自己计算。
    DeepSeek 在整条链路中的分工是：C-BAT-06 提供"事实"，C-BAT-07 提供"解释"。

【六部分事实（§15.B.1）】
    ① basic     景点基础信息        ← `spot`（名称/地址/介绍摘要）
    ② statistics 评论统计指标        ← `stat_spot`（Spark C-SPK-02 已验证结果）
    ③ sentiment 情感占比＋样本量     ← `sentiment` WHERE method='deepseek'
    ④ aspects   方面样本量与倾向     ← `aspect` WHERE method='deepseek'（样本<10 不纳入结论，BR-04）
    ⑤⑥ representative_reviews 代表性正面/负面评论 ← `review`（规则见 §15.B.3）

【幂等与版本】`UK(spot_id, version)`；每次生成写入指定 `version`（默认 v1），不覆盖历史版本（FR-IE-07）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from app.batch.task_registry import TaskRecorder, chunked, finish_task, register_task
from app.db import connection, query_all
from app.llm.prompts import ASPECT_CANDIDATES

# 事实包口径常量（写进 package_json.caliber，前端/论文可直接引用）
MIN_REVIEW_COUNT = 100    # BR-02
ASPECT_MIN_SAMPLE = 10    # BR-04
IP_VALID_SINCE = "2022-08-01"  # BR-01

REP_REVIEW_MIN, REP_REVIEW_MAX = 2, 5       # §15.B.3 各取 2–5 条
REP_PREFER_LEN = (30, 200)                  # 正文长度优先区间
INTRO_EXCERPT_CHARS = 200                   # 介绍摘要长度（控制事实包体积）

FACT_PACKAGE_VERSION = "v1"


# ---------------------------------------------------------------------------
# 一、SQL（全部为只读聚合）
# ---------------------------------------------------------------------------

TARGET_SPOTS_SQL = """
    SELECT s.spot_id, s.spot_name, s.address, s.introduction, s.source_scope,
           st.review_count, st.avg_score, st.positive_rate, st.negative_rate,
           st.image_rate, st.total_likes, st.first_comment_date, st.last_comment_date,
           st.stat_version
      FROM spot s
      JOIN stat_spot st ON st.spot_id = s.spot_id
     WHERE s.has_full_evaluation = 1
     ORDER BY st.review_count DESC, s.spot_id
"""

SENTIMENT_SQL = """
    SELECT polarity, COUNT(*) AS n
      FROM sentiment
     WHERE spot_id = %s AND method = 'deepseek'
     GROUP BY polarity
"""

ASPECT_SQL = """
    SELECT aspect_name,
           COUNT(*) AS sample_size,
           SUM(polarity = 'positive') AS positive_n,
           SUM(polarity = 'negative') AS negative_n
      FROM aspect
     WHERE spot_id = %s AND method = 'deepseek'
     GROUP BY aspect_name
     ORDER BY sample_size DESC, aspect_name
"""

# 代表评论候选池：排除低信息量与重复正文（§15.B.3 ①②），按点赞降序取回，
# "正文长度 30–200 字优先"在 Python 侧排序（SQL 里做长度偏好会牺牲可读性）。
# 正面 = score >= 4、负面 = score <= 2（§15.B.3 ④），因此写成两条常量，避免动态比较符。
REP_REVIEWS_SQL_TEMPLATE = """
    SELECT comment_id, content, like_count, score, image_count, content_length
      FROM review
     WHERE spot_id = %s
       AND is_low_info = 0
       AND is_dup_content = 0
       AND content IS NOT NULL
       AND score {comparison}
     ORDER BY like_count DESC, content_length DESC, comment_id
     LIMIT 400
"""

REP_REVIEWS_POSITIVE_SQL = REP_REVIEWS_SQL_TEMPLATE.format(comparison=">= 4")
REP_REVIEWS_NEGATIVE_SQL = REP_REVIEWS_SQL_TEMPLATE.format(comparison="<= 2")

FACT_PACKAGE_UPSERT_SQL = """
    INSERT INTO spot_fact_package
        (spot_id, version, package_json, review_count, stat_version, generated_at, task_id)
    VALUES (%s, %s, %s, %s, %s, NOW(), %s)
    ON DUPLICATE KEY UPDATE
        package_json = VALUES(package_json),
        review_count = VALUES(review_count),
        stat_version = VALUES(stat_version),
        generated_at = NOW(),
        task_id      = VALUES(task_id)
"""


# ---------------------------------------------------------------------------
# 二、事实包组装
# ---------------------------------------------------------------------------


@dataclass
class FactPackageStats:
    spots_total: int = 0
    spots_generated: int = 0
    spots_skipped: int = 0
    aspects_included: int = 0
    aspects_excluded: int = 0
    reviews_without_sentiment: int = 0   # 情感样本缺失的景点数（如实记录）

    def as_detail(self) -> dict[str, int]:
        return {k: int(v) for k, v in self.__dict__.items()}


def _rate(numerator: int, denominator: int) -> float | None:
    """占比：保留 2 位小数（§15.B.3 数值精度）；分母为 0 时返回 None（不造数）。"""
    if not denominator:
        return None
    return round(numerator / denominator, 2)


def _pick_representatives(rows: Sequence[dict], want_negative: bool = False) -> list[dict]:
    """按 §15.B.3 的规则挑代表评论。

    顺序：排除低信息量/重复正文（SQL 已做）→ 点赞降序 → 正文 30–200 字优先 → 取 2–5 条。
    **负面不足如实留少，不凑数**。
    """
    if want_negative:
        pool = [row for row in rows if (row["score"] or 0) <= 2]
    else:
        pool = [row for row in rows if (row["score"] or 0) >= 4]

    low, high = REP_PREFER_LEN
    pool.sort(
        key=lambda row: (
            0 if low <= int(row["content_length"] or 0) <= high else 1,  # 长度优先区间
            -int(row["like_count"] or 0),                                 # 点赞降序
            int(row["comment_id"]),
        )
    )
    picked = pool[:REP_REVIEW_MAX]
    return [
        {
            "comment_id": str(row["comment_id"]),
            "content": (row["content"] or "")[:300],
            "like_count": int(row["like_count"] or 0),
            "score": int(row["score"]) if row["score"] is not None else None,
        }
        for row in picked
    ]


def build_fact_package(
    spot_row: dict,
    sentiment_rows: Sequence[dict],
    aspect_rows: Sequence[dict],
    review_pool: Sequence[dict],
    *,
    version: str = FACT_PACKAGE_VERSION,
    stats: FactPackageStats | None = None,
) -> dict[str, Any]:
    """组装单个景点的事实包（纯函数，便于单测与复核）。"""
    stats = stats or FactPackageStats()
    review_count = int(spot_row["review_count"] or 0)

    # ---- ③ 情感占比（method='deepseek'）-----------------------------------
    counts = {row["polarity"]: int(row["n"]) for row in sentiment_rows}
    sentiment_sample = sum(counts.values())
    if sentiment_sample == 0:
        stats.reviews_without_sentiment += 1
    sentiment_block = {
        "method": "deepseek",
        "sample_size": sentiment_sample,
        "positive": _rate(counts.get("positive", 0), sentiment_sample),
        "neutral": _rate(counts.get("neutral", 0), sentiment_sample),
        "negative": _rate(counts.get("negative", 0), sentiment_sample),
    }

    # ---- ④ 方面分析（样本 <10 以 note 标注，不参与评价依据 BR-04）--------
    aspects: list[dict[str, Any]] = []
    for row in aspect_rows:
        sample = int(row["sample_size"] or 0)
        if sample >= ASPECT_MIN_SAMPLE:
            aspects.append(
                {
                    "aspect": row["aspect_name"],
                    "sample_size": sample,
                    "positive_rate": _rate(int(row["positive_n"] or 0), sample),
                    "negative_rate": _rate(int(row["negative_n"] or 0), sample),
                    "note": None,
                }
            )
            stats.aspects_included += 1
        else:
            aspects.append(
                {
                    "aspect": row["aspect_name"],
                    "sample_size": sample,
                    "positive_rate": None,
                    "negative_rate": None,
                    "note": f"样本不足（<{ASPECT_MIN_SAMPLE} 条），不纳入评价",
                }
            )
            stats.aspects_excluded += 1

    # ---- ⑤⑥ 代表性评论 -----------------------------------------------------
    representative = {
        "positive": _pick_representatives(review_pool, want_negative=False),
        "negative": _pick_representatives(review_pool, want_negative=True),
    }

    # ---- ② 统计指标（来自 Spark 的 stat_spot，不在此重新计算）-------------
    statistics = {
        "review_count": review_count,
        "avg_score": float(spot_row["avg_score"]) if spot_row["avg_score"] is not None else None,
        "positive_rate": float(spot_row["positive_rate"]) if spot_row["positive_rate"] is not None else None,
        "negative_rate": float(spot_row["negative_rate"]) if spot_row["negative_rate"] is not None else None,
        "image_rate": float(spot_row["image_rate"]) if spot_row["image_rate"] is not None else None,
        "total_likes": int(spot_row["total_likes"] or 0),
        "time_range": {
            "first": str(spot_row["first_comment_date"]) if spot_row["first_comment_date"] else None,
            "last": str(spot_row["last_comment_date"]) if spot_row["last_comment_date"] else None,
        },
    }

    intro = (spot_row["introduction"] or "").strip()
    package = {
        "spot_id": int(spot_row["spot_id"]),
        "spot_name": spot_row["spot_name"],
        "version": version,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "basic": {
            "address": spot_row["address"],
            "intro_excerpt": intro[:INTRO_EXCERPT_CHARS] if intro else None,
            "source_scope": spot_row["source_scope"],
        },
        "statistics": statistics,
        "sentiment": sentiment_block,
        "aspects": aspects,
        "representative_reviews": representative,
        "caliber": {
            "min_review_count": MIN_REVIEW_COUNT,
            "aspect_min_sample": ASPECT_MIN_SAMPLE,
            "ip_valid_since": IP_VALID_SINCE,
            "candidate_aspects": list(ASPECT_CANDIDATES),
        },
    }
    return package


# ---------------------------------------------------------------------------
# 三、主流程
# ---------------------------------------------------------------------------


def run_fact_package(
    *,
    spot_ids: Sequence[int] | None = None,
    limit: int | None = None,
    version: str = FACT_PACKAGE_VERSION,
    only_missing: bool = False,
) -> dict[str, Any]:
    """构造并写入景点事实包（零模型调用）。

    :param spot_ids: 只处理指定景点（调试/补跑）
    :param limit: 小样本：只处理前 N 个景点
    :param only_missing: 已有同版本事实包的景点跳过（默认重算覆盖，因为事实包要反映最新统计）
    """
    started_at = datetime.now()
    targets = query_all(TARGET_SPOTS_SQL)
    if spot_ids:
        wanted = {int(sid) for sid in spot_ids}
        targets = [row for row in targets if int(row["spot_id"]) in wanted]
    if limit:
        targets = targets[:limit]

    stats = FactPackageStats(spots_total=len(targets))
    packages: list[tuple[dict, dict]] = []
    for spot_row in targets:
        spot_id = int(spot_row["spot_id"])
        sentiment_rows = query_all(SENTIMENT_SQL, (spot_id,))
        aspect_rows = query_all(ASPECT_SQL, (spot_id,))
        # 代表性评论：正面 score>=4、负面 score<=2 各取一个候选池
        pool_positive = query_all(REP_REVIEWS_POSITIVE_SQL, (spot_id,))
        pool_negative = query_all(REP_REVIEWS_NEGATIVE_SQL, (spot_id,))
        pool_all = {int(r["comment_id"]): r for r in list(pool_positive) + list(pool_negative)}
        package = build_fact_package(
            spot_row, sentiment_rows, aspect_rows, list(pool_all.values()), version=version, stats=stats
        )
        packages.append((spot_row, package))

    result: dict[str, Any] = {"spots": len(packages), "version": version}

    with connection() as conn:
        task_id = register_task(
            conn,
            task_type="fact_package",
            task_name=f"景点事实包构造（C-BAT-06, version={version}）",
            total_count=len(packages),
        )
        recorder = TaskRecorder(conn, task_id)
        rows_to_write = []
        for spot_row, package in packages:
            if only_missing:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM spot_fact_package WHERE spot_id=%s AND version=%s",
                        (int(spot_row["spot_id"]), version),
                    )
                    if cur.fetchone():
                        stats.spots_skipped += 1
                        continue
            rows_to_write.append(
                (
                    int(spot_row["spot_id"]),
                    version,
                    json.dumps(package, ensure_ascii=False),
                    int(spot_row["review_count"] or 0),
                    str(spot_row["stat_version"] or "v1"),
                    task_id,
                )
            )
        for batch in chunked(rows_to_write, 50):
            with conn.cursor() as cur:
                cur.executemany(FACT_PACKAGE_UPSERT_SQL, batch)
        stats.spots_generated = len(rows_to_write)

        recorder.info(
            "事实包构造",
            f"生成 {stats.spots_generated} 个景点的事实包（version={version}），"
            f"方面纳入 {stats.aspects_included} / 排除 {stats.aspects_excluded}",
            detail=stats.as_detail(),
        )
        if stats.reviews_without_sentiment:
            recorder.warn(
                "事实包-情感样本",
                f"{stats.reviews_without_sentiment} 个景点的 DeepSeek 语义结果为空（情感占比为 null，不造数）",
            )
        finish_task(
            conn,
            task_id,
            status="success",
            success_count=stats.spots_generated,
            skip_count=stats.spots_skipped,
            started_at=started_at,
        )

    result.update(
        {
            "task_id": task_id,
            "generated": stats.spots_generated,
            "skipped": stats.spots_skipped,
            "aspects_included": stats.aspects_included,
            "aspects_excluded": stats.aspects_excluded,
            "spots_without_sentiment": stats.reviews_without_sentiment,
        }
    )
    return result


__all__ = ["run_fact_package", "build_fact_package", "FACT_PACKAGE_VERSION", "FactPackageStats"]
