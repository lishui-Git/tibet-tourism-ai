# -*- coding: utf-8 -*-
"""C-BAT-05 评论语义分析（设计 §4.9 / §15.A / §7；流程图 `DeepSeek语义分析流程图.md`）。

【组件职责】
    把 `review` 中的评论逐条交给 DeepSeek 做**语义理解**，产出结构化结果并落库：
      · `sentiment`(method='deepseek')  极性 + 强度 + 是否通过校验 + 原始返回
      · `aspect`                        方面级倾向 + 原文证据
      · `comment_semantic`              关键词 + 一句话摘要
    本组件**只回答"这条评论在说什么"**，不产出任何景点级结论
    （景点级事实与评价分别属于 C-BAT-06 / C-BAT-07，两者不得混淆）。

【输入】`review` 中 `sentiment` 尚无 `method='deepseek'` 的评论
【分层过滤（成本控制核心，CC-4）】
    ① 正文为空        → 标记不可分析，不调用模型（最终只写 sentiment 占位）
    ② 正文 ≤10 字      → 规则判定（`rule_based_semantic`），不调用模型（BR-05）
    ③ 重复正文        → 组内**代表评论**调用一次，其余成员复用结果（BR-06）
    ④ 其余            → 调用 DeepSeek

【幂等与断点续跑】
    · 断点游标 = `sentiment` 中已存在 `method='deepseek'` 的 `comment_id` 集合；
    · 每次启动先排除该集合，已成功的评论**既不重复调用也不重复写入**；
    · 写入用 `INSERT ... ON DUPLICATE KEY UPDATE`（键为表自身的业务唯一键）；
    · 中断后重跑同一命令即从断点继续，**不产生重复计费**。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from app.batch.task_registry import TaskRecorder, chunked, finish_task, register_task
from app.db import connection, query_all, query_one
from app.llm.client import CallStats, ChatClient, LlmError
from app.llm.prompts import (
    SEMANTIC_PROMPT_VERSION,
    build_semantic_messages,
    build_semantic_repair_messages,
    extract_json_text,
)
from app.llm.validators import SemanticResult, ValidationError, rule_based_semantic, validate_semantic

# 场景参数（设计 §7.2）：抽取类要稳定 → temperature 偏低；max_tokens 512
TEMPERATURE = 0.1
MAX_TOKENS = 512

WRITE_BATCH = 200       # 单次写库条数（断点粒度）
CALL_BATCH = 60         # 单批提交给线程池的条数（并发颗粒度）
MAX_FAILURE_LOGS = 200  # 失败明细写入 task_log 的上限，避免故障时刷出上万行


# ---------------------------------------------------------------------------
# 一、SQL
# ---------------------------------------------------------------------------

# 待处理候选（需要调用模型）：
#   · 正文非空、尚无 deepseek 结果
#   · 不属于"低信息量"（≤10 字走规则层 BR-05）
#   · 重复正文中**只取代表评论**（BR-06：组内只调一次，其余成员走复用层）
# 代表评论的选取规则与 Python 侧、复用层 SQL **完全一致**（非低信息量 > 正文长 > id 小），
# 三处口径必须一致，否则会出现"代表被漏掉 → 整组都没有结果"。
REPRESENTATIVE_SUBQUERY = """
        SELECT g.comment_id
          FROM (SELECT comment_id, dup_group_id, is_low_info, content_length,
                       ROW_NUMBER() OVER (
                           PARTITION BY dup_group_id
                           ORDER BY is_low_info ASC, content_length DESC, comment_id ASC
                       ) AS rn
                  FROM review
                 WHERE is_dup_content = 1 AND dup_group_id IS NOT NULL) g
         WHERE g.rn = 1
"""

PENDING_SQL = f"""
    SELECT r.comment_id, r.spot_id, r.content, r.content_length,
           r.is_low_info, r.is_dup_content, r.dup_group_id,
           r.like_count, r.score, s.spot_name
      FROM review r
      JOIN spot   s ON s.spot_id = r.spot_id
     WHERE r.is_low_info = 0
       AND (r.is_dup_content = 0 OR r.comment_id IN ({REPRESENTATIVE_SUBQUERY}))
       AND r.content IS NOT NULL
       AND TRIM(r.content) <> ''
       AND NOT EXISTS (SELECT 1 FROM sentiment se
                        WHERE se.comment_id = r.comment_id AND se.method = 'deepseek')
     ORDER BY r.comment_id
