# -*- coding: utf-8 -*-
"""逐段列出，确认图片数量与进度计划内容"""
import sys, zipfile, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

P = r"E:\tibet-tourism-ai\tmp\开题报告_修正预览.docx"
z = zipfile.ZipFile(P)
x = z.read("word/document.xml").decode("utf-8")

print("w:pict 总数:", len(re.findall(r"<w:pict>", x)))
for i, m in enumerate(re.finditer(r"<w:pict>", x), 1):
    e = x.find("</w:pict>", m.end())
    seg = x[m.start():e]
    alt = re.search(r'alt="([^"]*)"', seg)
    rid = re.search(r'r:id="(rId\d+)"', seg)
    print(f"  图{i}: alt={alt.group(1) if alt else '?'} rel={rid.group(1) if rid else '?'}")

print("\n=== 段落清单（含'进度'关键字的段） ===")
ps = []
for m in re.finditer(r"<w:p[ >]", x):
    e = x.find("</w:p>", m.end()) + len("</w:p>")
    seg = x[m.start():e]
    txt = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", seg, re.S))
    ps.append((m.start(), e, txt, "<w:pict>" in seg))

for i, (s, e, txt, pict) in enumerate(ps):
    ts = txt.strip()
    if ts.startswith("二、进度实施计划") or ts.startswith("三、预期提交") or \
       ts.startswith("第一阶段") or ts.startswith("第五阶段") or ts.startswith("第九阶段") or \
       ts.startswith("（一）开发阶段") or ts.startswith("说明：上述开发计划"):
        print(f"  P{i}: pict={pict} {ts[:95]!r}")

print("\n=== 进度计划全文 ===")
start = next(i for i, p in enumerate(ps) if p[2].strip().startswith("二、进度实施计划"))
end = next(i for i, p in enumerate(ps) if p[2].strip().startswith("三、预期提交"))
for i in range(start, end):
    print(f"  P{i}: {ps[i][2].strip()[:120]}")
