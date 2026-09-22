# -*- coding: utf-8 -*-
"""最终核对：合并版开题报告内容与结构"""
import sys, zipfile, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

P = r"E:\tibet-tourism-ai\tmp\开题报告_合并版预览.docx"
z = zipfile.ZipFile(P)
x = z.read("word/document.xml").decode("utf-8")
t = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", x, re.S))

print("=== 关键内容检查 ===")
checks = {
    "新版进度（开发阶段 9/20—10/31）": "（一）开发阶段（2026年9月20日—2026年10月31日）",
    "旧版进度已移除": "第一阶段（2026年9月）",
    "文字版流程图已移除": "说明：本图为便于排版的文字版流程图",
    "保留 5 个核心模块": "数据总览模块",
    "保留智能问答": "智能问答模块",
    "保留景点对比": "景点对比模块",
    "保留事实包": "景点事实包",
    "保留数据负责事实": "数据负责事实",
    "预期提交资料": "预期提交的实训资料",
}
for k, v in checks.items():
    hit = v in t
    ok = hit if "已移除" not in k else (not hit)
    print(f"  {'OK ' if ok else '异常'} {k}")

print("\n=== 图片结构 ===")
for m in re.finditer(r"<w:pict>", x):
    seg = x[m.start():m.start() + 700]
    alt = re.search(r'alt="([^"]*)"', seg)
    style = re.search(r'style="([^"]*)"', seg)
    rid = re.search(r'r:id="(rId\d+)"', seg)
    print(f"  图片 alt={alt.group(1) if alt else '?'} style={style.group(1) if style else '?'} rel={rid.group(1) if rid else '?'}")

print("\n=== 进度计划 ===")
i = t.find("（一）开发阶段")
j = t.find("三、预期提交")
print(t[i:j][:1200])

print("\n=== 图1 上下文 ===")
k = t.find("图1")
print(t[k - 80:k + 60])
