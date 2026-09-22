# -*- coding: utf-8 -*-
"""
按模板原貌生成开题报告（最终版 v3）
- 原始字符串替换，保留模板命名空间 / 样式 / 表格边框
- 每次替换后按**文本内容**重新定位，绝不缓存字符位置
- 段落整体重建，避免模板把一个段落拆成多个 run 时破坏 XML
"""
import sys, os, json, zipfile, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = r"E:\tibet-tourism-ai"
TMP = os.path.join(ROOT, "tmp")
TPL = os.path.join(TMP, "开题报告模板.docx")
OUT = os.environ.get("KAITI_OUT") or os.path.join(ROOT, "开题", "NIIT-GUET-2023-Class-2316030201-开题报告.docx")

RPR_BODY = ('<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" '
            'w:eastAsia="宋体" w:cs="Times New Roman"/><w:sz w:val="24"/><w:szCs w:val="24"/>')

HEAD_PREFIX = {"s1_body": "1、实训项目的主要内容",
               "s2_body": "2、准备情况",
               "s3_body": "3、实施方案"}


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def new_para(text, indent=True, bold=False, mono=False, sz=24):
    ind = '<w:ind w:firstLineChars="200" w:firstLine="480"/>' if indent else ""
    b = "<w:b/>" if bold else ""
    if mono:
        req = (f'<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:eastAsia="宋体"/>'
               f'<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/></w:rPr>')
        return (f'<w:p><w:pPr><w:pStyle w:val="a5"/><w:spacing w:after="0" w:line="260" w:lineRule="exact"/>'
                f'<w:jc w:val="left"/>{req}</w:pPr>'
                f'<w:r>{req}<w:t xml:space="preserve">{esc(text)}</w:t></w:r></w:p>')
    req = f'<w:rPr>{b}{RPR_BODY}</w:rPr>'
    return (f'<w:p><w:pPr><w:pStyle w:val="a5"/><w:spacing w:line="400" w:lineRule="exact"/>'
            f'{ind}{req}</w:pPr><w:r>{req}<w:t xml:space="preserve">{esc(text)}</w:t></w:r></w:p>')


def build_paras(lines):
    """__FIGURE__ 处插入已保存的流程图图片（从已有文档中提取，保持 VML 形式）"""
    out = []
    FLOW_CHARS = "↓↑┌┐└┘├┤┬┴─│"
    for ln in lines:
        if ln == "__FIGURE__":
            out.append(_figure_paras())
            continue
        s = ln.strip()
        if not s:
            out.append('<w:p><w:pPr><w:pStyle w:val="a5"/></w:pPr></w:p>')
            continue
        is_flow = (s == "↓" or any(c in ln for c in FLOW_CHARS))
        if is_flow:
            out.append(new_para(ln, indent=False, mono=True, sz=20))
            continue
        is_head = bool(re.match(r"^[一二三四五六七八九十]、", ln))
        out.append(new_para(ln, indent=not is_head, bold=is_head))
    return "".join(out)


FIGURE_SRC = os.path.join(ROOT, "开题", "NIIT-GUET-2023-Class-2316030201-开题报告.docx")
_fig_cache = {}


