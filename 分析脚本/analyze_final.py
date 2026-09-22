# -*- coding: utf-8 -*-
"""
项目现状分析脚本（只读，不修改任何数据文件）
输入：旅游评论数据集_最终版.csv / 景点来源标注.csv / 内容重复清单.csv
输出：控制台统计结果（可重定向保存）
"""
import csv, sys, collections, re, json, os

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

csv.field_size_limit(10 ** 9)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FINAL = os.path.join(ROOT, "旅游评论数据集_最终版.csv")
SRC = os.path.join(ROOT, "景点来源标注.csv")
DUP = os.path.join(ROOT, "内容重复清单.csv")

def open_csv(path):
    return open(path, "r", encoding="utf-8-sig", newline="")

out = []
def p(*a):
    s = " ".join(str(x) for x in a)
    out.append(s)
    print(s)

# ---------- 1. 字段结构 ----------
with open_csv(FINAL) as f:
    r = csv.reader(f)
    header = next(r)
    sample = [next(r) for _ in range(3)]

p("## 1. 字段结构")
p("列数:", len(header))
for i, h in enumerate(header):
    vals = [s[i] for s in sample]
    p(f"  {i+1:2d}. {h}  | 示例: {vals[0][:40]!r}")

# ---------- 2. 全量扫描 ----------
n = 0
ids = set()
spot_counter = collections.Counter()
year_counter = collections.Counter()
ip_counter = collections.Counter()
rating_counter = collections.Counter()
ratingdesc_counter = collections.Counter()
nick_counter = collections.Counter()
likes = []
imgnum = []
content_len = []
empty = collections.Counter()
spot_addr = {}
spot_intro = set()
spot_open = set()
spot_phone = set()
img_has = 0
len_buckets = collections.Counter()
like_nonzero = 0
img_nonzero = 0
dup_ids = 0
bad_date = 0
hotel_like_keywords = 0
sample_long = None
max_len = 0

DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")

with open_csv(FINAL) as f:
    r = csv.DictReader(f)
    for row in r:
        n += 1
        rid = row["评论编号"]
        if rid in ids:
            dup_ids += 1
        ids.add(rid)
        spot = row["景点名称"]
        spot_counter[spot] += 1
        spot_addr.setdefault(spot, row["地址"])
        if row["景点介绍"].strip():
            spot_intro.add(spot)
        if row["开放时间"].strip():
            spot_open.add(spot)
        if row["官方电话"].strip():
            spot_phone.add(spot)
        for c in header:
            if not (row.get(c) or "").strip():
                empty[c] += 1
        d = row["发布时间"]
        m = DATE_RE.match(d)
        if m:
            year_counter[m.group(1)] += 1
        else:
            bad_date += 1
        ip_counter[row["IP归属地"].strip()] += 1
        rating_counter[row["评分"].strip()] += 1
        ratingdesc_counter[row["评分描述"].strip()] += 1
        nick_counter[row["用户昵称"]] += 1
        try:
            lk = int(float(row["点赞数"] or 0))
        except Exception:
            lk = 0
        likes.append(lk)
        if lk > 0:
            like_nonzero += 1
        try:
            im = int(float(row["图片数"] or 0))
        except Exception:
            im = 0
        imgnum.append(im)
        if im > 0:
            img_nonzero += 1
        if row["图片URL"].strip():
            img_has += 1
        cl = len(row["评论内容"] or "")
        content_len.append(cl)
        if cl > max_len:
            max_len = cl
            sample_long = (spot, rid, row["评论内容"][:60])
        if cl == 0:
            len_buckets["0"] += 1
        elif cl <= 10:
            len_buckets["1-10"] += 1
        elif cl <= 30:
            len_buckets["11-30"] += 1
        elif cl <= 60:
            len_buckets["31-60"] += 1
        elif cl <= 100:
            len_buckets["61-100"] += 1
        elif cl <= 200:
            len_buckets["101-200"] += 1
        else:
            len_buckets["200+"] += 1

def q(lst, *ps):
    s = sorted(lst)
    return {f"p{int(x*100)}": s[min(len(s) - 1, int(len(s) * x))] for x in ps}

p("\n## 2. 规模")
p("总行数(读取):", n)
p("评论编号去重后:", len(ids), " 重复ID行数:", dup_ids)
p("景点数:", len(spot_counter))
p("用户昵称数(去重):", len(nick_counter))
p("IP归属地去重数:", len(ip_counter))
p("日期格式异常:", bad_date)

p("\n## 3. 评分分布")
tot_r = sum(v for k, v in rating_counter.items() if k)
for k in sorted(rating_counter, key=lambda x: (x == "", x)):
    v = rating_counter[k]
    p(f"  {k or '(空)'}: {v}  {v/n*100:.2f}%")
p("评分描述:", dict(ratingdesc_counter))

p("\n## 4. 互动与图片")
p("点赞数: 非零 %d (%.2f%%)  最大 %d  均值 %.3f  分位 %s" % (
    like_nonzero, like_nonzero/n*100, max(likes) if likes else 0,
    sum(likes)/n if n else 0, q(likes, .5, .9, .99)))
