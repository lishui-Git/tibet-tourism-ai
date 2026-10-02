# -*- coding: utf-8 -*-
"""数据访问帮助函数（C4 组件 `C-API-13` 的查询增强）。

`app/db.py` 提供的是"执行 SQL"的通用能力；本模块在其上补三件业务层反复需要的事：

1. **统一列名转换**：MySQL 驱动把 `DECIMAL` 返回成 `decimal.Decimal`、把 `DATE/DATETIME`
   返回成 `date/datetime`、把 `BIGINT UNSIGNED` 返回成 `int`，直接 JSON 化会失败。
   这里一次性把行转成"可 JSON 序列化 + 数值统一为 float/int"的字典。
2. **分页规范**（详细设计 §6.1）：`page` 从 1 开始、`page_size` 默认 20、上限 100；
   越界要返回业务码 1002 而不是静默纠正。
3. **口径标注**（详细设计 §6.1 / BR-10）：凡受 S1–S6 口径影响的接口必须带
   `caliber_note` 与 `sample_size`，避免"数字给了但没说口径"。

本模块**只读**，不含任何写操作，也不调用任何模型。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Sequence

from app.db import query_all, query_one

# 分页限制（详细设计 §6.1）
PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 100


class ParamError(ValueError):
    """参数非法：由路由层映射为业务码 1001 / 1002。"""

    def __init__(self, message: str, code: int = 1001) -> None:
        super().__init__(message)
        self.code = code


def jsonable(value: Any) -> Any:
    """把 MySQL 返回的常见类型转成可 JSON 序列化的形式。

    - `Decimal` → `float`（占比、评分等展示用；如需精确分位可比对原始表）
    - `date` / `datetime` → ISO 字符串
    - `bytes` → UTF-8 字符串（少数驱动配置下会返回 bytes）
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def clean_row(row: dict | None) -> dict | None:
    """把一行里的所有值转成可序列化形式。"""
    if row is None:
        return None
    return {key: jsonable(value) for key, value in row.items()}


def clean_rows(rows: Sequence[dict]) -> list[dict]:
    return [clean_row(row) or {} for row in rows]


def fetch_all(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    """执行只读查询并做类型清理。"""
    return clean_rows(query_all(sql, params))


def fetch_one(sql: str, params: Sequence[Any] | None = None) -> dict | None:
    """执行只读查询并返回首行（已做类型清理），无结果返回 None。"""
    return clean_row(query_one(sql, params))


def scalar(sql: str, params: Sequence[Any] | None = None, default: int = 0) -> int:
    """取单值（用于 COUNT 等），无结果返回默认值。"""
    row = query_one(sql, params) or {}
    value = list(row.values())[0] if row else None
    return default if value is None else int(value)


def parse_pagination(args: Any) -> tuple[int, int]:
    """解析 `page` / `page_size`，按 §6.1 校验。

    :raises ParamError: 非整数（1001）或越界（1002）
    """
    raw_page = args.get("page", "1")
    raw_size = args.get("page_size", str(PAGE_SIZE_DEFAULT))
    try:
        page = int(raw_page)
        page_size = int(raw_size)
    except (TypeError, ValueError) as exc:
        raise ParamError("page 与 page_size 必须为整数", code=1001) from exc
    if page < 1:
        raise ParamError("page 必须从 1 开始", code=1002)
    if page_size < 1 or page_size > PAGE_SIZE_MAX:
        raise ParamError(f"page_size 必须在 1–{PAGE_SIZE_MAX} 之间", code=1002)
    return page, page_size


def parse_int_arg(args: Any, name: str, *, required: bool = True, default: int | None = None) -> int | None:
    """解析整数型查询参数（如 spot_id）。"""
    raw = args.get(name)
    if raw is None or raw == "":
        if required:
            raise ParamError(f"缺少必填参数 {name}", code=1001)
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ParamError(f"参数 {name} 必须为整数", code=1001) from exc


# ---------------------------------------------------------------------------
# 口径说明（BR-10：受口径影响的接口必须回显）
# ---------------------------------------------------------------------------

# 客源地口径（BR-01）：携程自 2022-08 起才展示 IP，之前 100% 为"未知"
CALIBER_IP = "客源地统计仅使用发布时间 ≥ 2022-08-01 的样本（携程自该日期起才展示 IP 归属地）；聚合时再排除归属地为『未知』的评论。"

# 时间趋势口径（FR-OV-03）：评论发布时间 ≠ 实际到访时间
CALIBER_TREND = "时间趋势依据评论的发布时间统计，反映的是『评论量变化』，不代表实际客流量或到访量。"

# 情感口径：两种方法并列，不能混用
CALIBER_SENTIMENT = "情感结果按方法（method）并列存储：deepseek = 大模型语义判定口径；mllib = Spark 朴素贝叶斯基线（以星级为弱标签）。两种方法结论不可混用，切换查看请用 method 参数。"

# 方面口径（BR-04）
CALIBER_ASPECT = "方面分析要求该方面样本量 ≥ 10 条才给出倾向结论；不足 10 条时只展示样本量。"

# 门槛口径（BR-02/BR-03）
CALIBER_THRESHOLD = "评论量 ≥ 100 条的景点生成完整智能评价；不足 100 条仅提供基础统计并提示样本不足。"

# 低信息量口径（BR-05）
CALIBER_LOW_INFO = "正文 ≤ 10 字的评论被标记为低信息量（is_low_info=1）：统计类功能计入全量，文本语义类功能默认不调用模型、走规则判定。"


def caliber_block(note: str, sample_size: int | None = None) -> dict[str, Any]:
    """组装统一的口径说明块（§6.1 要求 data 中必须含 caliber_note 与 sample_size）。"""
    return {"caliber_note": note, "sample_size": sample_size}


__all__ = [
    "ParamError",
    "jsonable",
    "clean_row",
    "clean_rows",
    "fetch_all",
    "fetch_one",
    "scalar",
    "parse_pagination",
    "parse_int_arg",
    "caliber_block",
    "CALIBER_IP",
    "CALIBER_TREND",
    "CALIBER_SENTIMENT",
    "CALIBER_ASPECT",
    "CALIBER_THRESHOLD",
    "CALIBER_LOW_INFO",
    "PAGE_SIZE_DEFAULT",
    "PAGE_SIZE_MAX",
]
