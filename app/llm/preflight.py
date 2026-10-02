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

# 实测成本参数（32 次真实调用）：
#   单条平均 **480** token（区间 431–562）
#   输入 10,691 / 输出 4,681 / 合计 15,372  ⇒ 输入 69.5% / 输出 **30.5%**
#
# 【修正说明】此前这里写的是 `INPUT_SHARE=0.73 / OUTPUT_SHARE=0.27`，
# 但用实测汇总反算应为 **0.6955 / 0.3045**。判据：4,681 / 15,372 = 0.3045 恰好复算出
# 文档记录的实付金额 ¥0.0588（而 0.27 会算出 ¥0.0559）——所以 0.27 是错的。
# 影响：输出单价是输入的 4 倍，比例偏差会使费用估算**偏低约 1.8%**；
# 方向上是"低报"，对预算判断不利，必须按实测改正。
MEASURED_TOKENS_PER_CALL = 480
MEASURED_TOKENS_PER_CALL_MIN = 431
MEASURED_TOKENS_PER_CALL_MAX = 562
INPUT_SHARE = 0.6955
OUTPUT_SHARE = 0.3045

# 景点评价（C-BAT-07）单次调用的 token 估算。
#
# 输入：实测 57 份事实包的最大 JSON 为 3,553 字符，对应真实 prompt 总字符数约 4,900，
#       中文约 0.65 token/字 ⇒ 最坏约 **3,200 token**。这里取 **3,400**（略高于最坏实测）
#       而不是原先的 2,700——因为预算是硬约束，**宁可高报**。
# 输出：按 Schema 上限估算（而非中位数），同样取保守值。
REPORT_INPUT_TOKENS = 3_400
REPORT_OUTPUT_TOKENS = 700


@dataclass
class Preflight:
    """预检结果（结构化，便于打印与机器读取）。"""

    counts: dict[str, int] = field(default_factory=dict)
    workload: dict[str, int] = field(default_factory=dict)
    integrity: dict[str, int] = field(default_factory=dict)
    layering: dict[str, int] = field(default_factory=dict)
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
            "layering": self.layering,
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
    # 反方向：有 deepseek 情感行却没有 comment_semantic 行。
    # 为什么重要：若进程在"写完 sentiment、还没写 comment_semantic"时**跨事务**退出，
    # 游标（=`NOT EXISTS(sentiment)`）会认为这条已做完，于是**永远不再补写**语义行——
    # 库里就留下一条"有情感、无语义"的半成品。正常情况下三表在**同一事务**内提交，
    # 因此该值必须为 0；一旦非 0 就说明提交粒度被破坏，必须查清。
    "sentiment_without_semantic": "SELECT COUNT(*) FROM sentiment se "
                                  "LEFT JOIN comment_semantic cs ON cs.comment_id = se.comment_id "
                                  "WHERE se.method='deepseek' AND cs.comment_id IS NULL",
    # aspect 的孤儿行：`write_records` 对 aspect 是"先删后插"，若事务被破坏可能留下孤儿。
    "orphan_aspect": "SELECT COUNT(*) FROM aspect a "
                     "WHERE NOT EXISTS (SELECT 1 FROM sentiment se "
                     "                   WHERE se.comment_id = a.comment_id AND se.method='deepseek')",
    "orphan_aspect_review": "SELECT COUNT(*) FROM aspect a "
                            "LEFT JOIN review r ON r.comment_id = a.comment_id "
                            "WHERE r.comment_id IS NULL",
    # mock 标记行（`raw_json.mode='mock'`）：mock 假结果与真实结果在库里长得一样，
    # 混进结果表会直接污染"真实 API 完成数"与费用核算，必须能被检出（正常应为 0）。
    # 说明：31 条真实结果写入时还没有该字段，因此它们读作"无标记"（即真实），这是正确的。
    "mock_rows_in_results": "SELECT COUNT(*) FROM sentiment WHERE method='deepseek' "
                            "AND JSON_UNQUOTE(JSON_EXTRACT(raw_json,'$.mode')) = 'mock'",
    # 「测试痕迹」= mock 行 + 复用行（`mode` 为 mock/reuse）。
    # 为什么把 **reuse 也纳入**：复用行同样不该在"尚未全量运行"的库中长期残留，
    # 且它曾经不带标记；实测踩到过 11 条 mock + 4 条复用一起残留，
    # 其中复用行因无标记而与真实结果无法区分。现在两者都可被精确定位。
    # 注意：全量运行**正常**产生的复用行（`mode='reuse'`）也会计入这里——
    # 它不参与"真实 API 完成数"口径，仅作为"库内有无测试残留/复用副本"的可见指标。
    "test_trace_rows_in_results": "SELECT COUNT(*) FROM sentiment WHERE method='deepseek' "
                                  "AND JSON_UNQUOTE(JSON_EXTRACT(raw_json,'$.mode')) IN ('mock','reuse')",
    # 景点评价里的 mock 行：`spot_report.model` 在 mock 模式下写 'mock'（见 spot_report.py）。
    # 为什么必须单独查：景点评价表**没有** raw_json，sentiment 的那条检查覆盖不到它；
    # 而一条 mock 评价在库里与真实评价**完全一样**（同样的四段文案与字段），
    # 全量后若残留，会因"已有评价"被游标跳过而**永不重生成**。
    "mock_spot_report_rows": "SELECT COUNT(*) FROM spot_report WHERE model = 'mock'",
}