"""

# 规则层待处理（正文 ≤10 字，含重复组内的低信息量成员）：同样受断点跳过保护
PENDING_RULE_SQL = """
    SELECT r.comment_id, r.spot_id, r.content, r.content_length,
           r.is_low_info, r.is_dup_content, r.dup_group_id,
           r.like_count, r.score, s.spot_name
      FROM review r
      JOIN spot   s ON s.spot_id = r.spot_id
     WHERE r.is_low_info = 1
       AND r.content IS NOT NULL
       AND TRIM(r.content) <> ''
       AND NOT EXISTS (SELECT 1 FROM sentiment se
                        WHERE se.comment_id = r.comment_id AND se.method = 'deepseek')
     ORDER BY r.comment_id
"""

# 复用层待处理（重复正文中的"非代表"成员）。
# 代表评论的选取规则与 Python 侧 `_pick_representative` **完全一致**，用 SQL 直接算出代表 id，
# 避免"SQL 选一套、Python 选一套"造成口径漂移：
#   排序键 = (是否低信息量 ASC, 正文长度 DESC, comment_id ASC)
PENDING_REUSE_SQL = """
    SELECT r.comment_id, r.spot_id, r.dup_group_id, rep.representative_id
      FROM review r
      JOIN (SELECT g.dup_group_id, g.comment_id AS representative_id
              FROM (SELECT comment_id, dup_group_id, is_low_info, content_length,
                           ROW_NUMBER() OVER (
                               PARTITION BY dup_group_id
                               ORDER BY is_low_info ASC, content_length DESC, comment_id ASC
                           ) AS rn
                      FROM review
                     WHERE is_dup_content = 1 AND dup_group_id IS NOT NULL) g
             WHERE g.rn = 1) rep
        ON rep.dup_group_id = r.dup_group_id
     WHERE r.is_dup_content = 1
       AND r.dup_group_id IS NOT NULL
       AND r.comment_id <> rep.representative_id
       AND NOT EXISTS (SELECT 1 FROM sentiment se
                        WHERE se.comment_id = r.comment_id AND se.method = 'deepseek')
     ORDER BY r.comment_id
"""

# 重复组全部成员：用于确定"代表评论"（代表需在 Python 侧按 非低信息量 > 正文更长 > id 更小 选出）
DUP_MEMBERS_SQL = """
    SELECT comment_id, spot_id, dup_group_id, content, content_length, is_low_info
      FROM review
     WHERE is_dup_content = 1 AND dup_group_id IS NOT NULL
     ORDER BY dup_group_id, comment_id
"""

SENTIMENT_UPSERT_SQL = """
    INSERT INTO sentiment
        (comment_id, spot_id, method, polarity, intensity, confidence, is_valid, raw_json, created_at)
    VALUES (%s, %s, 'deepseek', %s, %s, NULL, %s, %s, NOW())
    ON DUPLICATE KEY UPDATE
        spot_id   = VALUES(spot_id),
        polarity  = VALUES(polarity),
        intensity = VALUES(intensity),
        is_valid  = VALUES(is_valid),
        raw_json  = VALUES(raw_json)
"""

ASPECT_DELETE_SQL = "DELETE FROM aspect WHERE comment_id = %s"

ASPECT_UPSERT_SQL = """
    INSERT INTO aspect (comment_id, spot_id, aspect_name, polarity, evidence, method, created_at)
    VALUES (%s, %s, %s, %s, %s, 'deepseek', NOW())
    ON DUPLICATE KEY UPDATE
        spot_id  = VALUES(spot_id),
        polarity = VALUES(polarity),
        evidence = VALUES(evidence)
"""

SEMANTIC_UPSERT_SQL = """
    INSERT INTO comment_semantic (comment_id, spot_id, keywords, summary, source, created_at)
    VALUES (%s, %s, %s, %s, %s, NOW())
    ON DUPLICATE KEY UPDATE
        spot_id  = VALUES(spot_id),
        keywords = VALUES(keywords),
        summary  = VALUES(summary),
        source   = VALUES(source)
