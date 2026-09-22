# -*- coding: utf-8 -*-
"""列出进度前后的段落，定位小标题是否丢失"""
import sys, zipfile, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

for P in [r"E:\tibet-tourism-ai\tmp\开题报告_合并版预览.docx", r"E:\tibet-tourism-ai\tmp\开题报告_修正预览.docx"]:
    print("=" * 30, P.split("\\")[-1], "=" * 30)
    z = zipfile.ZipFile(P)
    x = z.read("word/document.xml").decode("utf-8")
    ps = []
    for m in re.finditer(r"<w:p[ >]", x):
        e = x.find("</w:p>", m.end()) + len("</w:p>")
        seg = x[m.start():e]
        txt = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", seg, re.S))
        ps.append((m.start(), e, txt, "<w:pict>" in seg))
    # 找图1标题
    gi = next((i for i, p in enumerate(ps) if p[2].strip().startswith("图1")), None)
    print(f"  图1 在 P{gi}，总段落 {len(ps)}")
    for i in range(max(0, gi - 2), min(len(ps), gi + 22)):
        s, e, t, pict = ps[i]
        mark = "【图片】" if pict else ""
        print(f"    P{i:3d} {mark} {t.strip()[:88]!r}")
    print()
