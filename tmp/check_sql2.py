# -*- coding: utf-8 -*-
"""SQL 脚本深度校验：排除注释与字符串字面量后再校验括号；核对可疑注释与表数"""
import sys, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

P = r"E:\tibet-tourism-ai\docs\database\建表脚本.sql"
sql = open(P, encoding="utf-8").read()

# 1) 去注释
s = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
s = re.sub(r"--[^\n]*", "", s)
# 2) 去单引号字符串
s2 = re.sub(r"'(?:[^']|'')*'", "''", s)
# 3) 去反引号标识符
s3 = re.sub(r"`[^`]*`", "``", s2)

print("=" * 70)
print("括号平衡校验（逐层剥离）")
print("=" * 70)
print(f"  原始文本          ( = {sql.count('('):4d}   ) = {sql.count(')'):4d}   差 = {sql.count('(')-sql.count(')'):+d}")
print(f"  去注释后          ( = {s.count('('):4d}   ) = {s.count(')'):4d}   差 = {s.count('(')-s.count(')'):+d}")
print(f"  再去字符串/反引号 ( = {s3.count('('):4d}   ) = {s3.count(')'):4d}   差 = {s3.count('(')-s3.count(')'):+d}")
ok = s3.count("(") == s3.count(")")
print(f"\n  结论：{'SQL 语法括号平衡 ✅' if ok else '不平衡 ⚠'}")

# 4) 每条 CREATE TABLE 的括号配平（逐语句）
print("\n" + "=" * 70)
print("逐条 CREATE TABLE 语句配平校验")
print("=" * 70)
stmts = re.findall(r"CREATE TABLE `(\w+)`\s*\((.*?)\)\s*ENGINE=InnoDB", s, re.S)
print(f"  匹配到完整 CREATE TABLE 语句：{len(stmts)} 条")
bad_stmt = []
for name, body in stmts:
    # 去掉 body 内的字符串
    b = re.sub(r"'(?:[^']|'')*'", "''", body)
    diff = b.count("(") - b.count(")")
    if diff != 0:
        bad_stmt.append((name, diff))
print(f"  括号不配平的语句：{bad_stmt if bad_stmt else '无 ✅'}")

# 5) 可疑注释核实
print("\n" + "=" * 70)
print("可疑注释核实")
print("=" * 70)
for i, ln in enumerate(sql.split("\n"), 1):
    if "dup_group_id" in ln:
        print(f"  行{i}: {ln.strip()}")
        # 检查该注释内中文括号是否成对
        seg = ln[ln.find("COMMENT="):]
        op = seg.count("（")
        cl = seg.count("）")
        print(f"        中文括号：（ {op} 个，） {cl} 个 → {'配对 ✅' if op == cl else '不配对 ⚠'}")

# 6) 表数一致性核对
print("\n" + "=" * 70)
print("表数一致性核对")
print("=" * 70)
tables = re.findall(r"CREATE TABLE `(\w+)`", sql)
print(f"  SQL 脚本中定义的表：{len(tables)} 张")

for doc in [r"E:\tibet-tourism-ai\docs\database\数据库设计说明.md",
            r"E:\tibet-tourism-ai\docs\database\ER图.md",
            r"E:\tibet-tourism-ai\docs\设计阶段一致性检查报告.md"]:
    txt = open(doc, encoding="utf-8").read()
    nums = re.findall(r"(\d+)\s*张表", txt)
    print(f"  {doc.split(chr(92))[-1]}: 文中'张表'表述 → {sorted(set(nums))}")

print("\n" + "=" * 70)
print("校验完成")
print("=" * 70)
