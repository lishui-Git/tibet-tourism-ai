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
import itertools
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
TOUCHED_COMMENTS: set[int] = set()   # 本次验证写过的 comment_id（仅用于报告）
TOUCHED_SPOTS: set[int] = set()      # 本次验证写过的 spot_id（仅用于报告）
SNAPSHOT: dict[str, set[int]] = {"sentiment": set(), "semantic": set(), "fact_package": set(), "spot_report": set()}


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


def _pick_real_samples() -> dict[str, list[int]]:
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
# 【全量跑完后的可测性】临时合成样本
# ---------------------------------------------------------------------------
# 背景：本脚本需要"**尚未处理**的评论"来演练四个场景（规则层 / 复用层 / 断点续跑 /
# 失败注入后批次继续）。全量 semantic 一旦跑完，库里就几乎没有未处理评论了
# （实测只剩 2 条失败项），于是这些场景会因为"样本不足"而**无法验证**。
#
# 处置：临时**插入**一批合成评论（`comment_id` 从 990,000,001 起，远高于真实最大值
# 828,505,175；`dup_group_id` 用 999,001 起，远高于真实最大值 1,285），
# 让这些场景仍然可测；运行结束时**连同其结果一并删除**，并且启动时自愈上次的残留。
# 这样既不重跑真实 API、也不改动任何真实行。
TEMP_COMMENT_ID_BASE = 990_000_001
TEMP_DUP_GROUP_BASE = 999_001
TEMP_REVIEW_IDS: set[int] = set()   # 本次插入的临时评论（必须全部删除）
# 递增分配 ID：即使 `install_temp_samples` 被调用多次，也不会撞主键。
_TEMP_ID_SEQ = itertools.count(TEMP_COMMENT_ID_BASE)


