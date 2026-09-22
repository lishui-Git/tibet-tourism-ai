# -*- coding: utf-8 -*-
"""迁移前硬编码路径分级清单（只读，不修改任何文件）"""
import sys, os, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = r"E:\tibet-tourism-ai"
SKIP_DIR = ("\\.venv\\", "\\.git\\", "__pycache__", "_edge_profile", "\\tmp\\")
PAT = re.compile(r"毕业设计")

CATS = {
    "① 运行代码/脚本（迁移后需改）": [],
    "② 项目文档（AI 生成，含旧路径引用）": [],
    "③ 历史采集产物（第三方/日志，无需改）": [],
    "④ 其他": [],
}

for dirpath, dirnames, filenames in os.walk(ROOT):
    if any(s.strip("\\") in dirpath for s in SKIP_DIR):
        continue
    for fn in filenames:
        p = os.path.join(dirpath, fn)
        rel = os.path.relpath(p, ROOT)
        if any(s in p for s in SKIP_DIR):
            continue
        if os.path.getsize(p) > 5 * 1024 * 1024:
            continue
        try:
            with open(p, "rb") as f:
                raw = f.read()
            txt = raw.decode("utf-8", "ignore")
        except Exception:
            continue
        if not PAT.search(txt):
            continue

        # 分级
        if rel.startswith("爬虫工具" + os.sep):
            CATS["③ 历史采集产物（第三方/日志，无需改）"].append(rel)
        elif rel.startswith("docs" + os.sep) or rel.startswith("开发日报" + os.sep) \
                or rel.startswith("开题" + os.sep) or rel in ("ReadMe.md", "README_开发说明.md",
                                                              "项目现状分析.md"):
            CATS["② 项目文档（AI 生成，含旧路径引用）"].append(rel)
        elif fn.endswith((".py", ".ps1", ".js", ".bat", ".cmd", ".sql", ".json", ".env", ".cfg", ".toml")):
            CATS["① 运行代码/脚本（迁移后需改）"].append(rel)
        else:
            CATS["④ 其他"].append(rel)

for cat, items in CATS.items():
    print("=" * 76)
    print(f"{cat}  共 {len(items)} 个")
    print("=" * 76)
    for it in sorted(items):
        # 标注是否存在"真实绝对路径"（而非仅注释提及）
        p = os.path.join(ROOT, it)
        try:
            txt = open(p, "rb").read().decode("utf-8", "ignore")
        except Exception:
            txt = ""
        real = len(re.findall(r"[Ee]:[\\/]+毕业设计", txt))
        mention = len(re.findall(r"毕业设计", txt)) - real
        flag = []
        if real:
            flag.append(f"绝对路径 {real} 处")
        if mention:
            flag.append(f"文字提及 {mention} 处")
        print(f"  {it:56s} {' / '.join(flag)}")
    print()

# 重点：运行代码中真正的绝对路径
print("=" * 76)
print("重点：运行代码中的真实绝对路径（E:\\tibet-tourism-ai）")
print("=" * 76)
found = False
for it in CATS["① 运行代码/脚本（迁移后需改）"]:
    p = os.path.join(ROOT, it)
    try:
        lines = open(p, "rb").read().decode("utf-8", "ignore").split("\n")
    except Exception:
        continue
    for i, ln in enumerate(lines, 1):
        if re.search(r"[Ee]:[\\/]+毕业设计", ln):
            print(f"  {it}:{i}")
            print(f"      {ln.strip()[:120]}")
            found = True
if not found:
    print("  未发现（运行代码中的'毕业设计'仅出现在注释/文档字符串中）")