def _figure_paras():
    """
    从已有开题报告中提取"图1 系统总体流程图"标题段与图片段，原样复用（VML 图片）。
    图片关系 ID 保留原值，输出包中同步复制 media 与 rels。
    """
    if "xml" in _fig_cache:
        return _fig_cache["xml"]
    import zipfile as _zf
    z = _zf.ZipFile(FIGURE_SRC)
    src = z.read("word/document.xml").decode("utf-8")
    caption = None
    img_seg = None
    for m in re.finditer(r"<w:p[ >]", src):
        e = src.find("</w:p>", m.end()) + len("</w:p>")
        seg = src[m.start():e]
        # 只认流程图那张图片：按 o:title / alt 精确匹配，避免误抓封面校徽
        if "<w:pict>" in seg and ("系统总体流程图" in seg):
            img_seg = seg
            rel = re.search(r'r:id="(rId\d+)"', seg)
            _fig_cache["rid"] = rel.group(1) if rel else None
        txt = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", seg, re.S)).strip()
        if txt.startswith("图1") and caption is None:
            caption = txt
    z.close()
    if img_seg is None:
        raise SystemExit("未能在已有报告中找到流程图图片，请确认图片已插入")
    _fig_cache["caption"] = caption or "图1　系统总体流程图"
    # 标题段居中 + 图片段居中
    # 标题段居中 + 图片段居中（img_seg 自带 </w:p>，此处只取其中内容，避免重复闭合）
    body = img_seg[img_seg.find("<w:r>"):]
    if body.endswith("</w:p>"):
        body = body[: -len("</w:p>")]
    cap_p = ('<w:p><w:pPr><w:jc w:val="center"/><w:spacing w:before="120" w:after="60" w:line="360" w:lineRule="auto"/></w:pPr>'
             + run_plain(_fig_cache["caption"]) + '</w:p>')
    img_p = ('<w:p><w:pPr><w:jc w:val="center"/><w:spacing w:before="60" w:after="60"/></w:pPr>'
             + body + '</w:p>')
    _fig_cache["xml"] = cap_p + img_p
    return _fig_cache["xml"]


def run_plain(text):
    rpr = ('<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体"/>'
           '<w:sz w:val="24"/><w:szCs w:val="24"/>')
    return f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{esc(text)}</w:t></w:r>'


def tag_spans(x, tag):
    res = []
    for m in re.finditer(rf"<{tag}(?: [^>]*)?>", x):
        e = x.find(f"</{tag}>", m.end())
        if e == -1:
            continue
        res.append((m.start(), e + len(tag) + 3, m.end(), e))
    return res


def p_text(seg):
    """只取 <w:t> 的文本；注意不能用 <w:t[^>]*>，那会误匹配 <w:tabs>/<w:tab>"""
    return "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", seg, re.S))


def rewrite_paragraph(x, ps_index, text):
    ps = tag_spans(x, "w:p")
    s, e, is_, ie = ps[ps_index]
    p = x[s:e]
    ppr = re.search(r"</w:pPr>", p)
    cut = ppr.end() if ppr else p.find(">") + 1
    head = p[:cut]
    rpr = ""
    m = re.search(r"<w:r(?: [^>]*)?>(.*?)</w:r>", p[cut:], re.S)
    if m:
        mr = re.search(r"<w:rPr>.*?</w:rPr>", m.group(1), re.S)
        if mr:
            rpr = mr.group(0)
    if not rpr:
        rpr = f"<w:rPr>{RPR_BODY}</w:rPr>"
    return x[:s] + head + f'<w:r>{rpr}<w:t xml:space="preserve">{esc(text)}</w:t></w:r></w:p>' + x[e:]


def find_p_index(x, pred):
    for i, (s, e, _, _) in enumerate(tag_spans(x, "w:p")):
        if pred(p_text(x[s:e])):
            return i
    return None


def replace_label_cell(x, label, lines, table_idx):
    """按行内标签文本定位并替换**该行最后一个单元格**的内容"""
    tbls = tag_spans(x, "w:tbl")
    ts, te, _, _ = tbls[table_idx]
    seg_all = x[ts:te]
    for rs, re_, _, _ in tag_spans(seg_all, "w:tr"):
        seg = seg_all[rs:re_]
        cs = tag_spans(seg, "w:tc")
        if len(cs) < 2:
            continue
        for ci in range(len(cs)):
            if p_text(seg[cs[ci][2]:cs[ci][3]]).strip().rstrip("：:") == label.strip().rstrip("：:"):
                gs, ge = ts + rs + cs[-1][2], ts + rs + cs[-1][3]
                return x[:gs] + build_paras(lines) + x[ge:]
    raise SystemExit(f"未找到标签单元格: {label}")


