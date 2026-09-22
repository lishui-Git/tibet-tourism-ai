# -*- coding: utf-8 -*-
"""
在"合并版预览"（图片段落、进度计划均已正确）基础上，向上游修两处：
 1) 让封面校徽与系统总体流程图各自拥有正确的图片关系；
 2) 校验进度实施计划小标题与正文齐全。
做法：把缺失的图片关系补进 rels（校徽补一个新的 rId，流程图 rId6 指向 image2.jpeg），
     并确保 document.xml 中各段落引用到正确的 rId。
"""
import sys, os, json, zipfile, re
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

print("=== 原状 ===")
picts = []
for m in re.finditer(r"<w:pict>", x):
    e = x.find("</w:pict>", m.end())
    seg = x[m.start():e]
    alt = re.search(r'alt="([^"]*)"', seg)
    rid = re.search(r'r:id="(rId\d+)"', seg)
    picts.append((alt.group(1) if alt else "?", rid.group(1) if rid else None))
    print(f"  图片 alt={alt.group(1) if alt else '?'} rel={rid.group(1) if rid else '?'}")
print("  rels 中 media 关系:",
      dict(re.findall(r'<Relationship Id="(rId\d+)"[^>]*Target="(media/[^"]+)"', rels)))

# 现有 rId 集合
used = set(re.findall(r'Id="(rId\d+)"', rels))
def next_rid():
    i = 1
    while f"rId{i}" in used:
        i += 1
    used.add(f"rId{i}")
    return f"rId{i}"

IMG_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

# 需要确保存在的关系：image1.jpeg(校徽) 与 image2.jpeg(流程图)
have = {v: k for k, v in re.findall(r'<Relationship Id="(rId\d+)"[^>]*Target="(media/[^"]+)"', rels)}
add_rels = []
for target in ["media/image1.jpeg", "media/image2.jpeg"]:
    if target not in have:
        nr = next_rid()
        add_rels.append(f'<Relationship Id="{nr}" Type="{IMG_TYPE}" Target="{target}"/>')
        have[target] = nr
        print(f"  补关系: {nr} -> {target}")
if add_rels:
    rels = rels.replace("</Relationships>", "".join(add_rels) + "</Relationships>")

# 对每个图片段落，按 alt 指向正确的关系
new_x = x
for alt, rid in picts:
    if alt and "系统总体流程图" in alt:
        want = have["media/image2.jpeg"]
    elif alt and "校标" in alt:
        want = have["media/image1.jpeg"]
    else:
        continue
    if rid != want:
        # 只在该 pict 范围内替换 rId
        pat = re.compile(r"<w:pict>(?:(?!</w:pict>).)*?</w:pict>", re.S)
        def rep(mo, rid=rid, want=want, alt=alt):
            seg = mo.group(0)
            if alt[:6] in seg or alt in seg:
                return seg.replace(f'r:id="{rid}"', f'r:id="{want}"')
            return seg
        new_x = pat.sub(rep, new_x)
        print(f"  段落 {alt} 关系 {rid} -> {want}")

data["word/document.xml"] = new_x.encode("utf-8")
data["word/_rels/document.xml.rels"] = rels.encode("utf-8")

# 校验
final = data["word/document.xml"].decode("utf-8")
rels_f = data["word/_rels/document.xml.rels"].decode("utf-8")
r2t = dict(re.findall(r'<Relationship Id="(rId\d+)"[^>]*Target="([^"]+)"', rels_f))
print("\n=== 修正后 ===")
for m in re.finditer(r"<w:pict>", final):
    e = final.find("</w:pict>", m.end())
    seg = final[m.start():e]
    alt = re.search(r'alt="([^"]*)"', seg)
    rid = re.search(r'r:id="(rId\d+)"', seg)
    tgt = r2t.get(rid.group(1)) if rid else None
    size = len(data["word/" + tgt]) if tgt in data else 0
    print(f"  图片 {alt.group(1) if alt else '?'} -> {rid.group(1) if rid else '?'} -> {tgt} ({size} 字节)")
print(f"  进度计划小标题存在: {'二、进度实施计划' in final}")

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zo:
    for n in names:
        zo.writestr(n, data[n])
print("\n[生成]", OUT, os.path.getsize(OUT), "字节")
