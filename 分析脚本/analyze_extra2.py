# -*- coding: utf-8 -*-
"""补充分析2：景点级评分异常、IP有效口径、可用于建模的样本量估计"""
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

spot = collections.defaultdict(lambda: {"n": 0, "r": collections.Counter(), "len": [], "ip": collections.Counter()})
addr_prefix = collections.Counter()
mod = {}
with open(FINAL, encoding="utf-8-sig", newline="") as f:
    for row in csv.DictReader(f):
        s = row["景点名称"]
        d = spot[s]
        d["n"] += 1
        d["r"][row["评分"].strip() or "空"] += 1
        d["len"].append(len(row["评论内容"] or ""))
        d["ip"][row["IP归属地"]] += 1
        if row["发布时间"] >= "2022-08-01":
            mod[row["发布时间"][:7]] = mod.get(row["发布时间"][:7], 0) + 1
            addr_prefix[(s, "2022-08后")] = 1

print("## H. 有意义建模样本量（按景点条数门槛）")
for thr in [1, 3, 5, 10, 30, 50, 100, 300, 1000]:
    spots = [s for s, d in spot.items() if d["n"] >= thr]
    tot = sum(spot[s]["n"] for s in spots)
    print(f"   ≥{thr} 条: {len(spots)} 个景点, {tot} 条 ({tot/59033*100:.1f}%)")

print("\n## I. 景点级评分极端值（n≥100 的 57 个景点中）")
big = [(s, d) for s, d in spot.items() if d["n"] >= 100]
rows = []
for s, d in big:
    n = d["n"]
    five = d["r"].get("5", 0) / n * 100
    low = (d["r"].get("1", 0) + d["r"].get("2", 0)) / n * 100
    avg = sum(int(k) * v for k, v in d["r"].items() if k.isdigit()) / max(1, sum(v for k, v in d["r"].items() if k.isdigit()))
    rows.append((s, n, five, low, avg, src_map.get(s, "?")))
rows.sort(key=lambda x: -x[2])
print("   5星占比最高 Top8:")
for r in rows[:8]:
    print(f"     {r[0]}({r[5]}) n={r[1]} 5星{r[2]:.1f}% 1-2星{r[3]:.1f}% 均分{r[4]:.2f}")
print("   5星占比最低 Top8:")
for r in rows[-8:]:
    print(f"     {r[0]}({r[5]}) n={r[1]} 5星{r[2]:.1f}% 1-2星{r[3]:.1f}% 均分{r[4]:.2f}")
print("   均分最高/最低:")
rows2 = sorted(rows, key=lambda x: -x[4])
print(f"     最高 {rows2[0][0]} {rows2[0][4]:.2f} | 最低 {rows2[-1][0]} {rows2[-1][4]:.2f}")
cv = [r[2] for r in rows]
print(f"   5星占比分布: min={min(cv):.1f}% max={max(cv):.1f}% 中位={sorted(cv)[len(cv)//2]:.1f}%")

print("\n## J. 低分评论可得性（1~3 星）")
tot_low = 0
low_spot = collections.Counter()
with open(FINAL, encoding="utf-8-sig", newline="") as f:
    for row in csv.DictReader(f):
        if row["评分"].strip() in ("1", "2", "3"):
            tot_low += 1
            low_spot[row["景点名称"]] += 1
print("   1~3星合计:", tot_low, f"({tot_low/59033*100:.2f}%)")
print("   含≥30条低分评论的景点数:", sum(1 for v in low_spot.values() if v >= 30))
print("   含≥10条低分评论的景点数:", sum(1 for v in low_spot.values() if v >= 10))
print("   低分评论最多的10个景点:", low_spot.most_common(10))
print("   零低分评论的景点数:", sum(1 for s in spot if low_spot.get(s, 0) == 0), "/", len(spot))

print("\n## K. IP 有效口径（2022-08 之后）")
MON = re.compile(r"^(\d{4})-(\d{2})")
ip_after = collections.Counter()
n_after = 0
with open(FINAL, encoding="utf-8-sig", newline="") as f:
    for row in csv.DictReader(f):
        if row["发布时间"] >= "2022-08-01":
            n_after += 1
            ip_after[row["IP归属地"]] += 1
print("   2022-08-01 之后条数:", n_after, f"({n_after/59033*100:.1f}%)")
print("   其中仍为未知:", ip_after.get("未知", 0), f"({ip_after.get('未知',0)/n_after*100:.1f}%)")
print("   Top15 客源地:")
for k, v in ip_after.most_common(15):
    print(f"     {k}: {v} ({v/n_after*100:.2f}%)")

print("\n## L. 进藏沿线各条线路景点识别（按地址省份）")
prov = collections.Counter()
with open(FINAL, encoding="utf-8-sig", newline="") as f:
    seen = set()
    for row in csv.DictReader(f):
        s = row["景点名称"]
        if s in seen: continue
        seen.add(s)
        a = row["地址"]
        m = re.match(r"^(.{2,3}?(?:省|自治区|市))", a)
        prov[m.group(1) if m else a[:6]] += 1
for k, v in prov.most_common(20):
    print(f"   {k}: {v} 个景点")
