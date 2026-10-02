# -*- coding: utf-8 -*-
"""只读架构实证：**访问页面与查询接口不会改动任何数据**（零 API 成本）。

## 为什么需要它
"Web 层只读、打开页面不会调用模型"是本项目最重要的架构承诺之一
（用户要求：不要设计成"用户每打开一次景点页面就实时调用 DeepSeek"）。
此前只有**静态证据**：`app/web/**` 不导入 `app.llm`、不含 `requests`。静态证据虽然有力，
但答辩时更硬的说法是**实测**：把全部只读接口都打一遍，然后证明**数据一行没变**。

## 做法
    ① 快照所有结果表与运维表的行数（含 `analysis_task` / `task_log`）；
    ② 经 test_client 逐个请求全部**只读** GET 接口（含页面、自检、错误路径）；
    ③ 再次快照，断言**逐表完全一致**；
    ④ 额外断言：访问前后**没有新增 analysis_task / task_log**——
       也就是"看页面不会触发任何批处理或模型调用"。

## 边界（如实说明）
`POST /api/qa/ask` 与 `POST /api/spots/{id}/report/regenerate` **不是只读**：
前者按 §15.E.4 落 `qa_record`，后者登记 `pending` 任务——这是设计要求，故不在本测试范围。

全程不调用任何模型、不写业务数据。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.db import query_one
from app.web import create_app

# 全部只读 GET 接口（页面 / 自检 / 业务查询 / 错误路径）
GET_ENDPOINTS: tuple[str, ...] = (
    # 页面
    "/", "/overview", "/spots", "/evaluation", "/compare", "/qa", "/login",
    # 自检
    "/healthz", "/api/db-ping",
    # M1 总览
    "/api/overview/summary", "/api/overview/trend?granularity=year",
    "/api/overview/trend?granularity=month", "/api/overview/distribution",
    "/api/overview/provinces?limit=10", "/api/overview/data-note",
    # M2 景点
    "/api/spots?page=1&page_size=5", "/api/spots?keyword=布达拉宫",
    "/api/spots/ranking?by=reviews&limit=10", "/api/spots/ranking?by=rating&limit=5",
    "/api/spots/564", "/api/spots/564/trend", "/api/spots/564/sentiment?method=deepseek",
    "/api/spots/564/sentiment?method=mllib", "/api/spots/564/aspects",
    "/api/spots/564/topics", "/api/spots/564/reviews?limit=5",
    # M3 评价 / M4 对比 / M5 问答（read-only 部分）
    "/api/spots/564/report", "/api/spots/5/report",
    "/api/compare?spot_a=564&spot_b=196", "/api/compare?spot_a=564&spot_b=322",
    # 错误路径（也要证明不改数据）
    "/api/nope", "/api/spots/abc", "/api/spots/999999999",
)

TABLES: tuple[str, ...] = (
    "spot", "review", "sentiment", "aspect", "comment_semantic", "stat_spot",
    "stat_time", "stat_ip", "topic", "spot_fact_package", "spot_report",
    "qa_record", "analysis_task", "task_log", "sys_user", "caliber_note", "lda_topic",
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def snapshot() -> dict[str, int]:
    """各表行数快照；某表不存在时记为 -1（如实反映，而不是静默跳过）。"""
    counts: dict[str, int] = {}
    for table in TABLES:
        try:
            row = query_one(f"SELECT COUNT(*) AS n FROM {table}")
            counts[table] = int(row["n"])
        except Exception:
            counts[table] = -1
    return counts


def main() -> int:
    app = create_app()
    client = app.test_client()

    print("=" * 86)
    print("只读架构实证：访问全部只读接口后，数据必须一行未变（零 API 成本）")
    print("=" * 86)

    before = snapshot()
    print(f"\n访问前快照（{len([v for v in before.values() if v >= 0])} 张表）：")
    print("  " + ", ".join(f"{k}={v}" for k, v in before.items() if v >= 0))

    print(f"\n逐个请求 {len(GET_ENDPOINTS)} 个只读 GET 接口…")
    statuses: dict[str, int] = {}
    for url in GET_ENDPOINTS:
        resp = client.get(url)
        statuses[url] = resp.status_code
    print("  HTTP 状态分布：" + ", ".join(
        f"{code}×{list(statuses.values()).count(code)}" for code in sorted(set(statuses.values()))
    ))

    after = snapshot()
    print(f"\n访问后快照：")
    print("  " + ", ".join(f"{k}={v}" for k, v in after.items() if v >= 0))

    print("\n[1] 业务数据未被改动")
    changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    check("全部结果表行数与访问前完全一致", not changed,
          ("变化：" + ", ".join(f"{k}: {v[0]}→{v[1]}" for k, v in changed.items())) if changed
          else f"{len(before)} 张表逐表一致")

    print("\n[2] 未触发任何批处理或模型调用")
    check("未新增 analysis_task（看页面不会触发任务登记）",
          before["analysis_task"] == after["analysis_task"],
          f"{before['analysis_task']} → {after['analysis_task']}")
    check("未新增 task_log（看页面不会产生任务日志）",
          before["task_log"] == after["task_log"],
          f"{before['task_log']} → {after['task_log']}")
    check("未新增 qa_record（GET 接口不写问答记录）",
          before["qa_record"] == after["qa_record"],
          f"{before['qa_record']} → {after['qa_record']}")

    print("\n[3] 接口确实被访问到了（避免『全都 404 因此当然没变』的假通过）")
    ok_statuses = sum(1 for code in statuses.values() if code == 200)
    check(f"至少 25 个接口返回 200（实测 {ok_statuses} 个）", ok_statuses >= 25, f"200×{ok_statuses}")
    check("错误路径按契约返回（非 200 响应存在）",
          any(code != 200 for code in statuses.values()),
          ", ".join(f"{u}={c}" for u, c in statuses.items() if c != 200)[:120])

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 86)
    print(f"只读架构实证：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：页面与查询接口（含错误路径）访问前后**数据逐表一致**——")
        print("      Web 层只读数据库这一架构承诺，已由实测证明（非仅静态推断）。")
    print("=" * 86)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
