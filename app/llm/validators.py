# -*- coding: utf-8 -*-
""": 模型输出校验（设计 §15.A.4 七条校验 + §15.C.3 评价校验）。

**为什么必须有这一层**：模型返回的文本本身是不可信的输入。任何字段在写库前
都必须经过"枚举合法 / 数值在区间 / 长度可容纳 / 证据可回溯"四道检查，
否则就会出现"编造依据"或"字段超长导致写库中断"两类问题。

本文件属于 C-BAT-05（评论语义）与 C-BAT-07（景点评价）的公共校验层，
只做校验与规范化，**不访问数据库、不调用模型**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.llm.prompts import ASPECT_CANDIDATES, POLARITIES

# ---------------------------------------------------------------------------
# 常量（全部对应冻结的表结构长度，见 docs/database/建表脚本.sql）
# ---------------------------------------------------------------------------

INTENSITY_MIN, INTENSITY_MAX = 1, 5
EVIDENCE_MAX_CHARS = 30          # 设计 §15.A.2：evidence 不超过 30 字
SUMMARY_MAX_CHARS = 40           # comment_semantic.summary VARCHAR(128)，口径 ≤40 字
KEYWORD_MAX_CHARS = 10
KEYWORD_MAX_COUNT = 5            # comment_semantic.keywords VARCHAR(255)，逗号分隔
ASPECT_EVIDENCE_DB_MAX = 255     # aspect.evidence VARCHAR(255)
ASPECT_NAME_DB_MAX = 32          # aspect.aspect_name VARCHAR(32)

REPORT_SUMMARY_MIN, REPORT_SUMMARY_MAX = 120, 260
REPORT_ITEM_MIN, REPORT_ITEM_MAX = 2, 6
REPORT_ITEM_MAX_CHARS = 40
# 数字一致性阈值：设计 §15.C.3「误差超过 0.5 个百分点即标记 need_review」
NUMBER_TOLERANCE_PP = 0.5


# ---------------------------------------------------------------------------
# 一、通用小工具
# ---------------------------------------------------------------------------


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _as_int(value: Any) -> int | None:
    """尽力把模型给的值转成整数（容忍 "4"、"4分"、4.0 这类写法）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        match = re.search(r"-?\d+", value)
        if match:
            return int(match.group())
    return None


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text[:limit]


class ValidationError(ValueError):
    """校验失败（不可修复），调用方据此决定是否重试/记失败。"""


# ---------------------------------------------------------------------------
# 二、C-BAT-05：评论语义结果
# ---------------------------------------------------------------------------


@dataclass
class SemanticResult:
    """一条评论校验后的语义结果（对应三张结果表的写入内容）。"""

    polarity: str                                   # sentiment.polarity
    intensity: int | None                           # sentiment.intensity
    is_valid: int                                   # sentiment.is_valid（非法极性被判 neutral 时记 0）
    aspects: list[dict[str, str]] = field(default_factory=list)   # aspect 表
    keywords: str | None = None                     # comment_semantic.keywords（逗号分隔）
    summary: str | None = None                      # comment_semantic.summary
    dropped_aspects: int = 0                        # 被丢弃的方面数（质量统计）
    repairs: list[str] = field(default_factory=list)  # 做过的规范化动作（如实记录）

    def as_raw_json(self) -> dict[str, Any]:
        """写入 `sentiment.raw_json` 的复核信息（含校验过程的痕迹）。"""
        return {
            "polarity": self.polarity,
            "intensity": self.intensity,
            "is_valid": self.is_valid,
            "aspects": self.aspects,
            "keywords": self.keywords,
            "summary": self.summary,
            "dropped_aspects": self.dropped_aspects,
            "repairs": self.repairs,
        }


def _evidence_is_substring(evidence: str, content: str) -> bool:
    """校验 evidence 是否为评论原文的连续子串（§15.A.4 第 5 条，防编造的核心）。"""
    if not evidence:
        return False
    if evidence in content:
        return True
    # 容忍模型把省略号/空白写法弄丢：去掉空白后再比一次
    squeeze = lambda s: re.sub(r"\s+", "", s)
    return squeeze(evidence) in squeeze(content)


