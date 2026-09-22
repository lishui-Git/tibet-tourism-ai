# -*- coding: utf-8 -*-
"""读取当前开题报告：封面字段、图片、以及正文结构，供重新生成时保留"""
import sys, zipfile, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

P = r"E:\tibet-tourism-ai\开题\NIIT-GUET-2023-Class-2316030201-开题报告.docx"
z = zipfile.ZipFile(P)
print("=== zip 部件 ===")
for n in z.namelist():
    print("  ", n, z.getinfo(n).file_size)

x = z.read("word/document.xml").decode("utf-8")


def p_text(seg):
    return "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", seg, re.S))


def tag_spans(s, tag):
    res = []
    for m in re.finditer(rf"<{tag}(?: [^>]*)?>", s):
        e = s.find(f"</{tag}>", m.end())
        if e == -1:
            continue
        res.append((m.start(), e + len(tag) + 3, m.end(), e))
    return res


print("\n=== 封面表格字段 ===")
tbls = tag_spans(x, "w:tbl")
for ti in (0, 1):
    ts, te, _, _ = tbls[ti]
    seg = x[ts:te]
    for rs, re_, _, _ in tag_spans(seg, "w:tr"):
        rseg = seg[rs:re_]
        cs = tag_spans(rseg, "w:tc")
        cells = [p_text(rseg[c[2]:c[3]]).strip() for c in cs]
        print(f"  表{ti} 行: {cells}")

print("\n=== 图片 ===")
drawings = tag_spans(x, "w:drawing")
print("  w:drawing 数量:", len(drawings))
try:
    rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
    for m in re.finditer(r'Id="([^"]+)"[^>]*Target="([^"]*media[^"]*)"', rels):
        print("  图片关系:", m.group(1), "->", m.group(2))
except KeyError:
    pass

print("\n=== 日期段 ===")
for i, (s, e, _, _) in enumerate(tag_spans(x, "w:p")):
    t = p_text(x[s:e])
    if "年" in t and "月" in t and "日" in t and re.search(r"20\d{2}", t) and len(t.strip()) < 30:
        print(f"  P{i}: {t!r}")
        break

print("\n=== 正文中含'图1'上下文的段落 ===")
for i, (s, e, _, _) in enumerate(tag_spans(x, "w:p")):
    t = p_text(x[s:e]).strip()
    if t.startswith("图1") or "系统各模块之间的数据" in t or "文字版流程图" in t:
        print(f"  P{i}: {t[:80]!r}")