# 「分层自洽性」检查（只读）：证明 59,033 条评论被三层**恰好覆盖一次**，没有静默丢弃。
# 这一组检查回答的是"会不会有评论被漏掉却没人发现"——单看各层计数无法证明，
# 必须验证"三层 = 可分析总量"这个**互斥且穷尽**的划分。
#
# 划分口径（严格对齐 `app/batch/semantic_analysis.py` 的两条待处理 SQL + 规则定义）：
#   规则层 = **所有**低信息量（正文 ≤10 字），无论是否重复组成员  → BR-05
#   复用层 = 重复组中除代表外的成员，且**自身不是**低信息量       → BR-06
#   调用层 = 其余（真正进入模型）
# 实测数据印证了这一口径：1,071 条待复用成员全部 `is_low_info=0`；
# 2,583 条"低信息量的重复组成员"历史运行中已由规则层处理（与 BR-05 一致）。
#
# 注意：一个评论可能"既低信息量、又是重复组成员"——这类归**规则层**（不进入模型）。
# 若在复用层里重复计入，三层之和就会超过可分析总量（首版划分即犯此错，被本检查当场抓出）。
_LAYER_REUSE_CTE = """
    SELECT r.comment_id
      FROM review r
      JOIN (SELECT g.dup_group_id, g.comment_id AS rep_id
              FROM (SELECT comment_id, dup_group_id, is_low_info, content_length,
                           ROW_NUMBER() OVER (PARTITION BY dup_group_id
                               ORDER BY is_low_info ASC, content_length DESC, comment_id ASC) AS rn
                      FROM review WHERE is_dup_content = 1 AND dup_group_id IS NOT NULL) g
             WHERE g.rn = 1) rep ON rep.dup_group_id = r.dup_group_id
     WHERE r.is_dup_content = 1 AND r.dup_group_id IS NOT NULL AND r.comment_id <> rep.rep_id
       AND r.is_low_info = 0
"""