def validate_semantic(raw_text: str, content: str, *, max_aspects: int = 5) -> SemanticResult:
    """校验并规范化一次语义分析返回。

    :param raw_text: 模型返回的原始文本（允许带 ``` 围栏，由调用方先做 `extract_json_text`）
    :param content:  评论正文（用于 evidence 子串校验）
    :raises ValidationError: JSON 结构完全不可用（调用方重试/记失败）
    """
    import json

    try:
        data = json.loads(raw_text)
    except ValueError as exc:
        raise ValidationError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("顶层结构不是 JSON 对象")

    repairs: list[str] = []

    # ① polarity：枚举不合法 → 判 neutral 并标记 is_valid=0（§15.A.4 第 2 条）
    polarity = str(data.get("polarity") or "").strip().lower()
    is_valid = 1
    if polarity not in POLARITIES:
        repairs.append(f"polarity 非法({polarity or '空'}) → neutral, is_valid=0")
        polarity = "neutral"
        is_valid = 0

    # ② intensity：钳制到 1–5；无法解析则置空（列可空）
    intensity_raw = _as_int(data.get("intensity"))
    intensity: int | None = None
    if intensity_raw is not None:
        intensity = _clamp(intensity_raw, INTENSITY_MIN, INTENSITY_MAX)
        if intensity != intensity_raw:
            repairs.append(f"intensity 钳制 {intensity_raw} → {intensity}")

    # ③ aspects：方面名须在候选列表；**evidence 必须是原文子串**，否则丢弃该项
    aspects: list[dict[str, str]] = []
    dropped = 0
    seen: set[str] = set()
    raw_aspects = data.get("aspects")
    if isinstance(raw_aspects, list):
        for item in raw_aspects:
            if not isinstance(item, dict):
                dropped += 1
                continue
            name = str(item.get("aspect") or "").strip()
            if name not in ASPECT_CANDIDATES or name in seen or len(aspects) >= max_aspects:
                dropped += 1
                continue
            aspect_polarity = str(item.get("polarity") or "").strip().lower()
            if aspect_polarity not in POLARITIES:
                dropped += 1
                continue
            evidence = _truncate(str(item.get("evidence") or ""), EVIDENCE_MAX_CHARS)
            if not _evidence_is_substring(evidence, content):
                # 关键校验：证据无法在原文中定位 → 视为编造，弃用该方面
                dropped += 1
                continue
            aspects.append(
                {
                    "aspect": name[:ASPECT_NAME_DB_MAX],
                    "polarity": aspect_polarity,
                    "evidence": evidence[:ASPECT_EVIDENCE_DB_MAX],
                }
            )
            seen.add(name)
    elif raw_aspects is not None:
        dropped += 1
        repairs.append("aspects 不是数组，已按空数组处理")

    # ④ keywords：数量 ≤5、每项 ≤10 字
    keywords: list[str] = []
    raw_keywords = data.get("keywords")
    if isinstance(raw_keywords, list):
        for item in raw_keywords:
            word = _truncate(str(item or ""), KEYWORD_MAX_CHARS)
            if word and word not in keywords and len(keywords) < KEYWORD_MAX_COUNT:
                keywords.append(word)
    keywords_text = ",".join(keywords) if keywords else None

    # ⑤ summary：≤40 字
    summary = _truncate(str(data.get("summary") or ""), SUMMARY_MAX_CHARS) or None

    return SemanticResult(
        polarity=polarity,
        intensity=intensity,
        is_valid=is_valid,
        aspects=aspects,
        keywords=keywords_text,
        summary=summary,
        dropped_aspects=dropped,
        repairs=repairs,
    )


# ---------------------------------------------------------------------------
# 三、C-BAT-05：≤10 字短评的规则判定（BR-05 / CC-4，不调用模型）
# ---------------------------------------------------------------------------

# 规则词典：只收录"显式情感词"，命中才算极性；未命中一律 neutral（口径已与本人确认）
POSITIVE_WORDS: tuple[str, ...] = (
    "值得", "推荐", "很美", "漂亮", "震撼", "壮观", "满意", "喜欢", "不错", "好玩", "舒服", "惊艳", "出片",
)
NEGATIVE_WORDS: tuple[str, ...] = (
    "失望", "坑", "贵", "差", "脏", "乱", "挤", "难受", "后悔", "不值", "骗", "垃圾", "高反", "缺氧", "排队",
)


