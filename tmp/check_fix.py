# -*- coding: utf-8 -*-
"""修正预览件最终核对"""
import sys, zipfile, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

P = r"E:\tibet-tourism-ai\tmp\开题报告_修正预览.docx"
z = zipfile.ZipFile(P)
x = z.read("word/document.xml").decode("utf-8")
t = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", x, re.S))
rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
rid2t = dict(re.findall(r'<Relationship Id="(rId\d+)"[^>]*Target="([^"]+)"', rels))

print("=== 图片关系核对 ===")
for m in re.finditer(r"<w:pict>", x):
    seg = x[m.start():m.start() + 800]
    alt = re.search(r'alt="([^"]*)"', seg)
    rid = re.search(r'r:id="(rId\d+)"', seg)
    tgt = rid2t.get(rid.group(1)) if rid else None
    size = z.getinfo("word/" + tgt).file_size if tgt else 0
    print(f"  {alt.group(1) if alt else '?'} -> {rid.group(1) if rid else '?'} -> {tgt} ({size} 字节)")

print("\n=== 内容核对 ===")
checks = {
    "新版进度表头": "（一）开发阶段（2026年9月20日—2026年10月31日）",
    "旧版第一阶段": "第一阶段（2026年9月）",
    "文字版流程图残留": "说明：本图为便于排版的文字版流程图",
    "五个核心模块": "数据总览模块",
    "事实包": "景点事实包",
    "图题": "图1",
}
for k, v in checks.items():
    hit = v in t
    bad = ("旧版" in k or "残留" in k)
    ok = (not hit) if bad else hit
    print(f"  {'OK ' if ok else '异常'} {k}")

print("\n=== 进度计划 ===")
i = t.find("二、进度实施计划")
j = t.find("三、预期提交")
print(t[i:j][:900])
