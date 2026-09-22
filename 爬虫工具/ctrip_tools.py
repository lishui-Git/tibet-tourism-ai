# -*- coding: utf-8 -*-
"""
携程西藏景点评论采集工具 —— 仅供个人学术研究使用
=====================================================
四种模式：
  1) 盘点景点列表（不需要浏览器，最快）
       python ctrip_tools.py discover
     产出：data/out/景点清单_携程.csv   （景点名 / URL / 携程显示点评数）

  2) 试跑一页（第一次用，验证能不能跑通、字段对不对）
       python ctrip_tools.py crawl --test
     产出：data/raw/... 里的评论卡片 + 屏幕上的诊断信息

  3) 正式抓取（可断点续跑，中断了再跑一次即可）
       python ctrip_tools.py crawl
       python ctrip_tools.py crawl --window zuixin      # 只抓“最新”排序
       python ctrip_tools.py crawl --window cha         # 只抓“差评”

  4) 把抓下来的原始页面解析成 15 字段 CSV，并与现有数据合并去重
       python ctrip_tools.py parse

作者备注：本脚本只采集携程页面上公开显示的评论信息，随机延时、单线程、支持断点续跑，
         禁止用于商业用途；请遵守目标网站 robots.txt 与相关法律法规。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RAW = DATA / "raw"
RAW_EXT = DATA / "raw_ext"          # 进藏沿线（非西藏）扩展数据，单独存放，不并入西藏数据集
OUT = DATA / "out"
for d in (DATA, RAW, RAW_EXT, OUT):
    d.mkdir(parents=True, exist_ok=True)

# ============================ 配置区（改这里就行） ============================

CONFIG = {
    # 景点清单文件（每行：景点名称<TAB或逗号>URL），默认用 spots.txt
    "spots_file": "spots.txt",
    # 每个景点每个窗口最多翻多少页（携程硬上限 300 页）
    "max_pages": 300,
    # 翻页之间的随机延时（秒）——不要让网站有压力，也不要让自己被封
    "delay_range": (2.0, 4.5),
    # 页面加载后的额外等待（秒），网络慢就调大
    "wait_after_load": 4.0,
    # 是否无头模式（False = 显示浏览器窗口；携程有风控，必须保持 False）
    "headless": False,
    # 用哪个浏览器跑：msedge = 你日常的 Edge（实测能过携程风控）；chrome = 谷歌浏览器
    "browser_channel": "msedge",
    # 持久化配置目录（存 cookie 与风控凭证，第二次跑通常不用再验证）
    "user_data_dir": "_edge_profile",
    # 预热页：先访问一次首页累积风控 cookie，再去抓目标页（关键！少了这步会被拦）
    "warmup_url": "https://www.ctrip.com/",
    # 每抓多少个景点重新预热一次
    "warmup_every": 5,
    # 评论卡片选择器；留空 = 自动探测（推荐）
    "card_selector": "",
    # 现有数据文件（用于 parse 时对比“新增 / 重复”）
    "existing_csv": "../旅游评论数据集.csv",
}

# 抓取窗口：key 用命令行传，value 是要在页面上点击的按钮文字
WINDOWS = {
    "tuijian": {"label": "推荐排序", "clicks": ["推荐"]},
    "zuixin": {"label": "最新排序", "clicks": ["最新"]},
    "cha": {"label": "差评筛选", "clicks": ["差评"]},
    "hao": {"label": "好评筛选", "clicks": ["好评"]},
    "xiaofei": {"label": "消费后评价", "clicks": ["消费后评价"]},
    "quanbu": {"label": "全部（默认视图）", "clicks": []},
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 反自动化检测的最小补丁（配合 msedge 通道 + 首页预热，实测可通过携程风控）
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


def make_page(pw):
    """用日常 Edge + 持久化配置启动浏览器，并注入反检测脚本。返回 (context, page)。

    扩展模式（--ext）使用独立的配置目录，避免与西藏采集争抢同一个 profile。
    """
    udd = CONFIG["user_data_dir"]
    if "--ext" in sys.argv:
        udd = udd + "_ext"
    ctx = pw.chromium.launch_persistent_context(
        str(ROOT / udd),
        channel=CONFIG["browser_channel"],
        headless=CONFIG["headless"],
        args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        locale="zh-CN",
        viewport={"width": 1440, "height": 950},
        ignore_default_args=["--enable-automation"],
    )
    ctx.add_init_script(STEALTH_JS)
    return ctx, ctx.new_page()


def warmup(page, force=False):
    """先访问一次携程首页，累积风控 cookie。这是能否抓到数据的关键一步。"""
    if not CONFIG.get("warmup_url"):
        return
    try:
        page.goto(CONFIG["warmup_url"], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
    except Exception as e:
        log(f"  预热失败（忽略）：{e}")


def page_ok(page) -> bool:
    """判断当前页是不是被风控拦了。"""
    try:
        t = page.title() or ""
    except Exception:
        return False
    return "验证" not in t

# 最终输出的 15 个字段，顺序与现有数据集完全一致，不可改动
COLS = [
    "景点名称", "评论编号", "评分", "评分描述", "评论内容", "发布时间", "IP归属地",
    "用户昵称", "点赞数", "图片数", "图片URL", "地址", "开放时间", "官方电话", "景点介绍",
]


LOG_PATH = DATA / "采集日志.txt"
LOG_PATH_EXT = DATA / "采集日志_扩展.txt"


def _log_path() -> Path:
    """扩展模式（--ext）写独立日志，避免与西藏采集日志交错。"""
    import sys as _sys
    if "--ext" in _sys.argv:
        return LOG_PATH_EXT
    return LOG_PATH


def log(*a):
    """同时输出到屏幕和日志文件（立即落盘，方便随时查看真实进度）。"""
    msg = " ".join(str(x) for x in a)
    try:
        print(msg, flush=True)
    except Exception:
        pass
    try:
        with _log_path().open("a", encoding="utf-8") as fp:
            fp.write(msg + "\n")
    except Exception:
        pass


# ================================ 工具函数 ================================

def norm_key(s: str) -> str:
    """把景点名做归一（去掉空白、全角空格），用于匹配清单与数据。"""
    return re.sub(r"\s+", "", s or "").replace("\u3000", "")


def load_spots(path: Path) -> list[dict]:
    """读取景点清单：每行 名称<TAB或逗号>URL   （# 开头为注释）"""
    if not path.exists():
        raise SystemExit(f"找不到景点清单文件：{path}\n请先看 README，或先运行 discover 生成。")
    spots = []
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[\t,]", line, maxsplit=1)
        if len(parts) < 2:
            continue
        name, url = parts[0].strip(), parts[1].strip()
        if not url.startswith("http"):
            continue
        if url in seen:
            continue
        seen.add(url)
        spots.append({"name": name, "url": url})
    return spots


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def strip_tags(html: str) -> str:
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.S | re.I)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</(p|div|li)>", "\n", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    return html


def unescape_js(s: str) -> str:
    """处理页面内嵌 JSON 里的 \\uXXXX 转义。"""
    try:
        return s.encode("utf-8").decode("unicode_escape")
    except Exception:
        return s


# ============================ 模式 1：盘点景点清单 ============================

# 在渲染后的 DOM 里找景点卡片：景点名链接 + 同一个卡片里的“N条点评”
DISCOVER_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('a[href*="/sight/"]').forEach(a => {
    const name = (a.innerText || '').trim();
    if (!name || name.length > 40) return;
    const href = a.href || '';
    if (!/\.html/.test(href)) return;
    let card = a, txt = '';
    for (let i = 0; i < 6 && card; i++) {
      card = card.parentElement;
      if (card && /条点评/.test(card.innerText || '')) { txt = card.innerText || ''; break; }
    }
    const m = txt.match(/([\d,]+(?:\.\d+)?)\s*(万?)\s*条点评/);
    let cnt = null;
    if (m) cnt = Math.round(parseFloat(m[1].replace(/,/g, '')) * (m[2] ? 10000 : 1));
    out.push({name: name, url: href, cnt: cnt});
  });
  return out;
}
"""


def mode_discover(args):
    """抓携程西藏景点列表页，产出 景点名 / URL / 显示点评数。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx, page = make_page(pw)
        warmup(page)
        try:
            return _discover_loop(page, ctx, args)
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def _discover_loop(page, browser, args):
    rows: dict[str, dict] = {}
    max_page = args.pages
    for p in range(1, max_page + 1):
        url = f"https://you.ctrip.com/sight/tibet100003/s0-p{p}.html"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            if not page_ok(page):
                log(f"[{p}] 被风控拦截（{page.title()}），重新预热后重试一次")
                warmup(page, force=True)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                if not page_ok(page):
                    log(f"[{p}] 仍被拦截，停止。请在弹出的浏览器窗口里手动完成滑块验证，再重跑本命令。")
                    break
            # 列表页是服务端渲染的，但会被反爬拦截，所以统一走浏览器渲染后再取 DOM
            items = page.evaluate(DISCOVER_JS)
        except Exception as e:
            log(f"[{p}] 请求失败：{e}")
            break
        if not items:
            log(f"[{p}] 没有解析到景点，可能已到最后一页")
            break
        new = 0
        for it in items:
            link = it.get("url") or ""
            name = clean_text(it.get("name") or "")
            if not link or not name or len(name) > 40:
                continue
            if link not in rows:
                rows[link] = {"景点名称": name, "URL": link,
                              "携程显示点评数": it.get("cnt") if it.get("cnt") is not None else ""}
                new += 1
        log(f"[{p}] 新增 {new} 个，累计 {len(rows)} 个")
        if new == 0:
            log("连续无新增，判定列表结束")
            break
        time.sleep(random.uniform(0.8, 2.0))
    browser.close()

    out = OUT / "景点清单_携程.csv"
    ordered = sorted(rows.values(), key=lambda x: (x["携程显示点评数"] if isinstance(x["携程显示点评数"], int) else -1), reverse=True)
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["景点名称", "URL", "携程显示点评数"])
        w.writeheader()
        w.writerows(ordered)
    log(f"\n完成：共 {len(ordered)} 个景点 → {out}")
    log("提示：把想抓的景点挑出来，按 “景点名,URL” 每行一条，写进 spots.txt")


