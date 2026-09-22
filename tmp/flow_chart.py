# -*- coding: utf-8 -*-
"""生成 Word 矢量流程图（wps 画布 + 圆角矩形 + 直线箭头连接符）"""

NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
      'xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"')

# 每种节点类型：(填充色, 线框色, 字色)
STYLE = {
    "data": ("E8EEF7", "4472C4", "1F3864"),
    "proc": ("FFFFFF", "8496B0", "333333"),
    "store": ("E2EFDA", "548235", "375623"),
    "deep": ("FFF2CC", "BF8F00", "7F6000"),
    "note": ("F2F2F2", "BFBFBF", "595959"),
}

COL_X = {0: 25, 1: 25, 2: 345}
COL_W = {0: 280, 1: 280, 2: 240}
ROW_H = 42
ROW_Y0 = 8
GAP = 12


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _shape_xml(sid, name, x, y, cx, cy, text, kind, round_rect=True, sz=900, bold=True):
    fill, line, fg = STYLE[kind]
    prst = "roundRect" if round_rect else "rect"
    adj = ('<a:gd name="adj" fmla="val 8000"/>'
           '<a:gd name="adj2" fmla="val 4000"/>') if round_rect else ""
    paras = []
    for i, ln in enumerate(text.split("\n")):
        b = "<a:b/>" if (bold and i == 0) else ("<a:b/>" if bold else "")
        paras.append(
            f'<a:p><a:pPr algn="ctr"/><a:r><a:rPr lang="zh-CN" altLang="en-US" sz="{sz}" b="0">'
            f'<a:solidFill><a:srgbClr val="{fg}"/></a:solidFill>'
            f'<a:latin typeface="宋体"/><a:ea typeface="宋体"/></a:rPr>'
            f'<a:t>{esc(ln)}</a:t></a:r></a:p>')
    body = "".join(paras)
    return (
        f'<wps:wsp><wps:cNvPr id="{sid}" name="{name}"/>'
        f'<wps:cNvSpPr txBox="0"/><wps:spPr>'
        f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        f'<a:prstGeom prst="{prst}"><a:avLst>{adj}</a:avLst></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>'
        f'<a:ln w="9525"><a:solidFill><a:srgbClr val="{line}"/></a:solidFill></a:ln>'
        f'</wps:spPr><wps:txbx><w:txbxContent>'
        f'<w:p><w:pPr><w:jc w:val="center"/><w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
        f'<w:r><w:rPr><w:rFonts w:ascii="宋体" w:eastAsia="宋体" w:hAnsi="宋体"/><w:sz w:val="18"/></w:rPr>'
        f'<w:t>{esc(text.splitlines()[0] if text else "")}</w:t></w:r></w:p></w:txbxContent></wps:txbx>'
        f'<wps:bodyPr rot="0" vert="horz" wrap="square" lIns="36000" tIns="18000" rIns="36000" bIns="18000" '
        f'anchor="ctr" anchorCtr="0"><a:noAutofit/></wps:bodyPr></wps:wsp>')


def _line_xml(sid, x1, y1, x2, y2, arrow=True, dashed=False):
    tail = ('<a:tailEnd type="triangle" w="med" len="med"/>' if arrow else "")
    dash = '<a:prstDash val="dash"/>' if dashed else ""
    return (
        f'<wps:wsp><wps:cNvPr id="{sid}" name="连接符 {sid}"/>'
        f'<wps:cNvSpPr><a:spLocks noChangeShapeType="1"/></wps:cNvSpPr><wps:spPr>'
        f'<a:xfrm><a:off x="{min(x1, x2)}" y="{min(y1, y2)}"/>'
        f'<a:ext cx="{abs(x2 - x1)}" cy="{abs(y2 - y1)}"/></a:xfrm>'
        f'<a:prstGeom prst="line"><a:avLst/></a:prstGeom>'
        f'<a:ln w="12700"><a:solidFill><a:srgbClr val="595959"/></a:solidFill>{dash}{tail}</a:ln>'
        f'</wps:spPr><wps:style><a:lnRef idx="2"><a:scrgbClr r="0" g="0" b="0"/></a:lnRef>'
        f'<a:fillRef idx="0"><a:scrgbClr r="0" g="0" b="0"/></a:fillRef>'
        f'<a:effectRef idx="0"><a:scrgbClr r="0" g="0" b="0"/></a:effectRef>'
        f'<a:fontRef idx="minor"><a:schemeClr val="tx1"/></a:fontRef></wps:style>'
        f'<wps:bodyPr/></wps:wsp>')


def build_flow(flow, start_id=1000):
    """返回 (画布XML, 需要的高度pt)"""
    nodes = {n["id"]: n for n in flow["nodes"]}

    def geom(n):
        col, row = n["col"], n["row"]
        x = COL_X[col]
        w = COL_W[col]
        y = ROW_Y0 + row * (ROW_H + GAP)
        h = ROW_H if "\n" not in n["text"] else ROW_H + 16
        return x, y, w, h

    # 计算实际高度
    max_bottom = 0
    for n in flow["nodes"]:
        _, y, _, h = geom(n)
        max_bottom = max(max_bottom, y + h)
    canvas_w, canvas_h = 600, max_bottom + 14

    shapes = []
    sid = start_id
    for n in flow["nodes"]:
        x, y, w, h = geom(n)
        if n["kind"] == "note":
            shapes.append(_shape_xml(sid, n["id"], x, y, w, h, n["text"], "note",
                                     round_rect=False, sz=850, bold=False))
        else:
            shapes.append(_shape_xml(sid, n["id"], x, y, w, h, n["text"], n["kind"]))
        sid += 1
        n["_geom"] = (x, y, w, h)

    for e in flow["edges"]:
        a, b = nodes[e["from"]], nodes[e["to"]]
        ax, ay, aw, ah = a["_geom"]
        bx, by, bw, bh = b["_geom"]
        if abs(ax - bx) < 1:                      # 同列：竖直线
            x1, y1 = ax + aw // 2, ay + ah
            x2, y2 = bx + bw // 2, by
        else:                                      # 跨列
            if bx > ax:
                x1, y1 = ax + aw, ay + ah // 2
                x2, y2 = bx, by + bh // 2
            else:
                x1, y1 = ax, ay + ah // 2
                x2, y2 = bx + bw, by + bh // 2
        shapes.append(_line_xml(sid, x1, y1, x2, y2))
        sid += 1

    canvas = (
        f'<w:r><w:drawing><wp:inline xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        f'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
        f'xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup" '
        f'distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{canvas_w * 12700}" cy="{canvas_h * 12700}"/>'
        f'<wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{start_id}" name="系统总体流程图"/>'
        f'<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        f'<a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup">'
        f'<wpg:wgp xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup" '
        f'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        f'<wpg:cNvGrpSpPr/><wpg:grpSpPr><a:xfrm>'
        f'<a:off x="0" y="0"/><a:ext cx="{canvas_w * 12700}" cy="{canvas_h * 12700}"/>'
        f'<a:chOff x="0" y="0"/><a:chExt cx="{canvas_w * 12700}" cy="{canvas_h * 12700}"/>'
        f'</a:xfrm></wpg:grpSpPr>{"".join(shapes)}</wpg:wgp>'
        f'</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'
    )
    return canvas, canvas_h
