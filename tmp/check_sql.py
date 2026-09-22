# -*- coding: utf-8 -*-
"""建表脚本结构自检（无 MySQL 环境，做静态校验）"""
import sys, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

P = r"E:\tibet-tourism-ai\docs\database\建表脚本.sql"
sql = open(P, encoding="utf-8").read()

print("=" * 70)
print("建表脚本静态校验")
print("=" * 70)
print(f"文件大小：{len(sql)} 字符，{sql.count(chr(10))+1} 行")

# 1. CREATE TABLE 语句
tables = re.findall(r"CREATE TABLE `(\w+)`", sql)
print(f"\n【CREATE TABLE】共 {len(tables)} 张表：")
for i, t in enumerate(tables, 1):
    print(f"  {i:2d}. {t}")

# 2. 与 ER 图/设计说明中的表名核对
expected = ["sys_user", "spot", "review", "sentiment", "aspect", "comment_semantic",
            "topic", "topic_word", "stat_overview", "stat_spot", "stat_time", "stat_ip",
            "spot_fact_package", "spot_report", "spot_comparison", "qa_record",
            "analysis_task", "task_log", "clean_log", "llm_failure", "ml_model"]
print(f"\n【表清单核对】期望 {len(expected)} 张")
missing = [t for t in expected if t not in tables]
extra = [t for t in tables if t not in expected]
print(f"  缺失：{missing if missing else '无'}")
print(f"  多出：{extra if extra else '无'}")

# 3. 括号配平
print(f"\n【括号配平】开括号 {sql.count('(')}，闭括号 {sql.count(')')}，"
      f"{'平衡' if sql.count('(') == sql.count(')') else '不平衡 ⚠'}")

# 4. PRIMARY KEY / UNIQUE KEY 完整性
tbl_blocks = re.split(r"CREATE TABLE `", sql)[1:]
print(f"\n【每张表的主键与外键】")
pk_issues = []
for blk in tbl_blocks:
    name = blk.split("`")[0]
    body = blk.split(";")[0]
    has_pk = "PRIMARY KEY" in body
    fks = len(re.findall(r"FOREIGN KEY", body))
    uks = len(re.findall(r"UNIQUE KEY", body))
    idx = len(re.findall(r"\n  KEY ", body))
    if not has_pk:
        pk_issues.append(name)
    print(f"  {name:22s} PK={'有' if has_pk else '无 ⚠'}  UK={uks}  FK={fks}  普通索引={idx}")
print(f"  无主键的表：{pk_issues if pk_issues else '无'}")

# 5. 关键口径注释是否写入
print(f"\n【关键口径写入检查】")
for kw in ["59033", "837", "57", "35098", "2022-08", "≤10字", "7.18%", "17.33%", "BR-01", "BR-02", "BR-04", "BR-06"]:
    print(f"  {kw:10s} {'有' if kw in sql else '无 ⚠'}")

# 6. 字符集与外键开关
print(f"\n【其他】")
print(f"  utf8mb4 出现 {sql.count('utf8mb4')} 次")
print(f"  SET FOREIGN_KEY_CHECKS = 0 存在：{'是' if 'SET FOREIGN_KEY_CHECKS = 0' in sql else '否'}")
print(f"  结尾恢复 FOREIGN_KEY_CHECKS = 1：{'是' if 'SET FOREIGN_KEY_CHECKS = 1' in sql else '否'}")
print(f"  InnoDB 表数：{sql.count('ENGINE=InnoDB')}")

# 7. 危险语句检查
print(f"\n【危险语句检查】")
for danger in ["DROP DATABASE", "TRUNCATE", "DELETE FROM"]:
    n = sql.count(danger)
    print(f"  {danger:16s} {n} 处 {'⚠ 需确认' if n else 'OK'}")
drops = re.findall(r"DROP TABLE IF EXISTS `(\w+)`", sql)
print(f"  DROP TABLE IF EXISTS：{len(drops)} 处（均为建表前重建，脚本开头已声明可重复执行）")

print("\n" + "=" * 70)
print("校验完成")
print("=" * 70)