# ============================ 模式 2：抓取点评页 ============================

DETECT_JS = r"""
() => {
  const pat = /20\d{2}-\d{2}-\d{2}/;
  const score = /(超棒|满意|不错|一般|不佳|[1-5]\s*分)/;
  const map = new Map();
  document.querySelectorAll('div,li,section,article').forEach(el => {
    const t = el.innerText || '';
    if (t.length < 60 || t.length > 5000) return;
    if (!pat.test(t) || !score.test(t)) return;
    let sel = el.tagName.toLowerCase();
    const cls = (typeof el.className === 'string' ? el.className : '').trim();
    if (cls) sel += '.' + cls.split(/\s+/).slice(0, 3).join('.');
    const cur = map.get(sel) || {n: 0, len: 0};
    cur.n += 1; cur.len += t.length;
    map.set(sel, cur);
  });
  return [...map.entries()]
    .map(([sel, v]) => ({sel, count: v.n, avgLen: Math.round(v.len / v.n)}))
    .sort((a, b) => b.count - a.count)
    .slice(0, 10);
}
"""

EXTRACT_CARDS_JS = r"""
(sel) => {
  const out = [];
  document.querySelectorAll(sel).forEach(el => {
    out.push({html: el.outerHTML, text: el.innerText || ''});
  });
  return out;
}
"""

# 首选方案：直接读页面内嵌的 Next.js 数据 JSON（字段与现有数据集格式完全一致）
PAGE_DATA_JS = r"""
() => {
  const sc = [...document.querySelectorAll('script')].find(s => (s.textContent || '').includes('"commentId"'));
  if (!sc) return null;
  let j;
  try { j = JSON.parse(sc.textContent); } catch (e) { return null; }
  const findList = (o, d) => {
    d = d || 0; if (!o || d > 8) return null;
    if (Array.isArray(o)) {
      if (o.length && o[0] && typeof o[0] === 'object' && 'commentId' in o[0]) return o;
      for (const x of o) { const r = findList(x, d + 1); if (r) return r; }
      return null;
    }
    if (typeof o === 'object') { for (const k in o) { const r = findList(o[k], d + 1); if (r) return r; } }
    return null;
  };
  const findPoi = (o, d) => {
    d = d || 0; if (!o || d > 8) return null;
    if (typeof o === 'object' && !Array.isArray(o)) {
      if ('poiName' in o && ('address' in o || 'introduction' in o)) return o;
      for (const k in o) { const r = findPoi(o[k], d + 1); if (r) return r; }
    }
    return null;
  };
  const p = findPoi(j, 0) || {};
  const info = {};
  document.querySelectorAll('.baseInfoItem').forEach(it => {
    const t = it.querySelector('.baseInfoTitle'), v = it.querySelector('.baseInfoText');
    if (t && v) info[(t.innerText || '').trim()] = (v.innerText || '').trim();
  });
  return {
    poi: {
      poiName: p.poiName || '', address: p.address || '', introduction: p.introduction || '',
      commentCount: p.commentCount, commentScore: p.commentScore,
      districtName: p.districtName || '', poiId: p.poiId,
      openTime: info['开放时间'] || '', tel: info['官方电话'] || '',
      addressDom: info['地址'] || ''
    },
    comments: findList(j, 0) || []
  };
}
"""


def detect_selector(page):
    try:
        cands = page.evaluate(DETECT_JS)
    except Exception as e:
        log(f"  选择器探测失败：{e}")
        return "", []
    good = [c for c in cands if c["count"] >= 3 and 60 <= c["avgLen"] <= 4000]
    sel = good[0]["sel"] if good else ""
    return sel, cands


def click_by_text(page, text, timeout=4000) -> bool:
    """尽力点击页面上的某个按钮/标签，失败返回 False（不中断流程）。"""
    for loc in (
        page.get_by_role("link", name=text, exact=False),
        page.get_by_role("button", name=text, exact=False),
        page.get_by_text(text, exact=False),
    ):
        try:
            el = loc.first
            el.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


def goto_reviews(page, spot):
    page.goto(spot["url"], wait_until="domcontentloaded", timeout=60000)
    time.sleep(CONFIG["wait_after_load"])
    # 切到“用户点评”标签（有的页面本来就在点评区）
    click_by_text(page, "用户点评", timeout=5000)
    time.sleep(2.0)
    # 往下滚一屏，触发懒加载
    try:
        page.mouse.wheel(0, 2000)
        time.sleep(1.5)
    except Exception:
        pass


def find_next_button(page):
    for name in ("下一页", "下页", ">", "›"):
        try:
            loc = page.get_by_text(name, exact=True).last
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def btn_disabled(loc) -> bool:
    try:
        cls = (loc.get_attribute("class") or "")
        aria = (loc.get_attribute("aria-disabled") or "")
        return ("disabled" in cls) or (aria == "true")
    except Exception:
        return False


def save_jsonl(path: Path, cards: list[dict]):
    with path.open("a", encoding="utf-8") as f:
        for c in cards:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")


def crawl_one(page, spot, window_key, args) -> int:
    win = WINDOWS[window_key]
    safe = norm_key(spot["name"])
    wdir = RAW / safe / window_key
    wdir.mkdir(parents=True, exist_ok=True)
    state_file = wdir / "_state.json"
    done = set()
    if state_file.exists():
        try:
            done = set(json.loads(state_file.read_text(encoding="utf-8")).get("done_pages", []))
        except Exception:
            done = set()

    log(f"\n=== {spot['name']} / {win['label']} ===")
    goto_reviews(page, spot)

    # 切换排序/筛选
    for t in win["clicks"]:
        ok = click_by_text(page, t, timeout=5000)
        log(f"  点击“{t}”：{'成功' if ok else '未找到（将使用当前默认视图）'}")
        if ok:
            time.sleep(2.5)

    sel = CONFIG["card_selector"] or args.selector  # 仅作为 JSON 取不到时的兜底

    max_pages = 1 if args.test else CONFIG["max_pages"]
    total = 0
    for p in range(1, max_pages + 1):
        if p in done:
            log(f"  第 {p} 页：已抓过，跳过")
        else:
            page_file = wdir / f"p{p:03d}.json"
            data, comments = None, []
            try:
                data = page.evaluate(PAGE_DATA_JS)
                if data:
                    comments = data.get("comments") or []
            except Exception as e:
                log(f"  第 {p} 页 JSON 提取失败：{e}")
            if not comments:
                # 兜底：退回 DOM 卡片解析
                if not sel:
                    sel, cands = detect_selector(page)
                    for c in cands[:5]:
                        log(f"    候选选择器 {c['sel']}  命中 {c['count']} 个")
                if sel:
                    try:
                        comments = page.evaluate(EXTRACT_CARDS_JS, sel)
                    except Exception:
                        comments = []
            if not comments and p > 1:
                log(f"  第 {p} 页没有取到评论，判定列表结束")
                break
            payload = {"page": p, "poi": (data or {}).get("poi", {}), "comments": comments}
            page_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            done.add(p)
            state_file.write_text(json.dumps({"done_pages": sorted(done)}, ensure_ascii=False), encoding="utf-8")
            total += len(comments)
            c0 = comments[0] if comments and isinstance(comments[0], dict) and "commentId" in comments[0] else {}
            extra = f"（示例编号 {c0.get('commentId')}）" if c0 else ""
            log(f"  第 {p} 页：{len(comments)} 条（累计 {total}）{extra}")
        if p >= max_pages:
            break
        nxt = find_next_button(page)
        if nxt is None:
            log("  找不到“下一页”，结束")
            break
        if btn_disabled(nxt):
            log("  “下一页”已置灰，已到最后一页，结束")
            break
        try:
            nxt.click(timeout=8000)
        except Exception as e:
            log(f"  翻页点击失败：{e}；结束该窗口")
            break
        time.sleep(random.uniform(*CONFIG["delay_range"]))
    return total


def save_spot_meta(page, spot):
    """保存景点级字段（地址/开放时间/官方电话/介绍）的原始页面，便于解析。"""
    safe = norm_key(spot["name"])
    sdir = RAW / safe
    sdir.mkdir(parents=True, exist_ok=True)
    f = sdir / "_detail.html"
    if f.exists():
        return
    try:
        f.write_text(page.content(), encoding="utf-8")
    except Exception as e:
        log(f"  详情页存盘失败：{e}")


