# -*- coding: utf-8 -*-
"""阶段一 · 数据导入脚本：最终数据集 CSV → MySQL `spot` / `review` 两张核心表。

对应 C4 组件：`C-BAT-02` 数据校验、`C-BAT-03` 清洗与规范化、`C-BAT-04` 去重与分层
（仅实现「导入所必需」的部分；语义抽取等属后续阶段，不在本脚本内）。

设计依据（不得自行发挥）：
    docs/database/建表脚本.sql            —— 字段名与类型唯一准绳，本脚本不改表结构
    docs/database/数据库设计说明.md §3     —— 逐字段口径与实测覆盖数
    docs/diagrams/数据处理流程图.md        —— 12 个处理步骤与 4 类异常处理
    开题/业务需求文档.md §7                —— BR-02 / BR-05 / BR-06 / BR-09 / BR-11 / NR-R-03

字段口径（本脚本实现的全部规则，均可复核）：

  spot 表（837 行，由 CSV「景点名称」去重）
    · source_scope        ← 景点来源标注.csv：西藏→tibet、进藏沿线→route（554 / 283）
    · address / open_time / phone / introduction
                          ← 同一景点内**代表值**：出现次数最多者优先；并列时取最长；
                            再并列取首次出现顺序。仅辅助展示，不作分析维度（BR-09）
    · phone               ← 额外规范化：按换行/Tab 拆分、丢弃页面 UI 残留片段（如"全部"）、
                            以 "; " 重连、压缩连续空白。**原因**：建表脚本中 phone 为
                            VARCHAR(64)，而 CSV 中「羊卓雍错」的电话原文含换行与"全部"标签共 67 字，
                            在 MySQL 严格模式下必然报 1406 Data too long 而中断整个导入。
                            合并后仍超 64 字时（实测仅 1 个景点，合并为 65 字），退化保留首个片段，
                            并写一条 level=WARN 的 task_log，保证「截断了什么」可追溯。
    · review_count        ← 该景点在全量 CSV 中的评论条数（设计口径：全量评论量，冗余自 stat_spot）
    · has_full_evaluation ← review_count >= 100（BR-02，共 57 个景点）
    · poi_url             ← 阶段一留空（CSV 无此列；携程西藏景点清单.csv 仅覆盖 283/837，后续阶段再补）
    · 缺失即 NULL，不填补、不造数

  review 表（59,033 行，CSV 的 11 个评论级字段 + 派生字段）
    · score / score_desc  ← 同为空或同为非空（实测错配 0 条、空值 37 条 → NULL）
    · content             ← 1 条空值 → NULL
    · content_length      ← len(content.strip())，空正文记 0（中位数 31）
    · publish_date/year/month ← 发布时间拆分（实测格式全部为 YYYY-MM-DD，异常 0 条）
    · ip_location         ← 原值保留（便于溯源）
    · ip_is_unknown       ← ip_location == '未知'（24,025 条，占 40.70%）
    · ip_province         ← 仅当 publish_date >= 2022-08-01 且归属地非"未知"时填写（BR-01）
    · is_low_info         ← 正文非空且长度 <= 10（BR-05，10,228 条 / 17.33%）
    · is_dup_content / dup_group_id ← 内容重复清单.csv（BR-06，1,285 组 / 4,239 条）
    · 空字符串 → NULL（可空列）；NOT NULL 列保留 '' 或默认值

幂等性（NR-R-01／02）：
    两张表均以 INSERT ... ON DUPLICATE KEY UPDATE 写入，重复执行不产生重复行，
    支持断点续跑。`--reset` 会先清空两表（破坏性，需显式确认）。

用法：
    # 1) 只解析不写库，先看统计是否符合预期（推荐第一步）
    python -m app.batch.import_dataset --mode sample --limit 200 --dry-run

    # 2) 抽样写入 200 条评论（景点表仍按全量口径写 837 行）
    python -m app.batch.import_dataset --mode sample --limit 200

    # 3) 全量导入（强制行数校验必须等于 59,033，否则终止且不写入）
    python -m app.batch.import_dataset --mode full

    # 4) 清空两表后重导（破坏性）
    python -m app.batch.import_dataset --mode full --reset --yes
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from app.config import settings
from app.db import DatabaseError, connection

# ---------------------------------------------------------------------------
# 一、设计基线常量（全部来自设计文档，出现不符即说明数据或文档有问题，应停止并核对）
# ---------------------------------------------------------------------------

#: CSV 列顺序（不得改动；来源：数据采集/旅游评论数据集_最终版.csv 表头）
CSV_COLUMNS: tuple[str, ...] = (
    "景点名称",
    "评论编号",
    "评分",
    "评分描述",
    "评论内容",
    "发布时间",
    "IP归属地",
    "用户昵称",
    "点赞数",
    "图片数",
    "图片URL",
    "地址",
    "开放时间",
    "官方电话",
    "景点介绍",
)

#: 评论级字段（11 个）与景点级字段（4 个）的划分（BR-09：景点级字段仅展示，不作分析维度）
REVIEW_LEVEL_COLUMNS: tuple[str, ...] = CSV_COLUMNS[:11]
SPOT_LEVEL_COLUMNS: tuple[str, ...] = CSV_COLUMNS[11:]

EXPECTED_REVIEW_ROWS = 59033   # 最终数据集条数（行数校验基准，NR-R-03）
EXPECTED_SPOT_ROWS = 837       # 景点数
EXPECTED_TIBET_SPOTS = 554     # 西藏口径景点数
EXPECTED_ROUTE_SPOTS = 283     # 进藏沿线口径景点数
EXPECTED_FULL_EVAL_SPOTS = 57  # 评论量 >= 100 的景点数（BR-02）
EXPECTED_DUP_GROUPS = 1285     # 正文重复组数（BR-06）
EXPECTED_DUP_ROWS = 4239       # 正文重复条数（BR-06）

FULL_EVAL_THRESHOLD = 100      # 完整智能评价门槛（BR-02）
LOW_INFO_MAX_LENGTH = 10       # 低信息量正文长度阈值（BR-05，判据为「<= 10」）
IP_VALID_FROM = "2022-08-01"   # 客源地统计时间下限（BR-01）
IP_UNKNOWN = "未知"

#: 直辖市 / 省 / 自治区（31 个）。数据集中 ip_location 已为标准简称，无需再做映射。
#: 除本集合与「未知」以外的取值（中国港澳台及境外国家）均记 is_overseas=1。
DOMESTIC_PROVINCES: frozenset[str] = frozenset(
    {
        "北京", "天津", "河北", "山西", "内蒙古",
        "辽宁", "吉林", "黑龙江",
        "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东",
        "河南", "湖北", "湖南", "广东", "广西", "海南",
        "重庆", "四川", "贵州", "云南", "西藏",
        "陕西", "甘肃", "青海", "宁夏", "新疆",
    }
)

#: 携程页面上被一并抓下来的 UI 标签残留，规范化电话时丢弃
PHONE_UI_RESIDUE: frozenset[str] = frozenset({"全部", "收起", "展开", "查看更多", "更多"})

#: 评分 → 评分描述 的合法对应关系（用于一致性校验，实测错配 0 条）
SCORE_DESC_MAP: dict[str, str] = {"5": "超棒", "4": "满意", "3": "不错", "2": "一般", "1": "不佳"}

#: CSV 中代表「空」的取值（空字符串；「未知」是有效取值，不算空）
_EMPTY = ""

#: 批量写入分片大小（详细设计 §14：JDBC 批量 500–1000；此处 review 单行最长约 1.9 KB，取 500）
BATCH_SIZE = 500

#: `spot.phone` 的列宽上限（建表脚本中为 VARCHAR(64)，此处仅作导入侧守卫，不改表结构）
PHONE_MAX_LENGTH = 64


# ---------------------------------------------------------------------------
# 二、规范化工具
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"[ \t\u3000]+")


def norm_text(value: str | None) -> str:
    """基础文本规范化：去首尾空白（含全角空格）并压缩连续空白。"""
    if value is None:
        return _EMPTY
    return _WHITESPACE_RE.sub(" ", value.replace("\r", " ").replace("\n", " ")).strip()


def to_nullable(value: str) -> str | None:
    """空字符串 → NULL（可空列的入库规则：缺失即 NULL，不填补）。"""
    trimmed = value.strip()
    return trimmed if trimmed else None


def phone_parts(value: str) -> list[str]:
    """把抓取到的官方电话原文切成有意义的片段。

    ① 按换行/Tab 拆分 → ② 去首尾空白 → ③ 丢弃页面 UI 残留片段（如"全部"）。
    """
    if not value or not value.strip():
        return []
    parts = [p.strip() for p in re.split(r"[\r\n\t]+", value) if p.strip()]
    return [p for p in parts if p not in PHONE_UI_RESIDUE]


def norm_phone(value: str) -> str | None:
    """官方电话规范化（纯函数，**不做长度截断**）。

    步骤：按换行/Tab 拆片段 → 丢弃页面 UI 残留片段 → 以 "; " 重连 → 压缩连续空白。

    **长度上限由调用方 `build_spots` 统一处理并记录**——建表脚本中 `spot.phone` 为
    VARCHAR(64)，而「羊卓雍错」的电话合并后为 65 字，超限会在 MySQL 严格模式下报
    1406 Data too long 并中断整个导入。把截断与记录分开，是为了让「哪条被截断、
    为什么、保留了什么」进入 task_log，而不是悄悄丢字。
    """
    parts = phone_parts(value)
    if not parts:
        return None
    return norm_text("; ".join(parts))


def parse_date(value: str) -> tuple[str, int, int]:
    """解析发布时间，返回 (YYYY-MM-DD, 年, 月)。

    实测全部为 YYYY-MM-DD（异常 0 条）；若出现异常格式，按数据处理流程图 §4
    「记录异常并跳过」处理——此处抛出 ValueError，由调用方计入 abnormal_count。
    """
    text = value.strip()
    dt = datetime.strptime(text, "%Y-%m-%d")
    return dt.strftime("%Y-%m-%d"), dt.year, dt.month


def normalize_ip(ip_location: str) -> tuple[int, str | None, int]:
    """IP 归属地标准化。

    返回 (ip_is_unknown, ip_province, is_overseas_for_stat)：
        · ip_is_unknown：归属地为「未知」→ 1
        · ip_province  ：标准化省份／境外地区；「未知」为空
        · is_overseas  ：非「未知」且不属于 31 个境内省级行政区 → 1（供后续 stat_ip 使用）

    注意：**ip_province 的时间口径（>= 2022-08-01）由调用方按 publish_date 判定**（BR-01），
    本函数只负责值的标准化，不掺入时间判断，避免口径分散在两处。
    """
    value = norm_text(ip_location)
    if not value or value == IP_UNKNOWN:
        return 1, None, 0
    is_overseas = 0 if value in DOMESTIC_PROVINCES else 1
    return 0, value, is_overseas


def pick_representative(values: Iterable[str], normalizer=None) -> str | None:
    """在同一景点的多条评论中选取景点级字段的代表值。

    规则（本脚本新增，需在论文「数据清洗」章节说明）：
        ① 仅考虑规范化后非空的取值；② 出现次数最多者优先；
        ③ 次数并列时取字符数最长者；④ 仍并列时取首次出现顺序。
    """
    counts: "OrderedDict[str, int]" = OrderedDict()
    for raw in values:
        candidate = normalizer(raw) if normalizer else norm_text(raw)
        if not candidate:
            continue
        counts[candidate] = counts.get(candidate, 0) + 1
    return pick_from_counts(counts)


def pick_from_counts(counts: "OrderedDict[str, int]") -> str | None:
    """按代表值规则从「取值 → 出现次数」有序字典中选出键（规则②③④）。

    与 `pick_representative` 拆开，是为了让调用方在选出代表值之后仍能拿到
    该取值对应的中间结果（例如官方电话的原始片段列表，用于超长时安全退化）。
    """
    best: str | None = None
    best_key: tuple[int, int] | None = None
    for value, count in counts.items():
        key = (count, len(value))
        if best_key is None or key > best_key:
            best, best_key = value, key
    return best


# ---------------------------------------------------------------------------
# 三、源文件读取
# ---------------------------------------------------------------------------


@dataclass
class SourceData:
    """一次导入所需的全部源数据（全部只读，BR-11）。"""

    header: list[str] = field(default_factory=list)
    all_rows: list[list[str]] = field(default_factory=list)   # 最终数据集全部数据行（59,033）
    reviews_raw: list[list[str]] = field(default_factory=list)  # 本次实际入库的评论行（抽样时为其子集）
    spot_scope: dict[str, str] = field(default_factory=dict)      # 景点名称 → tibet/route
    spot_label_count: dict[str, int] = field(default_factory=dict)  # 景点名称 → 标注条数
    dup_group_by_comment: dict[str, int] = field(default_factory=dict)  # 评论编号 → 重复组号


def read_csv_rows(path: Path, encoding: str = "utf-8-sig") -> list[list[str]]:
    """读取 CSV 全部数据行（不含表头）。

    编码固定 utf-8-sig：最终数据集为 UTF-8 with BOM（项目现状分析.md §8.4 K9）。
    正文含全角逗号、长文本与少量内嵌 Tab，交由 csv 模块按 RFC4180 解析（K10）。
    """
    if not path.exists():
        raise FileNotFoundError(f"源文件不存在：{path}")
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle)
        rows = list(reader)
    if not rows:
        raise ValueError(f"源文件为空：{path}")
    return rows


def load_source_data(review_limit: int | None) -> SourceData:
    """读取并校验三份源文件（最终数据集 / 景点来源标注 / 内容重复清单）。"""
    data = SourceData()

    # 3.1 最终数据集
    raw = read_csv_rows(settings.paths.final_dataset)
    data.header = [h.strip() for h in raw[0]]
    if tuple(data.header) != CSV_COLUMNS:
        raise ValueError(
            "最终数据集表头与设计基线不一致，已终止导入。\n"
            f"  期望：{list(CSV_COLUMNS)}\n"
            f"  实际：{data.header}"
        )
    body = raw[1:]
    data.all_rows = body
    data.reviews_raw = body[:review_limit] if review_limit else body

    # 3.2 景点来源标注（景点名称 / 来源口径 / 条数）
    scope_rows = read_csv_rows(settings.paths.spot_source)
    for row in scope_rows[1:]:
        name, scope, count = row[0].strip(), row[1].strip(), row[2].strip()
        data.spot_scope[name] = {"西藏": "tibet", "进藏沿线": "route"}.get(scope, "tibet")
        data.spot_label_count[name] = int(count)

    # 3.3 内容重复清单（重复组号 / 重复条数 / 涉及景点数 / 评论内容 / 涉及景点 / 评论编号列表）
    dup_rows = read_csv_rows(settings.paths.dup_list)
    for row in dup_rows[1:]:
        group_id = int(row[0])
        for comment_id in row[5].split("、"):
            comment_id = comment_id.strip()
            if comment_id:
                data.dup_group_by_comment[comment_id] = group_id

    return data


# ---------------------------------------------------------------------------
# 四、构建待写入的记录
# ---------------------------------------------------------------------------


@dataclass
class BuildStats:
    """构建阶段的统计（写入 task_log.detail_json，四类计数口径来自数据处理流程图 §2）。"""

    input_count: int = 0
    output_count: int = 0
    dropped_count: int = 0
    abnormal_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_detail(self) -> str:
        return json.dumps(
            {
                "input_count": self.input_count,
                "output_count": self.output_count,
                "dropped_count": self.dropped_count,
                "abnormal_count": self.abnormal_count,
                "extra": self.extra,
            },
            ensure_ascii=False,
        )


def build_spots(all_rows: Sequence[Sequence[str]]) -> tuple[list[dict], BuildStats]:
    """按全量 CSV 构建 spot 表记录（837 行）。

    **注意**：即使抽样导入，景点表也按**全量**计算 review_count 与 has_full_evaluation，
    以保证与设计文档口径（837 个景点 / 57 个具备完整评价资格）一致；
    抽样模式下 review 表行数少于 review_count 属预期现象。
    """
    stats = BuildStats(input_count=len(all_rows))
    grouped: "OrderedDict[str, list[Sequence[str]]]" = OrderedDict()
    for row in all_rows:
        grouped.setdefault(norm_text(row[0]), []).append(row)

    # 来源标注覆盖性校验（数据质量报告已确认一致，此处作为每次导入的强制关卡）
    labeled = set(load_source_scope_names())
    missing_label = [name for name in grouped if name not in labeled]
    if missing_label:
        raise ValueError(f"以下 {len(missing_label)} 个景点在 景点来源标注.csv 中缺失：{missing_label[:5]}")

    scope_map = load_source_scope()
    spots: list[dict] = []
    phone_truncated: list[dict] = []
    for name, rows in grouped.items():
        address = pick_representative(r[11] for r in rows)
        open_time = pick_representative(r[12] for r in rows)
        introduction = pick_representative(r[14] for r in rows)
        phone, truncation = pick_phone((r[13] for r in rows))
        if truncation:
            truncation["spot_name"] = name
            phone_truncated.append(truncation)

        review_count = len(rows)
        spots.append(
            {
                "spot_name": name,
                "source_scope": scope_map.get(name, "tibet"),
                "address": address,
                "open_time": open_time,
                "phone": phone,
                "introduction": introduction,
                "review_count": review_count,
                "has_full_evaluation": 1 if review_count >= FULL_EVAL_THRESHOLD else 0,
            }
        )

    stats.output_count = len(spots)
    stats.extra.update(
        {
            "tibet_spots": sum(1 for s in spots if s["source_scope"] == "tibet"),
            "route_spots": sum(1 for s in spots if s["source_scope"] == "route"),
            "full_eval_spots": sum(s["has_full_evaluation"] for s in spots),
            "has_address": sum(1 for s in spots if s["address"]),
            "has_open_time": sum(1 for s in spots if s["open_time"]),
            "has_phone": sum(1 for s in spots if s["phone"]),
            "has_introduction": sum(1 for s in spots if s["introduction"]),
            "phone_truncated_spots": phone_truncated,
        }
    )
    return spots, stats


def pick_phone(values: Iterable[str]) -> tuple[str | None, dict | None]:
    """选取景点官方电话的代表值，并按 `spot.phone` 的 VARCHAR(64) 上限做安全退化。

    返回 `(最终入库值, 截断诊断或 None)`。

    为什么不能先合并再按 "; " 反拆：原始片段自身就含 " ; "，合并后无法可靠还原边界。
    因此这里在**片段层面**保留中间结果，超长时直接取首个片段，而不是对合并串做字符串切割。
    """
    counts: "OrderedDict[str, int]" = OrderedDict()
    parts_by_value: dict[str, list[str]] = {}
    for raw in values:
        parts = phone_parts(raw)
        if not parts:
            continue
        merged = norm_text("; ".join(parts))
        counts[merged] = counts.get(merged, 0) + 1
        parts_by_value.setdefault(merged, parts)

    merged = pick_from_counts(counts)
    if merged is None:
        return None, None
    if len(merged) <= PHONE_MAX_LENGTH:
        return merged, None

    # 超限：保留首个片段（实测仅「羊卓雍错」触发，合并 65 字 > 64）
    kept = norm_text(parts_by_value[merged][0])[:PHONE_MAX_LENGTH]
    return kept, {"merged_length": len(merged), "kept_length": len(kept), "kept": kept}


def build_reviews(
    rows: Sequence[Sequence[str]],
    spot_id_by_name: dict[str, int],
    dup_group_by_comment: dict[str, int],
) -> tuple[list[dict], BuildStats]:
    """构建 review 表记录（抽样模式下为前 N 条）。"""
    stats = BuildStats(input_count=len(rows))
    records: list[dict] = []

    for row in rows:
        try:
            spot_name = norm_text(row[0])
            comment_id = int(row[1].strip())
            publish_date, year, month = parse_date(row[5])
        except (ValueError, IndexError) as exc:
            # 数据处理流程图 §4：日期格式异常 → 记录异常并跳过（保留评论基础信息）
            stats.abnormal_count += 1
            stats.dropped_count += 1
            print(f"  [异常] 跳过评论 {row[1] if len(row) > 1 else '?'}：{exc}")
            continue

        score_raw = row[2].strip()
        content = row[4].strip()
        ip_is_unknown, ip_province_raw, _ = normalize_ip(row[6])
        # BR-01：客源地统计只承认 2022-08-01 及之后的归属地
        ip_province = ip_province_raw if (publish_date >= IP_VALID_FROM and ip_province_raw) else None

        records.append(
            {
                "comment_id": comment_id,
                "spot_id": spot_id_by_name[spot_name],
                "score": int(score_raw) if score_raw else None,
                "score_desc": to_nullable(row[3]),
                "content": content or None,
                "content_length": len(content),
                "publish_date": publish_date,
                "publish_year": year,
                "publish_month": month,
                "ip_location": norm_text(row[6]) or IP_UNKNOWN,
                "ip_is_unknown": ip_is_unknown,
                "ip_province": ip_province,
                "user_nick": norm_text(row[7]),
                "like_count": int(row[8].strip() or 0),
                "image_count": int(row[9].strip() or 0),
                "image_urls": to_nullable(row[10]),
                "is_low_info": 1 if (content and len(content) <= LOW_INFO_MAX_LENGTH) else 0,
                "is_dup_content": 1 if str(comment_id) in dup_group_by_comment else 0,
                "dup_group_id": dup_group_by_comment.get(str(comment_id)),
            }
        )

    stats.output_count = len(records)
    stats.extra.update(
        {
            "low_info": sum(r["is_low_info"] for r in records),
            "dup_content": sum(r["is_dup_content"] for r in records),
            "score_null": sum(1 for r in records if r["score"] is None),
            "content_null": sum(1 for r in records if r["content"] is None),
            "ip_unknown": sum(r["ip_is_unknown"] for r in records),
            "ip_province_filled": sum(1 for r in records if r["ip_province"]),
        }
    )
    return records, stats


# 来源标注缓存（避免在构建函数里重复读文件）
_SCOPE_CACHE: dict[str, str] | None = None


def load_source_scope() -> dict[str, str]:
    """读取 景点来源标注.csv → {景点名称: tibet/route}（带进程内缓存）。"""
    global _SCOPE_CACHE
    if _SCOPE_CACHE is None:
        rows = read_csv_rows(settings.paths.spot_source)
        mapping: dict[str, str] = {}
        for row in rows[1:]:
            mapping[row[0].strip()] = {"西藏": "tibet", "进藏沿线": "route"}.get(row[1].strip(), "tibet")
        _SCOPE_CACHE = mapping
    return _SCOPE_CACHE


def load_source_scope_names() -> list[str]:
    """来源标注文件中出现的全部景点名称。"""
    return list(load_source_scope().keys())


# ---------------------------------------------------------------------------
# 五、写库
# ---------------------------------------------------------------------------

SPOT_UPSERT_SQL = """
INSERT INTO spot
    (spot_name, source_scope, address, open_time, phone, introduction, review_count, has_full_evaluation)
