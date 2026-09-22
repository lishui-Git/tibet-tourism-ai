# -*- coding: utf-8 -*-
"""补充分析：IP字段形态、来源口径×字段、评分×来源、时间×来源"""
import csv, sys, collections, os, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
csv.field_size_limit(10 ** 9)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FINAL = os.path.join(ROOT, "旅游评论数据集_最终版.csv")
SRC = os.path.join(ROOT, "景点来源标注.csv")

src_map = {}
with open(SRC, encoding="utf-8-sig", newline="") as f:
    for x in csv.DictReader(f):
        src_map[x["景点名称"]] = x["来源口径"]

ip_raw = collections.Counter()
ip_by_src = collections.defaultdict(collections.Counter)
ip_by_year = collections.defaultdict(collections.Counter)
rating_by_src = collections.defaultdict(collections.Counter)
year_by_src = collections.defaultdict(collections.Counter)
len_by_src = collections.defaultdict(list)
like_by_src = collections.defaultdict(list)
img_by_src = collections.defaultdict(list)
anon_by_src = collections.Counter()
rating_by_year = collections.defaultdict(collections.Counter)

DATE = re.compile(r"^(\d{4})")
with open(FINAL, encoding="utf-8-sig", newline="") as f:
    for row in csv.DictReader(f):
        s = src_map.get(row["景点名称"], "?")
        ip = row["IP归属地"]
        ip_raw[repr(ip)] += 1
        ip_by_src[s][ip.strip() or "(空)"] += 1
        y = DATE.match(row["发布时间"])
        y = y.group(1) if y else "?"
        ip_by_year[y][ip.strip() or "(空)"] += 1
        rating_by_src[s][row["评分"].strip() or "(空)"] += 1
        rating_by_year[y][row["评分"].strip() or "(空)"] += 1
        year_by_src[s][y] += 1
        len_by_src[s].append(len(row["评论内容"] or ""))
        try: like_by_src[s].append(int(float(row["点赞数"] or 0)))
        except Exception: like_by_src[s].append(0)
        try: img_by_src[s].append(int(float(row["图片数"] or 0)))
        except Exception: img_by_src[s].append(0)
        if row["用户昵称"] == "匿名用户":
            anon_by_src[s] += 1

print("## A. IP归属地唯一值（共%d种）" % len(ip_raw))
for k, v in ip_raw.most_common(70):
    print("   ", k, v)

print("\n## B. 来源口径 × IP(空/未知) 占比")
for s in ip_by_src:
    tot = sum(ip_by_src[s].values())
    blank = ip_by_src[s].get("(空)", 0) + ip_by_src[s].get("未知", 0)
    print(f"   {s}: 总{tot} 空/未知{blank} ({blank/tot*100:.1f}%)  其他Top5={ip_by_src[s].most_common(6)}")

print("\n## C. 来源口径 × 年份")
for s in year_by_src:
    print(f"   {s}: {dict(sorted(year_by_src[s].items()))}")

print("\n## D. 来源口径 × 评分")
for s in rating_by_src:
    tot = sum(rating_by_src[s].values())
    print(f"   {s}: " + " ".join(f"{k}:{v}({v/tot*100:.1f}%)" for k, v in sorted(rating_by_src[s].items())))

print("\n## E. 来源口径 × 评论长度/点赞/图片")
for s in len_by_src:
    L = sorted(len_by_src[s]); lk = like_by_src[s]; im = img_by_src[s]
    print(f"   {s}: n={len(L)} 长度中位{ L[len(L)//2] } 均值{sum(L)/len(L):.1f} | 点赞非零{sum(1 for x in lk if x>0)/len(lk)*100:.1f}% | 图片非零{sum(1 for x in im if x>0)/len(im)*100:.1f}% | 匿名用户{anon_by_src[s]}")

print("\n## F. 年份 × 评分5星占比")
for y in sorted(rating_by_year):
    tot = sum(rating_by_year[y].values())
    five = rating_by_year[y].get("5", 0)
    one = rating_by_year[y].get("1", 0)
    print(f"   {y}: n={tot} 5星{five/tot*100:.1f}% 1星{one/tot*100:.1f}%")

print("\n## G. IP '未知' 的年份分布")
for y in sorted(ip_by_year):
    tot = sum(ip_by_year[y].values())
    unk = ip_by_year[y].get("未知", 0) + ip_by_year[y].get("(空)", 0)
    print(f"   {y}: {unk}/{tot} = {unk/tot*100:.1f}%")
