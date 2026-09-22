# -*- coding: utf-8 -*-
"""用嵌套配平的方式解析表格结构，定位标签所在单元格"""
import sys, zipfile, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def balanced_spans(x, tag):
    """正确配平嵌套的 <tag>...</tag>"""
    res = []
    open_re = re.compile(rf"<{tag}(?: [^>]*)?>")
    close_re = re.compile(rf"</{tag}>")
    pos = 0
    while True:
        m = open_re.search(x, pos)
        if not m:
            break
        depth = 0
        i = m.start()
        while i < len(x):
            mo = open_re.match(x, i)
            mc = close_re.match(x, i)
            if mo:
                depth += 1
                i = mo.end()
            elif mc:
                depth -= 1
                i = mc.end()
                if depth == 0:
                    break
            else:
                i += 1
        res.append((m.start(), i, m.end(), i - len(tag) - 3))
        pos = i
    return res


def p_text(seg):
    return "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", seg, re.S))


z = zipfile.ZipFile(r"E:\tibet-tourism-ai\tmp\开题报告模板.docx")
x = z.read("word/document.xml").decode("utf-8")

tbls = balanced_spans(x, "w:tbl")
print(f"表格数: {len(tbls)}")
for ti, (ts, te, _, _) in enumerate(tbls):
    seg = x[ts:te]
    trs = balanced_spans(seg, "w:tr")
    print(f"\n=== 表格{ti} 长度{len(seg)} 行数{len(trs)} ===")
    for ri, (rs, re_, _, _) in enumerate(trs):
        rseg = seg[rs:re_]
        tcs = balanced_spans(rseg, "w:tc")
        cells = []
        for ci, (cs, ce, _, _) in enumerate(tcs):
            cells.append(f"c{ci}:'{p_text(rseg[cs:ce]).strip()[:22]}'")
        print(f"  行{ri}: {len(tcs)}格 | " + " ".join(cells))