VALUES
    (%(spot_name)s, %(source_scope)s, %(address)s, %(open_time)s, %(phone)s,
     %(introduction)s, %(review_count)s, %(has_full_evaluation)s)
ON DUPLICATE KEY UPDATE
    source_scope        = VALUES(source_scope),
    address             = VALUES(address),
    open_time           = VALUES(open_time),
    phone               = VALUES(phone),
    introduction        = VALUES(introduction),
    review_count        = VALUES(review_count),
    has_full_evaluation = VALUES(has_full_evaluation)
"""

REVIEW_UPSERT_SQL = """
INSERT INTO review
    (comment_id, spot_id, score, score_desc, content, content_length,
     publish_date, publish_year, publish_month,
     ip_location, ip_is_unknown, ip_province, user_nick,
     like_count, image_count, image_urls,
     is_low_info, is_dup_content, dup_group_id)
VALUES
    (%(comment_id)s, %(spot_id)s, %(score)s, %(score_desc)s, %(content)s, %(content_length)s,
     %(publish_date)s, %(publish_year)s, %(publish_month)s,
     %(ip_location)s, %(ip_is_unknown)s, %(ip_province)s, %(user_nick)s,
     %(like_count)s, %(image_count)s, %(image_urls)s,
     %(is_low_info)s, %(is_dup_content)s, %(dup_group_id)s)
