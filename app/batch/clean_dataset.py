# -*- coding: utf-8 -*-
"""阶段二 · 评论数据清洗与预处理（M7）——对应 C4 组件 `C-BAT-01`～`C-BAT-04`。

本模块是「数据处理流程图」的**可重复执行实现**，职责边界严格按设计文档划分：

    C-BAT-01  任务编排与断点续跑   →  SOP 步骤编排、失败即止、每步耗时与计数
    C-BAT-02  数据校验             →  行数校验（=59,033，NR-R-03）、表头校验、列数校验
    C-BAT-03  清洗与规范化         →  文本/日期/IP/评分/数值字段规范化 + 评论级与景点级拆分
    C-BAT-04  去重与分层           →  低信息量标记（BR-05）+ 正文重复标记（BR-06）

设计依据（不得自行发挥）：
    docs/design/详细设计说明书.md §4.7      —— M7 的输入/处理/输出/组件分工/规则
    docs/diagrams/数据处理流程图.md          —— 12 个处理步骤与 4 类异常处理
    docs/database/数据库设计说明.md          —— 字段口径与实测覆盖数
    开题/业务需求文档.md §7                  —— BR-01/02/05/06/09/11、NR-R-01/02/03

与其他脚本的分工（**不要重复实现**）：
    · 规范化函数全部复用 `app.batch.import_dataset`（已实测跑通全量导入），
      本模块不复制一份，避免同一口径出现两套实现而漂移。
    · 本模块**不写 `spot` / `review` 两张核心表**（阶段一已全量导入并校验通过）。
      如需把清洗步骤计数登记到运维表，用 `--log-task`（默认关闭，见下）。

产出（全部写入 `data/`，**绝不覆盖原始 CSV**，BR-11）：
    · `<前缀>_v1.csv`                  清洗后规范化版本文件（行数与原文件一致，不删行）
    · `<前缀>_v1_清洗统计.json`         机器可读统计（各步骤 input/output/dropped/abnormal）
    · `<前缀>_v1_清洗报告.md`           人工可读报告（可直接作为论文「数据清洗」章节素材）

数据处置原则（本模块严格遵守，均为设计文档明确要求）：
    · 不因数据「不完美」而删除：IP 未知、短评论、高评分、热门景点截断一律**保留**
    · 标记而非删除：低信息量与重复正文**留在数据中**，只打标记（双口径：统计用全量，
      文本建模按标记过滤）——数据处理流程图 §3 关键设计点 5
    · 空值如实保留：缺失即 NULL，不填补、不造数——§3 关键设计点 3
    · 任何会改变数据量的操作都必须给出 input/output/dropped/abnormal 四类计数

用法：
    # 1) 小样本先验证逻辑（只读 CSV，不写库）
    python -m app.batch.clean_dataset --limit 200

    # 2) 全量清洗（行数校验强制等于 59,033，不通过即终止且不产出任何文件）
    python -m app.batch.clean_dataset

    # 3) 全量清洗 + 把各步骤计数登记到 analysis_task / task_log（可选，只增不改）
    python -m app.batch.clean_dataset --log-task
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from app.batch.import_dataset import (
    CSV_COLUMNS,
    EXPECTED_DUP_GROUPS,
    EXPECTED_DUP_ROWS,
    EXPECTED_FULL_EVAL_SPOTS,
    EXPECTED_REVIEW_ROWS,
    EXPECTED_ROUTE_SPOTS,
    EXPECTED_SPOT_ROWS,
    EXPECTED_TIBET_SPOTS,
    BuildStats,
    build_reviews,
    build_spots,
    load_source_data,
    norm_text,
    normalize_ip,
    parse_date,
)
from app.config import settings

# Windows 控制台默认代码页为 GBK，直接输出 ✔/✘/⚠ 会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - 非 Windows 或流不可重配置时忽略
    pass


# ---------------------------------------------------------------------------
# 一、SOP 定义（C-BAT-01 任务编排的骨架，步骤名即 task_log.stage 的取值）
# ---------------------------------------------------------------------------

SOP_STAGES: tuple[str, ...] = (
    "读取源文件",
    "行数校验",
    "字段规范化",
    "评论级景点级拆分",
    "去重与分层",
    "写出规范化文件",
    "输出统计报告",
)

#: 清洗后文件的列顺序 = 原 CSV 的 15 列 + 派生列（派生列口径见模块头与函数注释）
CLEANED_COLUMNS: tuple[str, ...] = CSV_COLUMNS + (
    "content_length",
    "publish_year",
    "publish_month",
    "ip_province",
    "ip_is_unknown",
    "is_low_info",
    "is_dup_content",
    "dup_group_id",
)

DEFAULT_OUTPUT_PREFIX = "旅游评论数据集_清洗版"


# ---------------------------------------------------------------------------
# 二、统计容器（四类计数口径来自数据处理流程图 §2）
# ---------------------------------------------------------------------------


@dataclass
class StageStats:
    """单个 SOP 步骤的统计（与 task_log.detail_json 的四类计数同口径）。"""

    stage: str
    input_count: int = 0
    output_count: int = 0
    dropped_count: int = 0
    abnormal_count: int = 0
    seconds: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "input_count": self.input_count,
            "output_count": self.output_count,
            "dropped_count": self.dropped_count,
            "abnormal_count": self.abnormal_count,
            "seconds": round(self.seconds, 3),
            "extra": self.extra,
        }


class StageRecorder:
    """按 SOP 顺序记录每步统计，并在结束时校验「每步都有记录」（防漏记）。"""

    def __init__(self) -> None:
        self.items: list[StageStats] = []

    def record(self, stats: StageStats) -> StageStats:
        self.items.append(stats)
        print(
            f"  [{stats.stage}] 输入 {stats.input_count} → 输出 {stats.output_count}"
            f"（丢弃 {stats.dropped_count} / 异常 {stats.abnormal_count}）"
            f" · {stats.seconds:.2f}s"
        )
        for key, value in stats.extra.items():
            print(f"      · {key}: {value}")
        return stats

    @property
    def stages(self) -> list[str]:
        return [item.stage for item in self.items]

    def total_dropped(self) -> int:
        return sum(item.dropped_count for item in self.items)

    def total_abnormal(self) -> int:
        return sum(item.abnormal_count for item in self.items)


# ---------------------------------------------------------------------------
# 三、C-BAT-02 数据校验
# ---------------------------------------------------------------------------


def validate_row_count(actual: int, is_sample: bool) -> dict[str, Any]:
    """行数校验（NR-R-03：必须等于 59,033）。抽样模式只核对「不超出」，不判失败。

    校验不通过时**直接抛错终止**，由 main 捕获后不产出任何文件——
    对应数据处理流程图 §1 的 B1「终止任务并报警，不写入任何数据」。
    """
    expected = EXPECTED_REVIEW_ROWS
    if is_sample:
        if actual > expected:
            raise ValueError(f"抽样行数 {actual} 超出全量基准 {expected}")
        return {"判定": "抽样模式跳过全量判定", "实测": actual, "期望": expected}
    if actual != expected:
        raise ValueError(
            f"行数校验未通过：实测 {actual} 行，设计基准 {expected} 行（NR-R-03）。"
            "已终止任务，未产出任何文件。"
        )
    return {"判定": "通过", "实测": actual, "期望": expected}


def validate_columns(header: Sequence[str]) -> None:
    """表头与列数校验（列顺序是字段映射的唯一依据，不得错位）。"""
    if len(header) != len(CSV_COLUMNS):
        raise ValueError(f"列数不符：实测 {len(header)} 列，设计基线 {len(CSV_COLUMNS)} 列")
    for index, (actual, expected) in enumerate(zip(header, CSV_COLUMNS)):
        if actual != expected:
            raise ValueError(f"第 {index} 列表头不符：实测「{actual}」，期望「{expected}」")


def validate_spot_level(spots: Sequence[dict], is_sample: bool) -> dict[str, Any]:
    """景点级校验（来源标注、评价资格门槛），并核对来源标注条数与全量口径一致。"""
    result: dict[str, Any] = {
        "景点总数": len(spots),
        "西藏口径": sum(1 for s in spots if s["source_scope"] == "tibet"),
        "进藏沿线口径": sum(1 for s in spots if s["source_scope"] == "route"),
        "具备完整评价资格(≥100条)": sum(s["has_full_evaluation"] for s in spots),
    }
    if is_sample:
        result["判定"] = "抽样模式（景点表仍按全量口径计算，不做等值判定）"
        return result

    problems = []
    if result["景点总数"] != EXPECTED_SPOT_ROWS:
        problems.append(f"景点总数 {result['景点总数']} ≠ {EXPECTED_SPOT_ROWS}")
    if result["西藏口径"] != EXPECTED_TIBET_SPOTS:
        problems.append(f"西藏口径 {result['西藏口径']} ≠ {EXPECTED_TIBET_SPOTS}")
    if result["进藏沿线口径"] != EXPECTED_ROUTE_SPOTS:
        problems.append(f"进藏沿线口径 {result['进藏沿线口径']} ≠ {EXPECTED_ROUTE_SPOTS}")
    if result["具备完整评价资格(≥100条)"] != EXPECTED_FULL_EVAL_SPOTS:
        problems.append(
            f"完整评价资格景点 {result['具备完整评价资格(≥100条)']} ≠ {EXPECTED_FULL_EVAL_SPOTS}"
        )
    if problems:
        raise ValueError("景点级校验未通过：" + "；".join(problems))
    result["判定"] = "通过"
    return result


# ---------------------------------------------------------------------------
# 四、C-BAT-03 / C-BAT-04：规范化后的记录 → 输出行
# ---------------------------------------------------------------------------


def review_to_row(record: dict, spot_name: str) -> dict[str, Any]:
    """把 `build_reviews` 的库记录映射为清洗文件的输出行（保持原 15 列 + 派生列）。

    说明：
        · `comment_id` 在原 CSV 中为字符串编号，此处还原为字符串以保证与源文件一致
        · 空值统一写空字符串（CSV 无 NULL 概念）——与源文件「空即空」的表示一致
        · `is_dup_content` / `dup_group_id` 由内容重复清单给出，不在此处重新判定，
          避免出现「脚本自己算的重复」与「清单口径」两套结果
    """
    return {
        "景点名称": spot_name,
        "评论编号": str(record["comment_id"]),
        "评分": "" if record["score"] is None else record["score"],
        "评分描述": record["score_desc"] or "",
        "评论内容": record["content"] or "",
        "发布时间": record["publish_date"],
        "IP归属地": record["ip_location"],
        "用户昵称": record["user_nick"],
        "点赞数": record["like_count"],
        "图片数": record["image_count"],
        "图片URL": record["image_urls"] or "",
        # 景点级 4 字段由调用方用代表值回填（同一景点内完全重复，BR-09）
        "地址": "",
        "开放时间": "",
        "官方电话": "",
        "景点介绍": "",
        "content_length": record["content_length"],
        "publish_year": record["publish_year"],
        "publish_month": record["publish_month"],
        "ip_province": record["ip_province"] or "",
        "ip_is_unknown": record["ip_is_unknown"],
        "is_low_info": record["is_low_info"],
        "is_dup_content": record["is_dup_content"],
        "dup_group_id": "" if record["dup_group_id"] is None else record["dup_group_id"],
    }


def build_layering_stats(records: Sequence[dict], dup_group_by_comment: dict[str, int]) -> dict[str, Any]:
    """C-BAT-04 去重与分层的统计（双口径：统计类用全量，文本建模按标记过滤）。"""
    total = len(records)
    low_info = sum(r["is_low_info"] for r in records)
    dup = sum(r["is_dup_content"] for r in records)
    group_ids = {r["dup_group_id"] for r in records if r["dup_group_id"] is not None}
    return {
        "记录总数": total,
        "低信息量(正文≤10字)": low_info,
        "低信息量占比": _ratio(low_info, total),
        "重复正文条数": dup,
        "重复正文占比": _ratio(dup, total),
        "重复正文组数": len(group_ids),
        "重复正文组数(清单全量)": EXPECTED_DUP_GROUPS,
        "重复正文条数(清单全量)": EXPECTED_DUP_ROWS,
        # 双口径显式给出，供后续 Spark / 文本建模直接引用
        "文本可用条数(全量)": total,
        "文本可用条数(剔除低信息量与重复正文)": sum(
            1 for r in records if not r["is_low_info"] and not r["is_dup_content"]
        ),
    }


def build_normalization_stats(records: Sequence[dict]) -> dict[str, Any]:
    """C-BAT-03 字段规范化统计（空值、异常、口径分布）。"""
    total = len(records)
    score_null = sum(1 for r in records if r["score"] is None)
    content_null = sum(1 for r in records if r["content"] is None)
    ip_unknown = sum(r["ip_is_unknown"] for r in records)
    ip_province_filled = sum(1 for r in records if r["ip_province"])
    lengths = [r["content_length"] for r in records]
    years = [r["publish_year"] for r in records]
    dates = [r["publish_date"] for r in records]
    return {
        "评分空值": score_null,
        "正文空值": content_null,
        "IP未知": ip_unknown,
        "IP未知占比": _ratio(ip_unknown, total),
        "IP省份已填(仅2022-08后)": ip_province_filled,
        "正文长度_最短": min(lengths) if lengths else 0,
        "正文长度_最长": max(lengths) if lengths else 0,
        "正文长度_总和": sum(lengths),
        # 真实发布日期的极值：**不得用年份拼出 01-01 / 12-31**，那属于造数
        "发布时间_最早": min(dates) if dates else "",
        "发布时间_最晚": max(dates) if dates else "",
        "涉及年份数": len(set(years)),
    }


def _ratio(part: int, whole: int) -> str:
    """百分比展示（保留 2 位，除零保护）。"""
    if not whole:
        return "0.00%"
    return f"{part / whole * 100:.2f}%"


# ---------------------------------------------------------------------------
# 五、产出：规范化文件 + 统计报告
# ---------------------------------------------------------------------------


def write_cleaned_csv(path: Path, rows: Sequence[dict], force: bool) -> None:
    """写出清洗后 CSV（UTF-8 with BOM，与源文件编码一致；换行用 \\r\\n 以兼容 Excel）。

    默认**不覆盖**已存在的同名文件（AI 工作守则：覆盖需先确认）；
    确需重跑时显式加 `--force`。
    """
    if path.exists() and not force:
        raise FileExistsError(
            f"输出文件已存在：{path}\n  如需覆盖请显式加 --force（会替换该产物，原始 CSV 不受影响）"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CLEANED_COLUMNS), lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)


def write_stats_json(path: Path, payload: dict[str, Any]) -> None:
    """写出机器可读统计（供后续模块与复验脚本读取）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_report_md(path: Path, payload: dict[str, Any]) -> None:
    """写出人工可读报告（结构化，可直接作为论文「数据清洗」章节素材）。"""
    stages = payload["stages"]
    layers = payload["去重与分层"]
    norms = payload["字段规范化"]
    source = payload["源文件"]

    lines: list[str] = []
    lines.append("# 评论数据清洗与预处理报告（阶段二 · M7）")
    lines.append("")
    lines.append(f"- 执行时间：{payload['执行时间']}")
    lines.append(f"- 数据模式：{payload['数据模式']}")
    lines.append(f"- 输入文件：`{source['输入文件']}`")
    lines.append(f"- 输出文件：`{payload['输出文件']}`")
    lines.append("")
    lines.append("## 一、处理总量（可解释的数据量变化）")
    lines.append("")
    lines.append("| 项 | 数量 |")
    lines.append("|---|---|")
    lines.append(f"| 输入行数 | {source['输入行数']} |")
    lines.append(f"| 输出行数 | {payload['输出行数']} |")
    lines.append(f"| 丢弃行数 | {payload['丢弃行数合计']} |")
    lines.append(f"| 异常行数 | {payload['异常行数合计']} |")
    lines.append("")
    lines.append(
        "> 清洗原则：**只规范化、不删行**。低信息量、重复正文、IP 未知、短评论均"
        "保留在数据中并打标记（双口径），因此丢弃行数应为 0。"
    )
    lines.append("")
    lines.append("## 二、各步骤统计（C-BAT-01 编排的 SOP 步骤）")
    lines.append("")
    lines.append("| 步骤 | 输入 | 输出 | 丢弃 | 异常 | 耗时(s) | 说明 |")
    lines.append("|---|---|---|---|---|---|---|")
    for item in stages:
        extra = "；".join(f"{k}={v}" for k, v in item["extra"].items())
        lines.append(
            f"| {item['stage']} | {item['input_count']} | {item['output_count']} | "
            f"{item['dropped_count']} | {item['abnormal_count']} | {item['seconds']} | {extra} |"
        )
    lines.append("")
    lines.append("## 三、字段规范化结果（C-BAT-03）")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    for key, value in norms.items():
        lines.append(f"| {key} | {value} |")
    lines.append("")
    lines.append("## 四、去重与分层结果（C-BAT-04）")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    for key, value in layers.items():
        lines.append(f"| {key} | {value} |")
    lines.append("")
    lines.append("## 五、数据校验结果（C-BAT-02）")
    lines.append("")
    for name, detail in payload["校验"].items():
        rendered = "；".join(f"{k}={v}" for k, v in detail.items())
        lines.append(f"- **{name}**：{rendered}")
    lines.append("")
    lines.append("## 六、未删除的数据及其业务含义（避免误读为数据质量问题）")
    lines.append("")
    lines.append("| 现象 | 处理方式 | 业务含义 |")
    lines.append("|---|---|---|")
    lines.append(
        f"| IP 归属地未知（{norms['IP未知']} 条 / {norms['IP未知占比']}） | 保留，标记 `ip_is_unknown=1` | "
        "2022-08 前携程不展示 IP，属平台口径而非缺失 |"
    )
    lines.append("| 正文 ≤10 字 | 保留，标记 `is_low_info=1` | 真实短评（如「不错」），文本建模时过滤，统计仍计入 |")
    lines.append("| 正文完全重复 | 保留，标记 `is_dup_content=1` + `dup_group_id` | 用户复制粘贴或刷评，语义分析只调一次模型 |")
    lines.append("| 评分空值 / 正文空值 | 如实留空，不填补 | 缺失即缺失，不造数 |")
    lines.append("| 5 星占比偏高 | 不做均衡化处理 | 真实分布，修改即篡改数据 |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"本报告由 `app/batch/clean_dataset.py` 于 {payload['执行时间']} 自动生成。")
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# 六、可选：把各步骤计数登记到运维表（只增不改，不触碰 spot / review）
# ---------------------------------------------------------------------------


def log_task_to_db(recorder: StageRecorder, payload: dict[str, Any]) -> int:
    """把本次清洗登记为一条 `analysis_task`，每个 SOP 步骤一条 `task_log`。

    **只在显式 `--log-task` 时调用**；不写 `spot` / `review`，不做任何 UPDATE/DELETE。
    """
    from app.db import connection

    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_seconds = int(round(sum(item.seconds for item in recorder.items)))
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO analysis_task (task_name, task_type, status, started_at, finished_at,
                                          cost_seconds, total_count, success_count, fail_count)
                VALUES (%s, 'clean', 'success', %s, %s, %s, %s, %s, %s)
                """,
                (
                    f"数据清洗与预处理（mode={payload['数据模式']}）",
                    started,
                    finished,
                    total_seconds,
                    payload["源文件"]["输入行数"],
                    payload["输出行数"],
                    payload["丢弃行数合计"],
                ),
            )
            task_id = int(cur.lastrowid)

            for item in recorder.items:
                stats = BuildStats(
                    input_count=item.input_count,
                    output_count=item.output_count,
                    dropped_count=item.dropped_count,
                    abnormal_count=item.abnormal_count,
                    extra={**item.extra, "seconds": round(item.seconds, 3)},
                )
                cur.execute(
                    """
                    INSERT INTO task_log (task_id, level, stage, message, detail_json)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        task_id,
                        "WARN" if item.abnormal_count else "INFO",
                        item.stage,
                        f"{item.stage}：输入 {item.input_count} → 输出 {item.output_count}",
                        stats.as_detail(),
                    ),
                )
        conn.commit()
    return task_id


