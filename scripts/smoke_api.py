# -*- coding: utf-8 -*-
"""只读业务接口冒烟测试（本地 Flask test_client，零 API 调用、零 DB 写入）。

覆盖：M1 数据总览 / M2 景点分析 / M3 智能评价 / M6 系统管理，
以及错误码分支（参数越界 1002、资源不存在 3001、参数格式 1001）。
"""
import json
import sys

sys.path.insert(0, ".")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.web import create_app

app = create_app()
client = app.test_client()

CASES = [
    # --- 页面（展示层，只渲染模板；数据由页面内 JS 调 /api/* 获取） ---
    ("/", 200, None),
    ("/overview", 200, None),
    ("/spots", 200, None),
    ("/evaluation", 200, None),
    ("/compare", 200, None),
    ("/tasks", 200, None),
    # --- 静态资源（本地 vendored，运行时不依赖外网/CDN） ---
    ("/static/vendor/echarts.min.js", 200, None),
    ("/static/vendor/axios.min.js", 200, None),
    ("/static/js/common.js", 200, None),
    ("/static/js/home.js", 200, None),
    ("/static/js/overview.js", 200, None),
    ("/static/js/spots.js", 200, None),
    ("/static/js/evaluation.js", 200, None),
    ("/static/js/compare.js", 200, None),
    ("/static/js/tasks.js", 200, None),
    ("/static/css/app.css", 200, None),
    # --- 自检接口 ---
    ("/healthz", 200, 0),
    ("/api/db-ping", 200, 0),
    ("/api/overview/summary", 200, 0),
    ("/api/overview/trend?granularity=year", 200, 0),
    ("/api/overview/trend?granularity=month", 200, 0),
    ("/api/overview/trend?granularity=day", 400, 1001),
    ("/api/overview/distribution", 200, 0),
    ("/api/overview/provinces", 200, 0),
    ("/api/overview/provinces?limit=999", 400, 1002),
    ("/api/overview/data-note", 200, 0),
    ("/api/spots", 200, 0),
    ("/api/spots?page=0", 400, 1002),
    ("/api/spots?page_size=101", 400, 1002),
    ("/api/spots?page=abc", 400, 1001),
    ("/api/spots?keyword=布达拉宫", 200, 0),
    ("/api/spots/ranking?by=reviews", 200, 0),
    ("/api/spots/ranking?by=rating", 200, 0),
    ("/api/spots/ranking?by=positive_rate", 200, 0),
    ("/api/spots/ranking?by=bogus", 400, 1001),
    ("/api/spots/999999999", 404, 3001),
    ("/api/spots/999999999/trend", 404, 3001),
    ("/api/spots/999999999/report", 404, 3001),
    ("/api/spots/564", 200, 0),
    ("/api/spots/564/trend", 200, 0),
    ("/api/spots/564/sentiment?method=mllib", 200, 0),
    ("/api/spots/564/sentiment?method=deepseek", 200, 0),
    ("/api/spots/564/sentiment?method=bogus", 400, 1001),
    ("/api/spots/564/aspects?method=deepseek", 200, 0),
    ("/api/spots/564/aspects?method=mllib", 400, 1001),   # 方面层只有 deepseek 来源
    ("/api/spots/564/topics", 200, 0),
    ("/api/spots/564/reviews", 200, 0),
    ("/api/spots/564/reviews?limit=999", 400, 1002),
    ("/api/spots/564/report", 200, 0),
    ("/api/admin/tasks", 200, 0),
    ("/api/admin/tasks?task_type=semantic", 200, 0),
    ("/api/admin/tasks/4/logs", 200, 0),
    ("/api/admin/tasks/999999999/logs", 404, 3001),
    ("/api/admin/caliber", 200, 0),
    # --- M4 景点对比（指标由后端算；解读默认关闭，零 API 消费） ---
    ("/api/compare?spot_a=564&spot_b=196", 200, 0),
    ("/api/compare?spot_a=564&spot_b=564", 400, 1002),      # 同一景点
    ("/api/compare?spot_a=564", 400, 1001),                 # 缺参数
    ("/api/compare?spot_a=abc&spot_b=196", 400, 1001),      # 非整数
    ("/api/compare?spot_a=564&spot_b=999999999", 404, 3001),  # 景点不存在
    ("/api/compare?spot_a=564&spot_b=322", 200, 0),         # 样本悬殊 → 可靠性提示
    ("/api/auth/login", 404, None),       # 未实现（预期 404）
]

