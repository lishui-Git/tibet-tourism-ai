# -*- coding: utf-8 -*-
"""提取 docx 模板的正文与表格内容（含表格结构），用于分析模板格式"""
import sys, zipfile, re
from xml.etree import ElementTree as ET

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def para_text(p):
    """按顺序取段落内所有文本（含 w:t / w:tab / w:br）"""
    out = []
    for node in p.iter():
        tag = node.tag
        if tag == W + "t":
            out.append(node.text or "")
        elif tag == W + "tab":
            out.append("\t")
        elif tag == W + "br":
            out.append("\n")
    return "".join(out)


def para_style(p):
    pPr = p.find(W + "pPr")
    if pPr is None:
        return ""
    st = pPr.find(W + "pStyle")
    if st is None:
        return ""
    return st.get(W + "val") or ""


def dump(path):
    z = zipfile.ZipFile(path)
    xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    body = root.find(W + "body")
    n_par = 0
    n_tbl = 0
    for el in body:
        if el.tag == W + "p":
            n_par += 1
            t = para_text(el).strip()
            st = para_style(el)
            tag = f"[{st}] " if st else ""
            if t or st:
                print(f"P{n_par:04d} {tag}{t}")
        elif el.tag == W + "tbl":
            n_tbl += 1
            print(f"\n===== 表格 {n_tbl} =====")
            for ri, tr in enumerate(el.findall(W + "tr"), 1):
                cells = []
                for tc in tr.findall(W + "tc"):
                    # 单元格内可能有多个段落
                    segs = [para_text(p).strip() for p in tc.findall(W + "p")]
                    cells.append(" / ".join(s for s in segs if s))
                print(f"  行{ri}: " + " | ".join(cells))
            print(f"===== 表格 {n_tbl} 结束 =====\n")
    print(f"\n[统计] 段落 {n_par} 个，表格 {n_tbl} 个")


if __name__ == "__main__":
    dump(sys.argv[1])