def mode_crawl(args):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("缺少 playwright，请先运行：\n    pip install playwright\n    playwright install chromium")

    spots = load_spots(ROOT / CONFIG["spots_file"])
    if args.spot:
        spots = [s for s in spots if args.spot in s["name"]]
    if args.test and not args.spot:
        spots = spots[:1]
    if not spots:
        raise SystemExit("spots.txt 里没有可抓的景点（或 --spot 没匹配到）")

    windows = args.window if args.window else ["tuijian"]
    log(f"准备抓取 {len(spots)} 个景点，窗口：{windows}")

    grand = 0
    with sync_playwright() as pw:
        ctx, page = make_page(pw)
        warmup(page)
        for idx, spot in enumerate(spots):
            if idx and CONFIG.get("warmup_every") and idx % CONFIG["warmup_every"] == 0:
                warmup(page, force=True)
            log(f"\n>>> {spot['name']}  {spot['url']}")
            try:
                page.goto(spot["url"], wait_until="domcontentloaded", timeout=60000)
                time.sleep(CONFIG["wait_after_load"])
                if not page_ok(page):
                    log("  被风控拦截，重新预热后重试一次")
                    warmup(page, force=True)
                    page.goto(spot["url"], wait_until="domcontentloaded", timeout=60000)
                    time.sleep(CONFIG["wait_after_load"])
                    if not page_ok(page):
                        log("  仍被拦截 → 请在弹出的浏览器窗口里手动完成滑块验证，然后重跑本命令（进度已保存）")
                        continue
                save_spot_meta(page, spot)
            except Exception as e:
                log(f"  打开失败：{e}")
                continue
            for w in windows:
                try:
                    grand += crawl_one(page, spot, w, args)
                except Exception as e:
                    log(f"  [{w}] 出错：{e}（继续下一个）")
                    continue
        try:
            ctx.close()
        except Exception:
            pass
    log(f"\n抓取结束，本次共保存评论卡片 {grand} 条 → {RAW}")
    log("下一步：python ctrip_tools.py parse")


# ============================ 模式 3：官方接口采集 ============================
# 实测结论：
#   接口 POST https://m.ctrip.com/restapi/soa2/13444/json/getCommentCollapseList
#   · 不需要 cookie、不受网页风控限制
#   · pageSize 最大 50（给 100 也只返回 50）
#   · 分页硬上限：offset ≤ 3000（即 pageIndex ≤ 61，最多 3,050 条/窗口）
#   · sortType：6=推荐，1(及3/4/5/7/8)=最新，2=另一种排序
#   · starType：0=全部，1~5=对应星级（精确分区，互不重叠）
#   因此：低星窗口能拿全，5星窗口拿 3,050，再用多排序叠加 → 覆盖远超网页的 300 页

API_URL = "https://m.ctrip.com/restapi/soa2/13444/json/getCommentCollapseList"
API_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0"),
    "Content-Type": "application/json",
    "Referer": "https://you.ctrip.com/",
    "Origin": "https://you.ctrip.com",
    "Accept": "application/json",
}
API_PAGE_SIZE = 50          # 实测上限
API_MAX_OFFSET = 3000       # 实测分页硬上限（offset > 3000 返回空）
API_MAX_PAGE = API_MAX_OFFSET // API_PAGE_SIZE + 1   # = 61

# 采集窗口：(sortType, starType, 说明, 档位)
#   档位 1 = 必跑（低星窗口小、能全拿，且是稀缺差评）
#   档位 2 = 池子大才跑（>1500 条）
#   档位 3 = 池子很大才跑（>6000 条）
API_WINDOWS = [
    (6, 0, "推荐-全部", 1),
    (6, 1, "推荐-1星", 1),
    (6, 2, "推荐-2星", 1),
    (6, 3, "推荐-3星", 1),
    (6, 4, "推荐-4星", 1),
    (1, 0, "最新-全部", 2),
    (6, 5, "推荐-5星", 2),
    (2, 0, "排序2-全部", 3),
    (1, 5, "最新-5星", 3),
]

META_FILE = OUT / "景点元数据.json"

# 非西藏省份关键词：用于过滤扩展 POI 时混入的邻近省份景点（如稻城亚丁属四川）
OTHER_PROVINCES = ("四川省", "四川", "云南省", "云南", "青海省", "青海", "甘肃省", "甘肃",
                   "新疆", "重庆市", "重庆", "贵州省", "贵州",
                   # 邻近省份的自治州/市（地址里常只写州名，不写省名）
                   "迪庆", "甘孜", "阿坝", "凉山", "玉树", "果洛", "海西", "黄南", "甘南",
                   "香格里拉", "丽江", "大理", "和田", "喀什", "克孜勒苏")


def is_tibet(addr: str) -> bool:
    """根据地址判断是否属于西藏。地址为空时不排除（保留原清单景点）。"""
    a = (addr or "").strip()
    if not a:
        return True
    if "西藏" in a:
        return True
    return not any(p in a for p in OTHER_PROVINCES)

POI_JS = r"""
() => {
  const s = [...document.querySelectorAll('script')].find(s => (s.textContent||'').includes('"commentId"'));
  if (!s) return null;
  let j; try { j = JSON.parse(s.textContent); } catch (e) { return null; }
  const findPoi = (o, d) => {
    d = d || 0; if (!o || d > 8) return null;
    if (typeof o === 'object' && !Array.isArray(o)) {
      if ('poiName' in o && 'commentCount' in o) return o;
      for (const k in o) { const r = findPoi(o[k], d + 1); if (r) return r; }
    }
    return null;
  };
  const p = findPoi(j, 0) || {};
  const info = {};
  document.querySelectorAll('.baseInfoItem').forEach(it => {
    const t = it.querySelector('.baseInfoTitle'), v = it.querySelector('.baseInfoText');
    if (t && v) info[(t.innerText || '').trim()] = (v.innerText || '').trim();
  });
  return {
    poiName: p.poiName || '', poiId: p.poiId, businessId: p.businessId,
    commentCount: p.commentCount, commentScore: p.commentScore,
    districtName: p.districtName || '',
    address: p.address || info['地址'] || '',
    openTime: info['开放时间'] || '',
    tel: info['官方电话'] || '',
    introduction: p.introduction || ''
  };
}
"""


def extract_poi_from_html(html: str) -> dict | None:
    """从景点页 HTML 里解析出 poiId 与景点级字段（不依赖浏览器渲染）。"""
    if not html:
        return None
    i = html.find('"commentId"')
    if i < 0:
        return None
    # 找到包含该 JSON 的 <script>...</script>
    start = html.rfind("<script", 0, i)
    end = html.find("</script>", i)
    if start < 0 or end < 0:
        return None
    body = html[html.find(">", start) + 1:end].strip()
    try:
        data = json.loads(body)
    except Exception:
        return None

    poi = {}

    def walk(o, d=0):
        nonlocal poi
        if poi or d > 12 or o is None:
            return
        if isinstance(o, dict):
            if "poiName" in o and ("commentCount" in o or "address" in o):
                poi = o
                return
            for v in o.values():
                walk(v, d + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, d + 1)

    walk(data)
    if not poi:
        return None
    # 基础信息栏（地址/开放时间/官方电话）用正则从 HTML 兜底取
    info = {}
    for m in re.finditer(
            r'<p class="baseInfoTitle">([^<]+)</p>\s*<p class="baseInfoText[^"]*">([^<]*)</p>', html):
        info[m.group(1).strip()] = m.group(2).strip()
    return {
        "poiName": poi.get("poiName", ""),
        "poiId": poi.get("poiId"),
        "businessId": poi.get("businessId"),
        "commentCount": poi.get("commentCount"),
        "commentScore": poi.get("commentScore"),
        "districtName": poi.get("districtName", ""),
        "address": poi.get("address") or info.get("地址", ""),
        "openTime": info.get("开放时间", ""),
        "tel": info.get("官方电话", ""),
        "introduction": poi.get("introduction", ""),
    }


FETCH_JS = r"""
async (u) => {
  try {
    const r = await fetch(u, {credentials: 'include'});
    if (!r.ok) return 'HTTP_' + r.status;
    return await r.text();
  } catch (e) { return 'ERR_' + e; }
}
"""


def load_meta() -> dict:
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_meta(meta: dict):
    """写入景点元数据。采用"读-合并-写"，避免两个采集进程并发时互相覆盖。"""
    try:
        disk = load_meta()
        disk.update(meta)
    except Exception:
        disk = meta
    META_FILE.write_text(json.dumps(disk, ensure_ascii=False, indent=1), encoding="utf-8")