ON DUPLICATE KEY UPDATE
    spot_id        = VALUES(spot_id),
    score          = VALUES(score),
    score_desc     = VALUES(score_desc),
    content        = VALUES(content),
    content_length = VALUES(content_length),
    publish_date   = VALUES(publish_date),
    publish_year   = VALUES(publish_year),
    publish_month  = VALUES(publish_month),
    ip_location    = VALUES(ip_location),
    ip_is_unknown  = VALUES(ip_is_unknown),
    ip_province    = VALUES(ip_province),
    user_nick      = VALUES(user_nick),
    like_count     = VALUES(like_count),
    image_count    = VALUES(image_count),
    image_urls     = VALUES(image_urls),
    is_low_info    = VALUES(is_low_info),
    is_dup_content = VALUES(is_dup_content),
    dup_group_id   = VALUES(dup_group_id)
"""


def chunked(items: Sequence[Any], size: int = BATCH_SIZE) -> Iterator[Sequence[Any]]:
    """把序列切成固定大小的批次。"""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def reset_tables(conn) -> None:
    """清空 spot / review（破坏性操作，仅在 --reset --yes 时调用）。

    review 通过外键引用 spot，需先关外键检查再清空；TRUNCATE 会同时重置自增列。
    """
    with conn.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        cur.execute("TRUNCATE TABLE review")
        cur.execute("TRUNCATE TABLE spot")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")


def write_data(
    spots: Sequence[dict],
    data: SourceData,
    spot_stats: BuildStats,
    args: argparse.Namespace,
) -> dict:
    """把构建好的记录写入数据库，并登记 analysis_task / task_log。

    执行顺序：登记任务 → （可选清表）→ 写 spot → 回读 spot_id → 构建并写 review → 写日志 → 收尾。
    **必须先写 spot 再构建 review**：review.spot_id 依赖数据库生成的自增主键，
    不能由 CSV 中的景点名称直接推算。
    """
    task_name = f"CSV 导入 spot/review（mode={args.mode}）"
    started_at = datetime.now()

    with connection() as conn:
        # 5.1 登记批处理任务（task_log.task_id 为 NOT NULL 外键，必须先落 analysis_task）
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO analysis_task (task_type, task_name, status, total_count, started_at)
                VALUES ('clean', %s, 'running', %s, %s)
                """,
                (task_name, len(data.reviews_raw), started_at.strftime("%Y-%m-%d %H:%M:%S")),
            )
            task_id = cur.lastrowid

        if args.reset:
            print("      [警告] 按 --reset 要求清空 spot / review 两表")
            reset_tables(conn)

        # 5.2 写入 spot（全量 837 行）
        with conn.cursor() as cur:
            cur.executemany(SPOT_UPSERT_SQL, spots)
        print(f"      spot   写入完成：{len(spots)} 行")
        _log_step(conn, task_id, "景点级字段拆分", spot_stats, "837 个景点去重并映射景点级字段")
        # 电话超长被截断的情况单独记 WARN，便于复核（不阻断导入）
        truncated = spot_stats.extra.get("phone_truncated_spots") or []
        if truncated:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO task_log (task_id, level, stage, message, detail_json)
                    VALUES (%s, 'WARN', '官方电话规范化', %s, %s)
                    """,
                    (
                        task_id,
                        f"{len(truncated)} 个景点的官方电话合并后超过 spot.phone 的 64 字符上限，已退化保留首个片段",
                        json.dumps({"extra": truncated}, ensure_ascii=False),
                    ),
                )

        # 5.3 回读 spot_id
        with conn.cursor() as cur:
            cur.execute("SELECT spot_id, spot_name FROM spot")
            spot_id_by_name = {row["spot_name"]: row["spot_id"] for row in cur.fetchall()}

        missing = {s["spot_name"] for s in spots} - set(spot_id_by_name)
        if missing:
            raise RuntimeError(f"回读 spot_id 失败，以下景点未入库：{sorted(missing)[:5]}")

        # 5.4 构建并写入 review（分片，便于断点续跑）
        reviews, review_stats = build_reviews(data.reviews_raw, spot_id_by_name, data.dup_group_by_comment)
        print(
            f"      review 构建完成：{len(reviews)} 行"
            f"（低信息量 {review_stats.extra['low_info']}、重复正文 {review_stats.extra['dup_content']}、"
            f"评分空 {review_stats.extra['score_null']}、归属地未知 {review_stats.extra['ip_unknown']}）"
        )
        with conn.cursor() as cur:
            for batch in chunked(reviews):
                cur.executemany(REVIEW_UPSERT_SQL, batch)
        print(f"      review 写入完成：{len(reviews)} 行")

        _log_step(conn, task_id, "评论级字段映射", review_stats, "评论级 11 字段映射与派生字段计算")
        _log_step(
            conn,
            task_id,
            "低信息量与重复正文标记",
            review_stats,
            "低信息量（正文<=10 字）与重复正文分组标记",
        )

        # 5.5 收尾：更新任务状态
        finished_at = datetime.now()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE analysis_task
                   SET status = 'success', success_count = %s, finished_at = %s, cost_seconds = %s
                 WHERE task_id = %s
                """,
                (
                    len(reviews),
                    finished_at.strftime("%Y-%m-%d %H:%M:%S"),
                    int((finished_at - started_at).total_seconds()),
                    task_id,
                ),
            )

    return {"task_id": task_id, "spots": len(spots), "reviews": len(reviews)}