p("图片数: 非零 %d (%.2f%%)  最大 %d  均值 %.3f  分位 %s" % (
    img_nonzero, img_nonzero/n*100, max(imgnum) if imgnum else 0,
    sum(imgnum)/n if n else 0, q(imgnum, .5, .9, .99)))
p("有图片URL字段值:", img_has, "%.2f%%" % (img_has/n*100))

p("\n## 5. 评论正文长度")
p("空值:", len_buckets["0"])
for k in ["1-10", "11-30", "31-60", "61-100", "101-200", "200+"]:
    p(f"  {k} 字: {len_buckets[k]}  {len_buckets[k]/n*100:.2f}%")
L = sorted(content_len)
p("长度分位: p10=%d p25=%d p50=%d p75=%d p90=%d p99=%d max=%d 均值=%.1f" % (
    L[int(.1*n)], L[int(.25*n)], L[int(.5*n)], L[int(.75*n)], L[int(.9*n)], L[int(.99*n)], L[-1], sum(L)/n))
p("最长示例:", sample_long)

p("\n## 6. 年份分布")
for k in sorted(year_counter):
    p(f"  {k}: {year_counter[k]}")

p("\n## 7. 景点分布")
cnts = sorted(spot_counter.values(), reverse=True)
p("最多:", cnts[0], " 最少:", cnts[-1], " 中位数:", sorted(cnts)[len(cnts)//2])
buckets = {"≥1000": 0, "301-999": 0, "101-300": 0, "31-100": 0, "11-30": 0, "1-10": 0}
sums = {k: 0 for k in buckets}
for v in cnts:
    if v >= 1000: k = "≥1000"
    elif v >= 301: k = "301-999"
    elif v >= 101: k = "101-300"
    elif v >= 31: k = "31-100"
    elif v >= 11: k = "11-30"
    else: k = "1-10"
    buckets[k] += 1; sums[k] += v
for k in buckets:
    p(f"  {k} 条: {buckets[k]} 个景点, 合计 {sums[k]} 条 ({sums[k]/n*100:.1f}%)")
p("Top30 景点:")
for s, c in spot_counter.most_common(30):
    p(f"  {s}: {c}")

p("\n## 8. 景点级辅助字段覆盖")
p("有地址的景点:", sum(1 for v in spot_addr.values() if v.strip()), "/", len(spot_counter))
p("有景点介绍的景点:", len(spot_intro), "/", len(spot_counter))
p("有开放时间的景点:", len(spot_open), "/", len(spot_counter))
p("有官方电话的景点:", len(spot_phone), "/", len(spot_counter))

p("\n## 9. IP 归属地 Top20")
for k, v in ip_counter.most_common(20):
    p(f"  {k or '(空)'}: {v} ({v/n*100:.2f}%)")
p("境外/海外IP样本:", [k for k, _ in ip_counter.most_common() if k and not re.match(r"^[\u4e00-\u9fa5]{2,}", k)][:20])

p("\n## 10. 用户活跃度")
nc = sorted(nick_counter.values(), reverse=True)
p("评论数>1 的用户数:", sum(1 for v in nc if v > 1), " 占比 %.2f%%" % (sum(1 for v in nc if v > 1)/len(nc)*100))
p("Top10 高产用户:", nick_counter.most_common(10))
p("单用户最多评论:", nc[0])
one = sum(1 for v in nc if v == 1)
p("只发1条的用户:", one, "%.2f%%" % (one/len(nc)*100))

# ---------- 11. 来源标注 ----------
p("\n## 11. 景点来源标注.csv")
with open_csv(SRC) as f:
    rows = list(csv.DictReader(f))
p("行数:", len(rows), "字段:", list(rows[0].keys()))
src_cnt = collections.Counter()
src_spots = collections.Counter()
src_rows = collections.Counter()
for x in rows:
    src_cnt[x["来源口径"]] += int(x["条数"])
    src_spots[x["来源口径"]] += 1
    src_rows[x["来源口径"]] += 1
for k in src_cnt:
    p(f"  {k}: {src_spots[k]} 个景点, {src_cnt[k]} 条")
p("标注景点数与数据集景点数是否一致:", len(rows) == len(spot_counter))

# ---------- 12. 重复清单 ----------
p("\n## 12. 内容重复清单.csv")
with open_csv(DUP) as f:
    drows = list(csv.DictReader(f))
p("字段:", list(drows[0].keys()))
p("重复组数:", len(drows))
tot = sum(int(x["重复条数"]) for x in drows)
p("涉及评论条数:", tot, "%.2f%%" % (tot/n*100))
cross = sum(1 for x in drows if int(x["涉及景点数"]) > 1)
p("跨景点重复组:", cross)
big = sorted(drows, key=lambda x: -int(x["重复条数"]))[:10]
p("最大重复组:")
for x in big:
    p(f"  {x['重复条数']}条/{x['涉及景点数']}景点: {x['评论内容'][:30]!r}")
short = sum(1 for x in drows if len(x["评论内容"]) <= 10)
p("重复内容 ≤10 字的组数:", short, "/", len(drows))

with open(os.path.join(ROOT, "分析脚本", "现状分析_统计输出.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("\n[已保存] 分析脚本/现状分析_统计输出.txt")
