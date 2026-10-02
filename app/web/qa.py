# -*- coding: utf-8 -*-
"""M5 智能问答（C4 组件 `C-API-11`，详细设计 §15.E）。

【这条链路最关键的设计事实】
    问答**不是聊天机器人**。整条链路里模型只在最后一步出现，前面三步全是本系统自己的规则与 SQL，
    并且设计内置了**三条"不调用模型"的分支**（§15.E.4 末尾的硬约束）：

        ① 问题分类为 OUT_OF_SCOPE      → 直接拒答，不调用模型（§15.E.1 的 D）
        ② 需要景点但没识别到实体        → 提示补充景点名，不调用模型（G）
        ③ 检索结果为空                  → 说明数据中没有相关信息，不调用模型（K）

    本文件把这条链路拆成四个可独立测试的部分：
        `classify_question`  规则分类 + 景点实体识别（**零成本**）
        `retrieve_facts`     按类型检索结构化事实（**零成本**，唯一数据出口）
        `build_context`      组装上下文（**零成本**，执行 §15.E.3 的 CC-3 规则）
        `answer_question`    编排上面三步，最后按 `APP_QA_LIVE` 决定是否调用模型

【上下文纪律（CC-3 / §15.E.3）】
    只传结构化事实（聚合值、占比、样本量）与**少量**代表性评论片段；
    **绝不传原始评论全集**；事实文字量 ≤3,000 字、条目数 ≤30；
    每段事实标注来源表名；客源地类问题必须附有效样本口径。

【与设计的差异（如实记录）】
    §15.E.2 的 DATA_METRIC 行写"直接检索 stat_overview"，
    但 `stat_overview` 在阶段三被明确"不重建"（设计输出清单未含该表，见 spark/README §11），
    因此这里改为**与其他接口一致的实时聚合**（`review` / `spot` / `stat_spot`），
    并在口径说明中写明来源，避免出现"读到一张空表"的假结论。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.web.data_access import (
    CALIBER_ASPECT,
    CALIBER_IP,
    CALIBER_THRESHOLD,
    CALIBER_TREND,
    fetch_all,
    fetch_one,
    scalar,
)

# ---------------------------------------------------------------------------
# 一、问题类型（§15.E.2，取值与 qa_record.question_type 一致）
# ---------------------------------------------------------------------------

QUESTION_TYPES = (
    "SPOT_EVALUATION",
    "SPOT_COMPARISON",
    "VISITOR_FOCUS",
    "SENTIMENT_EXPLAIN",
    "DATA_METRIC",
    "RANKING",
    "OUT_OF_SCOPE",
)

# 需要景点实体才能回答的类型
NEEDS_SPOT = {"SPOT_EVALUATION", "VISITOR_FOCUS", "SENTIMENT_EXPLAIN"}

# 超范围关键词（§15.E.2 的 OUT_OF_SCOPE 示例 + §8 的禁止生成范围）
OUT_OF_SCOPE_WORDS = (
    "路线", "行程", "攻略", "几天", "日游", "自驾", "包车", "租车",
    "天气", "气温", "下雨", "下雪",
    "门票预订", "订票", "买票", "预订", "订酒店", "酒店", "住宿推荐", "民宿",
    "机票", "火车票", "航班", "签证", "护照",
    "多少钱一晚", "预算", "推荐去", "帮我规划", "带我去",
)

# 各类型的判定关键词（按"越具体越先判"的顺序检查）
TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SPOT_COMPARISON", ("对比", "比较", "哪个好", "哪个更", "和", "与", "差别", "差异", "比起来")),
    ("RANKING", ("排行", "排名", "前十", "top", "TOP", "最多", "最高", "最火", "榜")),
    ("VISITOR_FOCUS", ("关注", "在意", "关心", "看重", "最常提", "提到最多", "吐槽", "喜欢什么")),
    ("SENTIMENT_EXPLAIN", ("为什么", "差评", "好评", "负面", "不满", "情感", "情绪", "口碑")),
    ("DATA_METRIC", ("多少", "几条", "条评论", "占比", "比例", "统计", "样本", "分布", "五星", "三星")),
    ("SPOT_EVALUATION", ("怎么样", "值得", "好不好", "如何", "评价", "体验", "推荐吗")),
)


@dataclass
class QaFacts:
    """检索到的结构化事实（送进上下文的原料）。"""

    question_type: str
    spots: list[dict[str, Any]] = field(default_factory=list)
    blocks: list[dict[str, Any]] = field(default_factory=list)   # [{source, title, items}]
    caliber_note: str = ""
    sample_size: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.blocks


# ---------------------------------------------------------------------------
# 二、问题分类与景点实体识别（零成本）
# ---------------------------------------------------------------------------


def classify_question(question: str) -> str:
    """规则分类（§15.E.1 的 B 步）。

    顺序很重要：**先判超范围**——"帮我订布达拉宫的门票"既含景点名又含"订"，
    若先做景点匹配会被误判成景点评价咨询。
    """
    text = (question or "").strip()
    if not text:
        return "OUT_OF_SCOPE"
    if any(word in text for word in OUT_OF_SCOPE_WORDS):
        return "OUT_OF_SCOPE"
    for qa_type, keywords in TYPE_KEYWORDS:
        if any(word in text for word in keywords):
            return qa_type
    return "OUT_OF_SCOPE"


def match_spots(question: str, limit: int = 5) -> list[dict[str, Any]]:
    """景点实体识别：用 `spot.spot_name` 做匹配，**含前缀模糊匹配**（§15.E.1 的 E 步）。

    为什么要前缀匹配：库里景点名常带通用后缀（如「纳木措**景区**」「巴松措**景区**」），
    而用户提问一般只说「纳木措」；只做整名包含会漏识别（实测踩到过）。

    规则与顺序：
        1. 优先整名出现在问题中；
        2. 否则取景点名的**前缀**（长度 ≥3）出现在问题中即视为命中；
        3. **长名优先**并记录已占用区间，避免「纳木措景区」被「纳木措」重复命中；
        4. 按在问题中出现的先后返回，符合阅读直觉。

    只读 `spot` 表；不调用模型。
    """
    text = (question or "").strip()
    if not text:
        return []

    rows = fetch_all(
        """
        SELECT spot_id, spot_name, review_count, has_full_evaluation
          FROM spot
         WHERE CHAR_LENGTH(spot_name) >= 2
         ORDER BY CHAR_LENGTH(spot_name) DESC
        """
    )

    # (匹配串, 行)：整名优先，其次前缀（前缀长度从长到短，取能命中的最长前缀）
    candidates: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        name = row["spot_name"]
        if name in text:
            candidates.append((name, row))
            continue
        for cut in range(len(name) - 1, 2, -1):     # 前缀长度 len-1 … 3
            prefix = name[:cut]
            if prefix in text:
                candidates.append((prefix, row))
                break

    # 长匹配串优先（避免短名吃掉长名），同长按出现位置
    candidates.sort(key=lambda item: (-len(item[0]), text.find(item[0])))

    matched: list[dict[str, Any]] = []
    used: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(not (end <= s or start >= e) for s, e in used)

    for token, row in candidates:
        start = text.find(token)
        if start < 0:
            continue
        end = start + len(token)
        if overlaps(start, end):
            continue
        used.append((start, end))
        if all(int(existing["spot_id"]) != int(row["spot_id"]) for existing in matched):
            matched.append(row)
        if len(matched) >= limit:
            break

    matched.sort(key=lambda r: min(
        (text.find(r["spot_name"]) if text.find(r["spot_name"]) >= 0 else text.find(r["spot_name"][:3])), 10 ** 6
    ))
    return matched


# ---------------------------------------------------------------------------
# 三、按类型检索结构化事实（零成本，唯一数据出口）
# ---------------------------------------------------------------------------


def retrieve_facts(question_type: str, question: str, spots: list[dict[str, Any]]) -> QaFacts:
    """按问题类型检索结构化事实（§15.E.1 的 H 步）。

    **只读数据库、不调用模型**；检索结果为空时上层会走"不调用模型"的分支。
    """
    if question_type == "OUT_OF_SCOPE":
        return QaFacts(question_type=question_type)

    if question_type == "SPOT_EVALUATION":
        return _facts_spot_evaluation(spots)
    if question_type == "SPOT_COMPARISON":
        return _facts_spot_comparison(spots)
    if question_type == "VISITOR_FOCUS":
        return _facts_visitor_focus(spots)
    if question_type == "SENTIMENT_EXPLAIN":
        return _facts_sentiment_explain(spots)
    if question_type == "DATA_METRIC":
        return _facts_data_metric(question)
    if question_type == "RANKING":
        return _facts_ranking(question)
    return QaFacts(question_type=question_type)


def _facts_spot_evaluation(spots: list[dict[str, Any]]) -> QaFacts:
    """SPOT_EVALUATION → `stat_spot` + `spot_report`（§15.E.2）。"""
    facts = QaFacts(question_type="SPOT_EVALUATION", spots=spots)
    if not spots:
        return facts
    for spot in spots[:2]:
        spot_id = int(spot["spot_id"])
        stat = fetch_one("SELECT * FROM stat_spot WHERE spot_id=%s", (spot_id,))
        report = fetch_one(
            "SELECT summary, advantages_json, issues_json, visitor_focus_json, "
            "fact_package_version, generated_at, need_review FROM spot_report WHERE spot_id=%s",
            (spot_id,),
        )
        items: list[dict[str, Any]] = []
        if stat:
            items.append({"指标": "评论量", "值": int(stat["review_count"] or 0)})
            items.append({"指标": "平均评分", "值": float(stat["avg_score"]) if stat["avg_score"] is not None else None})
            items.append({"指标": "好评率（4-5 星占比）", "值": float(stat["positive_rate"]) if stat["positive_rate"] is not None else None})
            items.append({"指标": "差评率（1-2 星占比）", "值": float(stat["negative_rate"]) if stat["negative_rate"] is not None else None})
        if report:
            items.append({"指标": "已生成的智能评价（离线）", "值": report["summary"]})
        facts.blocks.append(
            {
                "source": "stat_spot" + ("、spot_report" if report else ""),
                "title": f"{spot['spot_name']} 的统计与评价",
                "items": items,
            }
        )
        facts.sample_size += int((stat or {}).get("review_count") or 0)
    facts.caliber_note = CALIBER_THRESHOLD
    return facts


def _facts_spot_comparison(spots: list[dict[str, Any]]) -> QaFacts:
    """SPOT_COMPARISON → 双方 `stat_spot` + `aspect`（§15.E.2）。"""
    facts = QaFacts(question_type="SPOT_COMPARISON", spots=spots)
    if len(spots) < 2:
        return facts  # 不足两个景点 → 上层提示补充
    for spot in spots[:2]:
        spot_id = int(spot["spot_id"])
        stat = fetch_one("SELECT * FROM stat_spot WHERE spot_id=%s", (spot_id,))
        aspects = fetch_all(
            """
            SELECT aspect_name, COUNT(*) AS sample_size,
                   SUM(polarity='positive') AS positive_n, SUM(polarity='negative') AS negative_n
              FROM aspect WHERE spot_id=%s AND method='deepseek'
             GROUP BY aspect_name ORDER BY sample_size DESC LIMIT 8
            """,
            (spot_id,),
        )
        items: list[dict[str, Any]] = []
        if stat:
            items.append({"指标": "评论量", "值": int(stat["review_count"] or 0)})
            items.append({"指标": "平均评分", "值": float(stat["avg_score"]) if stat["avg_score"] is not None else None})
            items.append({"指标": "好评率", "值": float(stat["positive_rate"]) if stat["positive_rate"] is not None else None})
        for row in aspects:
            sample = int(row["sample_size"] or 0)
            items.append(
                {
                    "方面": row["aspect_name"],
                    "样本量": sample,
                    # BR-04：样本 <10 不出结论，只给样本量
                    "正面占比": round(int(row["positive_n"] or 0) / sample, 4) if sample >= 10 else None,
                    "负面占比": round(int(row["negative_n"] or 0) / sample, 4) if sample >= 10 else None,
                    "说明": None if sample >= 10 else "样本不足（<10 条），不出结论",
                }
            )
        facts.blocks.append({"source": "stat_spot、aspect", "title": f"{spot['spot_name']} 的指标与方面", "items": items})
        facts.sample_size += int((stat or {}).get("review_count") or 0)
    facts.caliber_note = CALIBER_THRESHOLD + " " + CALIBER_ASPECT
    return facts


def _facts_visitor_focus(spots: list[dict[str, Any]]) -> QaFacts:
    """VISITOR_FOCUS → `aspect` + `comment_semantic` 关键词（§15.E.2）。"""
    facts = QaFacts(question_type="VISITOR_FOCUS", spots=spots)
    spot_id = int(spots[0]["spot_id"]) if spots else None
    where = "WHERE method='deepseek'" + (" AND spot_id=%s" if spot_id else "")
    params = (spot_id,) if spot_id else ()
    aspects = fetch_all(
        f"""
        SELECT aspect_name, COUNT(*) AS sample_size,
               SUM(polarity='negative') AS negative_n
          FROM aspect {where}
         GROUP BY aspect_name ORDER BY sample_size DESC LIMIT 15
        """,
        params,
    )
    items = [
        {
            "方面": row["aspect_name"],
            "提及样本量": int(row["sample_size"] or 0),
            "负面占比": round(int(row["negative_n"] or 0) / int(row["sample_size"]), 4)
            if int(row["sample_size"] or 0) >= 10 else None,
        }
        for row in aspects
    ]
    if items:
        facts.blocks.append(
            {"source": "aspect", "title": ("该景点" if spot_id else "全库") + "游客提及最多的方面", "items": items}
        )
    # 关键词（来自 DeepSeek 语义结果的 comment_semantic.keywords）
    kw_where = "WHERE keywords IS NOT NULL AND keywords <> ''" + (" AND spot_id=%s" if spot_id else "")
    keywords = fetch_all(
        f"SELECT keywords FROM comment_semantic {kw_where} LIMIT 500", params
    )
    counter: dict[str, int] = {}
    for row in keywords:
        for word in str(row["keywords"]).split(","):
            word = word.strip()
            if word:
                counter[word] = counter.get(word, 0) + 1
    top_keywords = sorted(counter.items(), key=lambda kv: kv[1], reverse=True)[:15]
    if top_keywords:
        facts.blocks.append(
            {
                "source": "comment_semantic.keywords",
                "title": "语义分析提取的高频关键词",
                "items": [{"关键词": word, "出现条数": count} for word, count in top_keywords],
            }
        )
    facts.sample_size = sum(int(row["sample_size"] or 0) for row in aspects)
    facts.caliber_note = CALIBER_ASPECT + " 关键词来自 DeepSeek 离线语义分析结果。"
    return facts


def _facts_sentiment_explain(spots: list[dict[str, Any]]) -> QaFacts:
    """SENTIMENT_EXPLAIN → `sentiment` + `aspect`（§15.E.2）。"""
    facts = QaFacts(question_type="SENTIMENT_EXPLAIN", spots=spots)
    spot_id = int(spots[0]["spot_id"]) if spots else None

    sent_where = "WHERE method='deepseek'" + (" AND spot_id=%s" if spot_id else "")
    params = (spot_id,) if spot_id else ()
    sent = fetch_all(
        f"SELECT polarity, COUNT(*) AS n FROM sentiment {sent_where} GROUP BY polarity", params
    )
    total = sum(int(row["n"]) for row in sent)
    if sent:
        facts.blocks.append(
            {
                "source": "sentiment(method=deepseek)",
                "title": ("该景点" if spot_id else "全库") + "情感分布",
                "items": [
                    {"极性": row["polarity"], "条数": int(row["n"]),
                     "占比": round(int(row["n"]) / total, 4) if total else None}
                    for row in sent
                ],
            }
        )
    aspect_where = "WHERE method='deepseek'" + (" AND spot_id=%s" if spot_id else "")
    negative_aspects = fetch_all(
        f"""
        SELECT aspect_name, COUNT(*) AS sample_size, SUM(polarity='negative') AS negative_n
          FROM aspect {aspect_where}
         GROUP BY aspect_name HAVING sample_size >= 10
         ORDER BY (SUM(polarity='negative') / COUNT(*)) DESC LIMIT 10
        """,
        params,
    )
    if negative_aspects:
        facts.blocks.append(
            {
                "source": "aspect",
                "title": "负面倾向最明显的方面（样本 ≥10）",
                "items": [
                    {
                        "方面": row["aspect_name"],
                        "样本量": int(row["sample_size"]),
                        "负面占比": round(int(row["negative_n"] or 0) / int(row["sample_size"]), 4),
                    }
                    for row in negative_aspects
                ],
            }
        )
    facts.sample_size = total
    facts.caliber_note = CALIBER_ASPECT + " 情感为 deepseek 口径（与 mllib 基线不可混用）。"
    return facts


def _facts_data_metric(question: str) -> QaFacts:
    """DATA_METRIC → 数据集规模与分布（**实时聚合，不用空的 stat_overview**）。"""
    facts = QaFacts(question_type="DATA_METRIC")
    total = scalar("SELECT COUNT(*) FROM review")
    spots = scalar("SELECT COUNT(*) FROM spot")
    scored = scalar("SELECT COUNT(*) FROM review WHERE score IS NOT NULL")
    facts.blocks.append(
        {
            "source": "review、spot（实时聚合）",
            "title": "数据集规模",
            "items": [
                {"指标": "评论总数", "值": total},
                {"指标": "景点总数", "值": spots},
                {"指标": "有评分评论数", "值": scored},
                {"指标": "低信息量评论数（正文≤10 字）", "值": scalar("SELECT COUNT(*) FROM review WHERE is_low_info=1")},
                {"指标": "重复正文评论数", "值": scalar("SELECT COUNT(*) FROM review WHERE is_dup_content=1")},
            ],
        }
    )
    dist = fetch_all(
        "SELECT score, COUNT(*) AS n FROM review WHERE score IS NOT NULL GROUP BY score ORDER BY score DESC"
    )
    if dist:
        facts.blocks.append(
            {
                "source": "review.score",
                "title": "评分分布",
                "items": [
                    {"星级": f"{int(row['score'])} 星", "条数": int(row["n"]),
                     "占比": round(int(row["n"]) / scored, 4) if scored else None}
                    for row in dist
                ],
            }
        )
    ip_sample = fetch_one("SELECT MAX(sample_size) AS n FROM stat_ip WHERE scope_type='global'") or {}
    facts.blocks.append(
        {
            "source": "stat_ip（2022-08 后有效样本）",
            "title": "客源地有效样本",
            "items": [{"指标": "有效 IP 样本量", "值": int(ip_sample.get("n") or 0)}],
        }
    )
    facts.sample_size = total
    facts.caliber_note = CALIBER_IP + " " + CALIBER_TREND
    return facts


def _facts_ranking(question: str) -> QaFacts:
    """RANKING → `stat_spot` TopN（§15.E.2）。"""
    facts = QaFacts(question_type="RANKING")
    # 从问题里识别 TopN（"前十"→10，"Top5"→5），默认 10
    top_n = 10
    for token, value in (("前十", 10), ("前五", 5), ("前三", 3), ("前二十", 20)):
        if token in question:
            top_n = value
            break
    else:
        for i in range(3, 21):
            if f"top{i}" in question.lower() or f"前{i}" in question:
                top_n = i
                break
    min_reviews = 10  # 避免小样本景点占据排行榜（与 /api/spots/ranking 同一纪律）
    rows = fetch_all(
        """
        SELECT s.spot_name, st.review_count, st.avg_score, st.positive_rate
          FROM spot s JOIN stat_spot st ON st.spot_id = s.spot_id
         WHERE st.review_count >= %s
         ORDER BY st.review_count DESC LIMIT %s
        """,
        (min_reviews, top_n),
    )
    if rows:
        facts.blocks.append(
            {
                "source": "stat_spot",
                "title": f"评论量前 {top_n} 的景点",
                "items": [
                    {
                        "排名": index + 1,
                        "景点": row["spot_name"],
                        "评论量": int(row["review_count"] or 0),
                        "平均评分": float(row["avg_score"]) if row["avg_score"] is not None else None,
                    }
                    for index, row in enumerate(rows)
                ],
            }
        )
    facts.sample_size = sum(int(row["review_count"] or 0) for row in rows)
    facts.caliber_note = CALIBER_THRESHOLD + f" 排行仅纳入评论量 ≥{min_reviews} 条的景点，避免小样本失真。"
    return facts


# ---------------------------------------------------------------------------
# 四、上下文组装（§15.E.3 / CC-3）
# ---------------------------------------------------------------------------

CONTEXT_MAX_CHARS = 3000     # 单次上下文事实文字量上限
CONTEXT_MAX_ITEMS = 30       # 条目数上限


def build_context(facts: QaFacts) -> list[dict[str, Any]]:
    """把事实整理成"送进模型/展示给用户"的结构化上下文。

    **执行 §15.E.3**：只传结构化事实；总量 ≤3,000 字、条目 ≤30；
    每段标注来源表；口径单列一段。**绝不传原始评论全集。**
    """
    blocks: list[dict[str, Any]] = []
    used_chars = 0
    used_items = 0
    for block in facts.blocks:
        if used_items >= CONTEXT_MAX_ITEMS:
            break
        items = []
        for item in block["items"]:
            if used_items >= CONTEXT_MAX_ITEMS:
                break
            text = json.dumps(item, ensure_ascii=False)
            if used_chars + len(text) > CONTEXT_MAX_CHARS:
                blocks.append({"source": block["source"], "note": "（达到上下文长度上限，后续事实已截断）"})
                return blocks
            used_chars += len(text)
            used_items += 1
            items.append(item)
        blocks.append({"source": block["source"], "title": block["title"], "items": items})
    if facts.caliber_note:
        blocks.append({"source": "caliber", "title": "数据口径（必须随回答一并说明）", "items": [{"口径": facts.caliber_note}]})
    return blocks


# ---------------------------------------------------------------------------
# 五、回答生成（唯一可能调用模型的一步）
# ---------------------------------------------------------------------------

# 越界承诺词（§15.E.4 的"越界检测"）：回答里若出现，替换为能力边界说明
BOUNDARY_WORDS = ("路线", "行程", "门票预订", "订票", "天气", "酒店", "机票", "包车", "自驾")

BOUNDARY_REPLACEMENT = (
    "（本系统只基于旅游评论数据的分析结果作答，不提供路线规划、票务预订、天气等实时信息。）"
)

OUT_OF_SCOPE_ANSWER = (
    "本系统仅基于旅游评论数据分析结果回答问题，不支持路线规划、票务预订、酒店与天气等实时信息。"
    "可回答的问题包括：景点评价咨询、景点对比、游客关注点、情感与评价方面解释、数据指标查询与景点排行。"
)

QA_PROMPT_V1 = "p1"


def validate_answer(answer: str, facts: QaFacts) -> tuple[str, int, list[str]]:
    """回答校验（§15.E.4）：长度、越界词替换、数字一致性。

    :returns: `(处理后的回答, need_review(0/1), 处理说明列表)`
    """
    notes: list[str] = []
    text = (answer or "").strip()
    if len(text) > 600:
        text = text[:600]
        notes.append("回答超过 600 字，已截断")

    # 越界承诺词 → 替换为能力边界说明（不删整句，避免答非所问）
    hits = [word for word in BOUNDARY_WORDS if word in text]
    if hits:
        text = text + " " + BOUNDARY_REPLACEMENT
        notes.append("检出超范围表述：" + "、".join(hits) + "（已附能力边界说明）")

    # 数字一致性：回答中的数字必须能在事实里找到
    need_review = 0
    allowed = _collect_numbers(facts)
    if allowed:
        import re

        for token in re.findall(r"\d+(?:\.\d+)?", text):
            value = float(token)
            if value < 3:      # 1–2 这类结构性小数字（条数、星级）不参与判定
                continue
            if not any(abs(value - candidate) <= max(0.5, abs(candidate) * 0.01) for candidate in allowed):
                need_review = 1
                notes.append(f"回答中的数字 {token} 在提供的结构化事实中找不到对应值，已标记待复核")
                break
    return text, need_review, notes


def _collect_numbers(facts: QaFacts) -> list[float]:
    """收集事实中所有"允许被引用"的数字（用于数字一致性校验）。"""
    values: list[float] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            values.append(float(node))

    for block in facts.blocks:
        walk(block["items"])
    return values


def should_save_record(result: dict[str, Any]) -> bool:
    """判断本次问答是否应写入 `qa_record`（§15.E.1 / §15.E.4 的落库时机）。

    设计把"写入 qa_record"放在**生成回答之后**（流程图里的 P → Q），因此：

      · 超范围拒答（OUT_OF_SCOPE）——流程在分类后即返回（D 步），**不落库**；
      · 未识别到景点 / 需补充信息（SPOT_NOT_RECOGNIZED、NEED_TWO_SPOTS）——
        属"追问提示"，还没有任何分析结果可记，**不落库**；
      · 检索无数据（NO_DATA）——没有事实依据可记，**不落库**；
      · 模型已生成回答（成功或校验后标记）→ **落库**；
      · 已开启生成但因调用/配置失败（LLM_FAILED / LIVE_DISABLED / API_KEY_MISSING）——
        **落库**，因为这属于"用户提问过、系统处理过"，审计上应当留痕。

    这样既符合设计，也不会让 qa_record 里堆满"hi""你好"这类拒答噪音。
    """
    qa_type = result.get("question_type")
    reason = result.get("reason")
    if qa_type == "OUT_OF_SCOPE":
        return False
    if reason in {"SPOT_NOT_RECOGNIZED", "NEED_TWO_SPOTS", "NO_DATA", "EMPTY_QUESTION"}:
        return False
    return True


def answer_question(question: str) -> dict[str, Any]:
    """编排整条问答链路（§15.E.1）。

    **默认不调用模型**（`APP_QA_LIVE` 未开启）：走完分类 → 检索 → 组装三步，
    返回结构化事实 + 明确的不可用原因；用户仍能看到数据依据。
    开启后才在最后一步调用 DeepSeek 生成自然语言回答。
    """
    question = (question or "").strip()
    if not question:
        return _reply(question, "OUT_OF_SCOPE", [], None, "问题不能为空", available=False, reason="EMPTY_QUESTION")
    if len(question) > 200:  # §13.2 输入校验：问题长度上限
        question = question[:200]

    # ① 分类（零成本）
    qa_type = classify_question(question)

    # 分支 A：超范围 → 不调用模型（§15.E.1 的 D）
    if qa_type == "OUT_OF_SCOPE":
        return _reply(
            question, qa_type, [], OUT_OF_SCOPE_ANSWER,
            available=True, reason=None, reached_model=False,
            extra={"message_text": "该问题超出系统可回答范围，已直接拒答（未调用模型）。"},
        )

    # ② 景点实体识别（零成本）
    spots = match_spots(question)

    # 分支 B：需要景点但没识别到 → 不调用模型（§15.E.1 的 G）
    if qa_type in NEEDS_SPOT and not spots:
        return _reply(
            question, qa_type, [], None, available=False, reason="SPOT_NOT_RECOGNIZED",
            reached_model=False,
            extra={"message_text": "未能从问题中识别出景点名称，请补充具体景点（例如：布达拉宫怎么样）。"},
        )
    if qa_type == "SPOT_COMPARISON" and len(spots) < 2:
        return _reply(
            question, qa_type, [], None, available=False, reason="NEED_TWO_SPOTS",
            reached_model=False,
            extra={"message_text": "景点对比需要两个景点名称，请补充（例如：布达拉宫和纳木措哪个好）。"},
        )

    # ③ 检索结构化事实（零成本）
    facts = retrieve_facts(qa_type, question, spots)

    # 分支 C：检索不到事实 → 不调用模型（§15.E.1 的 K）
    if facts.is_empty:
        return _reply(
            question, qa_type, spots, None, available=False, reason="NO_DATA",
            reached_model=False, caliber=facts.caliber_note,
            extra={"message_text": "数据库中暂时没有与该问题相关的分析结果（不编造回答）。"},
        )

    context = build_context(facts)
    sources = sorted({block["source"] for block in context if block["source"] != "caliber"})

    # ④ 回答生成：唯一可能调用模型的一步
    from app.config import settings

    if not settings.web.qa_live:
        return _reply(
            question, qa_type, spots, None, available=False, reason="LIVE_DISABLED",
            reached_model=False, facts=facts, context=context, sources=sources,
            extra={"message_text": (
                "问答生成需要在线调用模型，当前已关闭（APP_QA_LIVE 未开启）。"
                "问题分类、景点识别与事实检索均已完成，以下为检索到的数据依据。"
            )},
        )
    if not settings.deepseek.is_configured:
        return _reply(
            question, qa_type, spots, None, available=False, reason="API_KEY_MISSING",
            reached_model=False, facts=facts, context=context, sources=sources,
            extra={"message_text": "未配置 DeepSeek API Key，无法生成回答；数据依据如下。"},
        )

    return _generate_answer(question, qa_type, spots, facts, context, sources)


def _generate_answer(
    question: str,
    qa_type: str,
    spots: list[dict[str, Any]],
    facts: QaFacts,
    context: list[dict[str, Any]],
    sources: list[str],
) -> dict[str, Any]:
    """调用 DeepSeek 生成回答（**仅在 `APP_QA_LIVE=1` 时执行**）。"""
    from app.llm.client import DeepSeekClient, LlmError
    from app.llm.prompts import build_qa_messages
    from app.llm.validators import ValidationError

    client = DeepSeekClient()
    messages = build_qa_messages(question, qa_type, context, facts.caliber_note)
    try:
        response = client.chat(messages, temperature=0.3, max_tokens=800)
    except LlmError as exc:
        return _reply(
            question, qa_type, spots, None, available=False, reason="LLM_FAILED",
            reached_model=True, facts=facts, context=context, sources=sources,
            extra={"message_text": f"模型调用失败（{exc.kind}）：{exc}", "error_code": 4001},
        )
    except ValidationError as exc:  # 兼容：prompts 层若抛校验异常
        return _reply(
            question, qa_type, spots, None, available=False, reason="LLM_BAD_FORMAT",
            reached_model=True, facts=facts, context=context, sources=sources,
            extra={"message_text": f"模型返回格式非法：{exc}", "error_code": 4002},
        )

    answer, need_review, notes = validate_answer(response.content, facts)
    return _reply(
        question, qa_type, spots, answer, available=True, reason=None,
        reached_model=True, facts=facts, context=context, sources=sources,
        extra={
            "need_review": need_review,
            "validation_notes": notes,
            "model": response.model,
            "prompt_version": QA_PROMPT_V1,
            "token_usage": int(response.usage.get("total_tokens") or 0),
        },
    )


def _reply(
    question: str,
    qa_type: str,
    spots: list[dict[str, Any]],
    answer: str | None,
    *,
    available: bool,
    reason: str | None,
    reached_model: bool = False,
    facts: QaFacts | None = None,
    context: list[dict[str, Any]] | None = None,
    sources: list[str] | None = None,
    caliber: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """统一回答结构（前端与 qa_record 都基于它）。"""
    payload: dict[str, Any] = {
        "question": question,
        "question_type": qa_type,
        "spots": [{"spot_id": int(s["spot_id"]), "spot_name": s["spot_name"]} for s in spots],
        "available": available,
        "reason": reason,
        "answer": answer,
        # 关键：明确告知本次是否真的调用了模型（成本与可解释性都靠它）
        "reached_model": reached_model,
        "facts": context or [],
        "sources": sources or [],
        "caliber_note": caliber or (facts.caliber_note if facts else ""),
        "sample_size": facts.sample_size if facts else 0,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S") if available else None,
    }
    if extra:
        payload.update(extra)
    return payload


__all__ = [
    "QUESTION_TYPES",
    "NEEDS_SPOT",
    "QaFacts",
    "classify_question",
    "match_spots",
    "retrieve_facts",
    "build_context",
    "validate_answer",
    "answer_question",
    "should_save_record",
    "OUT_OF_SCOPE_ANSWER",
]
