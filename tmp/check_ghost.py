# -*- coding: utf-8 -*-
"""排查已删除表的残留引用，区分"合理提及"与"幽灵引用" """
import sys, os, re, glob
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = r"E:\tibet-tourism-ai"
FILES = []
for sub in ("docs/architecture", "docs/design", "docs/database", "docs/diagrams"):
    FILES += glob.glob(os.path.join(ROOT, sub.replace("/", os.sep), "*.md"))
FILES += glob.glob(os.path.join(ROOT, "docs", "*.md"))
FILES += [os.path.join(ROOT, "开题", "业务需求文档.md")]

DELETED = ["ml_model", "llm_failure", "clean_log", "spot_comparison"]
# 合理提及的语境关键词（说明"已删除/已并入/不再"）
OK_CTX = ["已删除", "已并入", "删除", "并入", "不再", "无专用表", "已收敛", "原 ", "原`",
          "信息去处", "未建", "不建", "改为", "去掉", "精简", "迁移", "承接", "取消",
          "不缓存", "保留、未做", "范围外"]

problems = []
for f in sorted(set(FILES)):
    if not os.path.exists(f):
        continue
    lines = open(f, encoding="utf-8").read().split("\n")
    rel = os.path.relpath(f, ROOT)
    for i, ln in enumerate(lines, 1):
        for tbl in DELETED:
            if tbl in ln:
                ctx = "\n".join(lines[max(0, i - 2):i + 2])
                if not any(k in ctx for k in OK_CTX):
                    problems.append((rel, i, tbl, ln.strip()[:110]))

print("=" * 76)
print("已删除表残留引用排查")
print("=" * 76)
if problems:
    print(f"发现 {len(problems)} 处需要确认的引用：\n")
    for rel, i, tbl, ln in problems:
        print(f"  {rel}:{i}  [{tbl}]")
        print(f"      {ln}")
else:
    print("未发现幽灵引用 ✅")
    print("（所有提及均已出现在'已删除/已并入/不再使用'等说明性语境中）")

# 汇总每个表的提及数与语境
print("\n" + "=" * 76)
print("各已删表提及统计")
print("=" * 76)
for tbl in DELETED:
    total, ok = 0, 0
    for f in sorted(set(FILES)):
        if not os.path.exists(f):
            continue
        lines = open(f, encoding="utf-8").read().split("\n")
        for i, ln in enumerate(lines):
            if tbl in ln:
                total += 1
                ctx = "\n".join(lines[max(0, i - 2):i + 2])
                if any(k in ctx for k in OK_CTX):
                    ok += 1
    print(f"  {tbl:18s} 提及 {total:2d} 处，其中说明性语境 {ok:2d} 处")

# 新表清单在所有文档中的一致性
print("\n" + "=" * 76)
print("表数量表述一致性")
print("=" * 76)
for f in sorted(set(FILES)):
    if not os.path.exists(f):
        continue
    txt = open(f, encoding="utf-8").read()
    for m in re.finditer(r"(\d+)\s*张表", txt):
        n = m.group(1)
        if n not in ("17",):
            rel = os.path.relpath(f, ROOT)
            ln = txt[:m.start()].count("\n") + 1
            print(f"  ⚠ {rel}:{ln} 出现 '{n} 张表'")
print("  （仅 '17 张表' 为正确表述；上方若无输出即全部一致）")