def fetch_poi_meta(spots: list[dict], args) -> dict:
    """用浏览器逐个小访问一次景点页，取真实 poiId 与景点级字段。"""
    from playwright.sync_api import sync_playwright
    meta = load_meta()
    todo = [s for s in spots if norm_key(s["name"]) not in meta or not meta[norm_key(s["name"])].get("poiId")]
    if not todo:
        return meta
    log(f"需要用浏览器补齐 {len(todo)} 个景点的 poiId ...")
    with sync_playwright() as pw:
        ctx, page = make_page(pw)
        warmup(page)
        for i, s in enumerate(todo):
            if i and CONFIG.get("warmup_every") and i % CONFIG["warmup_every"] == 0:
                warmup(page, force=True)
            try:
                page.goto(s["url"], wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                info = page.evaluate(POI_JS)
            except Exception as e:
                log(f"  {s['name']}: 失败 {e}")
                continue
            if not info or not info.get("poiId"):
                log(f"  {s['name']}: 没取到 poiId（可能被风控），跳过")
                continue
            meta[norm_key(s["name"])] = info
            save_meta(meta)
            log(f"  {s['name']}: poiId={info['poiId']} 显示点评数={info.get('commentCount')}")
            time.sleep(random.uniform(1.0, 2.0))
        try:
            ctx.close()
        except Exception:
            pass
    return meta


def api_query(poi_id: int, page: int, sort: int, star: int, retry: int = 3):
    """调一次接口，返回 (totalCount, items)。"""
    import requests
    body = {
        "arg": {"channelType": 2, "collapseType": 0, "commentTagId": 0, "pageIndex": page,
                "pageSize": API_PAGE_SIZE, "poiId": poi_id, "sourceType": 1,
                "sortType": sort, "starType": star},
        "head": {"cid": "09031096217968696292", "ctok": "", "cver": "1.0", "lang": "01",
                 "sid": "8888", "syscode": "09", "auth": "", "xsid": "", "extension": []},
    }
    last = None
    for k in range(retry):
        try:
            r = requests.post(API_URL, headers=API_HEADERS, json=body, timeout=40)
            j = r.json()
            res = j.get("result") or {}
            return res.get("totalCount"), (res.get("items") or [])
        except Exception as e:
            last = e
            time.sleep(2 + 3 * k)
    log(f"    接口失败（第 {page} 页 sort={sort} star={star}）：{last}")
    return None, []


def mode_api(args):
    spots_file = Path(args.spots) if getattr(args, "spots", "") else (ROOT / CONFIG["spots_file"])
    if not spots_file.is_absolute():
        spots_file = ROOT / spots_file
    spots = load_spots(spots_file)
    if args.spot:
        spots = [s for s in spots if args.spot in s["name"]]
    if not spots:
        raise SystemExit("spots.txt 里没有可抓的景点（或 --spot 没匹配到）")

    meta = load_meta()
    windows = API_WINDOWS if not args.window else [
        w for w in API_WINDOWS if w[2] in args.window]
    log(f"准备采集 {len(spots)} 个景点；窗口 {len(windows)} 个")

    # 交错执行：缺 poiId 就用浏览器取一次，然后立刻采集这个景点（不必等全部取完）
    from playwright.sync_api import sync_playwright
    pw_ctx = sync_playwright().start()
    bt = {"ctx": None, "page": None}

    def ensure_page():
        if bt["page"] is None:
            bt["ctx"], bt["page"] = make_page(pw_ctx)
            warmup(bt["page"])
            log("浏览器已启动（用于取 poiId）")
        return bt["page"]

    grand = 0
    summary = []
    for idx, s in enumerate(spots):
        key = norm_key(s["name"])
        info = meta.get(key)
        if not info or not info.get("poiId"):
            page = ensure_page()
            if idx and CONFIG.get("warmup_every") and idx % CONFIG["warmup_every"] == 0:
                warmup(page, force=True)
            info = None
            # 快路径：用浏览器当 HTTP 客户端（不过 WAF，但省去页面渲染）
            try:
                if "you.ctrip.com" not in (page.url or ""):
                    page.goto("https://you.ctrip.com/sight/tibet100003/s0-p1.html",
                              wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(1200)
                html = page.evaluate(FETCH_JS, s["url"])
                if isinstance(html, str) and html.startswith("<"):
                    info = extract_poi_from_html(html)
            except Exception as e:
                log(f"  {s['name']}: 快路径失败 {str(e)[:60]}")
            # 慢路径兜底：完整加载页面
            if not info or not info.get("poiId"):
                try:
                    page.goto(s["url"], wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2000)
                    info = page.evaluate(POI_JS)
                except Exception as e:
                    log(f"  {s['name']}: 浏览器取 poiId 失败 {e}")
                    info = None
            if not info or not info.get("poiId"):
                log(f"\n>>> {s['name']}：没取到 poiId（可能被风控），跳过")
                continue
            meta[key] = info
            save_meta(meta)
            log(f"  {s['name']}: poiId={info['poiId']} 显示点评数={info.get('commentCount')}")
            time.sleep(random.uniform(0.8, 1.5))
        # 地域过滤：默认只采西藏；--ext 时只采非西藏（进藏沿线扩展，单独存放）
        tib = is_tibet(info.get("address"))
        want_ext = bool(getattr(args, "ext", False))
        if want_ext == tib:
            tag = "西藏景点" if tib else "非西藏景点"
            log(f"  {s['name']}: {tag}（{info.get('address')}），本次跳过")
            if not tib:
                with (OUT / "非西藏景点_已排除.csv").open("a", encoding="utf-8-sig") as fe:
                    fe.write(f'{s["name"]},{s["url"]},{info.get("address")}\n')
            continue
        poi_id = info["poiId"]
        sdir = (RAW_EXT if want_ext else RAW) / key
        sdir.mkdir(parents=True, exist_ok=True)
        out_file = sdir / "api.jsonl"
        seen = set()
        if out_file.exists() and not args.fresh:
            for line in out_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        seen.add(str(json.loads(line).get("commentId")))
                    except Exception:
                        pass
        log(f"\n>>> {s['name']}（poiId={poi_id}，页面显示 {info.get('commentCount')} 条，已有 {len(seen)} 条）")
        zero_streak = 0
        pool = None            # 接口返回的 starType=0 总数，用来决定跑哪些档位
        with out_file.open("a", encoding="utf-8") as f:
            for sort, star, label, tier in windows:
                if not args.test and pool is not None:
                    if tier == 2 and pool <= 1500:
                        continue
                    if tier == 3 and pool <= 6000:
                        continue
                added = 0
                pages_needed = 1 if args.test else API_MAX_PAGE
                p = 1
                while p <= pages_needed:
                    total, items = api_query(poi_id, p, sort, star)
                    if total is None:
                        break
                    if star == 0 and sort == 6 and total:
                        pool = int(total)
                    if not items:
                        break
                    if p == 1 and not args.test and total:
                        # 该窗口实际有多少页就翻多少页（小窗口不必翻满 61 页）
                        pages_needed = min(API_MAX_PAGE,
                                           (int(total) + API_PAGE_SIZE - 1) // API_PAGE_SIZE + 1)
                    new = 0
                    for it in items:
                        cid = str(it.get("commentId"))
                        if cid and cid not in seen:
                            seen.add(cid)
                            f.write(json.dumps(it, ensure_ascii=False) + "\n")
                            new += 1
                    added += new
                    if args.test:
                        break
                    if p % 10 == 0:
                        log(f"      ...{label} 第 {p}/{pages_needed} 页，本窗口已新增 {added}")
                    if p >= pages_needed:
                        break
                    if new == 0 and p > 2:
                        break
                    p += 1
                    time.sleep(random.uniform(0.8, 1.8))
                log(f"    [{label}] 新增 {added} 条（累计 {len(seen)}）")
                zero_streak = zero_streak + 1 if added == 0 else 0
                if args.test:
                    break
                if zero_streak >= 3:
                    log("    连续三个窗口无新增，判定已覆盖完，提前结束")
                    break
        f_count = len(seen)
        grand += f_count
        summary.append({"景点名称": s["name"], "接口池": info.get("commentCount"),
                        "已采集": f_count, "poiId": poi_id})
        log(f"    完成：{s['name']} 共 {f_count} 条")

    try:
        if bt["ctx"] is not None:
            bt["ctx"].close()
    except Exception:
        pass
    try:
        pw_ctx.stop()
    except Exception:
        pass

    if summary:
        sp = OUT / "采集进度.csv"
        with sp.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["景点名称", "接口池", "已采集", "poiId"])
            w.writeheader()
            w.writerows(summary)
        log(f"\n采集进度 → {sp}")
        cap = min(3, len(summary))
        log("汇总：")
        for r in summary:
            log(f"  {r['景点名称']}: 接口池 {r['接口池']} / 已采集 {r['已采集']}")
        _ = cap
    log(f"\n本次合计写入 {grand} 条 → {RAW}")
    log("下一步：python ctrip_tools.py parse")


# ============================ 模式 4：解析成 CSV ============================

RE_DATE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
RE_IP = re.compile(r"IP\s*属地\s*[：:]\s*([^\s|，,]+)")
RE_DESC = re.compile(r"(超棒|满意|不错|一般|不佳)")
RE_SCORE = re.compile(r"([1-5])\s*分")
RE_IMG = re.compile(r"(https://dimg\d+\.c-ctrip\.com/images/[^\s\"'<>]+?_W_640_10000\.jpg\?proc=autoorient)")
RE_IMG_ANY = re.compile(r"https://dimg\d+\.c-ctrip\.com/images/[^\s\"'<>]+?\.jpg[^\s\"'<>]*")
RE_ID_ATTR = re.compile(r'(?:data-[\w-]*(?:id|Id|ID)|commentId|reviewId|"id")\s*[=:]\s*["\']?(\d{6,14})')
RE_ID_NUM = re.compile(r"\b(\d{8,10})\b")


def extract_id(html: str, text: str) -> str:
    m = RE_ID_ATTR.search(html or "")
    if m:
        return m.group(1)
    for m in RE_ID_NUM.finditer(html or ""):
        return m.group(1)
    return ""


def extract_nick(html: str, text: str) -> str:
    m = re.search(r'href="[^"]*/members/[^"]*"[^>]*>(.*?)</a>', html or "", re.S)
    if m:
        n = clean_text(strip_tags(m.group(1)))
        if n:
            return n
    m = re.search(r'alt="([^"]{2,30})"', html or "")
    if m:
        return clean_text(m.group(1))
    lines = [clean_text(x) for x in (text or "").split("\n") if clean_text(x)]
    return lines[0] if lines else ""


def extract_likes(html: str, text: str) -> str:
    m = re.search(r"(?:点赞|有用|举报)\D{0,12}?(\d{1,4})", text or "")
    if m:
        return m.group(1)
    return "0"


def extract_content(text: str) -> str:
    """正文 = 卡片文本里最长的一段（剔除日期、IP、评分等短行）。"""
    body = (text or "").replace("\n", "\n")
    chunks = []
    for line in body.split("\n"):
        s = clean_text(line)
        if len(s) < 8:
            continue
        if RE_DATE.fullmatch(s) or RE_IP.search(s):
            continue
        if RE_DESC.fullmatch(s) or re.fullmatch(r"[1-5]\s*分\s*(超棒|满意|不错|一般|不佳)?", s):
            continue
        chunks.append(s)
    if not chunks:
        return clean_text(body)
    chunks.sort(key=len, reverse=True)
    return chunks[0]


DESC_MAP = {"1": "不佳", "2": "一般", "3": "不错", "4": "满意", "5": "超棒"}


def strip_html_simple(html: str) -> str:
    """把景点介绍的 HTML 转成纯文本，与现有数据集的写法保持一致。"""
    t = html or ""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>", " ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"),
                 ("&lt;", "<"), ("&gt;", ">"), ("\u3000", " ")):
        t = t.replace(a, b)
    return clean_text(t)


def parse_publish_time(s: str) -> str:
    """'/Date(1756268548000+0800)/' → '2025-08-27'（按携程给的时区偏移换算）。"""
    m = re.search(r"/Date\((\d+)([+-]\d{4})?\)/", s or "")
    if not m:
        m2 = re.search(r"(20\d{2}-\d{2}-\d{2})", s or "")
        return m2.group(1) if m2 else ""
    sec = int(m.group(1)) / 1000.0
    off = 0
    if m.group(2):
        sign = 1 if m.group(2)[0] == "+" else -1
        off = sign * (int(m.group(2)[1:3]) * 3600 + int(m.group(2)[3:5]) * 60)
    return (datetime(1970, 1, 1) + timedelta(seconds=sec + off)).strftime("%Y-%m-%d")


def row_from_comment(spot_name: str, c: dict, poi: dict) -> dict:
    """把携程内嵌 JSON 的一条评论映射成最终 15 个字段。"""
    imgs = []
    for im in (c.get("images") or []):
        u = (im.get("imageSrcUrl") or "").strip()
        if u:
            imgs.append(u)
    nick = ((c.get("userInfo") or {}).get("userNick") or "").strip()
    score = c.get("score")
    try:
        s = str(int(score)) if score else ""
    except Exception:
        s = ""
    ip = (c.get("ipLocatedName") or "").strip() or "未知"
    content = (c.get("content") or "").replace("\r", " ").replace("\n", " ").strip()
    try:
        likes = str(int(c.get("usefulCount") or 0))
    except Exception:
        likes = "0"
    return {
        "景点名称": spot_name,
        "评论编号": str(c.get("commentId") or "").strip(),
        "评分": s,
        "评分描述": DESC_MAP.get(s, ""),
        "评论内容": content,
        "发布时间": parse_publish_time(c.get("publishTime") or ""),
        "IP归属地": ip,
        "用户昵称": nick,
        "点赞数": likes,
        "图片数": len(imgs),
        "图片URL": "，".join(imgs),
        "地址": (poi.get("address") or poi.get("addressDom") or "").strip(),
        "开放时间": (poi.get("openTime") or "").strip(),
        "官方电话": (poi.get("tel") or "").strip(),
        "景点介绍": strip_html_simple(poi.get("introduction") or ""),
    }


def parse_card(spot_name: str, card: dict, meta: dict) -> dict:
    html, text = card.get("html", ""), card.get("text", "")
    imgs = RE_IMG.findall(html) or RE_IMG_ANY.findall(html)
    imgs = [re.sub(r"_D_\d+_\d+", "_W_640_10000", u).split("?")[0] + "?proc=autoorient" if "_W_640_10000" not in u else u
            for u in imgs]
    imgs = list(dict.fromkeys(imgs))
    score = RE_SCORE.search(text)
    desc = RE_DESC.search(text)
    date = RE_DATE.search(text)
    ip = RE_IP.search(text)
    return {
        "景点名称": spot_name,
        "评论编号": extract_id(html, text),
        "评分": score.group(1) if score else "",
        "评分描述": desc.group(1) if desc else "",
        "评论内容": extract_content(text),
        "发布时间": date.group(1) if date else "",
        "IP归属地": ip.group(1) if ip else "",
        "用户昵称": extract_nick(html, text),
        "点赞数": extract_likes(html, text),
        "图片数": len(imgs),
        "图片URL": "，".join(imgs),
        "地址": meta.get("地址", ""),
        "开放时间": meta.get("开放时间", ""),
        "官方电话": meta.get("官方电话", ""),
        "景点介绍": meta.get("景点介绍", ""),
    }


def parse_spot_meta(spot_dir: Path) -> dict:
    f = spot_dir / "_detail.html"
    if not f.exists():
        return {}
    html = f.read_text(encoding="utf-8", errors="ignore")
    txt = strip_tags(html)
    meta = {}
    for key, pats in {
        "地址": [r'"address"\s*:\s*"([^"]{4,80})"', r"地址\s*[:：]?\s*([^\n]{4,60})"],
        "开放时间": [r'"openTime"\s*:\s*"([^"]{2,80})"', r"开放时间\s*[:：]?\s*([^\n]{2,80})"],
        "官方电话": [r'"tel"\s*:\s*"([^"]{4,60})"', r"官方电话\s*[:：]?\s*([^\n]{4,60})"],
        "景点介绍": [r'"introduction"\s*:\s*"([^"]{20,2000})"', r"介绍\s*[:：]?\s*([^\n]{20,1000})"],
    }.items():
        for p in pats:
            m = re.search(p, html if p.startswith('"') else txt)
            if m:
                meta[key] = clean_text(unescape_js(m.group(1)))[:2000]
                break
    return meta


def mode_parse(args):
    if not RAW.exists():
        raise SystemExit("还没有抓取数据，请先运行 crawl")
    all_rows: list[dict] = []
    inventory: list[dict] = []
    spot_names = {}
    for line in (ROOT / CONFIG["spots_file"]).read_text(encoding="utf-8").splitlines():
        parts = re.split(r"[\t,]", line.strip(), maxsplit=1)
        if len(parts) == 2:
            spot_names[norm_key(parts[0])] = parts[0].strip()

    for spot_dir in sorted(RAW.iterdir()):
        if not spot_dir.is_dir():
            continue
        nice = spot_names.get(spot_dir.name, spot_dir.name)
        n = 0
        poi_last = {}
        # 新格式：p001.json（内嵌 JSON 原始评论对象）
        for jf in sorted(spot_dir.rglob("*.json")):
            if jf.name == "_state.json":
                continue
            try:
                payload = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:
                continue
            poi = payload.get("poi") or {}
            if poi:
                poi_last = poi
            for c in (payload.get("comments") or []):
                if isinstance(c, dict) and "commentId" in c:
                    all_rows.append(row_from_comment(nice, c, poi))
                    n += 1
        # 旧格式兜底：p001.jsonl（DOM 卡片）
        for jf in sorted(spot_dir.rglob("*.jsonl")):
            if jf.name == "api.jsonl":
                continue
            meta = parse_spot_meta(spot_dir)
            for line in jf.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    card = json.loads(line)
                except Exception:
                    continue
                all_rows.append(parse_card(nice, card, meta))
                n += 1
        # 官方接口采集结果：api.jsonl（每行一条原始评论）
        api_file = spot_dir / "api.jsonl"
        if api_file.exists():
            minfo = load_meta().get(spot_dir.name, {})
            if not is_tibet(minfo.get("address")):
                log(f"{nice}: 非西藏景点（{minfo.get('address')}），已从最终数据中排除")
                continue
            poi = {"address": minfo.get("address", ""), "openTime": minfo.get("openTime", ""),
                   "tel": minfo.get("tel", ""), "introduction": minfo.get("introduction", "")}
            for line in api_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    c = json.loads(line)
                except Exception:
                    continue
                all_rows.append(row_from_comment(nice, c, poi))
                n += 1
            if minfo:
                poi_last = {"commentCount": minfo.get("commentCount"),
                            "commentScore": minfo.get("commentScore"),
                            "address": minfo.get("address"), "openTime": minfo.get("openTime"),
                            "tel": minfo.get("tel")}
        if n:
            log(f"{nice}: 解析 {n} 条" + (f"（携程显示 {poi_last.get('commentCount')} 条）" if poi_last else ""))
            inventory.append({
                "景点名称": nice,
                "已抓到": n,
                "携程显示点评数": poi_last.get("commentCount", ""),
                "携程显示评分": poi_last.get("commentScore", ""),
                "地址": poi_last.get("address", ""),
                "开放时间": poi_last.get("openTime", ""),
                "官方电话": poi_last.get("tel", ""),
            })

    if not all_rows:
        raise SystemExit("没有解析到任何评论")

    if inventory:
        inv = OUT / "景点实测清单.csv"
        with inv.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["景点名称", "已抓到", "携程显示点评数", "携程显示评分", "地址", "开放时间", "官方电话"])
            w.writeheader()
            w.writerows(inventory)
        log(f"\n景点实测清单 → {inv}")
        over = [i for i in inventory if isinstance(i["携程显示点评数"], int) and i["已抓到"] > i["携程显示点评数"]]
        if over:
            log(f"注意：有 {len(over)} 个景点抓到的条数超过携程显示数（可能含多窗口/多来源）")

    # 去重：同一评论编号保留字段最全的一条
    best: dict[str, dict] = {}
    noid = 0
    for r in all_rows:
        key = r["评论编号"]
        if not key:
            noid += 1
            key = f"__NOID__{noid}"
        cur = best.get(key)
        if cur is None or sum(1 for c in COLS if r[c]) > sum(1 for c in COLS if cur[c]):
            best[key] = r
    rows = list(best.values())

    out1 = OUT / "新抓取_去重.csv"
    with out1.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    log(f"\n去重后新抓取 {len(rows)} 条（原始 {len(all_rows)} 条，无编号 {noid} 条）→ {out1}")

    # 与现有数据合并
    existing_path = (ROOT / CONFIG["existing_csv"]).resolve()
    if existing_path.exists():
        old = list(csv.DictReader(existing_path.open(encoding="utf-8-sig")))
        old_ids = {r.get("评论编号", "").strip() for r in old if r.get("评论编号")}
        new_only = [r for r in rows if r["评论编号"] and r["评论编号"] not in old_ids]
        log(f"现有数据 {len(old)} 条；真正新增 {len(new_only)} 条；与现有重复 {len(rows) - len(new_only)} 条")
        merged = old + new_only
        seen = set()
        dedup = []
        for r in merged:
            k = (r.get("评论编号") or "").strip()
            if k and k in seen:
                continue
            if k:
                seen.add(k)
            dedup.append({c: r.get(c, "") for c in COLS})
        dedup.sort(key=lambda x: (x["景点名称"], x["发布时间"]), reverse=True)
        out2 = OUT / "合并_总表.csv"
        with out2.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            w.writeheader()
            w.writerows(dedup)
        log(f"合并后总量 {len(dedup)} 条 → {out2}")
    else:
        log(f"没找到现有数据文件 {existing_path}，跳过合并")

    # 质量自检
    bad_img = sum(1 for r in rows if str(r["图片数"]) != str(len([u for u in r["图片URL"].split("，") if u])))
    log("\n—— 自检 ——")
    log(f"图片数与图片URL个数不一致：{bad_img} 条")
    log(f"缺评论编号：{sum(1 for r in rows if not r['评论编号'])} 条")
    log(f"缺发布时间：{sum(1 for r in rows if not r['发布时间'])} 条")
    log(f"缺评分：{sum(1 for r in rows if not r['评分'])} 条")


