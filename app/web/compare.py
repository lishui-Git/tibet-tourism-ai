# -*- coding: utf-8 -*-
"""M4 景点对比（C4 组件 `C-API-10`，详细设计 §15.D）。

【设计要点（§15.D.3）】
    · **对比由系统计算**：差值、比率、样本量比全部由后端算出，模型只做解释；
    · **禁止判定"哪个更好"**：Prompt 约束 + 校验层违禁词检测双重把关；
    · 样本量相差 ≥10 倍时标记 `reliability_warning`；
    · 任一方某方面样本 <10 条 → 该方面只给样本量、不出结论（BR-04）；
    · **失败降级**：解读失败时指标对比与方面对比照常返回（数据不依赖模型）。

【本文件的两条重要边界】
    1. 对比指标**不落库**：设计已删除 `spot_comparison` 表，指标实时计算（见 §15.D.3）；
    2. 解读是否调用模型由 `APP_COMPARE_LIVE` 控制，**默认关闭**。
       关闭时返回完整的指标与方面对比，并把 `interpretation.available` 置 false 且
       给出原因——这样"零 API 消费"是配置决定的，而不是靠人记得不要点。

【口径】统计与情感均按 BR-01/BR-04/BR-10 标注；情感占比取 `method=deepseek`，
        没有 deepseek 结果时如实返回 null（不退回 mllib 冒充同一口径）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.web.data_access import (
    CALIBER_ASPECT,
    CALIBER_SENTIMENT,
    CALIBER_THRESHOLD,
    ParamError,
    fetch_all,
    fetch_one,
)
from app.web.response import CODE_LLM_FAILED

# 样本量相差多少倍算"悬殊"（设计 §15.D.3：≥10 倍）
RELIABILITY_RATIO_THRESHOLD = 10
# 方面对比最多展示多少个方面
ASPECT_LIMIT = 12
# 解读场景参数（与 §7.2 一致：解读类 900 tokens、temperature 0.3）
COMPARE_MAX_TOKENS = 900
COMPARE_TEMPERATURE = 0.3


def compare_spots(spot_a: int, spot_b: int) -> dict[str, Any] | None:
    """计算出两个景点的对比结果。

    :returns: 对比结果字典；任一方景点不存在时返回 None（由路由层转 3001）
    :raises ParamError: 两个景点相同（1002）
    """
    if spot_a == spot_b:
        raise ParamError("不能对比同一个景点，请选择两个不同的景点", code=1002)

    a = _spot_snapshot(spot_a)
    if a is None:
        return None
    b = _spot_snapshot(spot_b)
    if b is None:
        return None

    diff = _diff(a, b)
    aspects = _aspect_compare(spot_a, spot_b)
    facts = {
        "spot_a": a,
        "spot_b": b,
        "diff": diff,
        "aspects": aspects,
        "caliber": {"aspect_min_sample": 10, "ip_valid_since": "2022-08-01"},
    }

    result: dict[str, Any] = {
        "facts": facts,
        "indicator_compare": _indicator_rows(a, b),
        "interpretation": _interpret(facts),
        "caliber_note": (
            CALIBER_THRESHOLD + " " + CALIBER_ASPECT + " " + CALIBER_SENTIMENT
            + " 对比指标由后端实时计算、不落库；样本量相差 ≥10 倍时给出可靠性提示。"
        ),
        "sample_size": {"spot_a": a["review_count"], "spot_b": b["review_count"]},
    }
    return result


def _spot_snapshot(spot_id: int) -> dict[str, Any] | None:
    """取单个景点的对比所需指标（不存在返回 None）。"""
    row = fetch_one(
        """
        SELECT s.spot_id, s.spot_name, s.source_scope, s.review_count, s.has_full_evaluation,
               st.avg_score, st.positive_rate, st.negative_rate, st.image_rate, st.total_likes
          FROM spot s LEFT JOIN stat_spot st ON st.spot_id = s.spot_id
         WHERE s.spot_id = %s
        """,
        (spot_id,),
    )
    if not row:
        return None

    counts = fetch_all(
        "SELECT polarity, COUNT(*) AS n FROM sentiment "
        "WHERE spot_id=%s AND method='deepseek' GROUP BY polarity",
        (spot_id,),
    )
    by_polarity = {r["polarity"]: int(r["n"]) for r in counts}
    total = sum(by_polarity.values())
    row["sentiment"] = {
        "method": "deepseek",
        "sample_size": total,
        "positive": round(by_polarity.get("positive", 0) / total, 4) if total else None,
        "neutral": round(by_polarity.get("neutral", 0) / total, 4) if total else None,
        "negative": round(by_polarity.get("negative", 0) / total, 4) if total else None,
    }
    row["id"] = row.pop("spot_id")
    return row


def _diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """系统计算差值、比率与可靠性告警（模型不参与）。"""

    def delta(key: str, digits: int = 4) -> float | None:
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None:
            return None
        return round(float(va) - float(vb), digits)

    count_a, count_b = int(a["review_count"] or 0), int(b["review_count"] or 0)
    ratio = None
    warning = None
    if count_a and count_b:
        ratio = round(max(count_a, count_b) / min(count_a, count_b), 2)
        if ratio >= RELIABILITY_RATIO_THRESHOLD:
            larger = a["spot_name"] if count_a > count_b else b["spot_name"]
            smaller = b["spot_name"] if count_a > count_b else a["spot_name"]
            warning = (
                f"两者评论量相差 {ratio} 倍（{larger} 明显多于 {smaller}），"
                "样本量差异会影响结论可靠性，请谨慎解读。"
            )

    return {
        "review_count_delta": count_a - count_b,
        "avg_score_delta": delta("avg_score", 2),
        "positive_rate_delta": delta("positive_rate"),
        "negative_rate_delta": delta("negative_rate"),
        "sample_ratio": ratio,
        "reliability_warning": warning,
    }


def _aspect_compare(spot_a: int, spot_b: int) -> list[dict[str, Any]]:
    """方面级对比：按两景点出现的全部方面合并（BR-04 在每一侧独立判定）。"""
    rows = fetch_all(
        """
        SELECT spot_id, aspect_name,
               COUNT(*) AS sample_size,
               SUM(polarity='positive') AS positive_n,
               SUM(polarity='negative') AS negative_n
          FROM aspect
         WHERE spot_id IN (%s, %s) AND method='deepseek'
         GROUP BY spot_id, aspect_name
        """,
        (spot_a, spot_b),
    )
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        side = "a" if int(row["spot_id"]) == spot_a else "b"
        sample = int(row["sample_size"] or 0)
        conclusive = sample >= 10
        merged.setdefault(row["aspect_name"], {"aspect": row["aspect_name"]})[side] = {
            "sample_size": sample,
            "conclusive": conclusive,
            "positive_rate": round(int(row["positive_n"] or 0) / sample, 4) if conclusive else None,
            "negative_rate": round(int(row["negative_n"] or 0) / sample, 4) if conclusive else None,
            "note": None if conclusive else "样本不足（<10 条），不出结论",
        }

    items = []
    for item in merged.values():
        item.setdefault("a", None)
        item.setdefault("b", None)
        items.append(item)
    # 样本量大的方面排前面，便于阅读
    items.sort(
        key=lambda i: max((i["a"] or {}).get("sample_size") or 0, (i["b"] or {}).get("sample_size") or 0),
        reverse=True,
    )
    return items[:ASPECT_LIMIT]


def _indicator_rows(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    """指标对比表（前端可直接渲染；每行含两侧值与差值）。"""
    rows = [
        ("评论量", "review_count", 0),
        ("平均评分", "avg_score", 2),
        ("好评率", "positive_rate", 4),
        ("差评率", "negative_rate", 4),
        ("图文率", "image_rate", 4),
        ("点赞总数", "total_likes", 0),
    ]
    out = []
    for label, key, digits in rows:
        va, vb = a.get(key), b.get(key)
        delta = None
        if va is not None and vb is not None:
            delta = round(float(va) - float(vb), digits)
        out.append({"label": label, "key": key, "spot_a": va, "spot_b": vb, "delta": delta})
    return out


def _interpret(facts: dict[str, Any]) -> dict[str, Any]:
    """生成对比解读。

    **默认不调用模型**（`APP_COMPARE_LIVE` 未开启时）：返回 `available=false` 与原因，
    指标与方面对比照常返回——这是设计 §15.D.3「失败降级」的同一条路径。

    开启后：调用 DeepSeek → `validate_compare` 校验（含违禁判定词检测）→ 返回解读；
    失败时同样降级为 `available=false`，不影响数据部分。
    """
    from app.config import settings

    # 开关在 WebSettings 下（属"Web 层是否允许在线生成解读"的部署决策）
    live = bool(settings.web.compare_live)
    if not live:
        return {
            "available": False,
            "reason": "LIVE_DISABLED",
            "message_text": (
                "对比解读为在线模型生成，当前已关闭（APP_COMPARE_LIVE 未开启）。"
                "指标对比与方面对比为系统计算结果，可正常使用。"
            ),
            "differences": [],
            "possible_reasons": [],
            "reliability_note": facts["diff"]["reliability_warning"] or "",
            "generated_at": None,
        }
    if not settings.deepseek.is_configured:
        return {
            "available": False,
            "reason": "API_KEY_MISSING",
            "message_text": "未配置 DeepSeek API Key，无法生成对比解读；指标对比不受影响。",
            "differences": [],
            "possible_reasons": [],
            "reliability_note": facts["diff"]["reliability_warning"] or "",
            "generated_at": None,
        }

    # ---- 在线路径：仅在显式开启时执行 ----
    from app.llm.client import DeepSeekClient, LlmError
    from app.llm.prompts import COMPARE_PROMPT_VERSION, build_compare_messages, extract_json_text
    from app.llm.validators import ValidationError, validate_compare

    client = DeepSeekClient()
    messages = build_compare_messages(facts)
    try:
        response = client.chat(messages, temperature=COMPARE_TEMPERATURE, max_tokens=COMPARE_MAX_TOKENS)
    except LlmError as exc:
        return _degraded(facts, f"解读调用失败（{exc.kind}）：{exc}", CODE_LLM_FAILED)

    try:
        parsed = validate_compare(extract_json_text(response.content))
    except ValidationError as exc:
        # 设计 §15.D.1：校验不过重试一次；仍不过则标记待复核
        try:
            retry = client.chat(
                messages + [
                    {"role": "assistant", "content": response.content[:600]},
                    {"role": "user", "content": f"上一次输出未通过校验：{exc}\n请严格按 Schema 重新输出 JSON。"},
                ],
                temperature=COMPARE_TEMPERATURE,
                max_tokens=COMPARE_MAX_TOKENS,
            )
            parsed = validate_compare(extract_json_text(retry.content))
            response = retry
        except (LlmError, ValidationError) as exc2:
            return _degraded(facts, f"解读校验失败：{exc2}", CODE_LLM_FAILED)

    return {
        "available": True,
        "differences": parsed.differences,
        "possible_reasons": parsed.possible_reasons,
        "reliability_note": parsed.reliability_note or (facts["diff"]["reliability_warning"] or ""),
        "need_review": parsed.has_verdict,
        "need_review_note": (
            "解读中出现疑似『判定优劣』的表述，已标记待复核。" if parsed.has_verdict else None
        ),
        "model": response.model,
        "prompt_version": COMPARE_PROMPT_VERSION,
        "token_usage": int(response.usage.get("total_tokens") or 0),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "repairs": parsed.repairs,
    }


def _degraded(facts: dict[str, Any], message: str, code: int) -> dict[str, Any]:
    """解读失败的统一下降级结构（数据部分不受影响，§15.D.3）。"""
    return {
        "available": False,
        "reason": "LLM_FAILED",
        "message_text": message,
        "error_code": code,
        "differences": [],
        "possible_reasons": [],
        "reliability_note": facts["diff"]["reliability_warning"] or "",
        "generated_at": None,
    }


__all__ = ["compare_spots", "RELIABILITY_RATIO_THRESHOLD"]