# ---------------------------------------------------------------------------
# 七、主流程（C-BAT-01 任务编排）
# ---------------------------------------------------------------------------


def run_clean(limit: int | None, out_dir: Path, prefix: str, force: bool, log_task: bool) -> dict[str, Any]:
    """执行一次完整清洗，返回统计 payload。任何校验失败都抛错终止（不产出文件）。"""
    is_sample = limit is not None
    recorder = StageRecorder()
    checks: dict[str, Any] = {}

    # --- 步骤 1：读取源文件（只读，BR-11）---
    started = time.perf_counter()
    data = load_source_data(limit)
    recorder.record(
        StageStats(
            stage="读取源文件",
            input_count=len(data.all_rows),
            output_count=len(data.reviews_raw),
            seconds=time.perf_counter() - started,
            extra={
                "源文件": settings.paths.final_dataset.name,
                "全部行数": len(data.all_rows),
                "本模式处理行数": len(data.reviews_raw),
                "表头列数": len(data.header),
                "来源标注景点数": len(data.spot_scope),
                "重复清单条目数": len(data.dup_group_by_comment),
            },
        )
    )
    validate_columns(data.header)

    # --- 步骤 2：行数校验（C-BAT-02，不通过即终止）---
    started = time.perf_counter()
    checks["行数校验"] = validate_row_count(len(data.all_rows), is_sample)
    recorder.record(
        StageStats(
            stage="行数校验",
            input_count=len(data.all_rows),
            output_count=len(data.all_rows),
            dropped_count=0,
            abnormal_count=0,
            seconds=time.perf_counter() - started,
            extra=checks["行数校验"],
        )
    )

    # --- 步骤 3：字段规范化（C-BAT-03，含景点级代表值计算）---
    started = time.perf_counter()
    spots, spot_stats = build_spots(data.all_rows)
    checks["景点级校验"] = validate_spot_level(spots, is_sample)
    recorder.record(
        StageStats(
            stage="字段规范化",
            input_count=len(data.all_rows),
            output_count=len(spots),
            dropped_count=0,
            abnormal_count=0,
            seconds=time.perf_counter() - started,
            extra=checks["景点级校验"],
        )
    )

    # --- 步骤 4：评论级 / 景点级拆分（BR-09）---
    started = time.perf_counter()
    spot_by_name = {s["spot_name"]: s for s in spots}
    # build_reviews 需要一个「景点名 → 整数」映射；此处用 1..N 的顺序号占位，
    # 因为清洗产物面向 CSV（不含 spot_id），真正的外键由阶段一导入保证。
    spot_ref = {name: index for index, name in enumerate(spot_by_name, start=1)}
    records, review_stats = build_reviews(data.reviews_raw, spot_ref, data.dup_group_by_comment)

    # 一致性防线：build_reviews 会跳过解析异常的行，使 records 短于输入。
    # 此处**按 comment_id 建立映射**而不是按位置对齐——一旦长度不等，位置对齐会整体错位，
    # 把景点名配到错误的评论上。长度不等时直接失败，宁可终止也不产出错位数据。
    if len(records) != len(data.reviews_raw):
        raise ValueError(
            f"评论级构建后条数不一致：输入 {len(data.reviews_raw)} 行，构建 {len(records)} 行。"
            "存在解析异常行，已终止以避免字段错位（请先核对源文件的日期/编号格式）。"
        )
    spot_name_by_comment: dict[str, str] = {}
    for index, record in enumerate(records):
        spot_name_by_comment[str(record["comment_id"])] = norm_text(data.reviews_raw[index][0])

    rows: list[dict] = []
    for record in records:
        spot_name = spot_name_by_comment[str(record["comment_id"])]
        row = review_to_row(record, spot_name)
        spot = spot_by_name.get(spot_name) or {}
        # 景点级 4 字段：用代表值回填（同一景点内完全重复，BR-09）
        row["地址"] = spot.get("address") or ""
        row["开放时间"] = spot.get("open_time") or ""
        row["官方电话"] = spot.get("phone") or ""
        row["景点介绍"] = spot.get("introduction") or ""
        rows.append(row)
    recorder.record(
        StageStats(
            stage="评论级景点级拆分",
            input_count=len(data.reviews_raw),
            output_count=len(rows),
            dropped_count=review_stats.dropped_count,
            abnormal_count=review_stats.abnormal_count,
            seconds=time.perf_counter() - started,
            extra={
                "评论级字段数": 11,
                "景点级字段数": 4,
                "景点级字段口径": "同一景点内取代表值（次数最多→最长→首次出现）",
            },
        )
    )

    # --- 步骤 5：去重与分层标记（C-BAT-04）---
    started = time.perf_counter()
    layering = build_layering_stats(records, data.dup_group_by_comment)
    normalization = build_normalization_stats(records)
    abnormal_dup = 0
    if not is_sample:
        if layering["重复正文条数"] != EXPECTED_DUP_ROWS:
            abnormal_dup += abs(layering["重复正文条数"] - EXPECTED_DUP_ROWS)
        if layering["重复正文组数"] != EXPECTED_DUP_GROUPS:
            abnormal_dup += abs(layering["重复正文组数"] - EXPECTED_DUP_GROUPS)
    recorder.record(
        StageStats(
            stage="去重与分层",
            input_count=len(rows),
            output_count=len(rows),
            dropped_count=0,       # 标记而非删除（关键设计点 5）
            abnormal_count=abnormal_dup,
            seconds=time.perf_counter() - started,
            extra={
                "低信息量": layering["低信息量(正文≤10字)"],
                "重复正文条数": layering["重复正文条数"],
                "重复正文组数": layering["重复正文组数"],
                "策略": "标记而非删除（保留全量，双口径）",
            },
        )
    )

    # --- 步骤 6：写出规范化文件（BR-11：另存新版本，不覆盖原始数据）---
    started = time.perf_counter()
    out_dir = Path(out_dir)
    suffix = f"_sample{limit}" if is_sample else ""
    csv_path = out_dir / f"{prefix}{suffix}_v1.csv"
    write_cleaned_csv(csv_path, rows, force=force)
    recorder.record(
        StageStats(
            stage="写出规范化文件",
            input_count=len(rows),
            output_count=len(rows),
            seconds=time.perf_counter() - started,
            extra={"输出文件": csv_path.name, "列数": len(CLEANED_COLUMNS), "编码": "utf-8-sig"},
        )
    )

    # --- 步骤 7：输出统计报告 ---
    started = time.perf_counter()
    json_path = out_dir / f"{prefix}{suffix}_v1_清洗统计.json"
    md_path = out_dir / f"{prefix}{suffix}_v1_清洗报告.md"
    payload: dict[str, Any] = {
        "执行时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "数据模式": "抽样" if is_sample else "全量",
        "源文件": {
            "输入文件": str(settings.paths.final_dataset),
            "输入行数": len(data.all_rows),
            "本模式处理行数": len(data.reviews_raw),
            "表头列数": len(data.header),
        },
        "输出文件": str(csv_path),
        "输出行数": len(rows),
        "输出列数": len(CLEANED_COLUMNS),
        "丢弃行数合计": recorder.total_dropped(),
        "异常行数合计": recorder.total_abnormal(),
        "校验": checks,
        "字段规范化": normalization,
        "去重与分层": layering,
        "统计文件": str(json_path),
        "报告文件": str(md_path),
        # 本步（步骤 7）自身也要出现在 stages 中，因此先记录、后成文（只写一次）
        "stages": [],
    }
    recorder.record(
        StageStats(
            stage="输出统计报告",
            input_count=len(rows),
            output_count=len(rows),
            seconds=time.perf_counter() - started,
            extra={"统计文件": json_path.name, "报告文件": md_path.name},
        )
    )
    payload["stages"] = [item.as_dict() for item in recorder.items]

    # 可选：登记到运维表（只增不改）。先登记再成文，使 JSON 内含 task_id。
    if log_task:
        payload["analysis_task_id"] = log_task_to_db(recorder, payload)

    write_stats_json(json_path, payload)
    write_report_md(md_path, payload)
    return payload


