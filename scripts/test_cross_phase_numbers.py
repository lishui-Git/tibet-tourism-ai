# -*- coding: utf-8 -*-
"""跨阶段数字自洽：阶段二（清洗）与阶段四（调用计划）的口径必须都对得上。

## 为什么需要它
项目里有**两个容易混淆的"可用评论数"**，直接相减就会得出"文档互相矛盾"的错误结论：

| 口径 | 数值 | 含义 |
|---|---|---|
| 阶段二 `文本可用条数`（清洗统计 JSON） | **47,149** | 既非低信息量、也非重复正文 |
| 阶段四 `调用层总量`（preflight） | **47,733** | 低信息量排除；重复组**代表仍要调用一次** |

两者相差 **584**，恰恰是"非低信息量的重复组代表数"。
本测试把这三件事都固定下来，避免以后误读：
    ① 用库里的标记位复核阶段二的 47,149（含**重叠**修正）；
    ② 复核阶段四的调用层 47,733 与各分层计数；
    ③ 断言两个口径的差 = 重复组代表数（非低信息量部分）。

同时验证 `data/` 下阶段二产物与数据库一致（59,033 行 / 837 景点 / 57 个合格景点）。

全程只读，不调用任何模型。
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATS_JSON = ROOT / "data" / "旅游评论数据集_清洗版_v1_清洗统计.json"
CLEAN_CSV = ROOT / "data" / "旅游评论数据集_清洗版_v1.csv"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    from app.db import query_one

    def n(sql: str, params: tuple = ()) -> int:
        return int(query_one(sql, params)["n"])

    print("=" * 88)
    print("跨阶段数字自洽：阶段二『文本可用』与阶段四『调用层』")
    print("=" * 88)

    total = n("SELECT COUNT(*) AS n FROM review")
    low = n("SELECT COUNT(*) AS n FROM review WHERE is_low_info=1")
    dup = n("SELECT COUNT(*) AS n FROM review WHERE is_dup_content=1")
    both = n("SELECT COUNT(*) AS n FROM review WHERE is_low_info=1 AND is_dup_content=1")
    usable_flags = n("SELECT COUNT(*) AS n FROM review WHERE is_low_info=0 AND is_dup_content=0")

    print("\n[1] 阶段二口径：用库内标记位复核『文本可用条数』")
    check("标记位自洽：非低信息量且非重复 = 总数 − 低信息量 − 重复 + 重叠",
          usable_flags == total - low - dup + both,
          f"{total:,} − {low:,} − {dup:,} + {both:,} = {total-low-dup+both:,}")
    check("低信息量与重复正文**确有重叠**（所以不能直接相减）", both > 0,
          f"重叠 {both:,} 条（占重复的 {round(both/dup*100,1)}%）")

    if STATS_JSON.exists():
        stats = json.loads(STATS_JSON.read_text(encoding="utf-8"))
        reported = stats["去重与分层"]["文本可用条数(剔除低信息量与重复正文)"]
        check(f"清洗统计 JSON 的『文本可用』= {reported:,} 与库内一致",
              reported == usable_flags, f"JSON {reported:,} vs 库内 {usable_flags:,}")
    else:
        check("清洗统计 JSON 存在（阶段二产物）", False, str(STATS_JSON))

    print("\n[2] 阶段四口径：调用层总量与分层计数")
    empty = n("SELECT COUNT(*) AS n FROM review WHERE content IS NULL OR TRIM(content)=''")
    analyzable = total - empty
    # 关键：分层是**互斥**的（低信息量优先，与是否重复无关）。
    # 因此"重复组的非代表"里，**属于低信息量的那部分已经在 10,228 里**，不能再减一次
    # （首版就是这样多减了，得出 45,850 这个错数——与我在手算时犯的是同一个错）。
    dup_non_rep = dup - n("SELECT COUNT(DISTINCT content) AS n FROM review WHERE is_dup_content=1")
    reps_total = n("SELECT COUNT(DISTINCT content) AS n FROM review WHERE is_dup_content=1")
    reps_low = n(
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT content FROM review WHERE is_dup_content=1 AND is_low_info=1 GROUP BY content"
        ") t"
    )
    reps_non_low = reps_total - reps_low
    # 组内非代表 = 重复条数 − 组数；其中非低信息量的部分才计入"复用层"
    reuse_layer = dup_non_rep - (n("SELECT COUNT(*) AS n FROM review WHERE is_dup_content=1 AND is_low_info=1") - reps_low)
    call_layer = analyzable - low - reuse_layer
    check(f"可分析评论 = {analyzable:,}（正文为空 {empty} 条不参与）", analyzable == 59032, "")
    check(f"复用层（组内非代表且非低信息量）= {reuse_layer:,}", reuse_layer == 1071,
          f"重复条数 {dup:,} − 组数 {reps_total:,} = {dup_non_rep:,}；再剔除低信息量部分")
    check(f"调用层总量 = {call_layer:,}", call_layer == 47733,
          f"{analyzable:,} − 低信息量 {low:,} − 复用层 {reuse_layer:,} = {call_layer:,}")

    print("\n[3] 两个口径的差必须能被完整解释（含那 1 条空正文）")
    # 两个口径对"空正文"的处理不同，这正是那 1 条差异的来源：
    #   · 阶段二『文本可用 47,149』= 既非低信息量、也非重复 —— **包含**空正文那条
    #     （它既不是低信息量、也不重复，所以被算进来了）
    #   · 阶段四『调用层 47,733』  = 从**可分析评论**里算起，**不含**空正文
    # 因此：调用层 − 文本可用 = 非低信息量的重复组代表数 − 空正文(1)
    empty_rows = n("SELECT COUNT(*) AS n FROM review WHERE content IS NULL OR TRIM(content)=''")
    usable_excl_empty = usable_flags - n(
        "SELECT COUNT(*) AS n FROM review "
        "WHERE is_low_info=0 AND is_dup_content=0 AND (content IS NULL OR TRIM(content)='')"
    )
    diff = call_layer - usable_excl_empty
    check(f"剔除空正文后，两口径相差 {diff:,} = 非低信息量的重复组代表数 {reps_non_low:,}",
          diff == reps_non_low, f"{call_layer:,} − {usable_excl_empty:,} = {diff:,}")
    check(f"空正文评论共 {empty_rows} 条，且确实被阶段二计入『文本可用』、被阶段四排除",
          empty_rows == 1 and usable_flags == usable_excl_empty + 1,
          f"usable {usable_flags:,} vs 剔除空正文 {usable_excl_empty:,}")
    check("重复组总数 = 1,285", reps_total == 1285, f"{reps_total:,}")
    check("三层互斥穷尽（低信息量 + 复用层 + 调用层 = 可分析评论）",
          low + reuse_layer + call_layer == analyzable,
          f"{low:,} + {reuse_layer:,} + {call_layer:,} = {low+reuse_layer+call_layer:,} vs {analyzable:,}")
    check("非低信息量的重复组代表数 = 585（与 preflight 的『重复组代表 585』一致）",
          reps_non_low == 585, f"{reps_non_low:,}")

    print("\n[4] 阶段二产物与数据库一致")
    if CLEAN_CSV.exists():
        rows = 0
        with io.open(CLEAN_CSV, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            for _ in reader:
                rows += 1
        check(f"清洗版 CSV 行数 = {rows:,} 与 review 表一致", rows == total, f"CSV {rows:,} vs DB {total:,}")
        check("清洗版 CSV 列数 = 23", len(header) == 23, f"{len(header)} 列")
    else:
        check("清洗版 CSV 存在（阶段二产物）", False, str(CLEAN_CSV))
    check("景点数一致（837）", n("SELECT COUNT(*) AS n FROM spot") == 837, "")
    check("合格景点数一致（57）",
          n("SELECT COUNT(*) AS n FROM spot WHERE has_full_evaluation=1") == 57, "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total_checks = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"跨阶段数字自洽：{passed}/{total_checks} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total_checks:
        print("结论：阶段二的 47,149 与阶段四的 47,733 都对，只是口径不同——")
        print("      剔除空正文后相差 585，正是『非低信息量的重复组代表数』，它们在")
        print("      全量时各调用一次（重复组其余成员复用，不再调用）。")
    print("=" * 88)
    return 0 if passed == total_checks else 1


if __name__ == "__main__":
    raise SystemExit(main())