def purge_temp_samples() -> int:
    """清掉上次异常退出残留的临时合成评论（`comment_id >= TEMP_COMMENT_ID_BASE`）。

    与 `purge_test_traces` 一样，属于"自愈"：万一上次在插入之后、清理之前被强杀，
    残留的临时评论会一直留在 `review` 里；这里在取快照前先清干净。
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT comment_id FROM review WHERE comment_id >= %s", (TEMP_COMMENT_ID_BASE,)
            )
            ids = [int(r["comment_id"]) for r in cur.fetchall()]
            if not ids:
                return 0
            ph = ",".join(["%s"] * len(ids))
            t = tuple(ids)
            cur.execute(f"DELETE FROM aspect WHERE comment_id IN ({ph})", t)
            cur.execute(f"DELETE FROM comment_semantic WHERE comment_id IN ({ph})", t)
            cur.execute(f"DELETE FROM sentiment WHERE comment_id IN ({ph})", t)
            cur.execute(f"DELETE FROM review WHERE comment_id IN ({ph})", t)
    TEMP_REVIEW_IDS.clear()
    return len(ids)


def install_temp_samples(need_normal: int = 24, need_low: int = 3, need_dup: int = 3) -> dict[str, list[int]]:
    """临时插入一批"未处理评论"，用于在全量跑完后继续验证四个场景。

    数量为什么是 24 条普通样本：三个场景会**依次**消耗"未处理评论"——
    `verify_semantic` 用 8 条、断点续跑用 3 条、失败注入用 6 条（合计 17 条），
    再留若干余量，避免"前一个场景把样本吃光、后一个场景又报样本不足"。

    数据全部以 `【临时测试】` 开头以便人工辨认；插入的 ID 记入 `TEMP_REVIEW_IDS`，
    由 `cleanup()` 负责删除（并在启动时由 `purge_temp_samples()` 兜底）。
    """
    spot_id = int(
        (query_all("SELECT spot_id FROM spot WHERE has_full_evaluation=1 ORDER BY spot_id LIMIT 1")
         or [{"spot_id": 1}])[0]["spot_id"]
    )
    rows: list[tuple] = []
    normal: list[int] = []
    low_info: list[int] = []
    dup: list[int] = []

    for i in range(need_normal):
        cid = next(_TEMP_ID_SEQ)
        body = f"【临时测试】普通评论样本 {i}：这里的风景很好，值得一去。"
        rows.append((cid, spot_id, body, len(body), 0, 0, None))
        normal.append(cid)
    for i in range(need_low):
        cid = next(_TEMP_ID_SEQ)
        body = f"不错{i}"          # ≤10 字 → 走规则层（BR-05）
        rows.append((cid, spot_id, body, len(body), 1, 0, None))
        low_info.append(cid)
    for i in range(need_dup):
        cid = next(_TEMP_ID_SEQ)
        body = "【临时测试】重复正文样本：景色不错。"   # 同组正文完全相同
        rows.append((cid, spot_id, body, len(body), 0, 1, TEMP_DUP_GROUP_BASE))
        dup.append(cid)

    with connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO review "
                "(comment_id, spot_id, content, content_length, is_low_info, is_dup_content, "
                " dup_group_id, publish_date, user_nick) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'2024-01-01','临时测试')",
                rows,
            )
    TEMP_REVIEW_IDS.update(r[0] for r in rows)
    print(
        f"      · 已临时插入合成样本 {len(rows)} 条"
        f"（普通 {need_normal} / 低信息量 {need_low} / 重复组 {need_dup}，"
        f"comment_id ≥ {TEMP_COMMENT_ID_BASE}）——运行结束会全部删除"
    )
    return {"normal": normal, "low_info": low_info, "dup": dup}


def pick_samples() -> dict[str, list[int]]:
    """取验证样本：优先用库里**真实**的未处理评论；不够时临时合成一批。

    为什么要"不够时合成"：全量 semantic 跑完后库里几乎没有未处理评论，
    而本脚本的四个场景（规则层 / 复用层 / 断点续跑 / 失败注入）都要求"有未处理评论"。
    若直接跳过这些场景，就等于"因为跑完了所以不再验证"，那是不可接受的。
    """
    real = _pick_real_samples()
    if len(real["normal"]) >= 8 and len(real["dup"]) >= 2:
        return real
    print(
        f"      · 库内未处理评论不足（普通 {len(real['normal'])} 条、重复组 {len(real['dup'])} 条）"
        "——全量已跑完，改为临时合成样本"
    )
    return install_temp_samples()


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
    rule_source_n = scalar(
        "SELECT COUNT(*) FROM comment_semantic WHERE source='rule' AND comment_id IN (%s)"
        % ",".join(["%s"] * len(ids)),
        tuple(ids),
    )
    low_info_left = scalar(
        "SELECT COUNT(*) FROM review r WHERE r.is_low_info=1 AND r.content IS NOT NULL "
        "AND TRIM(r.content)<>'' AND NOT EXISTS (SELECT 1 FROM sentiment se "
        "WHERE se.comment_id=r.comment_id AND se.method='deepseek')"
    )
    if samples["low_info"]:
        check(
            "规则层已执行（≤10 字走规则、未调模型）",
            rule_source_n > 0,
            f"本次 source='rule' 写入 {rule_source_n} 条（样本含低信息量 {len(samples['low_info'])} 条）",
        )
    else:
        # 库里的低信息量评论已被处理完（本轮真实/规则运行覆盖了全部 10,228 条），
        # 没有可用的样本 → 这不是失败，而是"该分支无可测输入"，如实记录。
        check(
            "规则层（≤10 字走规则）已无待处理样本（此前已全部覆盖）",
            low_info_left == 0,
            f"库内未处理的低信息量评论 = {low_info_left}；规则分支此前已实际执行并入库",
        )
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
        # 【为什么必须限定 task_id】样本里可能包含"历史上就失败过的评论"（例如全量跑完后
        # 遗留的 2 条真实失败项，它们在 task_log 里已有 resolved=0 的 ERROR 行）。
        # 若只按 ref_key 统计，会把**历史**错误也计进来（实测 6 条 vs 本次失败 2 条）。
        # 这条断言的本意是"**本次**失败被登记下来了"，因此按本次任务的 task_id 收窄。
        error_logs = scalar(
            f"SELECT COUNT(*) FROM task_log WHERE level='ERROR' AND stage='semantic' AND resolved=0 "
            f"AND task_id=%s AND ref_key IN ({placeholders6})",
            (int(outcome["task_id"]), *tuple(str(i) for i in fail_ids)),
        )
        check("失败已写入 task_log（可据此补跑）", error_logs == outcome["api_failed"],
              f"本次 ERROR 日志 {error_logs} 条 / 失败 {outcome['api_failed']} 条")

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


def purge_test_traces() -> int:
    """删除结果表里**确定属于测试痕迹**的行（`raw_json.mode='mock'`），返回删除条数。

    ## 【本轮修正的一处严重缺陷】为什么不再删 `mode='reuse'`
    原实现把 `mode IN ('mock','reuse')` 一律当测试痕迹删除，注释写着"绝不会误删真实结果"。
    这个假设在**全量生产运行之后不再成立**：

      · `mode='mock'` —— 只有 `--mock` 路径会写，**唯一**属于测试痕迹，可以安全清理；
      · `mode='reuse'` —— **生产也会合法写**！C-BAT-05 的复用层（BR-06：重复正文组内
        只调一次、其余成员复制代表结果）在全量运行时就会产出这种行。

    实测事故：全量 semantic 跑完后库内有 **1,071** 条合法的 `mode='reuse'` 行，
    本函数在启动自愈时把它们**连同 aspect / comment_semantic 一起删光**，
    等于"测试脚本破坏了生产数据"（总量 59,029 → 57,959）。
    因此这里改为**只清理 `mock`**，`reuse` 一律不动。

    需要清理历史遗留的复用行时，请**显式**处理，不要依赖本函数的自动自愈。

    范围仍限于本脚本会写的三张表：`sentiment`(deepseek) / `comment_semantic` / `aspect`。
    """
    marked: list[int] = []
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT comment_id FROM sentiment WHERE method='deepseek' "
                "AND JSON_UNQUOTE(JSON_EXTRACT(raw_json,'$.mode'))='mock'"
            )
            marked = [int(r["comment_id"]) for r in cur.fetchall()]
            if not marked:
                return 0
            ph = ",".join(["%s"] * len(marked))
            cur.execute(f"DELETE FROM aspect WHERE comment_id IN ({ph})", tuple(marked))
            cur.execute(f"DELETE FROM comment_semantic WHERE comment_id IN ({ph})", tuple(marked))
            cur.execute(
                f"DELETE FROM sentiment WHERE method='deepseek' AND comment_id IN ({ph})",
                tuple(marked),
            )
    return len(marked)


def count_reuse_rows() -> int:
    """统计库内 `mode='reuse'` 的**合法复用行**条数（只读，仅用于提示，绝不删除）。

    单独抽出来是为了让"复用行有多少"这个信息仍然可见——它是有意义的可见指标
    （代表 BR-06 复制了多少条），但**不是**测试痕迹。
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM sentiment WHERE method='deepseek' "
                "AND JSON_UNQUOTE(JSON_EXTRACT(raw_json,'$.mode'))='reuse'"
            )
            row = cur.fetchone() or {}
    return int(row.get("n") or 0)