def rule_based_semantic(content: str) -> SemanticResult:
    """≤10 字短评的规则判定（不调用模型，成本控制措施 CC-4）。

    口径（已与本人确认）：默认 `neutral`；仅当命中显式正/负词才给出极性。
    短评无法可靠抽取方面，因此 `aspects` 为空数组，只写 `comment_semantic`（source='rule'）。
    """
    text = (content or "").strip()
    polarity = "neutral"
    intensity: int | None = None
    repairs = ["规则判定（≤10 字，不调用模型）"]

    has_positive = any(word in text for word in POSITIVE_WORDS)
    has_negative = any(word in text for word in NEGATIVE_WORDS)
    if has_positive and not has_negative:
        polarity, intensity = "positive", 3
    elif has_negative and not has_positive:
        polarity, intensity = "negative", 3
    elif has_positive and has_negative:
        # 正负词同时出现：短评不足以判断主导倾向，保守记 neutral
        polarity, intensity = "neutral", None
        repairs.append("正负词同时命中 → neutral")

    keywords = [w for w in (POSITIVE_WORDS + NEGATIVE_WORDS) if w in text][:KEYWORD_MAX_COUNT]
    return SemanticResult(
        polarity=polarity,
        intensity=intensity,
        is_valid=1,
        aspects=[],
        keywords=",".join(keywords) if keywords else None,
        summary=_truncate(text, SUMMARY_MAX_CHARS) or None,
        dropped_aspects=0,
        repairs=repairs,
    )


# ---------------------------------------------------------------------------
# 四、C-BAT-07：景点评价结果与数字一致性校验
# ---------------------------------------------------------------------------


@dataclass
class ReportResult:
    """一份景点评价的校验结果（对应 spot_report 的写入内容）。"""

    summary: str
    advantages: list[str]
    issues: list[str]
    visitor_focus: list[str]
    need_review: int = 0
    number_mismatches: list[str] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)


def validate_report(raw_text: str) -> ReportResult:
    """校验四段结构与条目数量（§15.C.3）；数字一致性由 `check_number_consistency` 单独完成。"""
    import json

    try:
        data = json.loads(raw_text)
    except ValueError as exc:
        raise ValidationError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("顶层结构不是 JSON 对象")

    repairs: list[str] = []
    summary = str(data.get("summary") or "").strip()
    if not summary:
        raise ValidationError("summary 缺失（四段字段必须齐全）")
    if len(summary) > REPORT_SUMMARY_MAX:
        repairs.append(f"summary 超长截断 {len(summary)} → {REPORT_SUMMARY_MAX} 字")
        summary = summary[:REPORT_SUMMARY_MAX]

    def pick_items(key: str) -> list[str]:
        raw_items = data.get(key)
        items: list[str] = []
        if isinstance(raw_items, list):
            for item in raw_items:
                text = _truncate(str(item or ""), REPORT_ITEM_MAX_CHARS)
                if text:
                    items.append(text)
        if len(items) > REPORT_ITEM_MAX:
            repairs.append(f"{key} 超量截断 {len(items)} → {REPORT_ITEM_MAX} 条")
            items = items[:REPORT_ITEM_MAX]
        return items

    advantages = pick_items("advantages")
    issues = pick_items("issues")
    visitor_focus = pick_items("visitor_focus")
    for name, items in (("advantages", advantages), ("issues", issues), ("visitor_focus", visitor_focus)):
        if len(items) < REPORT_ITEM_MIN:
            # 少量保留，不编造（§15.C.3：条目不足不虚构）
            repairs.append(f"{name} 仅 {len(items)} 条（少于 {REPORT_ITEM_MIN} 条，按实际保留）")

    return ReportResult(
        summary=summary,
        advantages=advantages,
        issues=issues,
        visitor_focus=visitor_focus,
        repairs=repairs,
    )