passed = failed = 0
print("=" * 80)
for path, want_status, want_code in CASES:
    resp = client.get(path)
    body = resp.get_json(silent=True)
    code = body.get("code") if isinstance(body, dict) else None
    ok = resp.status_code == want_status and (want_code is None or code == want_code)
    if ok:
        passed += 1
        print(f"  [PASS] {path:<48} HTTP {resp.status_code} code={code}")
    else:
        failed += 1
        print(f"  [FAIL] {path:<48} HTTP {resp.status_code} code={code}  期望 HTTP {want_status} code={want_code}")

# 抽查关键数据形状
print("\n[数据形状抽查]")
detail = client.get("/api/spots/564").get_json()["data"]
print(f"  景点详情 564: {detail['spot']['spot_name']} 评论 {detail['review_count']} "
      f"评价可用={detail['evaluation_available']} 缺失字段={detail['missing_fields']}")

overview = client.get("/api/overview/summary").get_json()["data"]
print(f"  总览: 评论 {overview['totals']['review_count']} 景点 {overview['totals']['spot_count']} "
      f"评分档 {len(overview['score_distribution'])} 情感方法覆盖 {overview['coverage']['sentiment']}")

report = client.get("/api/spots/564/report").get_json()["data"]
print(f"  评价 564: available={report['available']} reason={report.get('reason')}")

small = client.get("/api/spots/1").get_json()["data"]
small_report = client.get(f"/api/spots/{small['spot']['spot_id']}/report").get_json()["data"]
print(f"  小景点 {small['spot']['spot_name']} 评论 {small['review_count']}: "
      f"available={small_report['available']} reason={small_report.get('reason')}")

sentiment = client.get("/api/spots/564/sentiment?method=deepseek").get_json()["data"]
print(f"  情感(deepseek) 564: {sentiment['distribution']} 可用方法={sentiment['available_methods']}")

aspects = client.get("/api/spots/564/aspects?method=deepseek").get_json()["data"]
print(f"  方面(deepseek) 564: {len(aspects['items'])} 项，可下结论 {aspects['conclusive_count']} 项")

topics = client.get("/api/spots/564/topics").get_json()["data"]
print(f"  主题 564: scope={topics['scope']} 主题数={len(topics['topics'])} "
      f"首主题词数={len(topics['topics'][0]['words']) if topics['topics'] else 0}")

prov = client.get("/api/overview/provinces").get_json()["data"]
print(f"  客源地: 省份数={prov['province_count']} 样本={prov['sample_size']} top1={prov['points'][0] if prov['points'] else None}")

cmp_data = client.get("/api/compare?spot_a=564&spot_b=196").get_json()["data"]
print(f"  对比 564 vs 196: A={cmp_data['facts']['spot_a']['spot_name']}({cmp_data['facts']['spot_a']['review_count']}) "
      f"B={cmp_data['facts']['spot_b']['spot_name']}({cmp_data['facts']['spot_b']['review_count']}) "
      f"样本比={cmp_data['facts']['diff']['sample_ratio']} 方面={len(cmp_data['facts']['aspects'])} "
      f"解读可用={cmp_data['interpretation']['available']}（原因 {cmp_data['interpretation']['reason']}）")

print("\n" + "=" * 80)
print(f"接口冒烟：{passed}/{passed + failed} 通过")
print("=" * 80)
sys.exit(0 if failed == 0 else 1)