# ============================ 模式 4：合并导出 ============================

def mode_merge(args):
    """把浏览器控制台导出的 CSV 与现有数据集合并，按评论编号去重，输出 15 字段总表。"""
    existing_path = (ROOT / CONFIG["existing_csv"]).resolve()
    old = []
    if existing_path.exists():
        old = list(csv.DictReader(existing_path.open(encoding="utf-8-sig")))
    # 现有数据的景点级字段，用来给新数据回填（地址/开放时间/官方电话/景点介绍）
    spot_meta: dict[str, dict] = {}
    for r in old:
        s = (r.get("景点名称") or "").strip()
        if not s:
            continue
        d = spot_meta.setdefault(s, {})
        for c in ("地址", "开放时间", "官方电话", "景点介绍"):
            v = (r.get(c) or "").strip()
            if v and not d.get(c):
                d[c] = v

    if args.files:
        files = [Path(x) for x in args.files]
    else:
        files = sorted(OUT.glob("携程评论_*.csv")) + sorted(OUT.glob("新抓取_去重.csv"))
    if not files:
        raise SystemExit("没找到要合并的 CSV（默认找 data/out/携程评论_*.csv）")

    new_rows = []
    for f in files:
        if not f.exists():
            log(f"跳过（不存在）：{f}")
            continue
        n0 = len(new_rows)
        for r in csv.DictReader(f.open(encoding="utf-8-sig")):
            row = {c: (r.get(c) or "").strip() for c in COLS}
            m = spot_meta.get(row["景点名称"], {})
            for c in ("地址", "开放时间", "官方电话", "景点介绍"):
                if not row[c] and m.get(c):
                    row[c] = m[c]
            row["图片URL"] = "，".join([u.strip() for u in row["图片URL"].replace("|", "，").split("，") if u.strip()])
            row["图片数"] = str(len([u for u in row["图片URL"].split("，") if u]))
            if not row["点赞数"].isdigit():
                row["点赞数"] = "0"
            new_rows.append(row)
        log(f"{f.name}：{len(new_rows) - n0} 条")

    old_ids = {(r.get("评论编号") or "").strip() for r in old if (r.get("评论编号") or "").strip()}
    new_only = [r for r in new_rows if r["评论编号"] and r["评论编号"] not in old_ids]
    log(f"\n现有数据：{len(old)} 条")
    log(f"导入新数据：{len(new_rows)} 条（其中真正新增 {len(new_only)} 条，与现有重复 {len(new_rows) - len(new_only)} 条）")

    merged, seen = [], set()
    for r in old + new_only:
        k = (r.get("评论编号") or "").strip()
        if k:
            if k in seen:
                continue
            seen.add(k)
        merged.append({c: (r.get(c) or "").strip() for c in COLS})
    merged.sort(key=lambda x: (x["景点名称"], x["发布时间"]), reverse=True)

    out = OUT / "合并_总表.csv"
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(merged)
    log(f"合并后总量：{len(merged)} 条 → {out}")

    # 本次新增按景点汇总
    from collections import Counter
    cnt = Counter(r["景点名称"] for r in new_only)
    if cnt:
        log("\n本次新增（按景点，前 30）：")
        for name, c in cnt.most_common(30):
            log(f"  {name}: +{c}")

    log("\n—— 质量自检 ——")
    log(f"缺评论编号：{sum(1 for r in merged if not r['评论编号'])} 条")
    log(f"缺发布时间：{sum(1 for r in merged if not r['发布时间'])} 条")
    log(f"缺评分：{sum(1 for r in merged if not r['评分'])} 条")
    log(f"图片数与图片URL个数不一致：{sum(1 for r in merged if r['图片数'] != str(len([u for u in r['图片URL'].split('，') if u])))} 条")


