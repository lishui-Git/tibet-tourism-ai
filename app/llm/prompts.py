# -*- coding: utf-8 -*-
"""Prompt 模板集中管理（阶段四要求：Prompt 不散落在业务代码里，且带版本号可追溯）。

设计依据：
    · 详细设计说明书 §7.3 Prompt 设计原则（所有场景必须包含「硬性约束」段，BR-07 的实现手段）
    · §15.A.2 评论语义分析 Prompt 与输出 Schema
    · §15.C.2 景点智能评价 Prompt 与输出 Schema

版本号约定：
    `SEMANTIC_PROMPT_VERSION`  → 写入 `sentiment.raw_json.prompt_version`（复核"当时用的是哪版 Prompt"）
    `REPORT_PROMPT_VERSION`    → 写入 `spot_report.prompt_version`
    模板内容有任何改动都必须**递增版本号**，否则历史结果无法解释。

注意：本文件只有模板文本，不含任何密钥；模型与温度由调用方按场景传入。
"""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# 零、版本号与候选枚举
# ---------------------------------------------------------------------------

# Prompt 模板版本（改动模板必须同步递增）
SEMANTIC_PROMPT_VERSION = "p1"
REPORT_PROMPT_VERSION = "p1"

# 候选方面列表（设计 §15.A.2 固定 10 类；`aspect.aspect_name` 为 VARCHAR(32)，长度安全）
ASPECT_CANDIDATES: tuple[str, ...] = (
    "风景",
    "交通",
    "门票",
    "服务",
    "设施",
    "住宿餐饮",
    "高原反应",
    "人流拥挤",
    "性价比",
    "其他",
)

POLARITIES: tuple[str, ...] = ("positive", "neutral", "negative")


# ---------------------------------------------------------------------------
# 一、C-BAT-05 评论语义分析 Prompt
# ---------------------------------------------------------------------------

SEMANTIC_SYSTEM_PROMPT = """你是一个旅游评论语义分析器。只输出 JSON，不要输出任何其他文字。
【硬性约束】
1. 只能依据给定评论文本作答，不得使用你的自身知识补充。
2. 不得编造数字。
3. aspects 只能从候选方面列表中选取：风景 / 交通 / 门票 / 服务 / 设施 / 住宿餐饮 / 高原反应 / 人流拥挤 / 性价比 / 其他
4. 无法归入任何方面时，aspects 返回空数组。
5. evidence 必须是评论文本中**原样出现**的片段（连续子串，不超过 30 字），不得改写、不得拼接。
6. summary 不超过 40 字；keywords 不超过 5 个、每个不超过 10 字。"""

SEMANTIC_OUTPUT_SCHEMA: dict[str, Any] = {
    "polarity": "positive | neutral | negative",
    "intensity": "1-5 的整数",
    "aspects": [
        {"aspect": "方面名（必须来自候选列表）", "polarity": "positive|neutral|negative", "evidence": "原文片段（≤30字）"}
    ],
    "keywords": ["关键词1", "关键词2"],
    "summary": "一句话摘要（≤40字）",
}


def build_semantic_messages(spot_name: str, content: str) -> list[dict[str, str]]:
    """组装评论语义分析的 messages（system + user）。

    调用粒度 = 单条评论；`spot_name` 只作为语境提示，不要求模型据此补充事实。
    """
    user = (
        f"景点：{spot_name or '未知'}\n"
        f"评论：{content}\n\n"
        f"[输出 JSON Schema]\n{json.dumps(SEMANTIC_OUTPUT_SCHEMA, ensure_ascii=False, indent=2)}"
    )
    return [
        {"role": "system", "content": SEMANTIC_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_semantic_repair_messages(
    spot_name: str,
    content: str,
    previous: str,
    reason: str,
) -> list[dict[str, str]]:
    """校验不通过时的**修复轮** messages（§15.A.4/A.5：非法则重试）。

    与首次调用共用同一份 Prompt 模板与版本号，只在末尾追加"上次错在哪"，
    这样 `prompt_version` 仍能唯一标识本次生成所用的模板。
    """
    messages = build_semantic_messages(spot_name, content)
    messages.append({"role": "assistant", "content": previous[:500]})
    messages.append(
        {
            "role": "user",
            "content": (
                f"上一次输出未通过校验：{reason}\n"
                "请严格按 Schema 重新输出 JSON。特别注意 evidence 必须是评论原文中出现过的连续片段。"
            ),
        }
    )
    return messages


# ---------------------------------------------------------------------------
# 二、C-BAT-07 景点智能评价 Prompt
# ---------------------------------------------------------------------------

REPORT_SYSTEM_PROMPT = """你是旅游景点评论分析报告的撰写者。你只能使用"事实数据"中给出的信息撰写报告。
【硬性约束】
1. 不得使用你自身的知识补充任何事实。
2. 引用数字必须与事实数据完全一致，不得四舍五入后改变量级。
3. 事实数据中未涉及的方面，不要提及。
4. 不得给出绝对的推荐结论，只陈述数据反映的情况。
5. 不得输出实时天气、实时门票价格、客流预测、酒店、路线、交通、个性化推荐等事实数据之外的内容。
6. 只输出 JSON，不要输出任何解释性前后缀。"""

REPORT_OUTPUT_SCHEMA: dict[str, Any] = {
    "summary": "综合评价，120-260 字",
    "advantages": ["主要优势，2-6 条，每条 ≤40 字"],
    "issues": ["主要问题，2-6 条，每条 ≤40 字"],
    "visitor_focus": ["游客关注点，2-6 条，每条 ≤40 字"],
}


def build_report_messages(package_json: dict[str, Any]) -> list[dict[str, str]]:
    """组装景点智能评价的 messages（system + 事实包 JSON）。

    **事实包是唯一输入**：所有数字都来自 C-BAT-06 的 SQL 聚合结果，
    模型只负责语言组织，不允许自行计算或补充统计数字。
    """
    user = (
        "事实数据：\n"
        + json.dumps(package_json, ensure_ascii=False, indent=2)
        + f"\n\n[输出 Schema]\n{json.dumps(REPORT_OUTPUT_SCHEMA, ensure_ascii=False, indent=2)}"
    )
    return [
        {"role": "system", "content": REPORT_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 三、通用工具
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def extract_json_text(raw: str) -> str:
    """从模型返回文本中提取 JSON 主体。

    模型常见的三种"不规范"：包 ```json 围栏、前后带解释性文字、JSON 后有赘述。
    这里只做**取主体**，真正的结构校验交给 `validators.py`（不在这里做业务判断）。
    """
    text = (raw or "").strip()
    text = _FENCE_RE.sub("", text).strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def format_aspect_candidates() -> str:
    """把候选方面拼成一行，供 README / 日志展示。"""
    return " / ".join(ASPECT_CANDIDATES)


__all__ = [
    "SEMANTIC_PROMPT_VERSION",
    "REPORT_PROMPT_VERSION",
    "ASPECT_CANDIDATES",
    "POLARITIES",
    "SEMANTIC_SYSTEM_PROMPT",
    "SEMANTIC_OUTPUT_SCHEMA",
    "REPORT_SYSTEM_PROMPT",
    "REPORT_OUTPUT_SCHEMA",
    "build_semantic_messages",
    "build_semantic_repair_messages",
    "build_report_messages",
    "extract_json_text",
    "format_aspect_candidates",
]
