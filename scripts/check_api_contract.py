# -*- coding: utf-8 -*-
"""接口「字段契约」校验：确认前端依赖的每个字段都真实存在于接口返回中。

【为什么需要这个脚本】
    `smoke_api.py` 只验证 HTTP 状态码与业务码——它无法发现
    "页面取 `data.source_scope`，但接口其实返回在 `distribution` 里"这类错误
    （这种错误只会让页面某一块静默空白，冒烟测试照样全绿）。
    本脚本把"前端读哪些字段"显式列出来，逐个断言存在，
    从而把"页面与接口的字段契约"变成可回归的测试。

【纪律】纯本地 Flask `test_client`，只读数据库，不调用任何模型，不写库。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.web import create_app

app = create_app()
client = app.test_client()

# 契约：路径 → 必须存在的字段路径（点号分隔，支持 list[0] 形式取首元素）
CONTRACTS: dict[str, list[str]] = {
    "/api/overview/summary": [
        "totals.review_count", "totals.dedup_review_count", "totals.spot_count",
        "totals.spot_tibet", "totals.spot_route", "totals.spot_ge100",
        "totals.low_info_count", "totals.dup_count", "totals.dup_group_count",
        "totals.image_review_count", "totals.ip_unknown_count", "totals.score_null_count",
        "score_distribution", "scored_total", "coverage.sentiment", "coverage.topic",
        "coverage.report.spots_ge100", "coverage.report.reports_generated",
        "coverage.report.fact_packages", "coverage.topic.topic_count",
        "coverage.topic.topic_word_count", "caliber_note", "sample_size",
    ],
    "/api/overview/trend?granularity=year": [
        "granularity", "points", "total", "caliber_note", "sample_size",
    ],
    "/api/overview/distribution": [
        "buckets", "source_scope", "caliber_note", "sample_size",
    ],
    "/api/overview/provinces?limit=10": [
        "points", "province_count", "caliber_note", "sample_size",
    ],
    "/api/overview/data-note": [
        "dataset", "source", "size", "quality", "limitations", "caliber_note", "sample_size",
    ],
    "/api/spots?page=1&page_size=5": [
        "items", "page", "page_size", "total", "caliber_note", "sample_size",
        "items[0].spot_id", "items[0].spot_name", "items[0].source_label",
        "items[0].review_count", "items[0].evaluation_available",
    ],
    "/api/spots/ranking?by=reviews&limit=5": [
        "by", "min_reviews", "items", "caliber_note", "sample_size",
        "items[0].spot_id", "items[0].spot_name", "items[0].review_count",
        "items[0].avg_score", "items[0].positive_rate", "items[0].negative_rate",
    ],
    "/api/spots/564": [
        "spot.spot_id", "spot.spot_name", "spot.source_scope", "spot.address",
        "spot.open_time", "spot.phone", "spot.introduction", "spot.poi_url",
        "statistics", "review_count", "evaluation_available", "availability_note",
        "missing_fields", "caliber_note", "sample_size",
    ],
    "/api/spots/564/trend?granularity=year": [
        "spot_id", "granularity", "points", "total", "caliber_note", "sample_size",
    ],
    "/api/spots/564/sentiment?method=mllib": [
        "spot_id", "method", "sample_size", "distribution.positive",
        "distribution.neutral", "distribution.negative", "rates",
        "available_methods", "caliber_note",
    ],
    "/api/spots/564/aspects?method=deepseek": [
        "spot_id", "method", "items", "conclusive_count", "caliber_note", "sample_size",
        "items[0].aspect", "items[0].sample_size", "items[0].conclusive",
        "items[0].positive_rate", "items[0].negative_rate", "items[0].note",
    ],
    "/api/spots/564/topics": [
        "spot_id", "scope", "topics", "caliber_note", "sample_size",
        "topics[0].topic_index", "topics[0].topic_rate", "topics[0].sample_size",
        "topics[0].words", "topics[0].words[0].word", "topics[0].words[0].weight",
    ],
    "/api/spots/564/reviews?limit=5": [
        "spot_id", "positive", "negative", "note", "caliber_note", "sample_size",
        "positive[0].comment_id", "positive[0].content", "positive[0].score",
        "positive[0].like_count", "positive[0].publish_date", "positive[0].is_dup_content",
    ],
    "/api/spots/564/report": [
        # 当前评价尚未生产 → 走 available=false 分支；生产后应改为校验 available=true 的字段
        "spot_id", "spot_name", "available", "review_count", "caliber_note",
    ],
    "/api/admin/tasks?limit=5": [
        "items", "summary", "total", "caliber_note", "sample_size",
        "items[0].task_id", "items[0].task_type", "items[0].task_name", "items[0].status",
        "items[0].total_count", "items[0].success_count", "items[0].fail_count",
        "items[0].skip_count", "items[0].cost_seconds", "items[0].created_at",
    ],
    "/api/admin/tasks/4/logs": [
        "task", "logs", "log_count", "error_count", "caliber_note", "sample_size",
        "logs[0].log_id", "logs[0].level", "logs[0].stage", "logs[0].message",
        "logs[0].ref_key", "logs[0].processed_count", "logs[0].created_at",
    ],
    "/api/admin/caliber": [
        "calibers", "caliber_note", "sample_size", "calibers[0].key", "calibers[0].note",
    ],
}


def resolve(data, path: str):
    """按 'a.b[0].c' 逐段取值；任一段缺失返回 (False, None)。"""
    current = data
    for part in path.split("."):
        index = None
        if part.endswith("]") and "[" in part:
            name, _, idx = part.partition("[")
            index = int(idx[:-1])
            part = name
        if part:
            if not isinstance(current, dict) or part not in current:
                return False, None
            current = current[part]
        if index is not None:
            if not isinstance(current, list) or len(current) <= index:
                return False, None
            current = current[index]
    return True, current


def main() -> int:
    total = missing = 0
    print("=" * 84)
    print("接口字段契约校验（只读、零模型调用）")
    print("=" * 84)
    for path, paths in CONTRACTS.items():
        resp = client.get(path)
        body = resp.get_json(silent=True) or {}
        data = body.get("data")
        if body.get("code") != 0 or data is None:
            print(f"  [FAIL] {path} → code={body.get('code')}（接口未返回成功数据）")
            missing += len(paths)
            total += len(paths)
            continue
        bad = [p for p in paths if not resolve(data, p)[0]]
        total += len(paths)
        missing += len(bad)
        if bad:
            print(f"  [FAIL] {path}")
            for name in bad:
                print(f"         缺少字段：{name}")
        else:
            print(f"  [PASS] {path}（{len(paths)} 个字段）")

    print("\n" + "=" * 84)
    print(f"字段契约：{total - missing}/{total} 个字段存在")
    print("=" * 84)
    return 0 if missing == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
