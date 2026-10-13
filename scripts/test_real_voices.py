# -*- coding: utf-8 -*-
"""景点评价页「游客真实声音」专项测试（**只读，零 API 消费**）。

## 为什么需要它
评价页新加的「游客真实声音」是**直接展示数据库里的评论原文**的模块，
因此最容易出问题的地方不是样式，而是：
    · 展示的原文是否与 `review` 表**逐字一致**（不许改写、不许补全）；
    · 每条评论下面的"话题"是否真的来自该评论的 `aspect` 标注（不许现编）；
    · 好评／中评／差评的分组是否与评分口径一致；
    · 评论不足时是否**如实少展示**（不许凑数、不许拿重复/无信息量短评填充）。

## 覆盖
    [1] 接口结构：positive / neutral / negative / topics
    [2] 原文一致性：逐条与数据库比对（content / score / publish_date / like_count）
    [3] 话题真实性：topics ⊆ 该评论的 aspect 标注；评论确实属于该景点
    [4] 分组口径：好评 ≥4 星、中评 =3 星、差评 ≤2 星；空组返回空数组（不凑数）
    [5] 低评论量景点（40 条、无综合评价）同样可用，且只有一组时不虚构其它组
    [6] 前端渲染纪律（静态）：剔除重复/短评、原文照录标签、AI 与真实评论区分
    [7] 只读性：全部调用后，数据库行数与冻结资产指纹逐表不变
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import query_all, query_one  # noqa: E402
from app.web import create_app  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

# 三类景点：评论量大且有报告（564）、评论量中等有报告（322）、评论量低无报告（796）
BIG, MID, SMALL = 564, 322, 796
LIMIT = 3

# 冻结资产指纹（与其它脚本同一口径：**剔除自增代理键**的无序内容指纹）
FROZEN = {
    "review": "SELECT COUNT(*) AS n, SUM(CRC32(CONCAT_WS('|',comment_id,spot_id,score,score_desc,"
              "content,content_length,publish_date,ip_location,user_nick,like_count,image_count,"
              "is_low_info,is_dup_content,dup_group_id))) AS fp FROM review",
    "spot": "SELECT COUNT(*) AS n, SUM(CRC32(CONCAT_WS('|',spot_id,spot_name,source_scope,address,"
            "open_time,phone,introduction,review_count,has_full_evaluation))) AS fp FROM spot",
    "spot_fact_package": "SELECT COUNT(*) AS n, SUM(CRC32(CONCAT_WS('|',spot_id,version,package_json,"
                         "review_count,stat_version,generated_at,task_id))) AS fp FROM spot_fact_package",
    "spot_report": "SELECT COUNT(*) AS n, SUM(CRC32(CONCAT_WS('|',spot_id,fact_package_version,summary,"
                   "advantages_json,issues_json,visitor_focus_json,model,prompt_version,need_review,"
                   "token_usage,generated_at,task_id))) AS fp FROM spot_report",
    "aspect": "SELECT COUNT(*) AS n, SUM(CRC32(CONCAT_WS('|',comment_id,spot_id,aspect_name,polarity,"
              "evidence,method,created_at))) AS fp FROM aspect",
    "comment_semantic": "SELECT COUNT(*) AS n, SUM(CRC32(CONCAT_WS('|',comment_id,spot_id,keywords,"
                        "summary,source,created_at))) AS fp FROM comment_semantic",
}


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def frozen_snapshot() -> dict[str, tuple[int, int]]:
    return {k: (int(query_one(sql)["n"]), int(query_one(sql)["fp"] or 0)) for k, sql in FROZEN.items()}


def main() -> int:
    app = create_app()
    client = app.test_client()
    before = frozen_snapshot()
    # `qa_record` 是问答历史，表里本来就可能已有记录（用户使用留下）。
    # 这里只核对"本脚本没有新增"，不去判定绝对条数、更不删除既有记录。
    qa_before = int(query_one("SELECT COUNT(*) AS n FROM qa_record")["n"])

    print("=" * 88)
    print("景点评价页「游客真实声音」专项测试（只读，零 API 消费）")
    print("=" * 88)

    def get(spot_id: int, limit: int = LIMIT) -> dict:
        resp = client.get(f"/api/spots/{spot_id}/reviews?limit={limit}")
        return (resp.get_json() or {}).get("data") or {}

    # ---------------- [1] 接口结构 ----------------
    print("\n[1] 接口结构：正 / 中 / 负三组 + 逐条话题")
    big = get(BIG)
    for key in ("positive", "neutral", "negative"):
        check(f"返回 {key} 分组", isinstance(big.get(key), list), f"{len(big.get(key) or [])} 条")
    check("每组条数不超过 limit", all(len(big.get(k) or []) <= LIMIT for k in ("positive", "neutral", "negative")),
          str({k: len(big.get(k) or []) for k in ("positive", "neutral", "negative")}))
    first = (big.get("positive") or [{}])[0]
    check("每条评论含原文/评分/时间/话题字段",
          all(k in first for k in ("content", "score", "publish_date", "like_count", "topics")),
          str(sorted(first.keys())))

    # ---------------- [2] 原文一致性（逐字比对数据库） ----------------
    print("\n[2] 原文一致性：与 review 表逐字比对（不许改写、不许补全）")
    mismatch: list[str] = []
    checked_rows = 0
    for key in ("positive", "neutral", "negative"):
        for row in big.get(key) or []:
            db = query_one(
                "SELECT content, score, publish_date, like_count FROM review WHERE comment_id=%s",
                (int(row["comment_id"]),),
            )
            checked_rows += 1
            if not db:
                mismatch.append(f"{row['comment_id']}:数据库无此评论")
                continue
            if db["content"] != row["content"]:
                mismatch.append(f"{row['comment_id']}:正文不一致")
            if int(db["score"] or -1) != int(row["score"] or -1):
                mismatch.append(f"{row['comment_id']}:评分不一致")
            if str(db["publish_date"] or "") != str(row["publish_date"] or ""):
                mismatch.append(f"{row['comment_id']}:时间不一致")
            if int(db["like_count"] or 0) != int(row["like_count"] or 0):
                mismatch.append(f"{row['comment_id']}:点赞数不一致")
    check(f"{checked_rows} 条评论逐字与数据库一致", not mismatch, "；".join(mismatch[:3]))

    # ---------------- [3] 话题真实性 ----------------
    print("\n[3] 话题真实性：topics 必须来自该评论的 aspect 标注，且评论属于该景点")
    bad_topic: list[str] = []
    topic_rows = 0
    for key in ("positive", "neutral", "negative"):
        for row in big.get(key) or []:
            if not row.get("topics"):
                continue
            topic_rows += 1
            real = {
                r["aspect_name"]
                for r in query_all(
                    "SELECT aspect_name FROM aspect WHERE comment_id=%s AND method='deepseek'",
                    (int(row["comment_id"]),),
                )
            }
            invented = [t for t in row["topics"] if t not in real]
            if invented:
                bad_topic.append(f"{row['comment_id']}:{invented}")
            owner = query_one("SELECT spot_id FROM review WHERE comment_id=%s", (int(row["comment_id"]),))
            if not owner or int(owner["spot_id"]) != BIG:
                bad_topic.append(f"{row['comment_id']}:不属于该景点")
    check(f"{topic_rows} 条评论的话题全部来自真实标注", not bad_topic, "；".join(bad_topic[:3]))
    check("话题最多 3 个（避免堆砌）", all(len(r.get("topics") or []) <= 3 for k in ("positive", "neutral", "negative")
                                     for r in big.get(k) or []), "")

    # ---------------- [4] 分组口径 + 不凑数 ----------------
    print("\n[4] 分组口径：好评 ≥4 星 / 中评 =3 星 / 差评 ≤2 星；不足时返回空数组")
    rules = {"positive": lambda s: s >= 4, "neutral": lambda s: s == 3, "negative": lambda s: s <= 2}
    for key, ok in rules.items():
        rows = big.get(key) or []
        check(f"{key} 的评分全部符合口径", all(ok(int(r["score"] or 0)) for r in rows),
              str(sorted({int(r["score"] or 0) for r in rows})))

    # ---------------- [5] 低评论量景点 ----------------
    print("\n[5] 低评论量景点（40 条评论、无综合评价）同样可用且不虚构分组")
    small = get(SMALL)
    small_groups = {k: len(small.get(k) or []) for k in ("positive", "neutral", "negative")}
    check("低评论量景点也能取到真实评论", sum(small_groups.values()) > 0, str(small_groups))
    db_groups = query_one(
        "SELECT SUM(score>=4) AS p, SUM(score=3) AS n, SUM(score<=2) AS g FROM review WHERE spot_id=%s",
        (SMALL,),
    )
    for key, db_key in (("positive", "p"), ("neutral", "n"), ("negative", "g")):
        empty_expected = int(db_groups[db_key] or 0) == 0
        if empty_expected:
            check(f"库内无该类评论时 {key} 返回空数组（不凑数）", small.get(key) == [], str(db_groups))
    check("不同评论量的三个景点都能取到数据",
          all(sum(len(get(sid).get(k) or []) for k in ("positive", "neutral", "negative")) > 0
              for sid in (BIG, MID, SMALL)), "")

    # ---------------- [6] 前端渲染纪律（静态） ----------------
    print("\n[6] 前端渲染纪律：剔除重复/短评、原文照录、与 AI 内容区分")
    js = (ROOT / "app" / "web" / "static" / "js" / "evaluation.js").read_text(encoding="utf-8")
    check("有「可用评论」过滤规则（排除内容重复与短评）",
          "is_dup_content" in js and "content_length" in js and "<= 10" in js, "")
    check("有「原文照录」标签（与 AI 生成内容区分）", "原文照录" in js, "")
    check("AI 内容带「由分析结果生成」标签", "由分析结果生成" in js, "")
    check("长评论折叠、不整段铺开", "查看完整评论" in js and "VOICE_CLAMP" in js, "")
    check("每组展示条数有上限", "VOICE_MAX_PER_GROUP" in js, "")
    check("生硬条目有人话改写函数（含原样兜底）", "humanizeReportItem" in js
          and "humanizeTerms" in js and "原样保留" in js, "")
    check("改写只做百分比换算（×100），不改变数值",
          "fmtRateText" in js and "Math.round(n * 1000) / 10" in js, "")
    check("重复罗列的整体指标已不再重复展示",
          "/^(整体|综合|总体|平均评分|平均分|综合评分)/" in js, "")

    # ---------------- [8] 人话改写的正确性守卫 ----------------
    print("\n[8] 改写守卫：这几条是实测踩过的坑，用断言固定下来")
    rate_line = [ln for ln in js.splitlines() if "RATE_SRC =" in ln][0]
    check("比率正则「百分数形式在前」（否则 14% 会被截成 1 → 输出 100%4%）",
          rate_line.index("%") < rate_line.index("[01]"), rate_line.strip())
    bare_line = [ln for ln in js.splitlines() if "RE_BARE_RATE =" in ln]
    check("兜底小数换算只认「[01].小数」形式（不会把「共1条评论」变成「100%条评论」）",
          bool(bare_line) and "([01]" in bare_line[0], "")
    check("兜底小数换算跳过已带 % 与量词的（避免二次换算 / 把 0.5万 当比率）",
          bool(bare_line) and "%" in bare_line[0] and "倍年岁万千米" in bare_line[0], "")
    check("星级口径与情感口径分开改写（不把情感占比写成好评率）",
          all(k in js for k in ("RE_STAR_POS", "RE_STAR_NEG", "RE_SENT_POS", "RE_SENT_NEG"))
          and "正面评价占比" in js, "")
    check("综合评价同样过术语改写（humanizeSummary）", "humanizeSummary(r.summary" in js, "")
    check("裸情感写法不会产生「正面面」这种重复字", "${which}评价占比" in js, "")

    # ---------------- [9] 只读性 ----------------
    print("\n[9] 只读性：全部调用后冻结资产指纹不变")
    after = frozen_snapshot()
    changed = {k: (before[k], after[k]) for k in FROZEN if before[k] != after[k]}
    check("6 张冻结资产表 行数+指纹 逐表未变", not changed, str(changed) or "全部一致")
    qa_after = int(query_one("SELECT COUNT(*) AS n FROM qa_record")["n"])
    check("本脚本未新增任何 qa_record（只读）", qa_after == qa_before, f"{qa_before} → {qa_after}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"「游客真实声音」专项测试：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：展示的评论原文与数据库逐字一致，话题来自真实标注，")
        print("      分组口径正确、不足时不凑数，且整个过程未写入任何数据。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
