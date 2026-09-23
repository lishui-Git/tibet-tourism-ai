# -*- coding: utf-8 -*-
"""阶段四（C-BAT-05～07）自动验证脚本。

用途：把任务书要求的小样本验收项做成**可重复执行**的检查，而不是靠人工点开数据库看。
默认全部使用 mock 客户端（零 API 消耗），且**只处理显式指定的少量评论**，
不会在 5 万条数据上写入任何内容。

默认覆盖的验收项：
    1  链路可运行：规则层（≤10 字不调模型）/ 复用层（重复正文只调一次）/ 调用层
    2  数据库写入正确且三张结果表自洽
    3  evidence 必须是评论原文子串（防模型编造依据，§15.A.4 第 5 条）
    4  枚举与数值约束（polarity 合法、intensity ∈ 1–5）
    5  幂等：重复执行不重复调用、不产生重复行
    6  断点续跑：先处理一部分，再处理剩余部分，合计不重不漏
    7  失败处理：注入失败时单条失败被记录且批次继续（不崩溃）
    8  失败补跑：按 comment_id 定向补跑成功
    9  C-BAT-06 事实包：零模型调用、六部分齐全、方面样本<10 带 note、数字与 stat_spot 一致
    10 C-BAT-07 评价：四段齐全、prompt_version 落库、幂等跳过、数字一致性校验生效
    11 全程无重复主键、无非法枚举

可选（`--full-mock-check`）：在 mock 下跑一次**全量**（5.9 万条）链路以验证规模表现，
执行前后与执行后都会清理，仅用本地开发库。**慎用**：会向结果表写入大量假数据后删除。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\verify_phase4.py
    .\\.venv\\Scripts\\python.exe scripts\\verify_phase4.py --keep          # 保留测试数据
    .\\.venv\\Scripts\\python.exe scripts\\verify_phase4.py --full-mock-check
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.batch.fact_package import run_fact_package
from app.batch.semantic_analysis import run_semantic_analysis
from app.batch.spot_report import run_spot_report
from app.db import connection, query_all, query_one
from app.llm.mock import MockClient

RUN_TAG = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
RESULTS: list[tuple[str, bool, str]] = []
TOUCHED_COMMENTS: set[int] = set()   # 本次验证写过的 comment_id（清理用）
TOUCHED_SPOTS: set[int] = set()      # 本次验证写过的 spot_id（清理用）


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def scalar(sql: str, params: tuple = ()) -> int:
    row = query_one(sql, params) or {}
    return int(list(row.values())[0] or 0)


def table_state() -> dict[str, int]:
    return {
        "sentiment": scalar("SELECT COUNT(*) FROM sentiment WHERE method='deepseek'"),
        "aspect": scalar("SELECT COUNT(*) FROM aspect WHERE method='deepseek'"),
        "semantic": scalar("SELECT COUNT(*) FROM comment_semantic"),
        "fact_package": scalar("SELECT COUNT(*) FROM spot_fact_package"),
        "spot_report": scalar("SELECT COUNT(*) FROM spot_report"),
    }


# ---------------------------------------------------------------------------
# 测试样本选择（保证三条分支都被覆盖，且样本量很小）
# ---------------------------------------------------------------------------


def pick_samples() -> dict[str, list[int]]:
    """挑出用于验证的评论：普通评论 / 低信息量评论 / 重复组成员（各自互不重叠）。"""
    normal = [
        int(r["comment_id"])
        for r in query_all(
            """
            SELECT comment_id FROM review
             WHERE is_low_info=0 AND is_dup_content=0
               AND content IS NOT NULL AND TRIM(content)<>''
               AND NOT EXISTS (SELECT 1 FROM sentiment se
                                WHERE se.comment_id=review.comment_id AND se.method='deepseek')
             ORDER BY comment_id LIMIT 12
            """
        )
    ]
    low_info = [
        int(r["comment_id"])
        for r in query_all(
            """
            SELECT comment_id FROM review
             WHERE is_low_info=1 AND is_dup_content=0
               AND content IS NOT NULL AND TRIM(content)<>''
               AND NOT EXISTS (SELECT 1 FROM sentiment se
                                WHERE se.comment_id=review.comment_id AND se.method='deepseek')
             ORDER BY comment_id LIMIT 3
            """
        )
    ]
    # 重复组：选一个"整组都还没处理过"的小组（2–5 个成员），
    # 这样一次执行就能同时验证"代表调用一次 + 其余成员复用"（BR-06）
    dup_group = query_all(
        """
        SELECT r.dup_group_id, COUNT(*) AS n,
               SUM(NOT EXISTS (SELECT 1 FROM sentiment se
                                WHERE se.comment_id = r.comment_id AND se.method='deepseek')) AS pending_n,
               SUM(r.is_low_info = 0) AS non_low_n
          FROM review r
         WHERE r.is_dup_content = 1 AND r.dup_group_id IS NOT NULL
         GROUP BY r.dup_group_id
        HAVING pending_n = COUNT(*) AND non_low_n >= 1 AND COUNT(*) BETWEEN 2 AND 5
         ORDER BY r.dup_group_id
         LIMIT 1
        """
    )
    dup: list[int] = []
    if dup_group:
        dup = [
            int(r["comment_id"])
            for r in query_all(
                "SELECT comment_id FROM review WHERE dup_group_id=%s ORDER BY comment_id",
                (int(dup_group[0]["dup_group_id"]),),
            )
        ]
    return {"normal": normal, "low_info": low_info, "dup": dup}


# ---------------------------------------------------------------------------
# C-BAT-05
# ---------------------------------------------------------------------------


def verify_semantic(samples: dict[str, list[int]]) -> None:
    print("\n[1] C-BAT-05 评论语义分析（小样本，mock）")
    ids = samples["normal"][:8] + samples["low_info"] + samples["dup"]
    if not ids:
        check("存在可用测试样本", False, "数据库中没有可用于验证的评论")
        return
    TOUCHED_COMMENTS.update(ids)
    TOUCHED_SPOTS.update(
        int(r["spot_id"]) for r in query_all(
            "SELECT DISTINCT spot_id FROM review WHERE comment_id IN (%s)"
            % ",".join(["%s"] * len(ids)),
            tuple(ids),
        )
    )

    before = table_state()
    summary = run_semantic_analysis(client=MockClient(), mock=True, only_comment_ids=ids)
    after = table_state()

    check("执行成功且未抛异常", summary["api_failed"] == 0, f"成功 {summary['api_success']} / 失败 {summary['api_failed']}")
    check("规则层已执行（≤10 字不调模型）", summary["rule_written"] > 0, f"规则判定 {summary['rule_written']} 条")
    check(
        "复用层已执行（重复正文只调一次）",
        summary["reuse_written"] > 0 and summary["dup_representatives"] > 0,
        f"复用 {summary['reuse_written']} 条，代表调用 {summary['dup_representatives']} 条"
        f"（测试组 {samples['dup']}）",
    )
    check(
        "三张结果表自洽（语义/摘要条数一致）",
        after["sentiment"] == before["sentiment"] + summary["written"]
        and after["semantic"] == after["sentiment"],
        f"sentiment {after['sentiment']} / semantic {after['semantic']} / aspect {after['aspect']}",
    )
    check(
        "写库条数 = 规则 + 复用 + 调用成功",
        summary["written"] == summary["rule_written"] + summary["reuse_written"] + summary["api_success"],
        f"{summary['written']} = {summary['rule_written']} + {summary['reuse_written']} + {summary['api_success']}",
    )

    # evidence 子串校验（只查本次写入的评论）
    placeholders = ",".join(["%s"] * len(ids))
    bad_evidence = scalar(
        f"""
        SELECT COUNT(*) FROM aspect a JOIN review r ON r.comment_id = a.comment_id
         WHERE a.method='deepseek' AND a.evidence IS NOT NULL AND a.evidence <> ''
           AND LOCATE(a.evidence, r.content) = 0
           AND a.comment_id IN ({placeholders})
        """,
        tuple(ids),
    )
    check("evidence 全部可在原文定位（防编造）", bad_evidence == 0, f"不匹配 {bad_evidence} 条")

    bad_polarity = scalar(
        f"SELECT COUNT(*) FROM sentiment WHERE method='deepseek' "
        f"AND polarity NOT IN ('positive','neutral','negative') AND comment_id IN ({placeholders})",
        tuple(ids),
    )
    bad_intensity = scalar(
        f"SELECT COUNT(*) FROM sentiment WHERE method='deepseek' "
        f"AND intensity IS NOT NULL AND (intensity<1 OR intensity>5) AND comment_id IN ({placeholders})",
        tuple(ids),
    )
    check("polarity 枚举合法", bad_polarity == 0, f"非法 {bad_polarity} 条")
    check("intensity ∈ 1–5", bad_intensity == 0, f"越界 {bad_intensity} 条")

    # 复用层的成员必须与代表同源（raw_json 记录 reused_from_comment_id）
    reuse_ok = scalar(
        f"""
        SELECT COUNT(*) FROM sentiment
         WHERE method='deepseek' AND comment_id IN ({placeholders})
           AND JSON_EXTRACT(raw_json, '$.source') = 'reuse'
           AND JSON_EXTRACT(raw_json, '$.reused_from_comment_id') IS NULL
        """,
        tuple(ids),
    )
    check("复用结果可追溯到代表评论", reuse_ok == 0, f"缺少来源标记 {reuse_ok} 条")

    # ---- 幂等：重跑同样这批 ID，应无待处理数据 ------------------------------
    calls_before = table_state()["sentiment"]
    rerun = run_semantic_analysis(client=MockClient(), mock=True, only_comment_ids=ids)
    calls_after = table_state()["sentiment"]
    dup_keys = scalar(
        "SELECT COUNT(*) FROM (SELECT comment_id FROM sentiment WHERE method='deepseek' "
        "GROUP BY comment_id HAVING COUNT(*)>1) t"
    )
    check("重跑无待处理数据（断点续跑生效）", rerun["api_calls_planned"] == 0, f"计划调用 {rerun['api_calls_planned']}")
    check("重跑不新增行", calls_after == calls_before, f"{calls_before} → {calls_after}")
    check("sentiment 无重复主键", dup_keys == 0, f"重复 {dup_keys} 条")

    # ---- 断点续跑：先 2 条，再续剩余 ---------------------------------------
    resume_ids = samples["normal"][8:12]
    if len(resume_ids) >= 3:
        TOUCHED_COMMENTS.update(resume_ids)
        first = run_semantic_analysis(client=MockClient(), mock=True, only_comment_ids=resume_ids[:2])
        mid = table_state()["sentiment"]
        second = run_semantic_analysis(client=MockClient(), mock=True, only_comment_ids=resume_ids[2:])
        end = table_state()["sentiment"]
        check(
            "模拟中断后可从断点继续",
            first["api_success"] == 2 and second["api_success"] == len(resume_ids) - 2,
            f"首轮 {first['api_success']} + 续跑 {second['api_success']} 条",
        )
        check("续跑合计不重不漏", end == mid + len(resume_ids) - 2, f"{mid} → {end}")
    else:
        check("模拟中断后可从断点继续", False, "可用样本不足（需 ≥3 条未处理评论）")

    # ---- 失败注入：单条失败不影响整批 + 补跑 --------------------------------
    fail_ids = [
        int(r["comment_id"])
        for r in query_all(
            """
            SELECT comment_id FROM review
             WHERE is_low_info=0 AND is_dup_content=0 AND content IS NOT NULL AND TRIM(content)<>''
               AND NOT EXISTS (SELECT 1 FROM sentiment se
                                WHERE se.comment_id=review.comment_id AND se.method='deepseek')
             ORDER BY comment_id LIMIT 6
            """
        )
    ]
    if len(fail_ids) == 6:
        TOUCHED_COMMENTS.update(fail_ids)
        outcome = run_semantic_analysis(client=MockClient(fail_every=3), mock=True, only_comment_ids=fail_ids)
        attempted = outcome["api_success"] + outcome["api_failed"]
        # 失败注入是"每 3 次调用失败 1 次"，因此 6 条候选必然产生 2 次失败；
        # 由于并发执行，"哪 2 条"不确定，这里只断言**总数**与"批次未崩溃"。
        check(
            "注入失败后批次继续（未崩溃）",
            outcome["api_failed"] == 2 and outcome["api_success"] == 4 and attempted == 6,
            f"成功 {outcome['api_success']} / 失败 {outcome['api_failed']} / 合计 {attempted}（计划 6）",
        )
        placeholders6 = ",".join(["%s"] * 6)
        error_logs = scalar(
            f"SELECT COUNT(*) FROM task_log WHERE level='ERROR' AND stage='semantic' AND resolved=0 "
            f"AND ref_key IN ({placeholders6})",
            tuple(str(i) for i in fail_ids),
        )
        check("失败已写入 task_log（可据此补跑）", error_logs == outcome["api_failed"],
              f"ERROR 日志 {error_logs} 条 / 失败 {outcome['api_failed']} 条")

        failed_ids = [
            int(r["comment_id"])
            for r in query_all(
                f"""
                SELECT comment_id FROM review
                 WHERE comment_id IN ({placeholders6})
                   AND NOT EXISTS (SELECT 1 FROM sentiment se
                                    WHERE se.comment_id=review.comment_id AND se.method='deepseek')
                """,
                tuple(fail_ids),
            )
        ]
        if failed_ids:
            retry = run_semantic_analysis(client=MockClient(), mock=True, only_comment_ids=failed_ids)
            check("失败条目可按 comment_id 补跑", retry["api_success"] == len(failed_ids), f"补跑 {retry['api_success']} 条")
    else:
        check("注入失败后批次继续（未崩溃）", False, "可用样本不足（需 6 条未处理评论）")


# ---------------------------------------------------------------------------
# C-BAT-06
# ---------------------------------------------------------------------------


def verify_fact_package() -> list[dict]:
    print("\n[2] C-BAT-06 景点事实包（零模型调用）")
    summary = run_fact_package(limit=3)
    check("事实包已生成", summary["generated"] >= 1, f"生成 {summary['generated']} 个景点")
    TOUCHED_SPOTS.update(
        int(r["spot_id"]) for r in query_all("SELECT spot_id FROM spot_fact_package ORDER BY spot_id LIMIT 3")
    )

    packages: list[dict] = []
    for row in query_all("SELECT package_json FROM spot_fact_package ORDER BY spot_id LIMIT 3"):
        package = row["package_json"]
        packages.append(json.loads(package) if isinstance(package, str) else package)

    if not packages:
        check("事实包结构", False, "没有读到事实包")
        return packages

    pkg = packages[0]
    sections = ["spot_id", "spot_name", "version", "basic", "statistics", "sentiment",
                "aspects", "representative_reviews", "caliber"]
    missing = [k for k in sections if k not in pkg]
    check("结构齐全（六部分 + 口径）", not missing, "缺少：" + str(missing))
    check(
        "caliber 口径齐全（BR-02 / BR-04 / BR-01）",
        pkg["caliber"]["min_review_count"] == 100
        and pkg["caliber"]["aspect_min_sample"] == 10
        and pkg["caliber"]["ip_valid_since"] == "2022-08-01",
        json.dumps(pkg["caliber"], ensure_ascii=False)[:90],
    )
    excluded = [a for a in pkg["aspects"] if a["sample_size"] < 10]
    check(
        "方面样本<10 带 note 且不给比率（BR-04）",
        all(a["note"] and a["positive_rate"] is None for a in excluded),
        f"排除 {len(excluded)} 个方面",
    )
    stat = query_one(
        "SELECT avg_score, review_count FROM stat_spot WHERE spot_id=%s", (int(pkg["spot_id"]),)
    )
    check(
        "统计数字与 stat_spot 完全一致（程序读取，非模型计算）",
        stat is not None
        and abs(float(pkg["statistics"]["avg_score"]) - float(stat["avg_score"])) < 1e-9
        and int(pkg["statistics"]["review_count"]) == int(stat["review_count"]),
        f"avg_score={pkg['statistics']['avg_score']} / review_count={pkg['statistics']['review_count']}",
    )
    check(
        "情感占比口径来自 method='deepseek'",
        pkg["sentiment"]["method"] == "deepseek"
        and pkg["sentiment"]["sample_size"] >= 0,
        f"样本量 {pkg['sentiment']['sample_size']}",
    )
    return packages


# ---------------------------------------------------------------------------
# C-BAT-07
# ---------------------------------------------------------------------------


def verify_spot_report(packages: list[dict]) -> None:
    print("\n[3] C-BAT-07 景点智能评价（mock）")
    if not packages:
        check("评价生成", False, "没有可用事实包")
        return
    spot_ids = [int(p["spot_id"]) for p in packages]
    TOUCHED_SPOTS.update(spot_ids)

    summary = run_spot_report(client=MockClient(), mock=True, spot_ids=spot_ids)
    check("评价已生成", summary["generated"] >= 1, f"生成 {summary['generated']} 份")

    row = query_one(
        "SELECT spot_id, fact_package_version, summary, advantages_json, visitor_focus_json, "
        "prompt_version, need_review, token_usage FROM spot_report WHERE spot_id IN (%s) "
        "ORDER BY generated_at LIMIT 1" % ",".join(["%s"] * len(spot_ids)),
        tuple(spot_ids),
    )
    if row:
        adv = row["advantages_json"]
        adv = json.loads(adv) if isinstance(adv, str) else adv
        foc = row["visitor_focus_json"]
        foc = json.loads(foc) if isinstance(foc, str) else foc
        check(
            "四段字段齐全且条目合规",
            bool(row["summary"]) and isinstance(adv, list) and len(adv) >= 2 and len(foc) >= 2,
            f"summary {len(row['summary'])} 字 / 优势 {len(adv)} 条 / 关注点 {len(foc)} 条",
        )
        check("prompt_version / token_usage 已落库（结果可追溯）",
              bool(row["prompt_version"]), f"prompt={row['prompt_version']} tokens={row['token_usage']}")

    again = run_spot_report(client=MockClient(), mock=True, spot_ids=spot_ids)
    check("重复生成被幂等跳过", again["skipped"] >= 1 and again["generated"] == 0,
          f"跳过 {again['skipped']} / 新生成 {again['generated']}")

    from app.llm.validators import ReportResult, check_number_consistency

    fake = ReportResult(
        summary="该景点好评率高达 99.99%，评论量 123456 条，整体表现远超同类。",
        advantages=["好评率高"],
        issues=["无明显问题"],
        visitor_focus=["景观"],
    )
    mismatches = check_number_consistency(fake, packages[0])
    check("数字与事实包不符会被检出（→ need_review=1）", len(mismatches) > 0,
          f"检出 {len(mismatches)} 处：{mismatches[:2]}")


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------


def cleanup() -> None:
    """清理本次验证写入的数据。

    除了本次显式处理过的 comment_id / spot_id，还必须清理**它们的连带结果**：
      · 复用层会为"同组的其他成员"写入语义结果（这些 id 不在处理清单里）；
      · 事实包与评价是按 spot 维度的，直接按本次涉及的 spot_id 删除。
    """
    with connection() as conn:
        with conn.cursor() as cur:
            task_ids = [
                int(r["task_id"])
                for r in query_all(
                    "SELECT task_id FROM analysis_task WHERE created_at >= %s "
                    "AND task_type IN ('semantic','fact_package','spot_report')",
                    (RUN_TAG,),
                )
            ]

            # 展开："复用自本次处理过的代表"的所有成员评论，也要一并清理
            comment_ids = set(TOUCHED_COMMENTS)
            if TOUCHED_COMMENTS:
                rep_ids = tuple(sorted(TOUCHED_COMMENTS))
                ph = ",".join(["%s"] * len(rep_ids))
                for row in query_all(
                    "SELECT comment_id FROM sentiment WHERE method='deepseek' "
                    f"AND JSON_EXTRACT(raw_json, '$.reused_from_comment_id') IN ({ph})",
                    rep_ids,
                ):
                    comment_ids.add(int(row["comment_id"]))

            if comment_ids:
                ids = tuple(sorted(comment_ids))
                ph = ",".join(["%s"] * len(ids))
                cur.execute(f"DELETE FROM aspect WHERE comment_id IN ({ph})", ids)
                cur.execute(f"DELETE FROM comment_semantic WHERE comment_id IN ({ph})", ids)
                cur.execute(f"DELETE FROM sentiment WHERE method='deepseek' AND comment_id IN ({ph})", ids)

            if TOUCHED_SPOTS:
                spots = tuple(sorted(TOUCHED_SPOTS))
                ph = ",".join(["%s"] * len(spots))
                cur.execute(f"DELETE FROM spot_report WHERE spot_id IN ({ph})", spots)
                cur.execute(f"DELETE FROM spot_fact_package WHERE spot_id IN ({ph})", spots)

            if task_ids:
                ph = ",".join(["%s"] * len(task_ids))
                cur.execute(f"DELETE FROM task_log WHERE task_id IN ({ph})", tuple(task_ids))
                cur.execute(f"DELETE FROM analysis_task WHERE task_id IN ({ph})", tuple(task_ids))


def precheck_clean() -> bool:
    """运行前的环境检查：结果表必须是空的，否则说明有未清理的测试数据。

    为什么需要：本脚本用 mock 写入**假结果**，如果库里有残留，用户可能误以为是真实结果；
    同时残留会占用测试样本（待处理集合变小），导致"某分支没被执行到"的假失败。
    """
    state = table_state()
    dirty = {k: v for k, v in state.items() if v}
    if dirty:
        print("\n[环境检查未通过] 阶段四结果表存在残留数据（可能来自上次未清理的 mock 运行）：")
        print("  " + json.dumps(dirty, ensure_ascii=False))
        print("  处理方式：确认这些不是真实结果后清空，或上次运行去掉 --keep 以便自动清理。")
        return False
    return True


def full_mock_check() -> None:
    """（可选）在 mock 下跑一次全量，验证 5.9 万条规模下的分组/断点/写库表现，并清理。"""
    print("\n[4] 全量链路 mock 压测（--full-mock-check；会写入后删除假数据）")
    before = table_state()["sentiment"]
    summary = run_semantic_analysis(client=MockClient(), mock=True)
    check("全量 mock 语义分析完成", summary["api_failed"] == 0 and summary["api_success"] > 40000,
          f"成功 {summary['api_success']} / 规则 {summary['rule_written']} / 复用 {summary['reuse_written']}")
    after = table_state()["sentiment"]
    check(
        "写入计数 = 库内实际新增行数（口径不虚高）",
        after - before == summary["written"],
        f"库内 {before} → {after}（新增 {after - before}），写入计数 {summary['written']}",
    )
    dup_keys = scalar(
        "SELECT COUNT(*) FROM (SELECT comment_id FROM sentiment WHERE method='deepseek' "
        "GROUP BY comment_id HAVING COUNT(*)>1) t"
    )
    check("全量后无重复主键", dup_keys == 0, f"重复 {dup_keys} 条")
    bad_evidence = scalar(
        "SELECT COUNT(*) FROM aspect a JOIN review r ON r.comment_id=a.comment_id "
        "WHERE a.method='deepseek' AND a.evidence IS NOT NULL AND a.evidence<>'' "
        "AND LOCATE(a.evidence, r.content)=0"
    )
    check("全量后 evidence 全部可定位", bad_evidence == 0, f"不匹配 {bad_evidence} 条")

    print("  清理全量 mock 数据 …")
    with connection() as conn:
        with conn.cursor() as cur:
            task_ids = [
                int(r["task_id"])
                for r in query_all(
                    "SELECT task_id FROM analysis_task WHERE created_at >= %s AND task_type='semantic'",
                    (RUN_TAG,),
                )
            ]
            cur.execute("DELETE FROM aspect WHERE method='deepseek' AND created_at >= %s", (RUN_TAG,))
            cur.execute("DELETE FROM comment_semantic WHERE created_at >= %s", (RUN_TAG,))
            cur.execute("DELETE FROM sentiment WHERE method='deepseek' AND created_at >= %s", (RUN_TAG,))
            if task_ids:
                ph = ",".join(["%s"] * len(task_ids))
                cur.execute(f"DELETE FROM task_log WHERE task_id IN ({ph})", tuple(task_ids))
                cur.execute(f"DELETE FROM analysis_task WHERE task_id IN ({ph})", tuple(task_ids))
    after = table_state()
    check("清理后结果表回到空状态",
          after["sentiment"] == 0 and after["semantic"] == 0 and after["aspect"] == 0,
          json.dumps(after, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="阶段四 C-BAT-05～07 自动验证（默认 mock，零 API 消耗）")
    parser.add_argument("--keep", action="store_true", help="保留测试数据（默认自动清理）")
    parser.add_argument("--full-mock-check", action="store_true", help="额外做一次全量 mock 压测（写入后清理）")
    args = parser.parse_args(argv)

    print("=" * 78)
    print(f"阶段四验证开始（mock 客户端，小样本；本次窗口 = {RUN_TAG}）")
    print("=" * 78)

    if not precheck_clean():
        return 2

    samples = pick_samples()
    print("测试样本：" + json.dumps({k: v[:6] for k, v in samples.items()}, ensure_ascii=False))

    verify_semantic(samples)
    packages = verify_fact_package()
    verify_spot_report(packages)
    if args.full_mock_check:
        full_mock_check()

    if not args.keep:
        before_cleanup = table_state()
        cleanup()
        left = table_state()
        check(
            "清理后结果表回到空状态（无假数据残留）",
            left["sentiment"] == 0 and left["semantic"] == 0 and left["aspect"] == 0
            and left["fact_package"] == 0 and left["spot_report"] == 0,
            f"清理前 {json.dumps(before_cleanup, ensure_ascii=False)} → 清理后 {json.dumps(left, ensure_ascii=False)}",
        )
        print("\n已清理本次验证写入的数据：" + json.dumps(left, ensure_ascii=False))
    else:
        print("\n按 --keep 保留测试数据（记得手工清理，避免假数据混入真实结果）")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 78)
    print(f"验证结果：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 78)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
