# -*- coding: utf-8 -*-
"""最终全量运行前的**零成本**预检（preflight）。

【本模块的唯一纪律】只读数据库、**绝不调用 DeepSeek**。
它存在的目的：在你明确授权全量生产之前，用一条命令回答：
    · 现在要花多少次调用、大概多少钱？
    · 库里的数据是否健康（有无重复结果 / 非法值 / 未解决失败）？
    · 核心表 `spot` / `review` 是否被改动过？
    · 现在能不能安全地开跑（STATUS: READY / BLOCKED）？

【为什么单独成模块】`__main__.py` 的 `check` 是"跑完之后的健康检查"，
本模块是"跑之前的开工检查"，两者关注点不同（后者要算工作量与费用、要比对核心表校验和），
放在一起会让 `check` 变重且难解释。

【成本参数来源】单价与单条 token 全部取自**实测**（32 次真实调用，见 `app/llm/README.md` §8.2），
并允许通过 .env 覆盖；绝不用"拍脑袋"的数字。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app import __version__
from app.db import query_all, query_one

# ---------------------------------------------------------------------------
# 一、基准值（冻结事实，改动必须写明原因）
# ---------------------------------------------------------------------------

# 全量导入完成时（阶段一）的核心表基准：行数 + CRC32 校验和。
# 这两个数字是"核心数据未被改写"的判据：只要它们一致，就能确定
# 后续所有阶段都只**读**了 spot / review，没有污染核心数据。
CORE_TABLE_BASELINE: dict[str, dict[str, Any]] = {
    "spot": {"rows": 837, "crc32": 559167604},
    "review": {"rows": 59033, "crc32": 1060622212},
}

# 实测成本参数（32 次真实调用）：单条平均 480 token，输入/输出 ≈ 73% / 27%
MEASURED_TOKENS_PER_CALL = 480
MEASURED_TOKENS_PER_CALL_MIN = 431
MEASURED_TOKENS_PER_CALL_MAX = 562
INPUT_SHARE = 0.73
OUTPUT_SHARE = 0.27

# 景点评价（C-BAT-07）单次调用的 token 估算：以实测事实包（3,553 字符 ≈ 2,368 token）
# 加 system 约束段（≈330 token）为输入，输出按 schema 上限估算。
REPORT_INPUT_TOKENS = 2_700
REPORT_OUTPUT_TOKENS = 700


@dataclass
class Preflight:
    """预检结果（结构化，便于打印与机器读取）。"""

    counts: dict[str, int] = field(default_factory=dict)
    workload: dict[str, int] = field(default_factory=dict)
    integrity: dict[str, int] = field(default_factory=dict)
    core_tables: list[dict[str, Any]] = field(default_factory=list)
    cost: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)      # 阻断项（BLOCKED）
    warnings: list[str] = field(default_factory=list)    # 提示项（不阻断，但必须知情）

    @property
    def status(self) -> str:
        """READY = 可以安全开跑；BLOCKED = 存在必须先处理的阻断问题。"""
        return "READY" if not self.issues else "BLOCKED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts,
            "workload": self.workload,
            "integrity": self.integrity,
            "core_tables": self.core_tables,
            "cost": self.cost,
            "issues": self.issues,
            "warnings": self.warnings,
            "status": self.status,
        }


# ---------------------------------------------------------------------------
# 二、SQL（全部只读）
# ---------------------------------------------------------------------------

SQL = {
    "review_total": "SELECT COUNT(*) FROM review",
    "spot_total": "SELECT COUNT(*) FROM spot",
    "spot_ge100": "SELECT COUNT(*) FROM spot WHERE has_full_evaluation = 1",
    "empty_content": "SELECT COUNT(*) FROM review WHERE content IS NULL OR TRIM(content) = ''",
    "low_info_total": "SELECT COUNT(*) FROM review WHERE is_low_info = 1",
    "dup_rows": "SELECT COUNT(*) FROM review WHERE is_dup_content = 1",
    "dup_groups": "SELECT COUNT(DISTINCT dup_group_id) FROM review WHERE is_dup_content = 1",
    # 规则层：低信息量且尚无 deepseek 结果
    "rule_pending": """
        SELECT COUNT(*) FROM review r
         WHERE r.is_low_info = 1 AND r.content IS NOT NULL AND TRIM(r.content) <> ''
           AND NOT EXISTS (SELECT 1 FROM sentiment se
                            WHERE se.comment_id = r.comment_id AND se.method = 'deepseek')
    """,
    "rule_done": "SELECT COUNT(*) FROM sentiment WHERE method = 'deepseek' AND is_valid = 1 "
                 "AND comment_id IN (SELECT comment_id FROM review WHERE is_low_info = 1)",
    # 调用层：非低信息量，且（非重复正文 或 是重复组代表），且尚无 deepseek 结果
    "call_pending": """
        SELECT COUNT(*) FROM review r
         WHERE r.is_low_info = 0
           AND (r.is_dup_content = 0 OR r.comment_id IN (
                 SELECT g.comment_id FROM (
                     SELECT comment_id, dup_group_id,
                            ROW_NUMBER() OVER (PARTITION BY dup_group_id
                                ORDER BY is_low_info ASC, content_length DESC, comment_id ASC) AS rn
                       FROM review WHERE is_dup_content = 1 AND dup_group_id IS NOT NULL) g
                  WHERE g.rn = 1))
           AND r.content IS NOT NULL AND TRIM(r.content) <> ''
           AND NOT EXISTS (SELECT 1 FROM sentiment se
                            WHERE se.comment_id = r.comment_id AND se.method = 'deepseek')
    """,
    "dup_representatives_in_pending": """
        SELECT COUNT(*) FROM review r
         WHERE r.is_low_info = 0 AND r.is_dup_content = 1
           AND r.comment_id IN (
                 SELECT g.comment_id FROM (
                     SELECT comment_id, dup_group_id,
                            ROW_NUMBER() OVER (PARTITION BY dup_group_id
                                ORDER BY is_low_info ASC, content_length DESC, comment_id ASC) AS rn
                       FROM review WHERE is_dup_content = 1 AND dup_group_id IS NOT NULL) g
                  WHERE g.rn = 1)
           AND NOT EXISTS (SELECT 1 FROM sentiment se
                            WHERE se.comment_id = r.comment_id AND se.method = 'deepseek')
    """,
    # 复用层：重复组中"非代表"成员且尚无结果
    "reuse_pending": """
        SELECT COUNT(*) FROM review r
          JOIN (SELECT g.dup_group_id, g.comment_id AS representative_id
                  FROM (SELECT comment_id, dup_group_id, is_low_info, content_length,
                               ROW_NUMBER() OVER (PARTITION BY dup_group_id
                                   ORDER BY is_low_info ASC, content_length DESC, comment_id ASC) AS rn
                          FROM review WHERE is_dup_content = 1 AND dup_group_id IS NOT NULL) g
                 WHERE g.rn = 1) rep ON rep.dup_group_id = r.dup_group_id
         WHERE r.is_dup_content = 1 AND r.comment_id <> rep.representative_id
           AND NOT EXISTS (SELECT 1 FROM sentiment se
                            WHERE se.comment_id = r.comment_id AND se.method = 'deepseek')
    """,
    "deepseek_done": "SELECT COUNT(*) FROM sentiment WHERE method = 'deepseek'",
    "deepseek_api_done": "SELECT COUNT(*) FROM sentiment WHERE method = 'deepseek' "
                         "AND JSON_UNQUOTE(JSON_EXTRACT(raw_json, '$.source')) = 'deepseek'",
    "rule_done_count": "SELECT COUNT(*) FROM comment_semantic WHERE source = 'rule'",
    "report_expected": "SELECT COUNT(*) FROM spot WHERE has_full_evaluation = 1",
    "report_done": "SELECT COUNT(*) FROM spot_report",
    "fact_package_done": "SELECT COUNT(*) FROM spot_fact_package",
    "report_pending": """
        SELECT COUNT(*) FROM spot s
         WHERE s.has_full_evaluation = 1
           AND NOT EXISTS (SELECT 1 FROM spot_report sr WHERE sr.spot_id = s.spot_id)
    """,
}

INTEGRITY_SQL = {
    "sentiment_dup_keys": "SELECT COUNT(*) FROM (SELECT comment_id FROM sentiment "
                          "WHERE method='deepseek' GROUP BY comment_id HAVING COUNT(*)>1) t",
    "aspect_dup_keys": "SELECT COUNT(*) FROM (SELECT comment_id, aspect_name FROM aspect "
                       "WHERE method='deepseek' GROUP BY comment_id, aspect_name HAVING COUNT(*)>1) t",
    "illegal_polarity": "SELECT COUNT(*) FROM sentiment WHERE method='deepseek' "
                        "AND polarity NOT IN ('positive','neutral','negative')",
    "illegal_intensity": "SELECT COUNT(*) FROM sentiment WHERE method='deepseek' "
                         "AND intensity IS NOT NULL AND (intensity < 1 OR intensity > 5)",
    "orphan_spot": "SELECT COUNT(*) FROM sentiment se LEFT JOIN spot s ON s.spot_id = se.spot_id "
                   "WHERE se.method='deepseek' AND s.spot_id IS NULL",
    "orphan_review": "SELECT COUNT(*) FROM sentiment se LEFT JOIN review r ON r.comment_id = se.comment_id "
                     "WHERE se.method='deepseek' AND r.comment_id IS NULL",
    "evidence_not_in_content": "SELECT COUNT(*) FROM aspect a JOIN review r ON r.comment_id = a.comment_id "
                              "WHERE a.method='deepseek' AND a.evidence IS NOT NULL AND a.evidence <> '' "
                              "AND LOCATE(a.evidence, r.content) = 0",
    "open_failures": "SELECT COUNT(*) FROM task_log WHERE level='ERROR' AND stage='semantic' AND resolved=0",
    "usage_missing": "SELECT COUNT(*) FROM sentiment WHERE method='deepseek' "
                     "AND JSON_UNQUOTE(JSON_EXTRACT(raw_json,'$.source'))='deepseek' "
                     "AND JSON_EXTRACT(raw_json,'$.usage') IS NULL",
    "semantic_without_sentiment": "SELECT COUNT(*) FROM comment_semantic cs "
                                  "LEFT JOIN sentiment se ON se.comment_id = cs.comment_id AND se.method='deepseek' "
                                  "WHERE se.comment_id IS NULL",
}


def _scalar(sql: str) -> int:
    row = query_one(sql) or {}
    return int(list(row.values())[0] or 0)


def core_table_checks() -> list[dict[str, Any]]:
    """核对 `spot` / `review` 的行数与 CRC32 是否与冻结基准一致。

    `CHECKSUM TABLE` 给出的是 MySQL 计算的整表校验和（CRC32），
    比"抽查几行"更能证明核心数据没有被后续阶段改写。
    """
    results: list[dict[str, Any]] = []
    for name, baseline in CORE_TABLE_BASELINE.items():
        rows = _scalar(f"SELECT COUNT(*) FROM {name}")
        checksum_rows = query_all(f"CHECKSUM TABLE {name}")
        crc = int(checksum_rows[0]["Checksum"]) if checksum_rows and checksum_rows[0].get("Checksum") else 0
        results.append(
            {
                "table": name,
                "rows": rows,
                "expected_rows": baseline["rows"],
                "crc32": crc,
                "expected_crc32": baseline["crc32"],
                "rows_ok": rows == baseline["rows"],
                "crc_ok": crc == baseline["crc32"],
            }
        )
    return results


def estimate_cost(calls: int, tokens_per_call: int) -> float:
    """按实测口径换算金额（元）。单价取自配置，默认 输入 ¥2 / 输出 ¥8 每百万 token。"""
    from app.config import settings

    tokens = calls * tokens_per_call
    return (
        tokens * INPUT_SHARE / 1_000_000 * settings.deepseek.price_input
        + tokens * OUTPUT_SHARE / 1_000_000 * settings.deepseek.price_output
    )


def estimate_report_cost(calls: int) -> float:
    """景点评价（C-BAT-07）单次调用的估算：输入按实测事实包大小，输出按 Schema 上限。"""
    from app.config import settings

    return (
        calls * REPORT_INPUT_TOKENS / 1_000_000 * settings.deepseek.price_input
        + calls * REPORT_OUTPUT_TOKENS / 1_000_000 * settings.deepseek.price_output
    )


def collect() -> Preflight:
    """执行全部只读检查，返回结构化结果。**不调用模型。**"""
    result = Preflight()

    result.counts = {key: _scalar(sql) for key, sql in SQL.items()}

    # 工作量与费用
    call_pending = result.counts["call_pending"]
    report_pending = result.counts["report_pending"]
    tokens_min = MEASURED_TOKENS_PER_CALL_MIN
    tokens_max = MEASURED_TOKENS_PER_CALL_MAX
    tokens_mid = MEASURED_TOKENS_PER_CALL

    report_cost = estimate_report_cost(report_pending)
    result.workload = {
        "comment_api_calls": call_pending,
        "spot_report_api_calls": report_pending,
        "fact_package_api_calls": 0,          # C-BAT-06 纯 SQL，零调用
        "total_api_calls": call_pending + report_pending,
    }
    result.cost = {
        "tokens_per_call_measured": tokens_mid,
        "comment_cost_min_cny": round(estimate_cost(call_pending, tokens_min), 2),
        "comment_cost_max_cny": round(estimate_cost(call_pending, tokens_max), 2),
        "spot_report_cost_cny": round(report_cost, 2),
        "total_cost_min_cny": round(estimate_cost(call_pending, tokens_min) + report_cost, 2),
        "total_cost_max_cny": round(estimate_cost(call_pending, tokens_max) + report_cost, 2),
    }

    result.integrity = {key: _scalar(sql) for key, sql in INTEGRITY_SQL.items()}
    result.core_tables = core_table_checks()

    # ---- 阻断项（BLOCKED 的判据）-------------------------------------------
    # 只有"数据本身有问题、会导致结果不可信"的才阻断；
    # 历史性的口径局限（例如早期调用未落库 usage）只提示，不阻断——否则会永远无法开工。
    blocking_keys = (
        "sentiment_dup_keys",
        "aspect_dup_keys",
        "illegal_polarity",
        "illegal_intensity",
        "orphan_spot",
        "orphan_review",
        "evidence_not_in_content",
        "semantic_without_sentiment",
    )
    issues: list[str] = []
    for key in blocking_keys:
        value = result.integrity.get(key, 0)
        if value:
            issues.append(f"数据完整性异常：{key} = {value}")
    for table in result.core_tables:
        if not table["rows_ok"]:
            issues.append(f"核心表 {table['table']} 行数异常：{table['rows']} ≠ {table['expected_rows']}")
        if not table["crc_ok"]:
            issues.append(
                f"核心表 {table['table']} 校验和不一致：{table['crc32']} ≠ {table['expected_crc32']}"
                "（可能被改动过，全量运行前必须查清）"
            )
    # 事实包 / 评价必须先有评论级语义结果，否则会生成"无证据"的评价
    if result.counts["fact_package_done"] and not result.counts["deepseek_done"]:
        issues.append("存在事实包但没有任何 DeepSeek 语义结果：事实包缺少证据来源")
    result.issues = issues

    # ---- 提示项（不阻断，但必须知情）---------------------------------------
    warnings: list[str] = []
    if result.integrity["usage_missing"]:
        warnings.append(
            f"{result.integrity['usage_missing']} 条真实调用未落库 token 用量"
            "（早期运行时的历史局限，此后每次调用都会写入 raw_json.usage；"
            "费用核对时这部分只能引用当时的运行日志）"
        )
    if result.integrity["open_failures"]:
        warnings.append(
            f"{result.integrity['open_failures']} 条失败记录未解决（task_log.resolved=0）；"
            "全量重跑会自动重试这些评论，无需手工处理"
        )
    if call_pending == 0 and report_pending == 0:
        warnings.append("当前没有待处理的调用：数据可能已经生产完毕，重复运行不会产生费用")
    result.warnings = warnings
    return result


# ---------------------------------------------------------------------------
# 三、打印
# ---------------------------------------------------------------------------


def print_report(preflight: Preflight | None = None) -> dict[str, Any]:
    """打印人类可读的预检报告，并返回结构化结果。

    报告固定包含三行机器可读结论，便于在日志里快速确认：
        REAL API CALLS: 0
        ESTIMATED API CALLS: N
        STATUS: READY / BLOCKED
    """
    pf = preflight or collect()
    counts, workload, integrity, cost = pf.counts, pf.workload, pf.integrity, pf.cost

    print("=" * 78)
    print(f"阶段四 · 最终全量运行预检（零成本，只读数据库）  app {__version__}")
    print("=" * 78)

    print("\n[1] 数据规模")
    print(f"  评论总数              : {counts['review_total']}")
    print(f"  景点总数 / ≥100 条景点 : {counts['spot_total']} / {counts['spot_ge100']}")
    print(f"  正文为空（不参与）     : {counts['empty_content']}")
    print(f"  低信息量 ≤10 字       : {counts['low_info_total']}")
    print(f"  重复正文 条/组        : {counts['dup_rows']} / {counts['dup_groups']}")

    print("\n[2] 三层处理现状（BR-05 / BR-06 的落地情况）")
    print(f"  规则层 已完成         : {counts['rule_done_count']} 条（source='rule'，零 API）")
    print(f"  规则层 待处理         : {counts['rule_pending']} 条")
    print(f"  调用层 已完成 DeepSeek: {counts['deepseek_api_done']} 条（真实 API 结果）")
    print(f"  调用层 待处理         : {counts['call_pending']} 条（其中重复组代表 {counts['dup_representatives_in_pending']} 条）")
    print(f"  复用层 待处理         : {counts['reuse_pending']} 条（复用代表结果，零 API）")

    print("\n[3] 后续两阶段现状")
    print(f"  事实包 spot_fact_package : {counts['fact_package_done']} 行（C-BAT-06，零 API）")
    print(f"  评价 spot_report         : {counts['report_done']} 行 / 待生成 {counts['report_pending']} 行")

    print("\n[4] 预计工作量与费用（按 32 次实测口径：{0} token/条，区间 {1}–{2}）".format(
        cost["tokens_per_call_measured"], MEASURED_TOKENS_PER_CALL_MIN, MEASURED_TOKENS_PER_CALL_MAX
    ))
    print(f"  评论级 API 次数        : {workload['comment_api_calls']}")
    print(f"  景点级 API 次数        : {workload['spot_report_api_calls']}（57 个景点评价）")
    print(f"  事实包 API 次数        : {workload['fact_package_api_calls']}（纯 SQL 聚合）")
    print(f"  API 次数合计           : {workload['total_api_calls']}")
    print(f"  预计费用               : 评论级 ¥{cost['comment_cost_min_cny']}–{cost['comment_cost_max_cny']}"
          f" + 景点级 ¥{cost['spot_report_cost_cny']}"
          f" = 合计 ¥{cost['total_cost_min_cny']}–{cost['total_cost_max_cny']}")

    print("\n[5] 数据完整性")
    bad = {k: v for k, v in integrity.items() if v and k not in ("usage_missing", "open_failures")}
    if bad:
        for key, value in bad.items():
            print(f"  ✗ {key} = {value}")
    else:
        print("  ✓ 无重复结果、无非法枚举、无越界数值、无孤立外键、无不在原文中的证据")

    print("\n[6] 核心数据是否被改动（spot / review 冻结基准）")
    for table in pf.core_tables:
        mark = "✓" if (table["rows_ok"] and table["crc_ok"]) else "✗"
        print(f"  {mark} {table['table']:<7} 行数 {table['rows']}（基准 {table['expected_rows']}）  "
              f"CRC32 {table['crc32']}（基准 {table['expected_crc32']}）")

    print("\n" + "-" * 78)
    print("REAL API CALLS: 0")
    print(f"ESTIMATED API CALLS: {workload['total_api_calls']}")
    print(f"ESTIMATED COST (CNY): {cost['total_cost_min_cny']} - {cost['total_cost_max_cny']}")
    print(f"STATUS: {pf.status}")
    print("-" * 78)
    if pf.warnings:
        print("\n提示（不阻断，但需要知情）：")
        for warning in pf.warnings:
            print(f"  · {warning}")
    if pf.issues:
        print("\n需要先处理的问题（阻断）：")
        for issue in pf.issues:
            print(f"  · {issue}")
        print("\n在问题解决前不要执行全量（避免产生无法解释的结果或不必要的费用）。")
    else:
        print("\n结论：数据状态健康，具备一次性全量运行条件。")
        print("提醒：全量会真实产生费用，必须显式加 --yes 才会执行；本命令自身零调用。")
    return pf.as_dict()


__all__ = [
    "Preflight",
    "collect",
    "print_report",
    "core_table_checks",
    "estimate_cost",
    "CORE_TABLE_BASELINE",
]
