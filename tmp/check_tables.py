# -*- coding: utf-8 -*-
"""核对精简后的表清单"""
import sys, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sql = open(r"E:\tibet-tourism-ai\docs\database\建表脚本.sql", encoding="utf-8").read()
tables = re.findall(r"CREATE TABLE `([A-Za-z_]+)`", sql)
print(f"建表脚本中的表：共 {len(tables)} 张")
for i, n in enumerate(tables, 1):
    print(f"  {i:2d}. {n}")

USER_LIST = ["sys_user", "spot", "review", "sentiment", "aspect", "comment_semantic",
             "topic", "topic_word", "stat_spot", "stat_time", "stat_ip",
             "spot_fact_package", "spot_report", "qa_record", "analysis_task", "task_log"]
DELETED = ["ml_model", "llm_failure", "clean_log", "spot_comparison"]
EXTRA = ["stat_overview"]

print(f"\n你指定的候选表（{len(USER_LIST)} 张）：")
for t in USER_LIST:
    print(f"  {'OK  ' if t in tables else '缺失'} {t}")

print(f"\n你要求删除的（{len(DELETED)} 张）：")
for t in DELETED:
    print(f"  {'OK 已删除' if t not in tables else '仍存在 ⚠'} {t}")

print(f"\n额外保留：")
for t in EXTRA:
    print(f"  {'OK 保留' if t in tables else '缺失'} {t}")

print(f"\n合计：{len(USER_LIST)} + {len(EXTRA)} = {len(USER_LIST) + len(EXTRA)}，脚本实际 {len(tables)} 张")