def _log_step(conn, task_id: int, stage: str, stats: BuildStats, message: str) -> None:
    """写入一条 task_log（承接原 clean_log 的职责：stage=步骤名 + 四类计数）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO task_log (task_id, level, stage, message, detail_json)
            VALUES (%s, 'INFO', %s, %s, %s)
            """,
            (task_id, stage, message, stats.as_detail()),
        )


# ---------------------------------------------------------------------------
# 六、导入后校验
# ---------------------------------------------------------------------------


def verify_import(mode: str = "full") -> dict[str, Any]:
    """导入后一致性校验（详细设计 §9 校验查询 + 建表脚本 §9「校验查询」）。

    期望值全部取自设计文档的实测口径，任一项不符即说明导入逻辑或数据有问题。
    抽样模式下 review 表只写入前 N 条，因此评论级指标标记为「抽样跳过」而不判为不符。
    """
    is_sample = mode == "sample"
    checks: list[dict[str, Any]] = []

    with connection() as conn:
        with conn.cursor() as cur:

            def scalar(sql: str) -> int:
                cur.execute(sql)
                row = cur.fetchone() or {}
                return int(list(row.values())[0] or 0)

            def add(name: str, expr: str, sql: str, expected: int, review_level: bool = False) -> None:
                actual = scalar(sql)
                if is_sample and review_level:
                    verdict = "抽样跳过"
                else:
                    verdict = "OK" if actual == expected else "不符"
                checks.append({"指标": name, "口径": expr, "实测": actual, "期望": expected, "结果": verdict})

            # --- 景点表（抽样模式下同样按全量口径写入，全部参与判定）---
            add("景点总数", "COUNT(*) FROM spot", "SELECT COUNT(*) FROM spot", EXPECTED_SPOT_ROWS)
            add("西藏口径景点", "source_scope='tibet'", "SELECT COUNT(*) FROM spot WHERE source_scope='tibet'", EXPECTED_TIBET_SPOTS)
            add("沿线口径景点", "source_scope='route'", "SELECT COUNT(*) FROM spot WHERE source_scope='route'", EXPECTED_ROUTE_SPOTS)
            add(
                "具备完整评价资格景点",
                "has_full_evaluation=1",
                "SELECT COUNT(*) FROM spot WHERE has_full_evaluation=1",
                EXPECTED_FULL_EVAL_SPOTS,
            )
            add("评论量为 0 的景点", "review_count=0", "SELECT COUNT(*) FROM spot WHERE review_count=0", 0)
            add("电话超长景点", "CHAR_LENGTH(phone)>64", "SELECT COUNT(*) FROM spot WHERE CHAR_LENGTH(phone)>64", 0)
            add("重复景点名", "COUNT(DISTINCT spot_name)", "SELECT COUNT(DISTINCT spot_name) FROM spot", EXPECTED_SPOT_ROWS)

            # --- 评论表（抽样模式下跳过判定）---
            add("评论总数", "COUNT(*) FROM review", "SELECT COUNT(*) FROM review", EXPECTED_REVIEW_ROWS, True)
            add("低信息量评论", "is_low_info=1", "SELECT COUNT(*) FROM review WHERE is_low_info=1", 10228, True)
            add("重复正文评论", "is_dup_content=1", "SELECT COUNT(*) FROM review WHERE is_dup_content=1", EXPECTED_DUP_ROWS, True)
            add(
                "重复正文组数",
                "COUNT(DISTINCT dup_group_id)",
                "SELECT COUNT(DISTINCT dup_group_id) FROM review",
                EXPECTED_DUP_GROUPS,
                True,
            )
            add("评分空值", "score IS NULL", "SELECT COUNT(*) FROM review WHERE score IS NULL", 37, True)
            add("正文空值", "content IS NULL", "SELECT COUNT(*) FROM review WHERE content IS NULL", 1, True)
            add("归属地未知", "ip_is_unknown=1", "SELECT COUNT(*) FROM review WHERE ip_is_unknown=1", 24025, True)
            add(
                "有效客源地样本",
                "ip_province IS NOT NULL",
                "SELECT COUNT(*) FROM review WHERE ip_province IS NOT NULL",
                34578,  # 35,098 条有效 IP 样本 − 其中 520 条归属地仍为"未知"
                True,
            )
            add("最长正文字数", "MAX(content_length)", "SELECT MAX(content_length) FROM review", 1829, True)
            add("评论量≥100 的评论数", "spot.review_count>=100", 
                "SELECT COUNT(*) FROM review r JOIN spot s ON s.spot_id=r.spot_id WHERE s.review_count>=100", 50562, True)
            add("外键失配评论数", "review.spot_id 无对应 spot",
                "SELECT COUNT(*) FROM review r LEFT JOIN spot s ON s.spot_id=r.spot_id WHERE s.spot_id IS NULL", 0)

    judged = [c for c in checks if c["结果"] != "抽样跳过"]
    return {
        "rows": checks,
        "all_ok": all(c["结果"] == "OK" for c in judged),
        "sample_mode": is_sample,
    }


