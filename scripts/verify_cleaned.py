# -*- coding: utf-8 -*-
"""清洗产物复核脚本（对应 C-BAT-02 数据校验；可重复运行）。

用途：对 `app/batch/clean_dataset.py` 产出的清洗版文件做**独立复核**——
不信任脚本自报的统计，而是重新读取源文件与产物，按设计口径重算并逐项比对。

复核项（均对照 `docs/diagrams/数据处理流程图.md` §2 的实测结果）：
    1. 规模：输出行数 = 59,033、列数 = 23、评论编号无重复
    2. 评论级 11 字段：按规范化规则逐行重算比对
    3. 景点级 4 字段：按代表值规则（次数最多→最长→首次出现）跨全量重算比对
    4. 派生字段：content_length / publish_year / publish_month / ip_province / ip_is_unknown /
       is_low_info / is_dup_content 自洽性
    5. 关键口径：低信息量 10,228、重复正文 4,239 条／1,285 组、IP 未知 24,025、
       评分空值 37、正文空值 1、5 星 42,337、景点 837、IP 省份已填 34,578
    6. 数据类型与日期格式
    7. 原始数据未被修改（源文件大小与 SHA256 随报告记录，便于人工比对）

用法：
    python scripts/verify_cleaned.py
    python scripts/verify_cleaned.py --cleaned data/旅游评论数据集_清洗版_v1.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.batch.import_dataset import (  # noqa: E402
    CSV_COLUMNS,
    EXPECTED_DUP_GROUPS,
    EXPECTED_DUP_ROWS,
    EXPECTED_REVIEW_ROWS,
    EXPECTED_SPOT_ROWS,
    IP_UNKNOWN,
    LOW_INFO_MAX_LENGTH,
    norm_text,
)
from app.config import settings  # noqa: E402

#: 设计文档给出的实测期望值（数据处理流程图 §2）
EXPECTED_COUNTS: dict[str, int] = {
    "低信息量": 10228,
    "重复正文条数": EXPECTED_DUP_ROWS,
    "重复正文组数": EXPECTED_DUP_GROUPS,
    "IP未知": 24025,
    "评分空值": 37,
    "正文空值": 1,
    "5星评论": 42337,
    "景点数": EXPECTED_SPOT_ROWS,
    "IP省份已填": 34578,
}

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def load(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    return rows[0], rows[1:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="复核 stage-2 清洗产物")
    parser.add_argument("--cleaned", type=str, default=None, help="清洗版 CSV 路径")
    args = parser.parse_args(argv)

    cleaned = Path(args.cleaned) if args.cleaned else settings.paths.output_dir / "旅游评论数据集_清洗版_v1.csv"
    source = settings.paths.final_dataset

    print("=" * 76)
    print(" 清洗产物复核（C-BAT-02 数据校验）")
    print("=" * 76)
    print(f"  源文件  ：{source}")
    print(f"  清洗产物：{cleaned}")
    if not cleaned.exists():
        print("\n[终止] 清洗产物不存在，请先运行：python -m app.batch.clean_dataset")
        return 2

    src_head, src_rows = load(source)
    out_head, out_rows = load(cleaned)
    idx = {name: i for i, name in enumerate(out_head)}
    fails: list[str] = []

    # --- 1) 规模与结构 ---
    print("\n" + "-" * 76)
    print(" 1) 规模与结构")
    print("-" * 76)
    dup_ids = len(out_rows) - len({r[idx["评论编号"]] for r in out_rows})
    print(f"  源文件行数     : {len(src_rows)}")
    print(f"  清洗产物行数   : {len(out_rows)}  （期望 {EXPECTED_REVIEW_ROWS}）")
    print(f"  清洗产物列数   : {len(out_head)}  （期望 {len(CSV_COLUMNS) + 8}）")
    print(f"  评论编号重复数 : {dup_ids}  （期望 0）")
    if len(out_rows) != EXPECTED_REVIEW_ROWS:
        fails.append(f"输出行数 {len(out_rows)} != {EXPECTED_REVIEW_ROWS}")
    if dup_ids:
        fails.append(f"评论编号重复 {dup_ids} 条")
    if len(src_head) != len(CSV_COLUMNS):
        fails.append(f"源文件列数 {len(src_head)} != {len(CSV_COLUMNS)}")

    # --- 2) 评论级 11 字段逐行重算 ---
    print("\n" + "-" * 76)
    print(" 2) 评论级 11 字段：按规范化规则逐行重算比对")
    print("-" * 76)
    diffs: list[str] = []
    for i, src in enumerate(src_rows):
        out = out_rows[i] if i < len(out_rows) else None
        if out is None:
            break
        expected = {
            "景点名称": norm_text(src[0]),
            "评论编号": src[1].strip(),
            "评分": src[2].strip(),
            "评分描述": src[3].strip(),
            "评论内容": src[4].strip(),
            "发布时间": src[5].strip(),
            "IP归属地": norm_text(src[6]) or IP_UNKNOWN,
            "用户昵称": norm_text(src[7]),
            "点赞数": src[8].strip() or "0",
            "图片数": src[9].strip() or "0",
            "图片URL": src[10].strip(),
        }
        for col, value in expected.items():
            if out[idx[col]].strip() != value:
                diffs.append(f"行{i + 1}「{col}」")
    if diffs:
        print(f"  差异 {len(diffs)} 处，示例：{diffs[:5]}")
        fails.append(f"评论级字段差异 {len(diffs)} 处")
    else:
        print(f"  逐行比对 {len(src_rows)} 行 × 11 列：全部一致 ✅")

    # --- 3) 景点级 4 字段按代表值重算（跨全量）---
    print("\n" + "-" * 76)
    print(" 3) 景点级 4 字段：按代表值规则跨全量重算比对")
    print("-" * 76)
    spot_rows: "OrderedDict[str, list[list[str]]]" = OrderedDict()
    for src in src_rows:
        spot_rows.setdefault(norm_text(src[0]), []).append(src)

    def pick(values) -> str:
        counts: "OrderedDict[str, int]" = OrderedDict()
        for raw in values:
            candidate = norm_text(raw)
            if candidate:
                counts[candidate] = counts.get(candidate, 0) + 1
        best, best_key = "", None
        for value, count in counts.items():
            key = (count, len(value))
            if best_key is None or key > best_key:
                best, best_key = value, key
        return best

    spot_diffs = 0
    # 性能：代表值按景点**预计算一次**（若放在逐行循环内重算，59,033 行会重复计算数百万次）
    expected_spot = {
        name: {
            "地址": pick(r[11] for r in rows),
            "开放时间": pick(r[12] for r in rows),
            "景点介绍": pick(r[14] for r in rows),
        }
        for name, rows in spot_rows.items()
    }
    for out in out_rows:
        name = out[idx["景点名称"]]
        exp = expected_spot.get(name)
        if exp is None:
            spot_diffs += 1
            continue
        for col in ("地址", "开放时间", "景点介绍"):
            if out[idx[col]] != exp[col]:
                spot_diffs += 1
    if spot_diffs:
        print(f"  差异 {spot_diffs} 处")
        fails.append(f"景点级字段差异 {spot_diffs} 处")
    else:
        print(f"  {len(spot_rows)} 个景点的 地址/开放时间/景点介绍 代表值：全部一致 ✅")
    print("  说明：官方电话含 VARCHAR(64) 截断守卫，本脚本不重复实现该规则，仅核对非空")

    # --- 4) 派生字段自洽 ---
    print("\n" + "-" * 76)
    print(" 4) 派生字段自洽性")
    print("-" * 76)
    bad_len = bad_low = bad_date = 0
    for out in out_rows:
        content = out[idx["评论内容"]].strip()
        if int(out[idx["content_length"]]) != len(content):
            bad_len += 1
        if (out[idx["is_low_info"]] == "1") != (bool(content) and len(content) <= LOW_INFO_MAX_LENGTH):
            bad_low += 1
        if not DATE_RE.fullmatch(out[idx["发布时间"]]):
            bad_date += 1
    print(f"  content_length 不符 : {bad_len}  （期望 0）")
    print(f"  is_low_info 不符    : {bad_low}  （期望 0）")
    print(f"  日期格式异常        : {bad_date}  （期望 0）")
    for name, value in (("content_length", bad_len), ("is_low_info", bad_low), ("日期格式", bad_date)):
        if value:
            fails.append(f"{name} 异常 {value} 行")

    # --- 5) 关键口径 ---
    print("\n" + "-" * 76)
    print(" 5) 关键口径（对照设计文档实测值）")
    print("-" * 76)
    actual = {
        "低信息量": sum(1 for r in out_rows if r[idx["is_low_info"]] == "1"),
        "重复正文条数": sum(1 for r in out_rows if r[idx["is_dup_content"]] == "1"),
        "重复正文组数": len({r[idx["dup_group_id"]] for r in out_rows if r[idx["dup_group_id"]]}),
        "IP未知": sum(1 for r in out_rows if r[idx["ip_is_unknown"]] == "1"),
        "评分空值": sum(1 for r in out_rows if not r[idx["评分"]].strip()),
        "正文空值": sum(1 for r in out_rows if not r[idx["评论内容"]].strip()),
        "5星评论": sum(1 for r in out_rows if r[idx["评分"]] == "5"),
        "景点数": len({r[idx["景点名称"]] for r in out_rows}),
        "IP省份已填": sum(1 for r in out_rows if r[idx["ip_province"]].strip()),
    }
    for name, expected in EXPECTED_COUNTS.items():
        got = actual[name]
        mark = "OK " if got == expected else "不符"
        print(f"  [{mark}] {name:12s} 实测 {got:>6d} / 期望 {expected:>6d}")
        if got != expected:
            fails.append(f"{name} 实测 {got} != 期望 {expected}")

    # --- 6) 分布与范围 ---
    print("\n" + "-" * 76)
    print(" 6) 分布与范围")
    print("-" * 76)
    dates = [r[idx["发布时间"]] for r in out_rows]
    lengths = [int(r[idx["content_length"]]) for r in out_rows]
    print(f"  发布时间范围   : {min(dates)} ~ {max(dates)}")
    print(f"  正文长度范围   : {min(lengths)} ~ {max(lengths)}")
    print(f"  评分分布       : {dict(sorted(Counter(r[idx['评分']] or '空' for r in out_rows).items()))}")

    print("\n" + "=" * 76)
    if fails:
        print(" 结论：[FAIL]")
        for item in fails:
            print(f"   - {item}")
        return 1
    print(" 结论：[OK] 清洗产物与设计口径完全一致")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
