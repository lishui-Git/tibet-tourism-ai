# -*- coding: utf-8 -*-
"""答辩演示动线的自动化核对（零 API 消费）。

## 为什么需要它
`docs/答辩演示手册.md` 的第四节写了一条 **8 分钟演示动线**，里面全是**可核对的断言**：
"5 张图""排行三排序""客源地必须带口径""未生产时显示 `REPORT_NOT_GENERATED`"、
"问答离线时显示 `LIVE_DISABLED`""样本相差 ≥10 倍时给可靠性提示"……

这些句子如果在演示现场对不上，就是**当着评委翻车**。本脚本按动线逐行核对，
让"手册说的"和"系统做的"不可能悄悄脱节。

## 做法
逐行执行手册的演示动线（页面 + 对应接口），断言手册里写明的**具体表现**确实出现。
全部为只读请求，不调用任何模型、不写库。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.web import create_app
from app.web.compare import RELIABILITY_RATIO_THRESHOLD, _diff
from app.web.services import RANKING_BY

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    app = create_app()
    c = app.test_client()

    # 动线里的"范围内"提问会按设计写入 qa_record（§15.E.4）。
    # 本脚本是**验证**工具，不该留下痕迹：先取 qa_id 水位，结束时删掉自己新增的行。
    from app.db import connection, query_one

    def max_qa_id() -> int:
        row = query_one("SELECT COALESCE(MAX(qa_id), 0) AS m FROM qa_record") or {}
        return int(row.get("m", 0))

    qa_watermark = max_qa_id()

    def api(url: str) -> dict:
        return c.get(url).get_json() or {}

    print("=" * 88)
    print("答辩演示动线核对（对应 docs/答辩演示手册.md 第四节；零 API 消费）")
    print("=" * 88)

    # ---- 行 0：首页 ----
    print("\n[行 0] 首页 /")
    check("首页可访问（HTTP 200）", c.get("/").status_code == 200, "")

    # ---- 行 1：数据总览，手册称"5 张图" + 客源地必须带口径 ----
    print("\n[行 1] 数据总览 /overview —— 手册称『5 张图』且客源地带口径")
    overview_eps = {
        "规模": "/api/overview/summary",
        "来源": "/api/overview/distribution",
        "趋势": "/api/overview/trend?granularity=year",
        "分档": "/api/overview/trend?granularity=month",
        "客源地": "/api/overview/provinces?limit=10",
    }
    ok_all = True
    for label, ep in overview_eps.items():
        b = api(ep)
        if b.get("code") != 0:
            ok_all = False
        print(f"      · {label}: {ep} → code={b.get('code')}")
    check("总览 5 组数据接口全部 code=0（对应 5 张图）", ok_all, "")
    prov = (api("/api/overview/provinces?limit=10").get("data") or {})
    check("客源地带 caliber_note（手册强调『必须带口径』）",
          bool(prov.get("caliber_note")), f"sample_size={prov.get('sample_size')}")

    # ---- 行 2：景点分析，手册称"排行三排序 + 情感双口径" ----
    print("\n[行 2] 景点分析 /spots —— 手册称『排行三排序、情感 mllib/deepseek 可切换』")
    bys = list(RANKING_BY.keys())
    check("排序依据恰为 3 种（reviews / rating / positive_rate）",
          len(bys) == 3, f"{bys}")
    ok_rank = True
    for by in bys:
        b = api(f"/api/spots/ranking?by={by}&limit=3")
        if b.get("code") != 0:
            ok_rank = False
    check("三种排序都能取到数据", ok_rank, f"{bys}")
    ok_sent = True
    for m in ("mllib", "deepseek"):
        b = api(f"/api/spots/564/sentiment?method={m}")
        if b.get("code") != 0:
            ok_sent = False
    check("情感两种口径（mllib / deepseek）均可切换", ok_sent, "")
    for label, ep in (
        ("检索分页", "/api/spots?page=1&page_size=5"),
        ("景点详情", "/api/spots/564"),
        ("趋势", "/api/spots/564/trend"),
        ("方面", "/api/spots/564/aspects"),
        ("主题", "/api/spots/564/topics"),
        ("代表评论", "/api/spots/564/reviews?limit=3"),
    ):
        b = api(ep)
        check(f"{label}可用", b.get("code") == 0, f"code={b.get('code')}")

    # ---- 行 3：智能评价，未生产时必须 REPORT_NOT_GENERATED ----
    print("\n[行 3] 智能评价 /evaluation —— 未生产时必须 REPORT_NOT_GENERATED")
    rep = (api("/api/spots/564/report").get("data") or {})
    if rep.get("available"):
        check("评价已生产（本机已跑过全量）", True, "available=True")
    else:
        check("未生产时 reason = REPORT_NOT_GENERATED（不编造评价）",
              rep.get("reason") == "REPORT_NOT_GENERATED",
              f"available={rep.get('available')} reason={rep.get('reason')}")
        check("同时给出可读说明，而不是空白", bool(rep.get("message_text")),
              (rep.get("message_text") or "")[:50])

    # ---- 行 4：景点对比，样本悬殊要有可靠性提示 ----
    print("\n[行 4] 景点对比 /compare —— 手册称『样本相差 ≥10 倍给可靠性提示』")
    cmp_data = (api("/api/compare?spot_a=564&spot_b=322").get("data") or {})
    diff = ((cmp_data.get("facts") or {}).get("diff") or {})
    ratio = cmp_data.get("sample_size") or {}
    check("对比接口返回 facts / indicator_compare（手册称『三张对比表 + 差值』）",
          bool(cmp_data.get("facts")) and bool(cmp_data.get("indicator_compare")), "")
    check(f"真实悬殊样本触发了可靠性提示（阈值 {RELIABILITY_RATIO_THRESHOLD} 倍）",
          bool(diff.get("reliability_warning")),
          f"{ratio} → {(diff.get('reliability_warning') or '(无)')[:52]}")
    # 阈值边界：手册写的是"≥10 倍"，用合成数据核对 9.9 / 10 两侧
    def mk(name, n):
        return {"spot_id": 1, "spot_name": name, "review_count": n, "avg_score": 4.5,
                "positive_rate": 0.9, "negative_rate": 0.05, "image_rate": 0.3, "total_likes": 10}

    below = _diff(mk("A", 1000), mk("B", 100))
    check("恰为 10 倍时**触发**（与文档『≥10 倍』一致）",
          bool(below.get("reliability_warning")), "")
    at9 = _diff(mk("A", 990), mk("B", 100))
    check("9.9 倍时**不触发**（阈值没有被放宽）",
          not at9.get("reliability_warning"), "")
    check("解读默认关闭时给 LIVE_DISABLED（零模型调用）",
          ((cmp_data.get("interpretation") or {}).get("reason") == "LIVE_DISABLED")
          or bool((cmp_data.get("interpretation") or {}).get("text")),
          f"reason={(cmp_data.get('interpretation') or {}).get('reason')}")

    # ---- 行 5：智能问答，离线给依据、超范围拒答 ----
    print("\n[行 5] 智能问答 /qa —— 离线仍给依据；超范围要拒答")
    cases = (
        ("评论量前十的景点", "RANKING"),
        ("一共采集了多少条评论", "DATA_METRIC"),
        ("今天北京天气怎么样", "OUT_OF_SCOPE"),
    )
    for q, expect in cases:
        d = (c.post("/api/qa/ask", json={"question": q}).get_json() or {}).get("data") or {}
        check(f"『{q}』归类为 {expect}", d.get("question_type") == expect,
              f"实际 {d.get('question_type')}")
        if expect == "OUT_OF_SCOPE":
            # 拒答的**正确**响应形态：available=True（它承载拒答文案，不是"没数据"），
            # 关键是 reached_model=False（一次模型都没调）且 saved=False（按 §15.E.1 不落库）。
            # 首版这里断言 available is False —— 那是我对契约的理解错了，不是系统的问题。
            check("超范围问题未触达模型（reached_model=False）",
                  d.get("reached_model") is False, f"reached_model={d.get('reached_model')}")
            check("超范围问题不写入 qa_record（saved=False）",
                  d.get("saved") is False, f"saved={d.get('saved')}")
            check("给出了明确的拒答文案（而非空白）",
                  bool(d.get("answer")) and "不支持" in (d.get("answer") or ""),
                  (d.get("answer") or "")[:44])
        else:
            check(f"『{q}』给出了数据依据", bool(d.get("facts")), f"facts={len(d.get('facts') or [])} 组")

    # ---- 行 6 / 7：任务页需登录；登录页可用 ----
    print("\n[行 6/7] 任务与口径（需登录）/ 登录页")
    check("/tasks 未登录 → 302 跳登录", c.get("/tasks").status_code == 302, "")
    check("/login 可访问", c.get("/login").status_code == 200, "")
    check("/api/admin/caliber 未登录 → 2001", api("/api/admin/caliber").get("code") == 2001, "")

    # ---- 收尾：清理本脚本按设计写入的 qa_record（验证工具不该留痕）----
    print("\n[收尾] 清理本脚本写入的 qa_record")
    created = max_qa_id() - qa_watermark
    if created > 0:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM qa_record WHERE qa_id > %s", (qa_watermark,))
                deleted = cur.rowcount
        left = max_qa_id()
        check(f"已清理新增的 {created} 条问答记录，回到基线", left == qa_watermark,
              f"删除 {deleted} 条；水位 {qa_watermark} → {left}")
    else:
        check("本次未新增问答记录（无需清理）", True, f"水位 {qa_watermark}")
    check("清理后库内与验证前一致（未污染真实数据）",
          max_qa_id() == qa_watermark, f"max qa_id = {max_qa_id()}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"演示动线核对：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：手册第四节的演示动线，逐行都能在系统上兑现（未生产的页面如实显示原因，不编造内容）。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