def print_verification(result: dict[str, Any]) -> None:
    """以中文表格打印校验结果。"""
    print("\n" + "=" * 78)
    print(" 导入后校验（期望值取自 docs/database/建表脚本.sql §9 与各设计文档实测口径）")
    print("=" * 78)
    print(f"{'指标':<22}{'实测':>10}{'期望':>10}   结果")
    print("-" * 78)
    for row in result["rows"]:
        print(f"{row['指标']:<22}{row['实测']:>10}{row['期望']:>10}   {row['结果']}")
    print("-" * 78)
    if result["all_ok"]:
        print(" 结论：全部通过 ✔")
    else:
        print(" 结论：⚠ 存在不符项，请把上面的完整输出发给 AI 排查。")
    if result["sample_mode"]:
        print(" 说明：抽样模式只写入部分评论，评论级指标标记「抽样跳过」属预期；")
        print("       景点级指标已按全量口径校验，可据此确认字段映射正确。")


# ---------------------------------------------------------------------------
# 七、命令行入口
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="阶段一：将最终数据集 CSV 导入 MySQL spot / review 两张核心表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=("sample", "full"),
        default="sample",
        help="sample=抽样验证（默认）；full=全量导入（强制行数校验 == 59033）",
    )
    parser.add_argument("--limit", type=int, default=200, help="sample 模式下导入的评论条数（默认 200）")
    parser.add_argument("--dry-run", action="store_true", help="只解析与统计，不写数据库")
    parser.add_argument("--reset", action="store_true", help="导入前清空 spot / review（破坏性）")
    parser.add_argument("--yes", action="store_true", help="与 --reset 连用，确认破坏性操作")
    parser.add_argument("--skip-verify", action="store_true", help="跳过导入后校验")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    print("=" * 78)
    print(" 阶段一 · 数据导入：最终数据集 CSV → MySQL spot / review")
    print("=" * 78)
    print(f" 数据库      ：{settings.db.summary}（口令不打印）")
    print(f" 最终数据集  ：{settings.paths.final_dataset}")
    print(f" 来源标注    ：{settings.paths.spot_source}")
    print(f" 重复清单    ：{settings.paths.dup_list}")
    print(f" 模式        ：{args.mode}" + (f"（前 {args.limit} 条评论）" if args.mode == "sample" else ""))
    print(f" 写库        ：{'否（--dry-run）' if args.dry_run else '是'}")
    print("-" * 78)

    if args.reset and not args.yes:
        print(" ✘ --reset 是破坏性操作（会清空 spot / review），需同时加 --yes 确认。已终止。")
        return 2

    # 7.1 读取源数据
    print("[1/5] 读取源文件…")
    try:
        limit = args.limit if args.mode == "sample" else None
        data = load_source_data(limit)
    except (FileNotFoundError, ValueError) as exc:
        print(f" ✘ 源文件读取失败：{exc}")
        return 2

    full_body = data.all_rows
    print(f"      最终数据集总行数：{len(full_body)}（期望 {EXPECTED_REVIEW_ROWS}）")
    print(f"      本次参与入库的评论行数：{len(data.reviews_raw)}")
    print(f"      景点来源标注：{len(data.spot_scope)} 个景点")
    print(f"      内容重复清单：{len(set(data.dup_group_by_comment.values()))} 组 / {len(data.dup_group_by_comment)} 条")

    # 7.2 行数校验（NR-R-03：全量导入必须等于 59,033，否则终止且不写入任何数据）
    print("[2/5] 行数校验…")
    if args.mode == "full" and len(full_body) != EXPECTED_REVIEW_ROWS:
        print(f" ✘ 行数校验未通过：实际 {len(full_body)}，期望 {EXPECTED_REVIEW_ROWS}。")
        print("    按 NR-R-03 终止任务，未写入任何数据。")
        return 3
    if args.mode == "full":
        print(f"      ✔ 通过：{len(full_body)} 行")
    else:
        print(f"      抽样模式：跳过强制校验（全量行为 {len(full_body)} 行）")

    # 7.3 校验评分与评分描述一致性（实测错配 0 条）
    print("[3/5] 评分一致性校验…")
    mismatch = 0
    for row in data.reviews_raw:
        score, desc = row[2].strip(), row[3].strip()
        if score and SCORE_DESC_MAP.get(score) != desc:
            mismatch += 1
    print(f"      评分 ↔ 评分描述 错配：{mismatch} 条" + ("（实测应为 0）" if mismatch else " ✔"))

    # 7.4 构建记录
    print("[4/5] 构建 spot / review 记录…")
    spots, spot_stats = build_spots(full_body)
    print(
        f"      spot  ：{len(spots)} 行"
        f"（tibet {spot_stats.extra['tibet_spots']} / route {spot_stats.extra['route_spots']}，"
        f"完整评价资格 {spot_stats.extra['full_eval_spots']} 个）"
    )
    print(
        f"              字段覆盖：地址 {spot_stats.extra['has_address']}、开放时间 {spot_stats.extra['has_open_time']}、"
        f"电话 {spot_stats.extra['has_phone']}、介绍 {spot_stats.extra['has_introduction']}"
    )

    if args.dry_run:
        print("      （--dry-run：跳过 review 构建与写库）")
        print("\n 试运行结束，未写入任何数据。")
        return 0

    # review 需要 spot_id，先落 spot 再回读；dry-run 模式到此为止
    print("[5/5] 写入数据库…")
    try:
        result = write_data(spots, data, spot_stats, args)
    except DatabaseError as exc:
        print(f" ✘ 数据库错误：{exc}")
        return 4

    print(f"      任务号 task_id = {result['task_id']}（已登记 analysis_task 与 task_log）")

    if not args.skip_verify:
        try:
            print_verification(verify_import(args.mode))
        except DatabaseError as exc:
            print(f" ⚠ 校验阶段数据库错误：{exc}")

    print("\n 导入完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
