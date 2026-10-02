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
from app.batch.write_recovery import write_with_retry
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


def generate_one(client: ChatClient, package: dict) -> tuple[Any, dict[str, int]]:
    """生成并校验一份景点评价。

    :returns: `(ReportResult, usage)`；`usage` 是**真实调用返回的用量**，
        含 `prompt_tokens` / `completion_tokens` / `total_tokens`（修复轮会累加）。
        为什么要保留输入/输出拆分（而不是只给总量）：输入与输出单价不同
        （¥2/M vs ¥8/M），只留总量就只能靠经验比例估算费用，
        无法满足"运行结束后输出**实际** usage 与实际费用"的要求。
    :raises LlmError: 调用失败
    :raises ValidationError: 结构不可用（修复轮后仍失败）
    """
    messages = build_report_messages(package)
    response = client.chat(messages, temperature=TEMPERATURE, max_tokens=MAX_TOKENS)
    usage = _usage_of(response)

    try:
        result = validate_report(extract_json_text(response.content))
    except ValidationError as exc:
        # 修复轮：四段字段缺失时重试一次（§15.C.3）
        repair = messages + [
            {"role": "assistant", "content": response.content[:800]},
            {"role": "user", "content": f"上一次输出未通过校验：{exc}\n请严格按 Schema 重新输出 JSON。"},
        ]
        retry = client.chat(repair, temperature=TEMPERATURE, max_tokens=MAX_TOKENS)
        usage = _add_usage(usage, _usage_of(retry))
        result = validate_report(extract_json_text(retry.content))
        result.repairs.append(f"首次校验失败后修复成功：{exc}")

    # 数字一致性校验（§15.C.3）：不通过仍入库，但标记 need_review=1
    mismatches = check_number_consistency(result, package)
    if mismatches:
        result.need_review = 1
        result.number_mismatches = mismatches
    return result, usage


def _usage_of(response: Any) -> dict[str, int]:
    """取一次调用的真实用量（缺失字段按 0 处理，不猜数）。"""
    raw = getattr(response, "usage", None) or {}
    return {
        "prompt_tokens": int(raw.get("prompt_tokens") or 0),
        "completion_tokens": int(raw.get("completion_tokens") or 0),
        "total_tokens": int(raw.get("total_tokens") or 0),
    }


def _add_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    """累加两次调用的用量（修复轮场景）。"""
    return {key: int(left.get(key, 0)) + int(right.get(key, 0)) for key in
            ("prompt_tokens", "completion_tokens", "total_tokens")}


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
                result, usage = generate_one(client, package)
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

            # 记录**真实**用量（含输入/输出拆分）——费用因此可按实际单价算出，而非按经验比例估算
            call_stats.record_success(_usage_result(usage))

            # 写库与"已付费结果"的保护：
            # ① 写失败只重试写库（绝不重新调用模型），仍失败则把结果落盘待补；
            # ② 每个景点写成功后**立刻提交**——否则整轮 57 个景点共用一个事务，
            #    任何一次写失败都会把前面已付费的成果全部回滚（实测到的设计缺口）。
            payload = {
                "spot_id": spot_id,
                "fact_package_version": item["version"],
                "summary": result.summary,
                "advantages": result.advantages,
                "issues": result.issues,
                "visitor_focus": result.visitor_focus,
                "need_review": result.need_review,
                "token_usage": int(usage.get("total_tokens") or 0),
                "model": settings.deepseek.model if not mock else "mock",
            }

            def _do_write(_payload=payload) -> None:
                with conn.cursor() as cur:
                    cur.execute(
                        REPORT_UPSERT_SQL,
                        (
                            _payload["spot_id"],
                            _payload["fact_package_version"],
                            _payload["summary"],
                            json.dumps(_payload["advantages"], ensure_ascii=False),
                            json.dumps(_payload["issues"], ensure_ascii=False),
                            json.dumps(_payload["visitor_focus"], ensure_ascii=False),
                            _payload["model"],
                            REPORT_PROMPT_VERSION,
                            _payload["need_review"],
                            _payload["token_usage"],
                            task_id,
                        ),
                    )

            outcome = write_with_retry(
                _do_write,
                prefix="spot_report",
                entries=[{"text": f"spot_id={spot_id} version={item['version']}", "payload": payload}],
                shard_key=spot_id,
                on_retry=lambda attempt, exc: print(f"      [写库重试] 第 {attempt} 次失败：{exc}"),
            )
            if not outcome.ok:
                # 不静默丢数据：记失败、写 task_log、结果已落盘待补
                stats.failed += 1
                where = outcome.recovery_path.name if outcome.recovery_path else "（磁盘不可写，未能落盘）"
                recorder.error(
                    "景点评价-写库",
                    f"第 {spot_id} 号景点评价写库失败（{outcome.last_error}）；"
                    f"已付费结果落盘待补：{where}",
                    ref_key=str(spot_id),
                )
                continue
            conn.commit()   # 逐景点提交：把断点粒度缩到一个景点
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


def _usage_result(usage: dict[str, int]):
    """把一次（或含修复轮的多次）真实用量包装成 `ChatResult`，用于 `CallStats` 统计。

    与旧版 `_zero_usage_result` 的区别：**保留 prompt/completion 拆分**。
    旧版只塞 `total_tokens`，导致 `CallStats.prompt_tokens` 恒为 0，
    费用只能按 7:3 的经验比例估算——那与"输出实际 usage 与实际费用"的要求不符。
    """
    from app.llm.client import ChatResult

    return ChatResult(
        content="",
        model="",
        usage={
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
        latency_ms=0,
        attempts=1,
    )


def _estimate_cost(call_stats: CallStats) -> dict[str, Any]:
    """景点级成本（按**实际**输入/输出 token 与单价计算，不再用经验比例估算）。

    字段与 `semantic_analysis.estimate_cost()` **保持同一形状**：
    两个生成组件若各报一套字段，费用汇总时就要写特例代码，也容易漏算。
    """
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
    text = getattr(client, "summary", None)
    return text if isinstance(text, str) else f"client={type(client).__name__}"


__all__ = ["run_spot_report", "generate_one", "load_fact_packages", "ReportStats"]
