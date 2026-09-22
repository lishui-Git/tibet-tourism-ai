# -*- coding: utf-8 -*-
"""检查图片在文档中的实际引用形式"""
import sys, zipfile, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

P = r"E:\tibet-tourism-ai\开题\NIIT-GUET-2023-Class-2316030201-开题报告.docx"
z = zipfile.ZipFile(P)
x = z.read("word/document.xml").decode("utf-8")

print("=== 图片相关标签统计 ===")
for tag in ["w:drawing", "w:pict", "v:shape", "v:imagedata", "w:object", "w:r:drawing",
            "wp:inline", "wp:anchor", "a:blip", "w:binData", "o:OLEObject"]:
    print(f"  {tag:14s} {len(re.findall(re.escape('<'+tag), x))}")

print("\n=== rId 引用统计 ===")
for rid in ["rId5", "rId6"]:
    print(f"  {rid}: {len(re.findall(re.escape(rid), x))} 次")

print("\n=== 关系文件全文 ===")
print(z.read("word/_rels/document.xml.rels").decode("utf-8"))

print("\n=== 含 image 的片段 ===")
for m in re.finditer(r"r:embed=\"[^\"]+\"|r:id=\"rId[56]\"", x):
    print(" ", m.group(0), "位置", m.start())
    print("   上下文:", x[max(0, m.start()-320):m.start()+160].replace("><", ">\n   <")[-700:])
