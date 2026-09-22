# -*- coding: utf-8 -*-
"""
扩展西藏 POI 池（URL 直连版，可靠）
  · 从现有 388 清单的 URL 中提取所有地区 slug，并补充已知市级 slug
  · 逐个 slug 直连 https://you.ctrip.com/sight/{slug}/s0-p{n}.html 翻页
  · 收集 POI 的 数字ID / 名称 / 点评数，与现有清单按 ID 去重
  · 输出 data/out/新发现景点.csv（增量保存，可重复运行）
"""
import csv, re, time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "out"
OUT.mkdir(parents=True, exist_ok=True)

# 已知市级 slug（实测可用的）
EXTRA_SLUGS = ["lhasa36", "nyingchi126", "shigatse100"]

STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
"""

LIST_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('a[href*="/sight/"]').forEach(a => {
    const name = (a.innerText || '').trim();
    const href = a.href || '';
    if (!name || name.length > 40 || !/\.html/.test(href)) return;
    const m = href.match(/\/sight\/([^\/]+)\/(\d+)\.html/);
    if (!m) return;
    let card = a, txt = '';
    for (let i = 0; i < 6 && card; i++) {
      card = card.parentElement;
      if (card && /条点评/.test(card.innerText || '')) { txt = card.innerText || ''; break; }
    }
    const c = txt.match(/([\d,]+(?:\.\d+)?)\s*(万?)\s*条点评/);
    out.push({slug: m[1], id: m[2], name: name,
              cnt: c ? Math.round(parseFloat(c[1].replace(/,/g,'')) * (c[2] ? 10000 : 1)) : null});
  });
  return out;
}
"""


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    with open(OUT / "扩展日志.txt", "a", encoding="utf-8") as f:
        f.write(msg + "\n")


# ---------- 收集候选 slug ----------
known = {}
kp = OUT / "景点清单_携程.csv"
if kp.exists():
    for row in csv.DictReader(kp.open(encoding="utf-8-sig")):
        u = row.get("URL") or ""
        m = re.search(r"/sight/([^/]+)/(\d+)\.html", u)
        if m:
            known[m.group(2)] = row.get("景点名称")
slugs = set(EXTRA_SLUGS)
for src in ("景点清单_携程.csv", "新发现景点.csv"):
    p = OUT / src
    if not p.exists():
        continue
    for row in csv.DictReader(p.open(encoding="utf-8-sig")):
        m = re.search(r"/sight/([^/]+)/", row.get("URL") or "")
        if m and not m.group(1).isdigit():
            slugs.add(m.group(1))
cs = OUT / "候选slug.txt"
if cs.exists():
    for line in cs.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.isdigit():
            slugs.add(s)

# 已处理过的 slug 不再重复遍历
done_file = OUT / "已处理slug.txt"
done_slugs = set()
if done_file.exists():
    done_slugs = {x.strip() for x in done_file.read_text(encoding="utf-8").splitlines() if x.strip()}
todo_slugs = sorted(s for s in slugs if s and s != "100003" and not s.isdigit() and s not in done_slugs)
log(f"已知清单 ID {len(known)} 个；slug 总 {len(slugs)} 个；本次待遍历 {len(todo_slugs)} 个")

found = {}
for i, v in enumerate(known.items()):
    found[v[0]] = {"name": v[1], "slug": "", "cnt": None}


def save():
    fresh = {i: v for i, v in found.items() if i not in known}
    rows = sorted(fresh.items(), key=lambda kv: -(kv[1]["cnt"] or 0))
    with (OUT / "新发现景点.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["景点名称", "URL", "携程显示点评数"])
        for i, v in rows:
            w.writerow([v["name"], f'https://you.ctrip.com/sight/{v["slug"]}/{i}.html',
                        v["cnt"] if v["cnt"] is not None else ""])
    log(f"  >> 累计 POI {len(found)}，新发现 {len(fresh)} -> data/out/新发现景点.csv")
    return len(fresh)


with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        "_edge_profile_probe", channel="msedge", headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        locale="zh-CN", viewport={"width": 1440, "height": 950},
        ignore_default_args=["--enable-automation"])
    ctx.add_init_script(STEALTH)
    page = ctx.new_page()
    page.goto("https://www.ctrip.com/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3500)

    slug_list = todo_slugs
    for si, slug in enumerate(slug_list, 1):
        empty = 0
        for p in range(1, 26):
            url = f"https://you.ctrip.com/sight/{slug}/s0-p{p}.html"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1800)
                t = page.title() or ""
                if "验证" in t:
                    log(f"[{si}/{len(slug_list)}] {slug} 被风控，跳过")
                    break
                items = page.evaluate(LIST_JS)
            except Exception as e:
                log(f"[{si}/{len(slug_list)}] {slug} p{p} 失败 {str(e)[:50]}")
                break
            new = 0
            for it in items:
                if it["slug"] and it["slug"] not in slugs and not it["slug"].isdigit():
                    slugs.add(it["slug"])
                if it["id"] not in found:
                    found[it["id"]] = {"name": it["name"], "slug": it["slug"], "cnt": it["cnt"]}
                    new += 1
            if p == 1 or new:
                log(f"[{si}/{len(slug_list)}] {slug} p{p}: 本页 {len(items)}，新增 {new}，累计 {len(found)}")
            if new == 0:
                empty += 1
                if empty >= 2:
                    break
            else:
                empty = 0
            time.sleep(0.5)
        with open(OUT / "已处理slug.txt", "a", encoding="utf-8") as f:
            f.write(slug + "\n")
        if si % 3 == 0:
            save()
    save()
    ctx.close()
