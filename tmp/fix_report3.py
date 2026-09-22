# -*- coding: utf-8 -*-
"""
最终修正：按每张图片的 o:title 精确分配关系，确保
  · 封面校徽  -> media/image1.jpeg
  · 系统总体流程图 -> media/image2.jpeg
其余内容一律不动；进度实施计划保持调用方（合并版）已有的新版内容。
"""
import sys, os, zipfile, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = r"E:\tibet-tourism-ai"
SRC = os.path.join(ROOT, "tmp", "开题报告_合并版预览.docx")
OUT = os.environ.get("FIX_OUT") or os.path.join(ROOT, "开题", "NIIT-GUET-2023-Class-2316030201-开题报告.docx")

z = zipfile.ZipFile(SRC)
names = z.namelist()
data = {n: z.read(n) for n in names}
z.close()

x = data["word/document.xml"].decode("utf-8")
rels = data["word/_rels/document.xml.rels"].decode("utf-8")

MEDIA = {t: len(data[t]) for t in names if t.startswith("word/media/")}
# 判定：校徽=较小的那张，流程图=较大的那张
logo_t = min(MEDIA, key=lambda k: MEDIA[k]).replace("word/", "")
flow_t = max(MEDIA, key=lambda k: MEDIA[k]).replace("word/", "")
print(f"校徽 = {logo_t} ({MEDIA['word/' + logo_t]})    流程图 = {flow_t} ({MEDIA['word/' + flow_t]})")

IMG_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

# 1) 为两张图各准备一个 rId（复用已有关系，缺失则新建，绝不重复）
existing = dict(re.findall(r'<Relationship Id="(rId\d+)"[^>]*Target="([^"]+)"', rels))
used = set(existing)
add = []


def rid_for(target):
    for k, v in existing.items():
        if v == target:
            return k
    i = 1
    while f"rId{i}" in used:
        i += 1
    rid = f"rId{i}"
    used.add(rid)
    existing[rid] = target
    add.append(f'<Relationship Id="{rid}" Type="{IMG_TYPE}" Target="{target}"/>')
    print(f"  新建关系 {rid} -> {target}")
    return rid


rid_logo = rid_for(logo_t)
rid_flow = rid_for(flow_t)
print(f"  分配：校徽 -> {rid_logo}    流程图 -> {rid_flow}")

# 去掉 rels 中指向 media 的重复关系（同一 Target 只保留一个）
seen_target = {}
dedup = []


def rel_repl(m):
    rid, typ, tgt = m.group(1), m.group(2), m.group(3)
    if "media/" in tgt:
        if tgt in seen_target:
            return ""          # 丢弃重复
        seen_target[tgt] = rid
    return m.group(0)


rels = re.sub(r'<Relationship Id="(rId\d+)" Type="([^"]+)" Target="(media/[^"]+)"/>', rel_repl, rels)
if add:
    rels = rels.replace("</Relationships>", "".join(add) + "</Relationships>")

# 2) 按 o:title 逐张重写引用
def fix_pict(m):
    seg = m.group(0)
    title = re.search(r'o:title="([^"]*)"', seg)
    if not title:
        return seg
    t = title.group(1)
    if "流程图" in t:
        want = rid_flow
    elif "校标" in t or "校徽" in t:
        want = rid_logo
    else:
        return seg
    seg2 = re.sub(r'r:id="rId\d+"', f'r:id="{want}"', seg)
    return seg2


before = x
x = re.sub(r"<v:imagedata[^>]*>", fix_pict, x)
print(f"  图片引用已重写: {before != x}")

data["word/document.xml"] = x.encode("utf-8")
data["word/_rels/document.xml.rels"] = rels.encode("utf-8")

# 3) 校验
r2t = dict(re.findall(r'<Relationship Id="(rId\d+)"[^>]*Target="([^"]+)"', rels))
print("\n=== 校验 ===")
for m in re.finditer(r"<v:imagedata[^>]*>", x):
    seg = m.group(0)
    title = re.search(r'o:title="([^"]*)"', seg).group(1)
    rid = re.search(r'r:id="(rId\d+)"', seg).group(1)
    tgt = r2t.get(rid)
    size = len(data["word/" + tgt]) if ("word/" + tgt) in data else "缺失"
    flag = "OK " if (("流程图" in title) == (tgt == flow_t)) and ("word/" + tgt) in data else "异常"
    print(f"  {flag} {title} -> {rid} -> {tgt} ({size} 字节)")
dups = [t for t in set(v for v in r2t.values() if "media" in v)
        if list(r2t.values()).count(t) > 1]
print(f"  重复 media 关系: {dups if dups else '无'}")

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zo:
    for n in names:
        zo.writestr(n, data[n])
print("\n[生成]", OUT, os.path.getsize(OUT), "字节")