# ============================ 模式 4b：解析进藏沿线扩展数据 ============================

def mode_parse_ext(args):
    """解析 data/raw_ext 下的进藏沿线（非西藏）数据，输出独立 CSV，不并入西藏数据集。"""
    if not RAW_EXT.exists() or not any(RAW_EXT.iterdir()):
        raise SystemExit("还没有扩展数据（data/raw_ext 为空），请先运行：python ctrip_tools.py api --ext --spots ext_spots.txt")
    meta = load_meta()
    spot_names = {}
    for line in (ROOT / CONFIG["spots_file"]).read_text(encoding="utf-8").splitlines():
        parts = re.split(r"[\t,]", line.strip(), maxsplit=1)
        if len(parts) == 2:
            spot_names[norm_key(parts[0])] = parts[0].strip()
    rows = []
    inv = []
    for spot_dir in sorted(RAW_EXT.iterdir()):
        if not spot_dir.is_dir():
            continue
        api_file = spot_dir / "api.jsonl"
        if not api_file.exists():
            continue
        nice = spot_names.get(spot_dir.name, spot_dir.name)
        minfo = meta.get(spot_dir.name, {})
        poi = {"address": minfo.get("address", ""), "openTime": minfo.get("openTime", ""),
               "tel": minfo.get("tel", ""), "introduction": minfo.get("introduction", "")}
        n = 0
        for line in api_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                c = json.loads(line)
            except Exception:
                continue
            rows.append(row_from_comment(nice, c, poi))
            n += 1
        inv.append({"景点名称": nice, "条数": n, "地址": minfo.get("address", "")})
    if not rows:
        raise SystemExit("扩展数据为空")
    best = {}
    for r in rows:
        k = r["评论编号"] or r["评论内容"][:50]
        if k not in best or sum(1 for c in COLS if r[c]) > sum(1 for c in COLS if best[k][c]):
            best[k] = r
    out_rows = list(best.values())
    out = OUT / "扩展_进藏沿线.csv"
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(out_rows)
    with (OUT / "扩展_景点清单.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["景点名称", "条数", "地址"])
        w.writeheader()
        w.writerows(sorted(inv, key=lambda x: -x["条数"]))
    log(f"扩展数据（进藏沿线）：{len(out_rows)} 条，{len(inv)} 个景点 → {out}")
    log("注意：这是独立数据集，未并入西藏数据集，是否采用由你决定。")


# ============================ 模式 4c：合并为最终数据集 ============================

def mode_combine(args):
    """把西藏数据集与进藏沿线扩展数据集合并为最终数据集（按评论编号去重，15 字段不变）。"""
    from collections import Counter
    a_path = OUT / "合并_总表.csv"
    b_path = OUT / "扩展_进藏沿线.csv"
    for p in (a_path, b_path):
        if not p.exists():
            raise SystemExit(f"缺少文件：{p}")

    best: dict[str, dict] = {}
    origin: dict[str, str] = {}
    stat = {}
    for path, tag in ((a_path, "西藏"), (b_path, "进藏沿线")):
        n = 0
        for r in csv.DictReader(path.open(encoding="utf-8-sig")):
            row = {c: (r.get(c) or "").strip() for c in COLS}
            n += 1
            k = row["评论编号"] or (row["景点名称"] + "|" + row["发布时间"] + "|" + row["评论内容"][:60])
            cur = best.get(k)
            if cur is None or sum(1 for c in COLS if row[c]) > sum(1 for c in COLS if cur[c]):
                best[k] = row
                origin[k] = tag
        stat[tag] = n
        log(f"{path.name}：{n} 条")

    rows = list(best.values())
    rows.sort(key=lambda x: (x["景点名称"], x["发布时间"]), reverse=True)

    out = OUT / "最终数据集.csv"
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    log(f"\n合并完成：{stat.get('西藏', 0)} + {stat.get('进藏沿线', 0)} → 去重后 {len(rows)} 条")
    log(f"最终数据集 → {out}")

    spot_origin: dict[str, str] = {}
    for k, r in best.items():
        spot_origin.setdefault(r["景点名称"], origin.get(k, ""))
    cnt = Counter(r["景点名称"] for r in rows)
    with (OUT / "景点来源标注.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["景点名称", "来源口径", "条数"])
        for name, n in cnt.most_common():
            w.writerow([name, spot_origin.get(name, ""), n])
    log(f"景点来源标注 → {OUT / '景点来源标注.csv'}")


# ============================ 模式 5：质量检查与报告 ============================

def mode_quality(args):
    """对最终数据集做质量体检，并生成 Markdown 报告。"""
    from collections import Counter, defaultdict

    merged_path = Path(args.input) if getattr(args, "input", "") else (OUT / "合并_总表.csv")
    if not merged_path.is_absolute():
        merged_path = ROOT / merged_path
    if not merged_path.exists():
        raise SystemExit("还没有合并结果，请先运行 parse")
    rows = list(csv.DictReader(merged_path.open(encoding="utf-8-sig")))
    log(f"读取合并总表：{len(rows)} 条")

    existing_path = (ROOT / CONFIG["existing_csv"]).resolve()
    old_n = 0
    if existing_path.exists():
        old_n = sum(1 for _ in csv.DictReader(existing_path.open(encoding="utf-8-sig")))

    # 原始采集总量（未去重）
    raw_n = 0
    for f in RAW.rglob("api.jsonl"):
        raw_n += sum(1 for line in f.read_text(encoding="utf-8").splitlines() if line.strip())

    # ---------- 基础统计 ----------
    spots = Counter(r["景点名称"] for r in rows)
    ids = [r["评论编号"] for r in rows]
    uniq_ids = set(i for i in ids if i)
    no_id = sum(1 for i in ids if not i)

    dates = [r["发布时间"] for r in rows if re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["发布时间"] or "")]
    years = Counter(d[:4] for d in dates)

    # ---------- 内容重复（按正文）----------
    content_rows = defaultdict(list)
    for r in rows:
        c = (r["评论内容"] or "").strip()
        if c:
            content_rows[c].append(r)
    dup_groups = {c: v for c, v in content_rows.items() if len(v) > 1}
    dup_rows = sum(len(v) for v in dup_groups.values())
    cross = [v for v in dup_groups.values()
             if len({x["景点名称"] for x in v}) > 1]

    # ---------- 缺失 ----------
    missing = {}
    for c in COLS:
        missing[c] = sum(1 for r in rows if not (r.get(c) or "").strip())

    # ---------- 异常 ----------
    bad = Counter()
    for r in rows:
        s = (r["评分"] or "").strip()
        if s and s not in {"1", "2", "3", "4", "5"}:
            bad["评分越界"] += 1
        if s and (r["评分描述"] or "") != DESC_MAP.get(s, ""):
            bad["评分与描述不匹配"] += 1
        d = (r["发布时间"] or "").strip()
        if d and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            bad["日期格式异常"] += 1
        n = len([u for u in (r["图片URL"] or "").split("，") if u.strip()])
        if (r["图片数"] or "0").strip() != str(n):
            bad["图片数不一致"] += 1
        if not (r["点赞数"] or "").strip().isdigit():
            bad["点赞数非数字"] += 1
        cid = (r["评论编号"] or "").strip()
        if cid and not re.fullmatch(r"\d{8,10}", cid):
            bad["评论编号格式异常"] += 1

    # ---------- 未采集 / 失败的景点 ----------
    all_spots = load_spots(ROOT / CONFIG["spots_file"])
    got = {d.name for d in RAW.iterdir() if d.is_dir() and (d / "api.jsonl").exists()}
    not_done = [s["name"] for s in all_spots if norm_key(s["name"]) not in got]
    meta = load_meta()
    no_poi = [s["name"] for s in all_spots
              if not meta.get(norm_key(s["name"]), {}).get("poiId")]

    # ---------- 输出报告 ----------
    L = []
    A = L.append
    A("# 携程西藏旅游评论数据集 · 采集与质量报告\n")
    A(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    A("## 一、总量\n")
    A("| 指标 | 数值 |")
    A("|---|---|")
    A(f"| 原始采集条数（未去重） | {raw_n} |")
    A(f"| 原有数据条数 | {old_n} |")
    A(f"| **最终去重后条数** | **{len(rows)}** |")
    A(f"| 覆盖景点数 | {len(spots)} |")
    A(f"| 评论ID唯一率 | {len(uniq_ids) / max(1, len(ids)):.4%} |")
    A(f"| 无评论ID条数 | {no_id} |")
    A("")
    A("## 二、时间范围\n")
    if dates:
        A(f"- 最早：**{min(dates)}**　最晚：**{max(dates)}**")
    A("- 按年份分布：\n")
    A("| 年份 | 条数 |")
    A("|---|---|")
    for y in sorted(years):
        A(f"| {y} | {years[y]} |")
    A("")
    A("## 三、景点分布\n")
    if spots:
        vals = sorted(spots.values())
        A(f"- 景点数：{len(spots)}　最多：{vals[-1]} 条　中位数：{vals[len(vals)//2]} 条　最少：{vals[0]} 条")
        for lo, hi, name in [(100, 10**9, "≥100 条"), (31, 99, "31–99 条"), (11, 30, "11–30 条"), (1, 10, "≤10 条")]:
            n = sum(1 for v in spots.values() if lo <= v <= hi)
            tot = sum(v for v in spots.values() if lo <= v <= hi)
            A(f"- {name}：{n} 个景点，合计 {tot} 条")
        A("\n**条数最多的 20 个景点**\n")
        A("| 景点 | 条数 |")
        A("|---|---|")
        for name, n in spots.most_common(20):
            A(f"| {name} | {n} |")
    A("")
    A("## 四、重复情况\n")
    A(f"- 按 `评论编号` 去重后：{len(rows)} 条（ID 唯一率 {len(uniq_ids)/max(1,len(ids)):.4%}）")
    A(f"- 正文完全相同的评论：**{len(dup_groups)} 组 / {dup_rows} 条**（占 {dup_rows/max(1,len(rows)):.2%}）")
    A(f"- 其中跨景点重复：{len(cross)} 组")
    A("- 说明：这些是**不同评论ID、内容恰好相同**的真实评论（如「值得一去」「好」），按研究要求保留原文、不做修改。\n")
    A("## 五、字段缺失\n")
    A("| 字段 | 空值数 | 占比 |")
    A("|---|---|---|")
    for c in COLS:
        A(f"| {c} | {missing[c]} | {missing[c]/max(1,len(rows)):.2%} |")
    A("")
    A("## 六、异常检查\n")
    if bad:
        A("| 异常类型 | 条数 |")
        A("|---|---|")
        for k, v in bad.most_common():
            A(f"| {k} | {v} |")
    else:
        A("未发现异常。")
    A("")
    A("## 七、采集覆盖情况\n")
    A(f"- 清单景点总数：{len(all_spots)}")
    A(f"- 已采集到数据的景点：{len(got)}")
    A(f"- 尚未采集的景点：{len(not_done)}")
    if not_done:
        A(f"  - 前 30 个：{'、'.join(not_done[:30])}")
    A(f"- 未取到 poiId（可能被风控/无点评）的景点：{len(no_poi)}")
    if no_poi:
        A(f"  - {'、'.join(no_poi[:30])}")
    A("")
    A("## 八、数据文件\n")
    A("| 文件 | 说明 |")
    A("|---|---|")
    A("| `data/out/合并_总表.csv` | **最终数据集**（15 列，UTF-8 BOM，按评论编号去重）|")
    A("| `data/out/新抓取_去重.csv` | 本次新采集部分 |")
    A("| `data/raw/*/api.jsonl` | 原始接口数据（未加工，永久保留）|")
    A("| `data/out/景点清单_携程.csv` | 携程西藏景点清单 |")
    A("| `data/out/采集进度.csv` | 各景点接口池 / 已采集 |")
    A("| `data/out/景点元数据.json` | 各景点 poiId 与景点级字段 |")
    A("| `data/out/内容重复清单.csv` | 正文完全相同的评论分组（未修改原文）|")
    A("| `data/out/非西藏景点_已排除.csv` | 扩展时混入的非西藏景点（未并入）|")
    A("")
    A("## 九、采集限制与下一步\n")
    A("**已实测的硬限制**")
    A("1. 点评接口分页上限为 `offset <= 3000`（每窗口最多 3,050 条），已试遍")
    A("   `channelType / sourceType / collapseType / commentTagId / pageSize / 多种排序+星级分区`，均无法突破。")
    A("2. 携程西藏景点池有限：不同排序的列表页对比后无新增景点，全部西藏 POI 已采集。")
    A("3. 因此**仅靠携程西藏景点**，可获取的真实评论总量存在上限（本报告已给出实际值）。")
    A("")
    A("**可行的扩量路径（需你确认，程序已就绪但未采用）**")
    A("- **进藏沿线扩展**：川藏/滇藏/青藏沿线景点（稻城亚丁、普达措、梅里雪山、新都桥、墨石公园等），")
    A("  同一平台、同一字段结构，已识别 93 个景点、显示点评合计约 4.5 万条。")
    A("  运行方式：`python ctrip_tools.py api --ext --spots ext_spots_top.txt` 然后 `python ctrip_tools.py parse-ext`；")
    A("  数据写入 `data/raw_ext`，产出 `data/out/扩展_进藏沿线.csv`，**默认不并入西藏数据集**。")
    A("- **其他平台**（马蜂窝/大众点评等）：字段结构不同（无「评分描述」等），会破坏字段统一性，需你决定是否接受。")
    A("")

    rep = OUT / ("最终质量报告.md" if getattr(args, "input", "") else "质量报告.md")
    rep.write_text("\n".join(L), encoding="utf-8")

    # 内容重复清单（独立文件，便于人工复核；不改动总表任何字段）
    if dup_groups:
        dupf = OUT / "内容重复清单.csv"
        with dupf.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["重复组号", "重复条数", "涉及景点数", "评论内容", "涉及景点", "评论编号列表"])
            for i, (content, items) in enumerate(
                    sorted(dup_groups.items(), key=lambda kv: -len(kv[1])), 1):
                spots_in = sorted({x["景点名称"] for x in items})
                w.writerow([i, len(items), len(spots_in), content,
                            "、".join(spots_in), "、".join(x["评论编号"] for x in items)])
        log(f"内容重复清单 → {dupf}")

    # 采集失败/未采集景点明细（独立文件）
    with (OUT / "采集失败景点.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["景点名称", "URL", "失败原因"])
        for s in all_spots:
            k = norm_key(s["name"])
            if k in got:
                continue
            reason = "未取到 poiId（无点评或页面无评论模块）" if not meta.get(k, {}).get("poiId") else "已获取 poiId 但未采集到评论"
            w.writerow([s["name"], s["url"], reason])
    log(f"采集失败/未采集明细 → {OUT / '采集失败景点.csv'}")

    # 阶段性快照（保留历史，不覆盖）
    snap_dir = OUT / "阶段快照"
    snap_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%m%d_%H%M")
    try:
        import shutil
        shutil.copy2(merged_path, snap_dir / f"合并_总表_{stamp}.csv")
        shutil.copy2(rep, snap_dir / f"质量报告_{stamp}.md")
        log(f"阶段性快照 → {snap_dir}\\合并_总表_{stamp}.csv")
    except Exception as e:
        log(f"快照保存失败：{e}")

    log(f"\n原始 {raw_n} 条 → 去重后 {len(rows)} 条；景点 {len(spots)} 个")
    log(f"ID 唯一率 {len(uniq_ids)/max(1,len(ids)):.4%}；正文重复 {dup_rows} 条（{dup_rows/max(1,len(rows)):.2%}）")
    log(f"未采集景点 {len(not_done)} 个；无 poiId {len(no_poi)} 个")
    if bad:
        log("异常：" + "，".join(f"{k}={v}" for k, v in bad.most_common()))
    log(f"报告 → {rep}")


# ============================ 模式 6：诊断 ============================

def mode_diagnose(args):
    spots = load_spots(ROOT / CONFIG["spots_file"])
    if not spots:
        raise SystemExit("spots.txt 为空")
    spot = spots[0]
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b, page = make_page(pw)
        warmup(page)
        goto_reviews(page, spot)
        sel, cands = detect_selector(page)
        log(f"\n景点：{spot['name']}")
        log("候选选择器：")
        for c in cands:
            log(f"  {c['sel']}   命中 {c['count']} 个, 平均长度 {c['avgLen']}")
        if sel:
            cards = page.evaluate(EXTRACT_CARDS_JS, sel)
            log(f"\n用 {sel} 取到 {len(cards)} 张卡片。第 1 张解析结果：")
            meta = {}
            row = parse_card(spot["name"], cards[0], meta) if cards else {}
            for k, v in row.items():
                log(f"  {k} = {str(v)[:120]}")
            log("\n第 1 张卡片 HTML 里的 ID 类特征（前 400 字）：")
            log(cards[0]["html"][:400] if cards else "（无）")
            log("\n第 1 张卡片纯文本：")
            log(cards[0]["text"][:600] if cards else "（无）")
        b.close()
    log("\n把上面这段输出发给助手，即可校准字段解析。")


# ================================ 入口 ================================

def main():
    ap = argparse.ArgumentParser(description="携程西藏景点评论采集工具（学术用途）")
    sub = ap.add_subparsers(dest="mode", required=True)

    d = sub.add_parser("discover", help="盘点携程西藏景点列表")
    d.add_argument("--pages", type=int, default=60, help="最多翻多少页（默认 60）")
    d.set_defaults(func=mode_discover)

    c = sub.add_parser("crawl", help="抓取点评页")
    c.add_argument("--test", action="store_true", help="试跑：只抓第 1 页、第 1 个景点")
    c.add_argument("--spot", default="", help="只抓名称包含该关键字的景点")
    c.add_argument("--window", action="append", choices=list(WINDOWS.keys()),
                   help="抓取窗口，可重复；默认 tuijian（推荐排序）")
    c.add_argument("--selector", default="", help="手工指定评论卡片 CSS 选择器")
    c.set_defaults(func=mode_crawl)

    p = sub.add_parser("parse", help="解析成 15 字段 CSV 并合并")
    p.set_defaults(func=mode_parse)

    a = sub.add_parser("api", help="★推荐：用官方接口采集（快、不受网页风控限制）")
    a.add_argument("--test", action="store_true", help="试跑：每个景点只抓第 1 页")
    a.add_argument("--spot", default="", help="只抓名称包含该关键字的景点")
    a.add_argument("--window", action="append", default=None,
                   help="窗口名，可重复（如 推荐-全部 / 最新-全部 / 推荐-5星）；默认全部窗口")
    a.add_argument("--fresh", action="store_true", help="忽略已有数据，重新采集")
    a.add_argument("--spots", default="", help="指定景点清单文件（默认用 spots.txt）")
    a.add_argument("--ext", action="store_true",
                   help="进藏沿线扩展模式：只采非西藏景点，数据写入 data/raw_ext（不并入西藏数据集）")
    a.set_defaults(func=mode_api)

    m = sub.add_parser("merge", help="把导出的 CSV 与现有数据合并去重，输出 15 字段总表")
    m.add_argument("files", nargs="*", help="要合并的 CSV（默认 data/out 下所有 携程评论_*.csv）")
    m.set_defaults(func=mode_merge)

    pe = sub.add_parser("parse-ext", help="解析进藏沿线扩展数据（data/raw_ext）→ 独立 CSV")
    pe.set_defaults(func=mode_parse_ext)

    co = sub.add_parser("combine", help="合并西藏数据集与进藏沿线扩展数据集 → 最终数据集.csv")
    co.set_defaults(func=mode_combine)

    q = sub.add_parser("quality", help="数据质量体检：重复率/缺失/异常/景点分布，并生成报告")
    q.add_argument("--input", default="", help="指定要体检的 CSV（默认 data/out/合并_总表.csv）")
    q.set_defaults(func=mode_quality)

    g = sub.add_parser("diagnose", help="诊断：打印候选选择器和第 1 张卡片的解析结果")
    g.set_defaults(func=mode_diagnose)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