def snapshot_results() -> dict[str, set[int]]:
    """记录验证开始前**已存在**的结果 ID。

    清理时只删除"新增的 ID"——这样库里的真实结果（本阶段已完成的小样本）
    不会被验证脚本误删，也不会因为测试碰到同一个 ID 而受牵连。
    """
    return {
        "sentiment": {
            int(r["comment_id"])
            for r in query_all("SELECT comment_id FROM sentiment WHERE method='deepseek'")
        },
        "semantic": {int(r["comment_id"]) for r in query_all("SELECT comment_id FROM comment_semantic")},
        "fact_package": {int(r["spot_id"]) for r in query_all("SELECT spot_id FROM spot_fact_package")},
        "spot_report": {int(r["spot_id"]) for r in query_all("SELECT spot_id FROM spot_report")},
    }


def cleanup() -> None:
    """清理本次验证写入的数据：**只删快照之后新增的 ID**。

    比"按时间窗删除"更精确：同一 ID 的旧行（真实结果）会被保留，
    本次新写入的行会被删除；复用层为其他成员写入的行也在"新增 ID"集合里。
    """
    snapshot = SNAPSHOT
    with connection() as conn:
        with conn.cursor() as cur:
            # 【先删临时合成评论】它们不在快照里，必须显式删除（含 review 行本身）。
            # 放在最前面：即使后面的快照差集逻辑出问题，临时测试数据也不会留在生产表里。
            if TEMP_REVIEW_IDS:
                tids = tuple(sorted(TEMP_REVIEW_IDS))
                ph = ",".join(["%s"] * len(tids))
                cur.execute(f"DELETE FROM aspect WHERE comment_id IN ({ph})", tids)
                cur.execute(f"DELETE FROM comment_semantic WHERE comment_id IN ({ph})", tids)
                cur.execute(f"DELETE FROM sentiment WHERE method='deepseek' AND comment_id IN ({ph})", tids)
                cur.execute(f"DELETE FROM review WHERE comment_id IN ({ph})", tids)
                TEMP_REVIEW_IDS.clear()

            task_ids = [
                int(r["task_id"])
                for r in query_all(
                    "SELECT task_id FROM analysis_task WHERE created_at >= %s "
                    "AND task_type IN ('semantic','fact_package','spot_report')",
                    (RUN_TAG,),
                )
            ]

            new_comments = {
                int(r["comment_id"])
                for r in query_all("SELECT comment_id FROM sentiment WHERE method='deepseek'")
            } - snapshot["sentiment"]
            new_comments |= {
                int(r["comment_id"]) for r in query_all("SELECT comment_id FROM comment_semantic")
            } - snapshot["semantic"]

            if new_comments:
                ids = tuple(sorted(new_comments))
                ph = ",".join(["%s"] * len(ids))
                cur.execute(f"DELETE FROM aspect WHERE comment_id IN ({ph})", ids)
                cur.execute(f"DELETE FROM comment_semantic WHERE comment_id IN ({ph})", ids)
                cur.execute(f"DELETE FROM sentiment WHERE method='deepseek' AND comment_id IN ({ph})", ids)

            new_spots = {int(r["spot_id"]) for r in query_all("SELECT spot_id FROM spot_fact_package")}
            new_spots |= {int(r["spot_id"]) for r in query_all("SELECT spot_id FROM spot_report")}
            new_spots -= snapshot["fact_package"] | snapshot["spot_report"]
            if new_spots:
                spots = tuple(sorted(new_spots))
                ph = ",".join(["%s"] * len(spots))
                cur.execute(f"DELETE FROM spot_report WHERE spot_id IN ({ph})", spots)
                cur.execute(f"DELETE FROM spot_fact_package WHERE spot_id IN ({ph})", spots)

            # 行数恢复到快照规模后，按快照外的 ID 再清一次（覆盖"复用写入但无 sentiment 行"等边角）
            if task_ids:
                ph = ",".join(["%s"] * len(task_ids))
                cur.execute(f"DELETE FROM task_log WHERE task_id IN ({ph})", tuple(task_ids))
                cur.execute(f"DELETE FROM analysis_task WHERE task_id IN ({ph})", tuple(task_ids))


