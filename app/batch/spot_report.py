# -*- coding: utf-8 -*-
"""C-BAT-07 景点智能评价生成（设计 §4.3 / §15.C / BR-02 / BR-07 / BR-12）。

【组件职责】
    把 C-BAT-06 已落库的**事实包**交给 DeepSeek，生成四段自然语言评价并写入 `spot_report`：
        summary（综合评价）/ advantages（主要优势）/ issues（主要问题）/ visitor_focus（游客关注点）

【DeepSeek 在这里负责什么、不负责什么】
    负责：语言组织、优缺点总结、体验特点归纳、自然语言表达。
    不负责：任何统计数字（评论量/评分/占比/样本量都必须原样引用事实包）、
            事实包未涉及的方面，以及实时天气、票价、客流预测、酒店、路线、交通、个性化推荐
            ——这些内容会被"数字一致性校验"与 Prompt 约束共同拦住。

【为什么必须有数字一致性校验】
    模型即使表达流畅也可能把 0.86 写成 0.9、把 3965 条写成 4000 条。
    设计 §15.C.3 要求：抽取回答中的数字与事实包比对，误差超过 0.5 个百分点即标记
    `need_review=1`（**仍入库**，但前端标注"待人工复核"）。这是"数据负责事实"的强制关卡。

【幂等】`spot_report` 以 `spot_id` 为主键：已有同 `fact_package_version` 的评价时默认跳过；
    `--force` 才重新生成并覆盖（对应 FR-IE-08 管理员强制重新生成）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from app.batch.task_registry import TaskRecorder, finish_task, register_task
from app.db import connection, query_all
from app.llm.client import CallStats, ChatClient, LlmError
from app.llm.prompts import REPORT_PROMPT_VERSION, build_report_messages, extract_json_text
from app.llm.validators import ValidationError, check_number_consistency, validate_report

# 场景参数（设计 §7.2）：生成类可略有变化 → temperature 0.3；max_tokens 1200
TEMPERATURE = 0.3
MAX_TOKENS = 1200

REPORT_UPSERT_SQL = """
    INSERT INTO spot_report
        (spot_id, fact_package_version, summary, advantages_json, issues_json,
         visitor_focus_json, model, prompt_version, need_review, token_usage, generated_at, task_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
    ON DUPLICATE KEY UPDATE
        fact_package_version = VALUES(fact_package_version),
        summary              = VALUES(summary),
        advantages_json      = VALUES(advantages_json),
        issues_json          = VALUES(issues_json),
        visitor_focus_json   = VALUES(visitor_focus_json),
        model                = VALUES(model),
        prompt_version       = VALUES(prompt_version),
        need_review          = VALUES(need_review),
        token_usage          = VALUES(token_usage),
        generated_at         = NOW(),
        task_id              = VALUES(task_id)
"""


@dataclass
class ReportStats:
    targets: int = 0
    generated: int = 0
    skipped: int = 0
    failed: int = 0
    need_review: int = 0
    number_mismatches: int = 0

    def as_detail(self) -> dict[str, int]:
        return {k: int(v) for k, v in self.__dict__.items()}


NOT_FOUND: list[int] = []  # 无可用事实包的景点（记录用，不参与生成）


def load_fact_packages(*, version: str | None = None, spot_ids: Sequence[int] | None = None) -> list[dict]:
    """读取事实包（C-BAT-07 的**唯一输入**）。"""
    sql = "SELECT spot_id, version, package_json, review_count FROM spot_fact_package WHERE 1=1"
    params: list[Any] = []
    if version:
        sql += " AND version = %s"
        params.append(version)
    if spot_ids:
        placeholders = ",".join(["%s"] * len(spot_ids))
        sql += f" AND spot_id IN ({placeholders})"
        params.extend(int(sid) for sid in spot_ids)
    sql += " ORDER BY review_count DESC, spot_id"
    return query_all(sql, tuple(params))


def generate_one(client: ChatClient, package: dict) -> tuple[Any, int]:
    """生成并校验一份景点评价。

    :returns: `(ReportResult, token_usage)`
    :raises LlmError: 调用失败
    :raises ValidationError: 结构不可用（修复轮后仍失败）
    """
    messages = build_report_messages(package)
    response = client.chat(messages, temperature=TEMPERATURE, max_tokens=MAX_TOKENS)
    tokens = int(response.usage.get("total_tokens") or 0)

    try:
        result = validate_report(extract_json_text(response.content))
    except ValidationError as exc:
        # 修复轮：四段字段缺失时重试一次（§15.C.3）
        repair = messages + [
            {"role": "assistant", "content": response.content[:800]},
            {"role": "user", "content": f"上一次输出未通过校验：{exc}\n请严格按 Schema 重新输出 JSON。"},
        ]
        retry = client.chat(repair, temperature=TEMPERATURE, max_tokens=MAX_TOKENS)
        tokens += int(retry.usage.get("total_tokens") or 0)
        result = validate_report(extract_json_text(retry.content))
        result.repairs.append(f"首次校验失败后修复成功：{exc}")

    # 数字一致性校验（§15.C.3）：不通过仍入库，但标记 need_review=1
    mismatches = check_number_consistency(result, package)
    if mismatches:
        result.need_review = 1
        result.number_mismatches = mismatches
    return result, tokens


def run_spot_report(
    *,
    client: ChatClient | None = None,
    limit: int | None = None,
    spot_ids: Sequence[int] | None = None,
    version: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    mock: bool = False,
) -> dict[str, Any]:
    """执行 C-BAT-07 批处理（景点级，规模为 57 个景点）。"""
    from app.config import settings

    if client is None:
        if mock:
            from app.llm.mock import MockClient

            client = MockClient()
        else:
            from app.llm.client import DeepSeekClient

            client = DeepSeekClient()

    started_at = datetime.now()
    packages = load_fact_packages(version=version, spot_ids=spot_ids)
    if limit:
        packages = packages[:limit]

    # 已有同版本评价 → 跳过（幂等，BR-12 的离线侧）
    existing = {}
    if packages:
        ids = [int(p["spot_id"]) for p in packages]
        placeholders = ",".join(["%s"] * len(ids))
        rows = query_all(
            f"SELECT spot_id, fact_package_version FROM spot_report WHERE spot_id IN ({placeholders})", tuple(ids)
        )
        existing = {int(r["spot_id"]): r["fact_package_version"] for r in rows}

    stats = ReportStats(targets=len(packages))
    summary: dict[str, Any] = {
        "mode": "dry-run" if dry_run else ("mock" if mock else "real"),
        "fact_packages": len(packages),
        "already_generated": sum(1 for p in packages if int(p["spot_id"]) in existing),
        "force": force,
        "version_filter": version or "(全部)",
        "client": _client_summary(client),
    }
    if dry_run:
        return summary

    call_stats = CallStats()
    task_name = f"景点智能评价生成（C-BAT-07, prompt={REPORT_PROMPT_VERSION}{'[mock]' if mock else ''}）"

    with connection() as conn:
        task_id = register_task(conn, task_type="spot_report", task_name=task_name, total_count=len(packages))
        recorder = TaskRecorder(conn, task_id)

        for item in packages:
            spot_id = int(item["spot_id"])
            if not force and spot_id in existing and existing[spot_id] == item["version"]:
                stats.skipped += 1
                continue
            package = item["package_json"]
            if isinstance(package, str):  # PyMySQL 在部分版本下把 JSON 列返回为字符串
                package = json.loads(package)
            try:
                result, tokens = generate_one(client, package)
            except LlmError as exc:
                stats.failed += 1
                call_stats.record_failure(attempts=1, kind=exc.kind)
                recorder.error("spot_report", f"评价生成失败：{exc}", ref_key=str(spot_id))
                continue
            except ValidationError as exc:
                stats.failed += 1
                call_stats.record_failure(attempts=1, kind="validation")
                recorder.error("spot_report", f"评价校验失败：{exc}", ref_key=str(spot_id))
                continue
            except Exception as exc:  # 兜底：单个景点失败不影响其余景点
                stats.failed += 1
                call_stats.record_failure(attempts=1, kind="unexpected")
                recorder.error("spot_report", f"未预期异常：{type(exc).__name__}: {exc}", ref_key=str(spot_id))
                continue

            call_stats.record_success(_zero_usage_result(tokens))
            with conn.cursor() as cur:
                cur.execute(
                    REPORT_UPSERT_SQL,
                    (
                        spot_id,
                        item["version"],
                        result.summary,
                        json.dumps(result.advantages, ensure_ascii=False),
                        json.dumps(result.issues, ensure_ascii=False),
                        json.dumps(result.visitor_focus, ensure_ascii=False),
                        settings.deepseek.model if not mock else "mock",
                        REPORT_PROMPT_VERSION,
                        result.need_review,
                        tokens,
                        task_id,
                    ),
                )
            stats.generated += 1
            stats.need_review += result.need_review
            stats.number_mismatches += len(result.number_mismatches)
            if result.need_review:
                recorder.warn(
                    "景点评价-数字一致性",
                    f"第 {spot_id} 号景点评价存在与事实包不符的数字，已标记 need_review=1",
                    detail={"mismatches": result.number_mismatches[:5]},
                )

        recorder.info(
            "景点评价生成",
            f"生成 {stats.generated} 份（跳过 {stats.skipped}、失败 {stats.failed}、待复核 {stats.need_review}）",
            detail=stats.as_detail(),
        )
        finish_task(
            conn,
            task_id,
            status="success" if stats.failed == 0 else "partial",
            success_count=stats.generated,
            fail_count=stats.failed,
            skip_count=stats.skipped,
            started_at=started_at,
        )

    summary.update(
        {
            "task_id": task_id,
            "generated": stats.generated,
            "skipped": stats.skipped,
            "failed": stats.failed,
            "need_review": stats.need_review,
            "call_stats": call_stats.as_dict(),
            "cost": _estimate_cost(call_stats),
        }
    )
    return summary


def _zero_usage_result(total_tokens: int):
    """把"只关心 token 总量"的场景包装成 `ChatResult`，用于复用 `CallStats` 统计。"""
    from app.llm.client import ChatResult

    return ChatResult(content="", model="", usage={"total_tokens": total_tokens}, latency_ms=0, attempts=1)


def _estimate_cost(call_stats: CallStats) -> dict[str, Any]:
    """景点级成本估算（token 已记录在 `spot_report.token_usage`，这里只做汇总）。"""
    from app.config import settings

    # 修复轮的 token 无法区分输入/输出，按 7:3 的经验比例拆分，仅用于估算
    prompt_tokens = int(call_stats.total_tokens * 0.7)
    completion_tokens = call_stats.total_tokens - prompt_tokens
    cost = (
        prompt_tokens / 1_000_000 * settings.deepseek.price_input
        + completion_tokens / 1_000_000 * settings.deepseek.price_output
    )
    return {
        "total_tokens": call_stats.total_tokens,
        "cost_total_cny_estimated": round(cost, 4),
        "price_input_per_million": settings.deepseek.price_input,
        "price_output_per_million": settings.deepseek.price_output,
    }


def _client_summary(client: ChatClient) -> str:
    text = getattr(client, "summary", None)
    return text if isinstance(text, str) else f"client={type(client).__name__}"


__all__ = ["run_spot_report", "generate_one", "load_fact_packages", "ReportStats"]