"""


# ---------------------------------------------------------------------------
# 二、工作集切分（分层过滤的落地）
# ---------------------------------------------------------------------------


@dataclass
class SemanticStats:
    """过程统计（写入 `task_log.detail_json`，四类计数口径与 clean_log 一致）。"""

    call_candidates_total: int = 0     # 全库"需要调用模型"的评论总数（未截断）
    call_candidates_sampled: int = 0   # 本轮实际纳入调用的条数（小样本时被 --limit 截断）
    rule_pending_total: int = 0        # 全库待处理的低信息量评论数
    reuse_pending_total: int = 0       # 全库待处理的重复组非代表成员数
    already_done: int = 0              # 已存在 deepseek 结果的评论数（断点游标规模）
    empty_content: int = 0             # 正文为空（不可分析）
    low_info: int = 0                  # 本轮走规则判定的条数
    dup_members: int = 0               # 本轮复用的重复组成员数
    dup_representatives: int = 0       # 本轮调用的"重复组代表"条数
    api_calls_planned: int = 0         # 本轮计划调用次数
    rule_written: int = 0
    reuse_written: int = 0
    api_success: int = 0
    api_failed: int = 0
    aspects_dropped: int = 0

    def as_detail(self) -> dict[str, int]:
        return {k: int(v) for k, v in self.__dict__.items()}


@dataclass
class WorkPlan:
    """切分结果：谁走规则、谁复用、谁调用。"""

    rule_rows: list[dict] = field(default_factory=list)                  # ≤10 字 → 规则
    reuse_rows: list[tuple[dict, dict]] = field(default_factory=list)    # (成员, 代表)
    call_rows: list[dict] = field(default_factory=list)                  # 需要调用模型
    stats: SemanticStats = field(default_factory=SemanticStats)


def build_reuse_rows(pending_members: Sequence[dict], representative_map: dict[int, int]) -> list[tuple[dict, dict]]:
    """构造复用工作项：把"非代表成员"与它的"代表评论"配对。

    :param pending_members: `PENDING_REUSE_SQL` 的结果（含 representative_id）
    :param representative_map: comment_id → 代表评论行（用于记录代表信息）
    """
    rows: list[tuple[dict, dict]] = []
    for member in pending_members:
        rep_id = int(member["representative_id"])
        representative = representative_map.get(rep_id)
        if representative is None:
            continue
        rows.append((member, representative))
    return rows


def split_workload(
    call_candidates: Sequence[dict],
    rule_candidates: Sequence[dict],
    reuse_candidates: Sequence[tuple[dict, dict]],
) -> WorkPlan:
    """把三类待处理工作组装成执行计划。

    三类的划分**完全由 SQL 口径决定**（与设分层一致，见文件顶部的分层说明）：
      · `call_candidates`  ：非低信息量、非重复正文、尚无 deepseek 结果 → 调用模型
      · `rule_candidates`  ：正文 ≤10 字且尚无结果 → 规则判定（BR-05，不调用模型）
      · `reuse_candidates` ：重复正文中"非代表"成员且尚无结果 → 复用代表结果（BR-06）
    """
    plan = WorkPlan()
    plan.call_rows = list(call_candidates)
    plan.rule_rows = list(rule_candidates)
    plan.reuse_rows = list(reuse_candidates)

    stats = plan.stats
    stats.api_calls_planned = len(plan.call_rows)
    stats.low_info = len(plan.rule_rows)
    stats.dup_members = len(plan.reuse_rows)
    # 调用层中属于"重复组代表"的条数（即：因为去重而"省下来"的调用次数）
    stats.dup_representatives = sum(1 for row in plan.call_rows if row["is_dup_content"] == 1)
    return plan


# ---------------------------------------------------------------------------
# 三、规则层（② 低信息量，不调用模型）
# ---------------------------------------------------------------------------


def build_rule_record(row: dict) -> dict[str, Any]:
    """低信息量评论的规则判定结果（写入 source='rule'）。"""
    result = rule_based_semantic(row["content"])
    return {
        "comment_id": row["comment_id"],
        "spot_id": row["spot_id"],
        "result": result,
        "source": "rule",
        "raw": {"source": "rule", "prompt_version": SEMANTIC_PROMPT_VERSION, **result.as_raw_json()},
    }


def _build_api_record(row: dict, result: SemanticResult, usage: dict[str, int] | None = None) -> dict[str, Any]:
    """把一次成功的模型调用结果整理成待写库记录。

    注意旧版本兼容：`validate_semantic` 现在返回结构化对象，`result` 也可能是
    `(SemanticResult, raw_text)` 元组（保留 `return_raw=True` 的调用方式）。

    `usage` 一并写入 `raw_json`：否则"本次真实花了多少 token"只存在于进程内存里，
    事后无法仅凭数据库复核费用（论文的实验成本章节需要这个依据）。
    """
    if isinstance(result, tuple):
        result, _ = result
    raw = {
        "source": "deepseek",
        "prompt_version": SEMANTIC_PROMPT_VERSION,
        **result.as_raw_json(),
    }
    if usage:
        raw["usage"] = {k: int(v or 0) for k, v in usage.items()}
    return {
        "comment_id": row["comment_id"],
        "spot_id": row["spot_id"],
        "result": result,
        "source": "deepseek",
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# 四、调用与校验（④ 正常调用）
# ---------------------------------------------------------------------------


def analyze_one(client: ChatClient, row: dict) -> SemanticResult:
    """一条评论的"调用 → 解析 → 校验"，必要时做一次修复轮（§15.A.4/A.5）。

    :raises LlmError: 调用失败（已重试到上限）
    :raises ValidationError: 返回结构不可用（修复轮后仍失败）
    """
    messages = build_semantic_messages(row["spot_name"], row["content"])
    response = client.chat(messages, temperature=TEMPERATURE, max_tokens=MAX_TOKENS)
    try:
        return validate_semantic(extract_json_text(response.content), row["content"])
    except ValidationError as exc:
        # 修复轮：把"上次错在哪"回传；仍失败则由上层记失败（不阻塞整批）
        repair_messages = build_semantic_repair_messages(
            row["spot_name"], row["content"], response.content, str(exc)
        )
        retry = client.chat(repair_messages, temperature=TEMPERATURE, max_tokens=MAX_TOKENS)
        try:
            result = validate_semantic(extract_json_text(retry.content), row["content"])
        except ValidationError as exc2:
            raise ValidationError(f"修复轮仍失败：{exc2}") from exc2
        result.repairs.append(f"首次校验失败后修复成功：{exc}")
        return result


def call_batch(
    client: ChatClient,
    rows: Sequence[dict],
    concurrency: int,
    stats: CallStats,
) -> tuple[list[dict[str, Any]], list[tuple[dict, str, str]]]:
    """并发处理一批评论。

    :returns: `(成功记录列表, 失败列表[(行, 失败类别, 失败原因)])`
    **单条失败绝不影响整批**（§7.5）——所有异常都被收敛成失败项。
    """
    records: list[dict[str, Any]] = []
    failures: list[tuple[dict, str, str]] = []
    if not rows:
        return records, failures

    def worker(row: dict):
        """返回 (行, ChatResult, 校验结果, 失败类别, 失败原因)。"""
        try:
            messages = build_semantic_messages(row["spot_name"], row["content"])
            response = client.chat(messages, temperature=TEMPERATURE, max_tokens=MAX_TOKENS)
            try:
                result = validate_semantic(extract_json_text(response.content), row["content"])
            except ValidationError as exc:
                repair_messages = build_semantic_repair_messages(
                    row["spot_name"], row["content"], response.content, str(exc)
                )
                retry = client.chat(repair_messages, temperature=TEMPERATURE, max_tokens=MAX_TOKENS)
                try:
                    result = validate_semantic(extract_json_text(retry.content), row["content"])
                except ValidationError as exc2:
                    raise ValidationError(f"修复轮仍失败：{exc2}") from exc2
                result.repairs.append(f"首次校验失败后修复成功：{exc}")
                return row, retry, result, None, ""
            return row, response, result, None, ""
        except LlmError as exc:
            return row, None, None, exc.kind, str(exc)
        except ValidationError as exc:
            return row, None, None, "validation", str(exc)
        except Exception as exc:  # 兜底：任何未预期异常都不得终止整批
            return row, None, None, "unexpected", f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(worker, row) for row in rows]
        for future in as_completed(futures):
            row, response, result, kind, reason = future.result()
            if result is None:
                stats.record_failure(attempts=1, kind=kind or "unknown")
                failures.append((row, kind or "unknown", reason))
                continue
            if response is not None:
                stats.record_success(response)
            records.append(_build_api_record(row, result, response.usage if response else None))
    return records, failures


# ---------------------------------------------------------------------------
# 五、写库（幂等）
# ---------------------------------------------------------------------------


def write_records(conn, records: Sequence[dict[str, Any]], stats: SemanticStats) -> None:
    """把一批语义结果写入三张结果表。

    幂等做法：
      · `sentiment` / `comment_semantic`：按业务唯一键 upsert；
      · `aspect`：行的数量会随重跑变化（模型可能给出不同方面数），
        故**先删该评论的旧方面行再插入**，避免残留脏数据。
    """
    if not records:
        return

    sentiment_rows = [
        (
            rec["comment_id"],
            rec["spot_id"],
            rec["result"].polarity,
            rec["result"].intensity,
            rec["result"].is_valid,
            json.dumps(rec["raw"], ensure_ascii=False),
        )
        for rec in records
    ]
    semantic_rows = [
        (
            rec["comment_id"],
            rec["spot_id"],
            rec["result"].keywords,
            rec["result"].summary,
            rec["source"],
        )
        for rec in records
    ]
    aspect_rows = [
        (rec["comment_id"], rec["spot_id"], a["aspect"], a["polarity"], a["evidence"])
        for rec in records
        for a in rec["result"].aspects
    ]

    with conn.cursor() as cur:
        cur.executemany(SENTIMENT_UPSERT_SQL, sentiment_rows)
        cur.executemany(SEMANTIC_UPSERT_SQL, semantic_rows)
        for rec in records:
            cur.execute(ASPECT_DELETE_SQL, (rec["comment_id"],))
        if aspect_rows:
            cur.executemany(ASPECT_UPSERT_SQL, aspect_rows)

    stats.aspects_dropped += sum(len(rec["result"].dropped_aspects) for rec in records)


# ---------------------------------------------------------------------------
# 六、主流程
# ---------------------------------------------------------------------------


def run_semantic_analysis(
    *,
    client: ChatClient | None = None,
    limit: int | None = None,
    concurrency: int | None = None,
    dry_run: bool = False,
    mock: bool = False,
    only_comment_ids: Sequence[int] | None = None,
    log_failures: bool = True,
    bound_auxiliary: bool = False,
) -> dict[str, Any]:
    """执行 C-BAT-05 批处理。

    :param limit: **小样本模式**——只对前 N 条"需要调用模型"的评论发起调用（按 comment_id 升序，
                  确定性，便于复现）。规则层与复用层默认不受影响（它们不消耗 API 调用），
                  因此小样本也能验证 BR-05/BR-06 两条成本控制分支。
    :param dry_run: 只统计与切分，不调用模型、不写库
    :param mock: 使用假客户端（仅小样本联调；CLI 拒绝在全量下使用）
    :param only_comment_ids: 只处理指定评论（用于失败补跑）
    :param bound_auxiliary: 同时限制规则层/复用层的处理量（**mock 联调时为 True**）：
                  避免 `--limit 5 --mock` 却把全库 1 万条规则结果写进库。真实运行时为 False，
                  此时规则层与复用层应全量处理（它们不产生 API 费用）。
    """
    from app.config import settings

    if client is None:
        if mock:
            from app.llm.mock import MockClient

            client = MockClient()
        else:
            from app.llm.client import DeepSeekClient

            client = DeepSeekClient()
    concurrency = concurrency or settings.deepseek.max_concurrency

    started_at = datetime.now()

    # 三类待处理工作全部由 SQL 口径给出（均已排除"已有 deepseek 结果"→ 断点续跑）
    call_rows = query_all(PENDING_SQL)
    rule_rows = query_all(PENDING_RULE_SQL)
    reuse_pending = query_all(PENDING_REUSE_SQL)
    if only_comment_ids:
        wanted = {int(cid) for cid in only_comment_ids}
        call_rows = [r for r in call_rows if int(r["comment_id"]) in wanted]
        rule_rows = [r for r in rule_rows if int(r["comment_id"]) in wanted]
        reuse_pending = [r for r in reuse_pending if int(r["comment_id"]) in wanted]

    already_done = int((query_one("SELECT COUNT(*) AS c FROM sentiment WHERE method = 'deepseek'") or {}).get("c", 0))

    # 代表评论信息（用于复用层配对）。
    # 注意：代表评论**可能本身已处理过**（此时不在待处理列表里），因此必须按代表 id 单独回查，
    # 否则"成员待复用、代表已完成"这一最常见的情形会被漏掉。
    representative_map: dict[int, dict] = {}
    if reuse_pending:
        for member in query_all(DUP_MEMBERS_SQL):
            representative_map[int(member["comment_id"])] = member
        rep_ids = sorted({int(row["representative_id"]) for row in reuse_pending})
        missing = [rid for rid in rep_ids if rid not in representative_map]
        if missing:
            placeholders = ",".join(["%s"] * len(missing))
            for row in query_all(
                f"SELECT comment_id, spot_id, dup_group_id FROM review WHERE comment_id IN ({placeholders})",
                tuple(missing),
            ):
                representative_map[int(row["comment_id"])] = row
    reuse_rows = build_reuse_rows(reuse_pending, representative_map)

    # 小样本：只截断"需要调用模型"的部分
    call_rows_total = len(call_rows)
    if limit:
        call_rows = call_rows[:limit]
    # mock 联调时同时限制规则层与复用层（真实运行时不需要——它们不产生 API 费用）
    if bound_auxiliary and limit:
        aux_cap = max(limit * 5, 50)
        rule_rows = rule_rows[:aux_cap]
        reuse_rows = reuse_rows[:aux_cap]

    plan = split_workload(call_rows, rule_rows, reuse_rows)
    stats = plan.stats
    stats.call_candidates_total = call_rows_total
    stats.call_candidates_sampled = len(call_rows)
    stats.rule_pending_total = len(rule_rows)
    stats.reuse_pending_total = len(reuse_rows)
    stats.already_done = already_done
    stats.empty_content = int(
        (query_one("SELECT COUNT(*) AS c FROM review WHERE content IS NULL OR TRIM(content) = ''") or {}).get("c", 0)
    )

    summary: dict[str, Any] = {
        "mode": "dry-run" if dry_run else ("mock" if mock else "real"),
        "call_candidates_total": stats.call_candidates_total,
        "call_candidates_sampled": stats.call_candidates_sampled,
        "api_calls_planned": stats.api_calls_planned,
        "rule_pending_total": stats.rule_pending_total,
        "reuse_pending_total": stats.reuse_pending_total,
        "dup_representatives": stats.dup_representatives,
        "already_done_in_db": already_done,
        "empty_content_excluded": stats.empty_content,
        "concurrency": concurrency,
        "client": _client_summary(client),
    }
    if dry_run:
        return summary

    # 把"本轮为何没事干"说清楚，避免误以为程序坏了：
    # `--limit N` 的语义是"处理接下来的 N 条待处理数据"，而不是"反复处理同一批 N 条"。
    if plan.stats.api_calls_planned == 0 and not plan.rule_rows and not plan.reuse_rows:
        print(
            f"      本次没有需要处理的数据（库内已有 deepseek 结果 {already_done} 条）。\n"
            f"      --limit N 处理的是「接下来的 N 条待处理数据」，重跑会继续往后推进；\n"
            f"      若要验证幂等跳过，请用 --only-ids <comment_id,...> 指定已处理过的评论。"
        )

    # ---- 真实/模拟执行：登记任务 → 规则层 → 调用层 → 复用层 → 收尾 ----------
    task_name = "评论语义分析（C-BAT-05%s）" % ("[mock]" if mock else "")
    call_stats = CallStats()
    failures: list[tuple[dict, str, str]] = []
    written = 0

    with connection() as conn:
        task_id = register_task(
            conn,
            task_type="semantic",
            task_name=task_name,
            total_count=len(plan.rule_rows) + stats.api_calls_planned + stats.dup_members,
        )
        recorder = TaskRecorder(conn, task_id)

        # ② 规则层（低信息量，不消耗调用）
        rule_records = [build_rule_record(row) for row in plan.rule_rows]
        for batch in chunked(rule_records, WRITE_BATCH):
            write_records(conn, batch, stats)
            written += len(batch)
        stats.rule_written = len(rule_records)
        recorder.info(
            "语义抽取-规则层",
            f"低信息量（正文≤10字）按规则判定 {len(rule_records)} 条，未调用模型",
            detail={"input_count": len(rule_records), "output_count": len(rule_records)},
        )

        # ③ 复用层（重复组非代表成员）—— 需要代表评论已成功写库，故先查已处理代表
        first_reuse = _write_reuse_rows(conn, plan.reuse_rows, stats)
        stats.reuse_written = first_reuse
        written += first_reuse
        recorder.info(
            "语义抽取-去重复用",
            f"重复正文组内复用 {stats.reuse_written}/{stats.dup_members} 条（未重复调用模型）",
            detail={"input_count": stats.dup_members, "output_count": stats.reuse_written},
        )

        # ④ 调用层（并发）
        pending = list(plan.call_rows)
        for index in range(0, len(pending), CALL_BATCH):
            batch_rows = pending[index : index + CALL_BATCH]
            records, batch_failures = call_batch(client, batch_rows, concurrency, call_stats)
            failures.extend(batch_failures)
            stats.api_success += len(records)
            stats.api_failed += len(batch_failures)
            for batch in chunked(records, WRITE_BATCH):
                write_records(conn, batch, stats)
                written += len(batch)
            recorder.progress(written)
            planned = len(plan.rule_rows) + stats.api_calls_planned + stats.dup_members
            print(
                f"      进度：已写入 {written} / 计划约 {planned} 条"
                f"（本批成功 {len(records)}/{len(batch_rows)}，累计失败 {stats.api_failed}）"
            )

        # 调用层结束后再补一次复用：本轮新产生的代表结果可以被同组其它成员复用
        second_reuse = _write_reuse_rows(conn, plan.reuse_rows, stats)
        stats.reuse_written += second_reuse
        written += second_reuse

        if log_failures and failures:
            _flush_failures(conn, task_id, recorder, failures)

        finish_task(
            conn,
            task_id,
            status="success" if stats.api_failed == 0 else "partial",
            success_count=written,
            fail_count=stats.api_failed,
            skip_count=stats.already_done,
            started_at=started_at,
            error_message=None if stats.api_failed == 0 else f"{stats.api_failed} 条调用失败，已写入 task_log 待补跑",
        )

    summary.update(
        {
            "task_id": task_id,
            "written": written,
            "rule_written": stats.rule_written,
            "reuse_written": stats.reuse_written,
            "api_success": stats.api_success,
            "api_failed": stats.api_failed,
            "aspects_dropped": stats.aspects_dropped,
            "call_stats": call_stats.as_dict(),
            "cost": estimate_cost(call_stats),
            "failures_preview": [
                {"comment_id": row["comment_id"], "kind": kind, "reason": reason[:120]}
                for row, kind, reason in failures[:10]
            ],
        }
    )
    return summary


def _existing_deepseek_ids(conn, comment_ids: Sequence[int]) -> set[int]:
    """查询给定评论中**已经写入 deepseek 结果**的集合（复用层与断点续跑的判据）。"""
    if not comment_ids:
        return set()
    found: set[int] = set()
    ids = list(comment_ids)
    for batch in chunked(ids, WRITE_BATCH):
        placeholders = ",".join(["%s"] * len(batch))
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT comment_id FROM sentiment WHERE method='deepseek' AND comment_id IN ({placeholders})",
                tuple(batch),
            )
            found.update(int(row["comment_id"]) for row in cur.fetchall())
    return found


def _write_reuse_rows(conn, reuse_rows: Sequence[tuple[dict, dict]], stats: SemanticStats) -> int:
    """把组内代表评论的语义结果复制给其余成员（不重新调用模型）。

    两个要点：
    1. `spot_id` 使用**成员自己的**景点：同一段正文出现在不同景点时，
       语义内容可复用，但归属必须是本条评论实际所属的景点；
    2. 先排除**已经写过**的成员——调用层之后会再执行一次复用（此时本轮新产生的代表结果才可用），
       若不过滤就会把同一行重复写一遍，导致统计口径虚高（实测过：写入计数 60,889 ≠ 实际 59,032 行）。
    """
    if not reuse_rows:
        return 0
    # 先排除已经写过的成员（见上文第 2 点的原因）
    already_done = _existing_deepseek_ids(conn, [m["comment_id"] for m, _ in reuse_rows])
    todo = [(member, rep) for member, rep in reuse_rows if int(member["comment_id"]) not in already_done]
    if not todo:
        return 0

    written = 0
    for batch in chunked(todo, WRITE_BATCH):
        rep_ids = sorted({int(rep["comment_id"]) for _, rep in batch})
        placeholders = ",".join(["%s"] * len(rep_ids))
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT comment_id, polarity, intensity, is_valid FROM sentiment "
                f"WHERE method='deepseek' AND comment_id IN ({placeholders})",
                rep_ids,
            )
            sentiment_map = {int(r["comment_id"]): r for r in cur.fetchall()}
            cur.execute(
                f"SELECT comment_id, keywords, summary, source FROM comment_semantic "
                f"WHERE comment_id IN ({placeholders})",
                rep_ids,
            )
            semantic_map = {int(r["comment_id"]): r for r in cur.fetchall()}
            cur.execute(
                f"SELECT comment_id, aspect_name, polarity, evidence FROM aspect "
                f"WHERE method='deepseek' AND comment_id IN ({placeholders})",
                rep_ids,
            )
            aspect_map: dict[int, list[dict]] = {}
            for row in cur.fetchall():
                aspect_map.setdefault(int(row["comment_id"]), []).append(row)

        records: list[dict[str, Any]] = []
        for member, representative in batch:
            rep_id = int(representative["comment_id"])
            source = sentiment_map.get(rep_id)
            if not source:
                # 代表评论尚未成功（本轮失败或尚未跑）→ 本条留待下次重跑，不写半成品
                continue
            result = SemanticResult(
                polarity=source["polarity"],
                intensity=source["intensity"],
                is_valid=int(source["is_valid"]),
                aspects=[
                    {
                        "aspect": a["aspect_name"],
                        "polarity": a["polarity"],
                        "evidence": a["evidence"] or "",
                    }
                    for a in aspect_map.get(rep_id, [])
                ],
                keywords=(semantic_map.get(rep_id) or {}).get("keywords"),
                summary=(semantic_map.get(rep_id) or {}).get("summary"),
            )
            records.append(
                {
                    "comment_id": member["comment_id"],
                    "spot_id": member["spot_id"],
                    "result": result,
                    "source": (semantic_map.get(rep_id) or {}).get("source") or "deepseek",
                    "raw": {
                        "source": "reuse",
                        "prompt_version": SEMANTIC_PROMPT_VERSION,
                        "reused_from_comment_id": rep_id,
                        # 复用不产生新的 API 费用，因此 usage 记为 0（便于成本核算时区分）
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    },
                }
            )
        write_records(conn, records, stats)
        written += len(records)
    return written


def _flush_failures(conn, task_id: int, recorder: TaskRecorder, failures: Sequence[tuple[dict, str, str]]) -> None:
    """失败明细写入 `task_log`（level=ERROR, stage=semantic, ref_key=comment_id, resolved=0）。"""
    lines = list(failures)[:MAX_FAILURE_LOGS]
    with conn.cursor() as cur:
        for row, kind, reason in lines:
            cur.execute(
                """
                INSERT INTO task_log (task_id, level, stage, message, ref_key, retry_count, resolved)
                VALUES (%s, 'ERROR', 'semantic', %s, %s, %s, 0)
                """,
                (task_id, f"[{kind}] {reason}"[:512], str(row["comment_id"]), 0),
            )
    if len(failures) > len(lines):
        recorder.warn(
            "语义抽取-失败记录",
            f"失败 {len(failures)} 条，仅登记前 {len(lines)} 条明细（可按 stage=semantic 补跑）",
        )


def estimate_cost(call_stats: CallStats) -> dict[str, Any]:
    """按实测 token 与配置单价估算费用（真实费用以 API 账单为准）。"""
    from app.config import settings

    price_in = settings.deepseek.price_input
    price_out = settings.deepseek.price_output
    cost_in = call_stats.prompt_tokens / 1_000_000 * price_in
    cost_out = call_stats.completion_tokens / 1_000_000 * price_out
    return {
        "prompt_tokens": call_stats.prompt_tokens,
        "completion_tokens": call_stats.completion_tokens,
        "total_tokens": call_stats.total_tokens,
        "price_input_per_million": price_in,
        "price_output_per_million": price_out,
        "cost_input_cny": round(cost_in, 4),
        "cost_output_cny": round(cost_out, 4),
        "cost_total_cny": round(cost_in + cost_out, 4),
        "avg_tokens_per_call": round(call_stats.total_tokens / call_stats.requests, 1) if call_stats.requests else 0,
    }


def _client_summary(client: ChatClient) -> str:
    """安全打印客户端信息（真实客户端的 summary 不含 Key）。"""
    summary = getattr(client, "summary", None)
    if isinstance(summary, str):
        return summary
    return f"client={type(client).__name__}"


__all__ = [
    "run_semantic_analysis",
    "split_workload",
    "estimate_cost",
    "SEMANTIC_PROMPT_VERSION",
]
