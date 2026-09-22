# -*- coding: utf-8 -*-
"""
极简 Markdown → docx 生成器（不依赖第三方库）
支持：# 标题、表格、有序/无序列表、代码块、引用、粗体、水平线
用于生成《业务需求文档》等交接文档。
"""
import sys, os, re, zipfile
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- 文本内联 ----------
def run(text, bold=False, font="宋体", sz=24, color=None, mono=False):
    rpr = f'<w:rFonts w:ascii="{"Consolas" if mono else "Times New Roman"}" w:hAnsi="{"Consolas" if mono else "Times New Roman"}" w:eastAsia="{font}"/>'
    if bold:
        rpr += "<w:b/>"
    if color:
        rpr += f'<w:color w:val="{color}"/>'
    rpr += f'<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/>'
    return f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{esc(text)}</w:t></w:r>'


def inline(text, base_sz=24):
    """处理 **粗体** 与 `代码`"""
    parts = re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text)
    out = []
    for p in parts:
        if not p:
            continue
        if p.startswith("**") and p.endswith("**") and len(p) > 4:
            out.append(run(p[2:-2], bold=True, sz=base_sz))
        elif p.startswith("`") and p.endswith("`") and len(p) > 2:
            out.append(run(p[1:-1], mono=True, sz=base_sz - 2))
        else:
            out.append(run(p, sz=base_sz))
    return "".join(out)


def para(text, style=None, align=None, sz=24, before=0, after=60, line=360,
         indent_first=False, bold=False, keep_next=False):
    ppr = ""
    if style:
        ppr += f'<w:pStyle w:val="{style}"/>'
    if keep_next:
        ppr += "<w:keepNext/>"
    ppr += f'<w:spacing w:before="{before}" w:after="{after}" w:line="{line}" w:lineRule="auto"/>'
    if indent_first:
        ppr += '<w:ind w:firstLineChars="200" w:firstLine="480"/>'
    if align:
        ppr += f'<w:jc w:val="{align}"/>'
    body = inline(text, sz) if text else ""
    return f'<w:p><w:pPr>{ppr}</w:pPr>{body}</w:p>'