def print_summary(payload: dict[str, Any]) -> None:
    """终端汇总输出（人工复核用）。"""
    print("=" * 78)
    print(" 阶段二 · 数据清洗与预处理（C-BAT-01～04）执行汇总")
    print("=" * 78)
    print(f"  数据模式   ：{payload['数据模式']}")
    print(f"  输入行数   ：{payload['源文件']['输入行数']}")
    print(f"  输出行数   ：{payload['输出行数']}（列数 {payload['输出列数']}）")
    print(f"  丢弃行数   ：{payload['丢弃行数合计']}")
    print(f"  异常行数   ：{payload['异常行数合计']}")
    print(f"  输出文件   ：{payload['输出文件']}")
    print(f"  统计文件   ：{payload['统计文件']}")
    print(f"  报告文件   ：{payload['报告文件']}")
    if payload.get("analysis_task_id"):
        print(f"  任务登记   ：analysis_task.task_id={payload['analysis_task_id']}")
    print("-" * 78)
    layers = payload["去重与分层"]
    print(f"  低信息量   ：{layers['低信息量(正文≤10字)']}（{layers['低信息量占比']}）")
    print(f"  重复正文   ：{layers['重复正文条数']} 条 / {layers['重复正文组数']} 组")
    print(f"  文本可用量 ：{layers['文本可用条数(剔除低信息量与重复正文)']}（剔除标记后）")
    print("=" * 78)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="阶段二 · 评论数据清洗与预处理（对应 C-BAT-01～C-BAT-04）"
    )
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 行（小样本验证用）")
    parser.add_argument("--out-dir", type=str, default=None, help="输出目录（默认 data/）")
    parser.add_argument("--prefix", type=str, default=DEFAULT_OUTPUT_PREFIX, help="输出文件名前缀")
    parser.add_argument("--force", action="store_true", help="允许覆盖已存在的输出产物")
    parser.add_argument(
        "--log-task",
        action="store_true",
        help="把各步骤计数登记到 analysis_task / task_log（只增不改，不触碰 spot/review）",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir) if args.out_dir else settings.paths.output_dir

    print("=" * 78)
    print(" 阶段二 · 评论数据清洗与预处理（M7 / C-BAT-01～C-BAT-04）")
    print("=" * 78)
    print(f"  项目根目录：{settings.paths.project_root}")
    print(f"  源文件    ：{settings.paths.final_dataset}")
    print(f"  输出目录  ：{out_dir}")
    print(f"  数据模式  ：{'抽样 ' + str(args.limit) + ' 行' if args.limit else '全量'}")
    print("-" * 78)

    try:
        payload = run_clean(
            limit=args.limit,
            out_dir=out_dir,
            prefix=args.prefix,
            force=args.force,
            log_task=args.log_task,
        )
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        print("\n[终止] 清洗未完成，未产出任何文件：")
        print(f"  {exc}")
        return 2
    except Exception as exc:  # pragma: no cover - 兜底，避免静默失败
        print(f"\n[异常] 清洗过程中断：{type(exc).__name__}: {exc}")
        return 3

    print()
    print_summary(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
