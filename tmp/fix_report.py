# -*- coding: utf-8 -*-
"""
只做两件事，其余内容一律不动：
 1) 修正流程图图片的关系项：把 o:title="系统总体流程图" 所在段落引用的 rId 指向真正的流程图图片；
 2) 只替换"进度实施计划"一节（从 二、进度实施计划 到 三、预期提交的实训资料 之前）。
不新增图片、不改其他段落、不动封面。
"""
import sys, os, json, zipfile, re, shutil
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = r"E:\tibet-tourism-ai"
SRC = os.path.join(ROOT, "tmp", "开题报告_合并版预览.docx")   # 含本人流程图图片的版本
OUT = os.environ.get("FIX_OUT") or os.path.join(ROOT, "开题", "NIIT-GUET-2023-Class-2316030201-开题报告.docx")
CFG = json.load(open(os.path.join(ROOT, "tmp", "katixiangqing.json"), encoding="utf-8"))

print("=== 1. 检查源文件图片与关系 ===")
zs = zipfile.ZipFile(SRC)
names = zs.namelist()
data = {n: zs.read(n) for n in names}
zs.close()

x = data["word/document.xml"].decode("utf-8")
rels = data["word/_rels/document.xml.rels"].decode("utf-8")

# 找出流程图段落引用的 rId，以及各 rId 指向的 media
m_img = None
for m in re.finditer(r"<w:pict>", x):
    seg = x[m.start():m.start() + 800]
    if "系统总体流程图" in seg:
        m_img = re.search(r'r:id="(rId\d+)"', seg)
        break
assert m_img, "未找到流程图图片段落"
cur_rid = m_img.group(1)
print(f"  流程图段落当前引用关系: {cur_rid}")

rid2target = dict(re.findall(r'<Relationship Id="(rId\d+)"[^>]*Target="([^"]+)"', rels))
print("  当前关系映射:")
for k, v in rid2target.items():
    if "media" in v:
        print(f"    {k} -> {v}")

# 真正的流程图图片：media 里除封面校徽外的那张（体积最大）
media = [n for n in names if n.startswith("word/media/")]
sizes = {n: len(data[n]) for n in media}
flow_media = max(sizes, key=lambda n: sizes[n])
logo_media = min(sizes, key=lambda n: sizes[n])
print(f"  判定：流程图={flow_media}({sizes[flow_media]})  校徽={logo_media}({sizes[logo_media]})")

target = flow_media.replace("word/", "")
need_fix = (rid2target.get(cur_rid) != target)
print(f"  是否需要修正关系: {need_fix}")

if need_fix:
    # 找目标图片已存在的关系；没有则复用当前 rId，把它指向流程图图片
    rid_for_flow = next((k for k, v in rid2target.items() if v == target), None)
    if rid_for_flow is None:
        rid_for_flow = cur_rid
        rels = re.sub(rf'(<Relationship Id="{cur_rid}"[^>]*Target=")[^"]+(")',
                      rf'\g<1>{target}\g<2>', rels)
        print(f"  已把 {cur_rid} 指向 {target}")
    else:
        x = re.sub(rf'(<w:pict>(?:(?!</w:pict>).)*?系统总体流程图(?:(?!</w:pict>).)*?r:id="){cur_rid}(")',
                   rf'\g<1>{rid_for_flow}\g<2>', x, flags=re.S)
        print(f"  已把流程图段落改引 {rid_for_flow}（原本就指向 {target}）")
    data["word/_rels/document.xml.rels"] = rels.encode("utf-8")

# 复查
chk = data["word/document.xml"].decode("utf-8")
mm = None
for m in re.finditer(r"<w:pict>", chk):
    seg = chk[m.start():m.start() + 800]
    if "系统总体流程图" in seg:
        mm = re.search(r'r:id="(rId\d+)"', seg)
        break
rid2target2 = dict(re.findall(r'<Relationship Id="(rId\d+)"[^>]*Target="([^"]+)"',
                              data["word/_rels/document.xml.rels"].decode("utf-8")))
print(f"  修正后：段落 {mm.group(1)} -> {rid2target2.get(mm.group(1))}")

print("\n=== 2. 替换进度实施计划一节 ===")


def tag_spans(s, tag):
    res = []
    for m in re.finditer(rf"<{tag}(?: [^>]*)?>", s):
        e = s.find(f"</{tag}>", m.end())
        if e == -1:
            continue
        res.append((m.start(), e + len(tag) + 3, m.end(), e))
    return res


def p_text(seg):
    return "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", seg, re.S))


RPR = ('<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体" '
       'w:cs="Times New Roman"/><w:sz w:val="24"/><w:szCs w:val="24"/>')


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def new_para(text, indent=True, bold=False):
    ind = '<w:ind w:firstLineChars="200" w:firstLine="480"/>' if indent else ""
    b = "<w:b/>" if bold else ""
    req = f'<w:rPr>{b}{RPR}</w:rPr>'
    return (f'<w:p><w:pPr><w:pStyle w:val="a5"/><w:spacing w:line="400" w:lineRule="exact"/>'
            f'{ind}{req}</w:pPr><w:r>{req}<w:t xml:space="preserve">{esc(text)}</w:t></w:r></w:p>')


# 取出 s3_body 中"进度实施计划"之后的段落（到 三、预期提交 之前）
s3 = CFG["s3_body"]
i_plan = next(i for i, s in enumerate(s3) if s.startswith("二、进度实施计划"))
i_end = next(i for i, s in enumerate(s3) if s.startswith("三、预期提交"))
plan_lines = s3[i_plan + 1:i_end]
print(f"  新进度段落数: {len(plan_lines)}")

ps = tag_spans(chk, "w:p")
idx_start = None
idx_end = None
for i, (s, e, _, _) in enumerate(ps):
    t = p_text(chk[s:e]).strip()
    if t.startswith("二、进度实施计划"):
        idx_start = i
    if t.startswith("三、预期提交的实训资料"):
        idx_end = i
        break
assert idx_start is not None and idx_end is not None, f"定位失败: {idx_start}, {idx_end}"
print(f"  旧进度段落范围: P{idx_start} ~ P{idx_end - 1}（共 {idx_end - idx_start} 段）")

old_txt = "".join(p_text(chk[ps[i][0]:ps[i][1]]) for i in range(idx_start, min(idx_start + 3, idx_end)))
print(f"  旧内容样例: {old_txt[:80]!r}")

repl = "".join(new_para(t, indent=not bool(re.match(r"^[一二三四五六七八九]、", t)),
                        bold=bool(re.match(r"^[一二三四五六七八九]、", t))) for t in plan_lines)
new_x = chk[:ps[idx_start][0]] + repl + chk[ps[idx_end][0]:]
data["word/document.xml"] = new_x.encode("utf-8")

# 写入
shutil.copy2(SRC, OUT)  # 保留包内其他部件
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zo:
    for n in names:
        zo.writestr(n, data[n])
print("\n[生成]", OUT, os.path.getsize(OUT), "字节")
