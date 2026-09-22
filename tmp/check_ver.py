# -*- coding: utf-8 -*-
"""检查当前开题报告正文内容版本"""
import sys, zipfile, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

P = r"E:\tibet-tourism-ai\开题\NIIT-GUET-2023-Class-2316030201-开题报告.docx"
z = zipfile.ZipFile(P)
x = z.read("word/document.xml").decode("utf-8")
t = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", x, re.S))

print("总文字量:", len(t))
print()
print("=== 版本特征检查 ===")
checks = {
    "新版进度(开发阶段 9月20日—10月31日)": "（一）开发阶段（2026年9月20日—2026年10月31日）",
    "旧版进度(第一阶段2026年9月)": "第一阶段（2026年9月）",
    "5 个核心模块表述": "数据总览模块",
    "智能问答模块": "智能问答模块",
    "景点对比模块": "景点对比模块",
    "事实包": "景点事实包",
    "文字版流程图": "文字版流程图",
    "旧版4项研究内容(可视化系统)": "4．分析结果可视化系统的设计与实现",
}
for k, v in checks.items():
    print(f"  {'有' if v in t else '无'}  {k}")

print()
print("=== 进度计划段落 ===")
i = t.find("进度实施计划")
print(t[i:i+400] if i >= 0 else "未找到")

print()
print("=== 图1 附近段落顺序 ===")
for i, m in enumerate(re.finditer(r"<w:p[ >]", x)):
    seg_end = x.find("</w:p>", m.end())
    seg = x[m.start():seg_end]
    txt = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", seg, re.S)).strip()
    if "图1" in txt or "文字版流程图" in txt or "系统各模块之间" in txt or "系统总体流程图" in txt:
        has_pict = "<w:pict>" in seg
        print(f"  P{i}: pict={has_pict} text={txt[:70]!r}")
