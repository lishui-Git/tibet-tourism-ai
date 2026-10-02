# -*- coding: utf-8 -*-
"""BR-02 / BR-03 核对：评价生成门槛（≥100 条）与"仅统计"景点的如实表现。

设计要求：
    · **BR-02**：仅 ≥100 条评论的 **57 个**景点生成完整智能评价；
    · **BR-03**：<100 条的景点**只展示基础统计并提示样本不足**。

这两条是"不做没有依据的评价"的业务落点，也是最容易被追问的点
（"评论少的景点你怎么处理？会不会硬生成一段？"）。

## 实测要点
    ① 合格景点：评价接口的不可用原因应是 `REPORT_NOT_GENERATED`（尚未生产），
       **不是**门槛类原因；
    ② 不合格景点：应返回 `REVIEW_COUNT_BELOW_THRESHOLD` + 明确文案，
       且 **HTTP 层不是错误**（`code=0`，属正常业务结果）；
    ③ 不合格景点**仍可看基础统计**（详情/趋势/情感基线/评论）；
    ④ 提示要一路传到前端：详情接口带 `evaluation_available=false` 与
       `availability_note`（页面据此显示"仅统计"与原因）。

全程只读、零 API 消费。
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import query_one
from app.web import create_app

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 88)
    print("BR-02 / BR-03 核对：评价门槛与『仅统计』景点的如实表现（零 API 消费）")
    print("=" * 88)

    app = create_app()
    c = app.test_client()

    def api(url: str) -> dict:
        return c.get(url).get_json() or {}

    # ---------- ① 门槛本身 ----------
    print("\n[1] 门槛与规模（BR-02）")
    eligible = int(
        query_one("SELECT COUNT(*) AS n FROM spot WHERE has_full_evaluation = 1")["n"]
    )
    below = int(query_one("SELECT COUNT(*) AS n FROM spot WHERE has_full_evaluation = 0")["n"])
    check("具备完整评价资格的景点恰为 57 个（BR-02）", eligible == 57, f"{eligible}")
    check("其余景点数 = 837 − 57 = 780", below == 837 - 57, f"{below}")
    # 门槛值与评论数必须一致（不能出现"标记合格但只有 90 条"）
    inconsistent = int(
        query_one(
            "SELECT COUNT(*) AS n FROM spot s JOIN stat_spot st ON st.spot_id = s.spot_id "
            "WHERE s.has_full_evaluation = 1 AND st.review_count < 100"
        )["n"]
    )
    check("标记为合格的景点评论数都 ≥100（标记与数据一致）", inconsistent == 0, f"越界 {inconsistent} 个")

    big = query_one(
        "SELECT s.spot_id, s.spot_name, st.review_count FROM spot s "
        "JOIN stat_spot st ON st.spot_id = s.spot_id WHERE s.has_full_evaluation = 1 "
        "ORDER BY st.review_count DESC LIMIT 1"
    )
    small = query_one(
        "SELECT s.spot_id, s.spot_name, st.review_count FROM spot s "
        "JOIN stat_spot st ON st.spot_id = s.spot_id WHERE s.has_full_evaluation = 0 "
        "ORDER BY st.review_count DESC LIMIT 1"
    )
    print(f"      合格样本：{big['spot_name']}（{big['review_count']} 条）")
    print(f"      不合格样本：{small['spot_name']}（{small['review_count']} 条）")

    # ---------- ② 合格景点：原因不应是门槛 ----------
    print("\n[2] 合格景点：评价不可用的原因不该是『样本不足』")
    d_big = (api(f"/api/spots/{big['spot_id']}/report").get("data") or {})
    reason_big = d_big.get("reason")
    check("可用性或原因是『尚未生成』（REPORT_NOT_GENERATED）",
          d_big.get("available") or reason_big == "REPORT_NOT_GENERATED", f"reason={reason_big}")
    check("原因**不是**门槛类（BR-02 已通过门槛）",
          reason_big != "REVIEW_COUNT_BELOW_THRESHOLD", str(reason_big))

    # ---------- ③ 不合格景点：明确说明且不是错误 ----------
    print("\n[3] 不合格景点：明确说明『样本不足』，且 HTTP 层不是错误（BR-03）")
    resp = api(f"/api/spots/{small['spot_id']}/report")
    d_small = resp.get("data") or {}
    check("返回 code=0（属正常业务结果，不是报错）", resp.get("code") == 0, f"code={resp.get('code')}")
    check("available=false", d_small.get("available") is False, str(d_small.get("available")))
    check("reason=REVIEW_COUNT_BELOW_THRESHOLD",
          d_small.get("reason") == "REVIEW_COUNT_BELOW_THRESHOLD", str(d_small.get("reason")))
    check("回显该景点真实评论数（供用户判断差距）",
          d_small.get("review_count") == small["review_count"],
          f"{d_small.get('review_count')} vs {small['review_count']}")
    msg = d_small.get("message_text") or ""
    check("文案明确说明『仅提供基础统计、不生成智能评价』",
          "不足 100" in msg and "基础统计" in msg, msg[:60])

    # ---------- ④ 不合格景点仍可看基础统计 ----------
    print("\n[4] 不合格景点仍可看基础统计（BR-03：只展示基础统计）")
    for label, ep in (
        ("详情", ""),
        ("趋势", "/trend"),
        ("情感基线", "/sentiment?method=mllib"),
        ("评论", "/reviews?limit=3"),
    ):
        r = api(f"/api/spots/{small['spot_id']}{ep}")
        check(f"{label}可用（code=0）", r.get("code") == 0, f"code={r.get('code')}")

    # ---------- ⑤ 提示要传到前端 ----------
    print("\n[5] 提示传到前端（页面据此显示『仅统计』与原因）")
    det_small = (api(f"/api/spots/{small['spot_id']}").get("data") or {})
    det_big = (api(f"/api/spots/{big['spot_id']}").get("data") or {})
    check("不合格景点：evaluation_available=false 且带 availability_note",
          det_small.get("evaluation_available") is False and bool(det_small.get("availability_note")),
          (det_small.get("availability_note") or "")[:56])
    check("合格景点：evaluation_available=true（无需提示）",
          det_big.get("evaluation_available") is True, str(det_big.get("evaluation_available")))
    listing = api("/api/spots?page=1&page_size=100").get("data") or {}
    items = listing.get("items") or []
    check("列表项带 has_full_evaluation（前端据此标『可评价/仅统计』）",
          bool(items) and all("has_full_evaluation" in it for it in items), f"{len(items)} 项")
    check("列表项带 evaluation_available（详情页提示依据）",
          bool(items) and all("evaluation_available" in it for it in items), "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"BR-02 / BR-03 核对：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：评价门槛严格执行；评论不足的景点只说清原因、照常提供基础统计，")
        print("      并且**没有**出现『硬生成一段没有依据的评价』的情况。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