LAYERING_SQL: dict[str, str] = {
    # 可分析总量：正文非空（正文为空的 1 条不参与任何层，单独确认）
    "analyzable": "SELECT COUNT(*) FROM review WHERE content IS NOT NULL AND TRIM(content) <> ''",
    # 规则层：全部低信息量（不进入模型）—— 与 `low_info_total` 同口径，便于交叉核对
    "layer_rule_total": "SELECT COUNT(*) FROM review WHERE content IS NOT NULL AND TRIM(content) <> '' "
                        "AND (is_low_info = 1 OR CHAR_LENGTH(TRIM(content)) <= 10)",
    "layer_rule_done": "SELECT COUNT(*) FROM review r JOIN comment_semantic cs ON cs.comment_id = r.comment_id "
                       "WHERE cs.source = 'rule'",
    # 复用层：非低信息量的重复组非代表成员（复制代表结果，零调用）
    "layer_reuse_total": f"SELECT COUNT(*) FROM ({_LAYER_REUSE_CTE}) t",
    "layer_reuse_done": "SELECT COUNT(*) FROM comment_semantic WHERE source = 'reuse'",
    # 调用层：既非低信息量、又非复用成员的其余评论（真正进入模型）
    "layer_call_total": f"""
        SELECT COUNT(*) FROM review r
         WHERE r.content IS NOT NULL AND TRIM(r.content) <> ''
           AND NOT (r.is_low_info = 1 OR CHAR_LENGTH(TRIM(r.content)) <= 10)
           AND r.comment_id NOT IN ({_LAYER_REUSE_CTE})
    """,
    # 调用层已完成：只数**调用层口径内**的已完成（其余不含低信息量、也不含重复组成员）。
    # 若直接数 `sentiment` 全表，会把规则层/复用层复制过来的行也算成"模型已完成"，
    # 导致 `pending ≠ 总量 − 已完成`（这一处口径错误由本节的一致性检查当场抓出）。
    "layer_call_done": "SELECT COUNT(*) FROM review r JOIN sentiment se ON se.comment_id = r.comment_id "
                       "AND se.method='deepseek' "
                       "WHERE r.content IS NOT NULL AND TRIM(r.content) <> '' "
                       "AND NOT (r.is_low_info = 1 OR CHAR_LENGTH(TRIM(r.content)) <= 10) "
                       "AND r.is_dup_content = 0",
    # 异常组合（正常应全为 0）
    "rule_row_on_normal": "SELECT COUNT(*) FROM review r JOIN comment_semantic cs ON cs.comment_id = r.comment_id "
                          "WHERE cs.source='rule' AND r.is_low_info = 0 "
                          "AND CHAR_LENGTH(TRIM(r.content)) > 10",
    "empty_content_in_any_layer": "SELECT COUNT(*) FROM comment_semantic cs JOIN review r ON r.comment_id = cs.comment_id "
                                  "WHERE r.content IS NULL OR TRIM(r.content) = ''",
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
    # 费用：先算并**四舍五入到分**各分项，再由分项相加得到合计。
    # 为什么要这样：报告里会打印"评论级 ¥A + 景点级 ¥B = 合计 ¥C"，
    # 若合计用**未舍入**的中间值再舍入，会出现 A+B ≠ C 的观感
    # （实测：74.43 + 0.63 = 75.06，而合计显示 75.05）——
    # 答辩时被人当场按计算器质疑"加不起来"是完全可以避免的。
    comment_min = round(estimate_cost(call_pending, tokens_min), 2)
    comment_max = round(estimate_cost(call_pending, tokens_max), 2)
    report_cost_rounded = round(report_cost, 2)
    result.cost = {
        "tokens_per_call_measured": tokens_mid,
        "comment_cost_min_cny": comment_min,
        "comment_cost_max_cny": comment_max,
        "spot_report_cost_cny": report_cost_rounded,
        "total_cost_min_cny": round(comment_min + report_cost_rounded, 2),
        "total_cost_max_cny": round(comment_max + report_cost_rounded, 2),
    }

    result.integrity = {key: _scalar(sql) for key, sql in INTEGRITY_SQL.items()}
    result.layering = {key: _scalar(sql) for key, sql in LAYERING_SQL.items()}
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
        # 跨表一致性的"反方向"与 aspect 孤儿：见 INTEGRITY_SQL 里的注释。
        # 这三项正常都应为 0；一旦非 0 说明"同事务提交"的保证被破坏，
        # 会留下半成品（例如有情感无语义），必须查清后再全量运行。
        "sentiment_without_semantic",
        "orphan_aspect",
        "orphan_aspect_review",
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

    # mock 结果混入真实结果表：会污染"真实 API 完成数"口径与费用核算，必须阻断
    if result.integrity.get("mock_rows_in_results"):
        issues.append(
            f"结果表中存在 {result.integrity['mock_rows_in_results']} 条 mock 假结果"
            "（raw_json.mode='mock'）：会污染真实调用数与费用核算，必须清除后再全量运行"
        )

    # 景点评价表里的 mock 行：sentiment 的那条检查覆盖不到它（该表没有 raw_json）。
    # 一条 mock 评价在库里与真实评价完全一样，全量后会因"已有评价"而被跳过、永不重生成。
    if result.integrity.get("mock_spot_report_rows"):
        issues.append(
            f"spot_report 中存在 {result.integrity['mock_spot_report_rows']} 条 mock 评价"
            "（model='mock'）：全量运行会因『已有评价』而跳过这些景点，必须清除后再全量运行"
        )

    # ---- 分层自洽性：三层是否**恰好覆盖**全部可分析评论（不重不漏）----------
    layer = result.layering
    layer_sum = layer["layer_rule_total"] + layer["layer_reuse_total"] + layer["layer_call_total"]
    if layer_sum != layer["analyzable"]:
        # 说明存在既不属于低信息量、也不属于重复组成员、又没有被计入调用层的评论——
        # 这类评论会被"静默跳过"，永远拿不到结果，且从各层计数上看不出来。
        issues.append(
            f"分层不覆盖：低信息量 {layer['layer_rule_total']} + 重复组成员 {layer['layer_reuse_total']} "
            f"+ 其余 {layer['layer_call_total']} = {layer_sum} ≠ 可分析评论 {layer['analyzable']}"
            f"（差值 {layer['analyzable'] - layer_sum} 条会被静默跳过）"
        )
    for key, label in (
        ("rule_row_on_normal", "非低信息量评论被写成了规则层结果"),
        ("empty_content_in_any_layer", "正文为空的评论被写入了结果表（不应参与分析）"),
    ):
        if layer.get(key):
            issues.append(f"分层异常：{label}（{layer[key]} 条）")

    # 「待处理为 0」必须真的等于"该层已全部完成"。
    # 否则 preflight 会一边显示"待处理 0 条"、一边把工作量算少，全量跑完仍留下没结果的评论。
    rule_pending = result.counts.get("rule_pending", 0)
    if rule_pending == 0 and layer.get("layer_rule_done", 0) != layer.get("layer_rule_total", 0):
        issues.append(
            f"分层自相矛盾：规则层显示待处理 0 条，但已完成 {layer.get('layer_rule_done')} 条 "
            f"≠ 规则层总量 {layer.get('layer_rule_total')} 条"
        )
    reuse_pending = result.counts.get("reuse_pending", 0)
    if reuse_pending == 0 and layer.get("layer_reuse_done", 0) != layer.get("layer_reuse_total", 0):
        issues.append(
            f"分层自相矛盾：复用层显示待处理 0 条，但已完成 {layer.get('layer_reuse_done')} 条 "
            f"≠ 复用层总量 {layer.get('layer_reuse_total')} 条"
        )
    # 调用层待处理应与 pending 计数一致（两处口径必须对上）
    if result.counts.get("call_pending", 0) != layer.get("layer_call_total", 0) - layer.get("layer_call_done", 0):
        issues.append(
            f"调用层口径不一致：pending {result.counts.get('call_pending')} 条 ≠ "
            f"调用层总量 {layer.get('layer_call_total')} − 已完成 {layer.get('layer_call_done')}"
        )
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

    # 分层覆盖：证明"没有评论被静默跳过"（各层计数本身证明不了这一点）
    layer = pf.layering
    if layer:
        layer_sum = layer["layer_rule_total"] + layer["layer_reuse_total"] + layer["layer_call_total"]
        mark = "✓" if layer_sum == layer["analyzable"] else "✗"
        print(f"\n[2b] 分层覆盖自洽性（证明可分析评论被恰好覆盖一次）")
        print(f"  {mark} 低信息量 {layer['layer_rule_total']} + 重复组成员 {layer['layer_reuse_total']}"
              f" + 其余 {layer['layer_call_total']} = {layer_sum}"
              f" / 可分析评论 {layer['analyzable']}（正文为空 {counts['empty_content']} 条不参与）")
        print(f"    已完成：规则 {layer['layer_rule_done']}/{layer['layer_rule_total']}、"
              f"复用 {layer['layer_reuse_done']}/{layer['layer_reuse_total']}、"
              f"模型 {layer['layer_call_done']}/{layer['layer_call_total']}"
              f"（各层已完成不超过总量；模型层待处理 = 总量 − 已完成）")

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