# 百分比写法：[数字]%／[数字] 个百分点／[数字] 成
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|％|个百分点)")
# 纯数字（用于比对评论量、均分等）
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def check_number_consistency(result: ReportResult, package: dict[str, Any]) -> list[str]:
    """数字一致性校验（§15.C.3，本设计的强制关卡）。

    思路：把事实包中所有"可被引用的数字"整理成一张清单（分百分比与普通数值两组），
    再从句子里抽出百分比与数字，逐个判断能否在清单中找到允许范围内的对应值。
    找不到对应值的数字**不一定错**（可能是"2 条优势"这类结构性数字），
    因此只对**百分比**与**事实包存在的同类量级**做判定，避免误伤。

    返回不符合项描述列表（非空 → `spot_report.need_review = 1`）。
    """
    mismatches: list[str] = []
    percent_values, plain_values = _collect_package_numbers(package)

    text = " ".join([result.summary, *result.advantages, *result.issues, *result.visitor_focus])

    # ① 百分比：误差 > 0.5 个百分点即视为与事实不符
    for match in _PERCENT_RE.finditer(text):
        value = float(match.group(1))
        if not percent_values:
            continue
        if not any(abs(value - candidate) <= NUMBER_TOLERANCE_PP for candidate in percent_values):
            mismatches.append(f"百分比 {value}% 与事实包不符（允许误差 {NUMBER_TOLERANCE_PP} 个百分点）")

    # ② 普通数字：只检查"看起来在引用事实"的量级（评论量、点赞数、均分）
    for match in _NUMBER_RE.finditer(text):
        token = match.group()
        # 跳过被百分比匹配走的数字
        start, end = match.span()
        if end < len(text) and text[end] in "%％":
            continue
        value = float(token)
        if value < 3:  # 1–2 这类结构性小数字（星级、条目数）不参与判定
            continue
        if not plain_values:
            continue
        if not _close_to_any(value, plain_values):
            # 与任何事实数字都对不上：记录但不必然判错（可能是"3 个方面"等表达）
            mismatches.append(f"数字 {token} 在事实包中找不到对应值（请人工确认是否为模型自造）")

    return mismatches


def _close_to_any(value: float, candidates: list[float]) -> bool:
    """数值是否与候选清单中的某一项接近（整数比等值；小数按 1% 相对误差）。"""
    for candidate in candidates:
        if abs(value - candidate) <= max(0.5, abs(candidate) * 0.01):
            return True
    return False


def _collect_package_numbers(package: dict[str, Any]) -> tuple[list[float], list[float]]:
    """从事实包中收集"允许被引用的数字"。

    返回 `(百分比清单, 普通数值清单)`：
        · 百分比：事实包中的各类占比（**换算为 0–100**），允许 ±0.5pp 误差；
        · 普通数值：评论量、点赞数、均分、样本量。
    """
    percent_values: list[float] = []
    plain_values: list[float] = []

    stats = package.get("statistics") or {}
    for key in ("review_count", "avg_score", "total_likes", "image_rate", "positive_rate", "negative_rate"):
        value = stats.get(key)
        if value is None:
            continue
        number = float(value)
        if key.endswith("_rate"):
            percent_values.append(round(number * 100, 2))
        else:
            plain_values.append(number)

    sentiment = package.get("sentiment") or {}
    for key in ("positive", "neutral", "negative"):
        value = sentiment.get(key)
        if value is not None:
            percent_values.append(round(float(value) * 100, 2))
    if sentiment.get("sample_size") is not None:
        plain_values.append(float(sentiment["sample_size"]))

    for aspect in package.get("aspects") or []:
        for key in ("positive_rate", "negative_rate"):
            value = aspect.get(key)
            if value is not None:
                percent_values.append(round(float(value) * 100, 2))
        if aspect.get("sample_size") is not None:
            plain_values.append(float(aspect["sample_size"]))

    return percent_values, plain_values


__all__ = [
    "ValidationError",
    "SemanticResult",
    "ReportResult",
    "validate_semantic",
    "rule_based_semantic",
    "validate_report",
    "check_number_consistency",
    "INTENSITY_MIN",
    "INTENSITY_MAX",
    "NUMBER_TOLERANCE_PP",
]
