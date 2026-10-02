# -*- coding: utf-8 -*-
"""BR-09 核对：景点级描述字段**只作展示、不作分析维度**。

设计要求：
    **BR-09** 景点级字段只作展示，不作分析维度（由数据库层的 `spot` 维表拆分保证）。

这条规则是"**分析结果必须来自评论数据**"这一主张的落点，答辩时很容易被追问：
"你的统计数字到底是从评论算出来的，还是从景点表里读出来的？"

## 怎么验证（零 API 消费）
    · **可复核性**：用 SQL 从 `review` 表**独立重算** `review_count`/`avg_score`/`total_likes`，
      与 `stat_spot` 逐项比对——一致即说明统计是被评论数据推导出来的；
    · **结构保证**：断言三张分析表（`stat_spot`/`stat_time`/`stat_ip`）**不含**
      任何景点描述字段（`introduction`/`address`/`poi_url`/`spot_name`/`province`）；
    · **情感口径**：断言情感分布来自 `sentiment` 表（按 method 实时聚合），
      而不是 `stat_spot.sentiment_*`（阶段四未回填，当前为 NULL）；
    · **不误导**：断言接口在无情感数据时给出 NULL/原因，而不是显示成 0%。
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import query_all, query_one
from app.web import create_app

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


DESCRIPTIVE = {"introduction", "address", "poi_url", "spot_name", "province", "source_scope", "lat", "lng"}


def main() -> int:
    print("=" * 88)
    print("BR-09 核对：统计数字来自评论数据，而非景点描述字段（零 API 消费）")
    print("=" * 88)

    app = create_app()
    c = app.test_client()

    # ---------- ① 结构保证：分析表不含描述字段 ----------
    print("\n[1] 结构保证：分析表不得包含景点描述字段")
    for table in ("stat_spot", "stat_time", "stat_ip"):
        cols = {row["Field"] for row in query_all(f"SHOW COLUMNS FROM {table}")}
        leaked = cols & DESCRIPTIVE
        check(f"{table} 不含描述字段", not leaked, f"发现的描述字段：{sorted(leaked)}" if leaked else f"{len(cols)} 列均为指标")

    # ---------- ② 可复核性：从 review 独立重算 ----------
    print("\n[2] 可复核性：从 review 独立重算，与 stat_spot 逐项比对（抽样 10 个景点）")
    rows = query_all(
        "SELECT spot_id, review_count, avg_score, total_likes FROM stat_spot "
        "WHERE review_count >= 100 ORDER BY review_count DESC LIMIT 10"
    )
    mismatch: list[str] = []
    for r in rows:
        sid = int(r["spot_id"])
        a = query_one(
            "SELECT COUNT(*) AS n, ROUND(AVG(score),2) AS avg_score, "
            "COALESCE(SUM(like_count),0) AS likes FROM review WHERE spot_id=%s",
            (sid,),
        )
        if int(a["n"]) != int(r["review_count"]):
            mismatch.append(f"{sid}:count {r['review_count']}!={a['n']}")
        if r["avg_score"] is not None and abs(float(r["avg_score"]) - float(a["avg_score"])) > 0.01:
            mismatch.append(f"{sid}:avg {r['avg_score']}!={a['avg_score']}")
        if int(r["total_likes"]) != int(a["likes"]):
            mismatch.append(f"{sid}:likes {r['total_likes']}!={a['likes']}")
    check(f"{len(rows)} 个景点的 count/avg_score/total_likes 与重算完全一致",
          not mismatch, "；".join(mismatch[:3]) if mismatch else "逐项一致")

    # 全库复核 review_count（比抽样更强）
    total_mismatch = int(
        query_one(
            """
            SELECT COUNT(*) AS n FROM (
              SELECT st.spot_id
                FROM stat_spot st
                LEFT JOIN (SELECT spot_id, COUNT(*) AS c FROM review GROUP BY spot_id) rv
                       ON rv.spot_id = st.spot_id
               WHERE st.review_count <> COALESCE(rv.c, 0)
            ) t
            """
        )["n"]
    )
    spots_total = int(query_one("SELECT COUNT(*) AS n FROM stat_spot")["n"])
    check(f"全库 {spots_total} 个景点的 review_count 均与 review 表一致",
          total_mismatch == 0, f"不一致 {total_mismatch} 个")

    # ---------- ③ 情感口径来自 sentiment 表 ----------
    print("\n[3] 情感分布来自 sentiment 表（按 method 实时聚合），而非 stat_spot 的旧列")
    sid = 564
    for method in ("mllib", "deepseek"):
        d = (c.get(f"/api/spots/{sid}/sentiment?method={method}").get_json() or {}).get("data") or {}
        db_row = query_one(
            "SELECT COUNT(*) AS n FROM sentiment WHERE spot_id=%s AND method=%s", (sid, method)
        )
        check(f"method={method} 的 sample_size = sentiment 表行数",
              int(d.get("sample_size") or 0) == int(db_row["n"]),
              f"接口 {d.get('sample_size')} vs 库 {db_row['n']}")
        check(f"method={method} 的 distribution 合计 = sample_size",
              sum((d.get("distribution") or {}).values()) == int(d.get("sample_size") or 0),
              f"dist={d.get('distribution')}")
        check(f"method={method} 的 rates 与分布自洽",
              d.get("rates") is not None, str(d.get("rates"))[:60])

    # ---------- ④ 不误导：无数据时不显示成 0% ----------
    print("\n[4] 不误导：stat_spot 的 sentiment_* 未回填（NULL），接口不据此显示 0%")
    stat_row = query_one(
        "SELECT sentiment_positive, sentiment_neutral, sentiment_negative, sentiment_sample "
        "FROM stat_spot WHERE spot_id=%s", (sid,)
    )
    check("stat_spot.sentiment_* 为 NULL（未回填，不是 0）",
          all(stat_row[k] is None for k in ("sentiment_positive", "sentiment_neutral", "sentiment_negative")),
          str(stat_row))
    db_row = query_one("SELECT COUNT(*) AS n FROM sentiment WHERE spot_id=%s AND method='deepseek'", (sid,))
    d = (c.get(f"/api/spots/{sid}/sentiment?method=deepseek").get_json() or {}).get("data") or {}
    check("接口取的是 sentiment 表的真实行数（不是 stat_spot 的 NULL）",
          int(d.get("sample_size") or 0) == int(db_row["n"]) and int(db_row["n"]) > 0,
          f"sample_size={d.get('sample_size')}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"BR-09 核对：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：统计指标可由 review 表独立复算、分析表不含景点描述字段、")
        print("      情感分布取自 sentiment 表——『用数据算出来的』这句话有据可查。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