def precheck_clean() -> bool:
    """运行前的环境检查：不得残留 **mock 假数据**，但**真实结果必须保留**。"""
    mock_tasks = query_all("SELECT task_id FROM analysis_task WHERE task_name LIKE %s", ("%[mock]%",))
    if mock_tasks:
        ids = tuple(int(r["task_id"]) for r in mock_tasks)
        ph = ",".join(["%s"] * len(ids))
        print(f"\n[环境检查] 发现 {len(ids)} 个历史 mock 任务，清理其登记：{ids}")
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM task_log WHERE task_id IN ({ph})", ids)
                cur.execute(f"DELETE FROM analysis_task WHERE task_id IN ({ph})", ids)

    state = table_state()
    print("[环境检查] 真实结果将被保留，本脚本只清理自己新写入的数据")
    print("  运行前结果表：" + json.dumps(state, ensure_ascii=False))
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

    print("  清理全量 mock 数据（按快照差异删除，保留真实结果）…")
    cleanup()
    after = table_state()
    expected = {k: len(v) for k, v in SNAPSHOT.items()}
    check(
        "全量压测后清理干净（真实结果未被误删）",
        after["sentiment"] == expected["sentiment"]
        and after["semantic"] == expected["semantic"]
        and after["fact_package"] == expected["fact_package"]
        and after["spot_report"] == expected["spot_report"],
        f"快照 {json.dumps(expected, ensure_ascii=False)} → 清理后 {json.dumps(after, ensure_ascii=False)}",
    )


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

    # 【自愈】先把上一次"非正常退出"留下的**测试痕迹**清掉，再取快照。
    # 为什么必须有这一步（实测踩到的坑）：
    #   若本脚本在**写入之后、清理之前**被强杀（例如外部中断），`finally` 不会执行，
    #   于是 mock 行会残留在结果表里。更糟的是：**下一次运行时，
    #   这些残留会被并进快照**，从而被当成"验证前就存在的真实数据"而**永不被清理**——
    #   污染会变得"粘住"，只能靠人工发现（这次就是被 preflight 的 mock 检查抓出来的）。
    #   在取快照前清掉它们，使脚本对"被中断"具备自愈能力。
    #
    # 【⚠️ 只清 mock，不清 reuse】原实现连 `mode='reuse'` 一起删，但复用行**生产也会合法写**
    # （BR-06：重复正文组内只调一次代表，其余成员复制结果）。实测已造成事故：
    # 全量 semantic 跑完后的 1,071 条合法复用行被本函数删光。详见 `purge_test_traces` 的说明。
    purged = purge_test_traces()
    if purged:
        print(f"[自愈] 清理上次残留的 mock 测试痕迹：{purged} 条")
    purged_temp = purge_temp_samples()
    if purged_temp:
        print(f"[自愈] 清理上次残留的**临时合成评论**：{purged_temp} 条（comment_id ≥ {TEMP_COMMENT_ID_BASE}）")
    reuse_rows = count_reuse_rows()
    if reuse_rows:
        print(
            f"[信息] 库内现有 {reuse_rows} 条 `mode='reuse'` 行——这是 **BR-06 的合法生产结果**"
            "（重复正文组内复用代表结果），**不会**被本脚本清理。"
        )

    global SNAPSHOT
    SNAPSHOT = snapshot_results()

    samples = pick_samples()
    print("测试样本：" + json.dumps({k: v[:6] for k, v in samples.items()}, ensure_ascii=False))

    # 【必须 try/finally】验证步骤若中途抛异常（历史上真实发生过：一处
    # `estimate_cost` 被同名导入遮蔽导致 TypeError），清理就永远不会执行，
    # 于是 mock 行留在结果表里、污染"真实调用数"与费用核算，
    # 而且任务行会停在 running。把清理放进 finally，保证"崩了也要收拾干净"。
    crashed = False
    try:
        verify_semantic(samples)
        packages = verify_fact_package()
        verify_spot_report(packages)
        if args.full_mock_check:
            full_mock_check()
    except Exception as exc:  # noqa: BLE001
        crashed = True
        print(f"\n[异常] 验证过程中断：{type(exc).__name__}: {exc}")
        print("       仍会执行清理（避免 mock 数据残留在结果表里）")
    finally:
        if not args.keep:
            before_cleanup = table_state()
            cleanup()
            left = table_state()
            expected = {k: len(v) for k, v in SNAPSHOT.items()}
            check(
                "清理后回到验证前状态（真实结果未被误删）",
                left["sentiment"] == expected["sentiment"]
                and left["semantic"] == expected["semantic"]
                and left["fact_package"] == expected["fact_package"]
                and left["spot_report"] == expected["spot_report"],
                f"快照 {json.dumps(expected, ensure_ascii=False)} → 清理后 {json.dumps(left, ensure_ascii=False)}"
                f"（清理前 {json.dumps(before_cleanup, ensure_ascii=False)}）",
            )
            print("\n已清理本次验证新写入的数据；库内原有结果保留：" + json.dumps(left, ensure_ascii=False))
            # 【安全网】无论快照逻辑如何，收尾都必须确保没有带标记的测试行残留。
            # 取快照前也清了一次（见 purge_test_traces）；这里是最后一道保险，
            # 因为"残留会被并进下次快照"会让污染粘住，必须在每次运行结束时归零。
            leftover = purge_test_traces()
            check("收尾后无 mock/复用测试痕迹残留（防污染被下次快照吸收）",
                  leftover == 0, f"又清掉 {leftover} 条" if leftover else "0 条")
        else:
            print("\n按 --keep 保留测试数据（记得手工清理，避免假数据混入真实结果）")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    if crashed:
        # 崩溃必须让脚本以非 0 退出：否则一键验证会把"跑了一半"当成通过。
        check("验证过程未异常中断", False, "见上文 [异常] 行")
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
