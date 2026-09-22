# -*- coding: utf-8 -*-
"""列出图1前后的段落，确认要删除的文字版流程图范围"""
import sys, zipfile, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

P = r"E:\tibet-tourism-ai\开题\NIIT-GUET-2023-Class-2316030201-开题报告.docx"
z = zipfile.ZipFile(P)
x = z.read("word/document.xml").decode("utf-8")

ps = []
for m in re.finditer(r"<w:p[ >]", x):
    e = x.find("</w:p>", m.end())
    e = e + len("</w:p>")
    seg = x[m.start():e]
    txt = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", seg, re.S))
    ps.append((m.start(), e, txt, "<w:pict>" in seg))

print("总段落数:", len(ps))
start = None
for i, (s, e, t, pict) in enumerate(ps):
    if "系统各模块之间的数据" in t:
        start = i
        break
print("起始段落 index:", start)
print()
print("=== 从起始段开始连续列出 40 段 ===")
for i in range(start, min(start + 40, len(ps))):
    s, e, t, pict = ps[i]
    tag = "【图片】" if pict else ""
    print(f"  P{i:3d} len={e-s:5d} {tag} {t[:78]!r}")