def heading(text, level):
    cfg = {1: (32, "黑体", 240, 120), 2: (28, "黑体", 200, 100),
           3: (26, "黑体", 160, 80), 4: (24, "黑体", 120, 60)}[level]
    sz, font, before, after = cfg
    rpr = f'<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="{font}"/><w:b/><w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/>'
    return (f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/><w:keepNext/>'
            f'<w:spacing w:before="{before}" w:after="{after}" w:line="340" w:lineRule="auto"/></w:pPr>'
            f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{esc(text)}</w:t></w:r></w:p>')


def hline():
    return ('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="BFBFBF"/></w:pBdr>'
            '<w:spacing w:before="60" w:after="60"/></w:pPr></w:p>')


def code_block(lines):
    out = []
    for ln in lines:
        rpr = ('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:eastAsia="宋体"/>'
               '<w:sz w:val="18"/><w:szCs w:val="18"/>')
        out.append(f'<w:p><w:pPr><w:pStyle w:val="CodeBlock"/>'
                   f'<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="exact"/>'
                   f'<w:ind w:left="360"/></w:pPr>'
                   f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{esc(ln) if ln else " "}</w:t></w:r></w:p>')
    return "".join(out)


def table(rows, widths=None):
    """rows: [[cell,...],...] 第一行为表头"""
    ncol = max(len(r) for r in rows)
    total = 9350
    if not widths:
        widths = [int(total / ncol)] * ncol
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    xml = [f'<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
           f'<w:tblW w:w="{total}" w:type="dxa"/><w:jc w:val="center"/>'
           f'<w:tblBorders>'
           f'<w:top w:val="single" w:sz="6" w:color="7F7F7F"/>'
           f'<w:left w:val="single" w:sz="6" w:color="7F7F7F"/>'
           f'<w:bottom w:val="single" w:sz="6" w:color="7F7F7F"/>'
           f'<w:right w:val="single" w:sz="6" w:color="7F7F7F"/>'
           f'<w:insideH w:val="single" w:sz="4" w:color="A6A6A6"/>'
           f'<w:insideV w:val="single" w:sz="4" w:color="A6A6A6"/>'
           f'</w:tblBorders></w:tblPr><w:tblGrid>{grid}</w:tblGrid>']
    for ri, r in enumerate(rows):
        head = (ri == 0)
        cells = r + [""] * (ncol - len(r))
        tr = ["<w:tr>"]
        if head:
            tr.append('<w:trPr><w:tblHeader/></w:trPr>')
        for ci, c in enumerate(cells):
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="DEEAF6"/>' if head else ""
            tr.append(f'<w:tc><w:tcPr><w:tcW w:w="{widths[ci]}" w:type="dxa"/>{shade}'
                      f'<w:vAlign w:val="center"/></w:tcPr>'
                      f'<w:p><w:pPr><w:spacing w:before="20" w:after="20" w:line="280" w:lineRule="auto"/>'
                      f'<w:jc w:val="left"/></w:pPr>{inline(c, 21)}</w:p></w:tc>')
        tr.append("</w:tr>")
        xml.append("".join(tr))
    xml.append("</w:tbl>")
    xml.append(para("", after=60))
    return "".join(xml)


# ---------- Markdown 解析 ----------
def md_to_body(md):
    lines = md.split("\n")
    out = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        s = ln.rstrip()

        # 代码块
        if s.strip().startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append(code_block(buf))
            continue

        # 表格
        if s.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            rows = []
            hdr = [c.strip() for c in s.strip().strip("|").split("|")]
            rows.append(hdr)
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append(table(rows))
            continue

        # 水平线
        if re.match(r"^\s*---+\s*$", s):
            out.append(hline())
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            out.append(heading(m.group(2).strip(), len(m.group(1))))
            i += 1
            continue

        # 引用
        if s.strip().startswith(">"):
            txt = s.strip().lstrip(">").strip()
            out.append(para(txt, sz=22, indent_first=False, before=40, after=40))
            i += 1
            continue

        # 列表
        mo = re.match(r"^(\s*)(\d+)\.\s+(.*)$", s)
        mu = re.match(r"^(\s*)[-*]\s+(.*)$", s)
        if mo or mu:
            level = len((mo or mu).group(1)) // 2
            txt = mo.group(3) if mo else mu.group(2)
            prefix = f"{mo.group(2)}. " if mo else "· "
            out.append(para(prefix + txt, sz=24, before=0, after=40,
                            indent_first=False, line=340))
            # 增加左缩进
            out[-1] = out[-1].replace('<w:spacing',
                                      f'<w:ind w:left="{360 + level * 360}"/><w:spacing')
            i += 1
            continue

        # 空行
        if not s.strip():
            i += 1
            continue

        # 普通段落
        out.append(para(s, indent_first=False, sz=24, before=0, after=80, line=360))
        i += 1
    return "".join(out)


# ---------- docx 组装 ----------
DOC_TPL = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>{body}
<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1418" w:right="1276" w:bottom="1418" w:left="1418" w:header="851" w:footer="992" w:gutter="0"/><w:cols w:space="720"/><w:docGrid w:type="lines" w:linePitch="312"/></w:sectPr></w:body></w:document>'''

STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W}">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体" w:cs="Times New Roman"/><w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:line="360" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:outlineLvl w:val="1"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:outlineLvl w:val="2"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading4"><w:name w:val="heading 4"/><w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:outlineLvl w:val="3"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="CodeBlock"><w:name w:val="Code Block"/><w:basedOn w:val="Normal"/></w:style>
<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4" w:color="auto"/><w:left w:val="single" w:sz="4" w:color="auto"/><w:bottom w:val="single" w:sz="4" w:color="auto"/><w:right w:val="single" w:sz="4" w:color="auto"/><w:insideH w:val="single" w:sz="4" w:color="auto"/><w:insideV w:val="single" w:sz="4" w:color="auto"/></w:tblBorders></w:tblPr></w:style>
</w:styles>'''.replace("{W}", W)

CONTENT_TYPES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
</Types>'''

RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>
</Relationships>'''

ROOT_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''

SETTINGS = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="{W}"><w:zoom w:percent="100"/><w:defaultTabStop w:val="420"/></w:settings>'''


def cover(title, subtitle_lines):
    out = [para("", after=0), para("", after=0), para("", after=0)]
    out.append(para(title, align="center", sz=52, before=200, after=200, line=400))
    for t in subtitle_lines:
        out.append(para(t, align="center", sz=28, before=60, after=60, line=360))
    out.append(para("", after=0))
    return "".join(out)


def page_break():
    return '<w:p><w:pPr><w:spacing w:after="0"/></w:pPr><w:r><w:br w:type="page"/></w:r></w:p>'


def toc_field():
    """插入 Word 目录域（打开文档后按 F9 或右键"更新域"即可生成目录）"""
    rpr = ('<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体"/>'
           '<w:sz w:val="24"/><w:szCs w:val="24"/><w:noProof/>')
    hpr = ('<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="黑体"/>'
           '<w:b/><w:sz w:val="32"/><w:szCs w:val="32"/>')
    return (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/><w:keepNext/>'
        '<w:spacing w:before="240" w:after="120"/></w:pPr>'
        f'<w:r><w:rPr>{hpr}</w:rPr>'
        '<w:t>目录</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:spacing w:after="0"/></w:pPr>'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:rPr>{rpr}</w:rPr><w:instrText xml:space="preserve"> TOC \\o "1-3" \\h \\z \\u </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        f'<w:r><w:rPr>{rpr}</w:rPr><w:t>【请在 Word 中右键此处选择"更新域"以生成目录】</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')


def build(md_path, out_path, cover_title, cover_lines):
    md = open(md_path, encoding="utf-8").read()
    # 去掉一级标题（作为封面标题）
    md = re.sub(r"^#\s+.*\n", "", md, count=1)
    # 手工目录段（从"## 目录"到下一个"---"）替换为 Word 目录域
    md = re.sub(r"##\s*目录\s*\n.*?\n---\n", "<TOC/>\n", md, count=1, flags=re.S)
    # 封面分页
    body = cover(cover_title, cover_lines) + page_break() + toc_field() + page_break() + md_to_body(md)
    doc = DOC_TPL.replace("{W}", W).replace("{body}", body)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("word/document.xml", doc.encode("utf-8"))
        z.writestr("word/styles.xml", STYLES.encode("utf-8"))
        z.writestr("word/settings.xml", SETTINGS.encode("utf-8"))
        z.writestr("word/_rels/document.xml.rels", RELS)
    print("[生成]", out_path, os.path.getsize(out_path), "字节")


if __name__ == "__main__":
    build(
        r"E:\tibet-tourism-ai\开题\业务需求文档.md",
        os.environ.get("BRD_OUT") or r"E:\tibet-tourism-ai\开题\业务需求文档.docx",
        "业务需求文档（BRD）",
        ["项目名称：基于 DeepSeek 的西藏旅游景点智能评价与分析系统",
         "文档版本：V1.1",
         "编写日期：2026-09-20",
         "阶段：开题阶段 — 需求分析"],
    )