def replace_section_cell(x, key, keep):
    """把表格3 中某小节的内容行，保留前 keep 段后替换为正文"""
    tbls = tag_spans(x, "w:tbl")
    t3s, t3e, _, _ = tbls[2]
    trs = tag_spans(x[t3s:t3e], "w:tr")
    prefix = HEAD_PREFIX[key]
    for i, (rs, re_, _, _) in enumerate(trs):
        seg = x[t3s + rs:t3s + re_]
        if p_text(seg).strip().startswith(prefix):
            crs, cre, _, _ = trs[i + 1]
            cbase = t3s + crs
            ccs = tag_spans(x[cbase:t3s + cre], "w:tc")
            gs, ge = cbase + ccs[0][2], cbase + ccs[0][3]
            cell = x[gs:ge]
            cps = tag_spans(cell, "w:p")
            head_end = cps[keep - 1][1] if len(cps) >= keep else 0
            return x[:gs] + cell[:head_end] + build_paras(cfg_holder[key]) + x[ge:], i + 1
    raise SystemExit(f"未找到小节: {key}")


cfg_holder = {}


def main():
    cfg = json.load(open(os.path.join(TMP, "katixiangqing.json"), encoding="utf-8"))
    cfg_holder.update(cfg)

    z = zipfile.ZipFile(TPL)
    names = z.namelist()
    data = {n: z.read(n) for n in names}
    z.close()
    x = data["word/document.xml"].decode("utf-8")

    # A. 封面标题
    i = find_p_index(x, lambda t: "课程实践开题报告" in t)
    assert i is not None, "未找到封面标题"
    x = rewrite_paragraph(x, i, cfg["cover_title"])
    print(f"[A] 封面标题 -> {cfg['cover_title']}")

    # B. 题目
    x = replace_label_cell(x, "题目", [cfg["题目"]], 0)
    print("[B] 题目已填入")

    # C. 封面字段（一律保留模板原值，由本人自行填写）
    for label, key in [("组长", "组长"), ("组员", "组员"), ("学院", "学院"),
                       ("专业", "专业"), ("指导教师", "指导教师")]:
        val = (cfg.get(key) or "").strip()
        if not val or val == "__KEEP__":
            print(f"[C] {label}: 保留模板原值（不改动）")
            continue
        x = replace_label_cell(x, label, [val], 1)
        print(f"[C] {label} -> {val}")

    # D. 日期
    di = find_p_index(x, lambda t: ("年" in t and "月" in t and "日" in t
                                    and re.search(r"20\d{2}", t) and len(t.strip()) < 30))
    assert di is not None, "未找到日期段落"
    x = rewrite_paragraph(x, di, "   " + cfg["日期"])
    print(f"[D] 日期 -> {cfg['日期']}")

    # E. 正文三部分（从后往前，避免行号漂移）
    for key, keep in [("s3_body", 2), ("s2_body", 1), ("s1_body", 3)]:
        x, row = replace_section_cell(x, key, keep)
        print(f"[E] {key}: 第 {row + 1} 行 保留前 {keep} 段 + 新增 {len(cfg[key])} 段")

    # F. 说明段
    tbls = tag_spans(x, "w:tbl")
    t3e = tbls[2][1]
    x = x[:t3e] + new_para(cfg["note"], indent=True) + x[t3e:]

    data["word/document.xml"] = x.encode("utf-8")

    # H. 若正文插入了流程图图片，从已有报告复制图片文件并补上关系项
    rid = _fig_cache.get("rid")
    if rid:
        import zipfile as _zf
        zs = _zf.ZipFile(FIGURE_SRC)
        try:
            rels_src = zs.read("word/_rels/document.xml.rels").decode("utf-8")
            m = re.search(rf'<Relationship Id="{rid}"[^>]*Target="([^"]+)"', rels_src)
            if m:
                target = m.group(1)
                data["word/" + target] = zs.read("word/" + target)
                if "word/" + target not in names:
                    names = names + ["word/" + target]
                key = "word/_rels/document.xml.rels"
                rels_new = data[key].decode("utf-8")
                if f'Id="{rid}"' not in rels_new:
                    rel_xml = (f'<Relationship Id="{rid}" '
                               f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                               f'Target="{target}"/>')
                    rels_new = rels_new.replace("</Relationships>", rel_xml + "</Relationships>")
                    data[key] = rels_new.encode("utf-8")
                print(f"[H] 已复制流程图图片 {target}（关系 {rid}）")
        finally:
            zs.close()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zo:
        for n in names:
            zo.writestr(n, data[n])
    print("[生成]", OUT, os.path.getsize(OUT), "字节")


if __name__ == "__main__":
    main()
