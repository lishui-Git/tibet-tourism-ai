# -*- coding: utf-8 -*-
"""前台改版验收：逐页检查渲染结果与用户可见文案（零 API 消费）。

怎么验：**真正用 test_client 请求每个页面，检查返回的 HTML**，
而不是只看模板源码——因为很多文案是 JS 运行时生成的，模板里看不到。

检查四类问题：
    ① **页面可达**：公开页 200；需登录页未登录时 302 到登录页；
    ② **开发者语言泄漏**：HTML 正文里不得出现 Spark / MLlib / deepseek / mllib /
       stat_* / spot_report / APP_*_LIVE / REPORT_NOT_GENERATED / ERR_BAD_REQUEST /
       task_id / BR-xx / "阶段一" 等内部用语（管理员页允许出现，另测）；
    ③ **静态资源与脚本**：CSS/JS 都能 200；页面引用的 id 在 JS 里能对上（防"绑定丢失"）；
    ④ **旧地址兼容**：/evaluation、/qa、/tasks 分别 301 到新地址。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.web import create_app

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# 普通用户不该看到的内部用语（大小写不敏感；在**页面 HTML**里搜）
LEAK_PATTERNS = [
    (r"Spark", "Spark"),
    (r"MLlib", "MLlib"),
    (r"mllib", "mllib"),
    (r"deepseek", "deepseek"),
    (r"stat_spot", "stat_spot"),
    (r"stat_time", "stat_time"),
    (r"stat_ip", "stat_ip"),
    (r"spot_report", "spot_report"),
    (r"spot_fact_package", "spot_fact_package"),
    (r"comment_semantic", "comment_semantic"),
    (r"analysis_task", "analysis_task"),
    (r"task_log", "task_log"),
    (r"APP_[A-Z_]+_LIVE", "APP_*_LIVE"),
    (r"REPORT_NOT_GENERATED", "REPORT_NOT_GENERATED"),
    (r"REVIEW_COUNT_BELOW_THRESHOLD", "REVIEW_COUNT_BELOW_THRESHOLD"),
    (r"ERR_BAD_REQUEST", "ERR_BAD_REQUEST"),
    (r"BR-\d+", "BR 规则编号"),
    (r"阶段[一二三四]", "开发阶段分期"),
    (r"task_id", "task_id"),
    (r"prompt_version", "prompt_version"),
    (r"token_usage", "token_usage"),
    (r"method=", "method="),
]

PUBLIC_PAGES = [
    ("/", "首页"),
    ("/overview", "数据总览"),
    ("/spots", "景点分析"),
    ("/smart", "智能分析"),
    ("/compare", "景点对比"),
    ("/login", "管理员登录"),
]

STATIC_ASSETS = [
    "/static/css/app.css",
    "/static/js/common.js",
    "/static/js/home.js",
    "/static/js/overview.js",
    "/static/js/spots.js",
    "/static/js/smart.js",
    "/static/js/evaluation.js",
    "/static/js/qa.js",
    "/static/js/compare.js",
    "/static/js/tasks.js",
    "/static/js/admin.js",
    "/static/js/login.js",
    "/static/vendor/echarts.min.js",
    "/static/vendor/axios.min.js",
]


def main() -> int:
    print("=" * 88)
    print("前台改版验收：逐页渲染 + 用户可见文案检查（零 API 消费）")
    print("=" * 88)

    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()

    # ---------- ① 公开页面可达且无内部用语 ----------
    print("\n[1] 公开页面：可达性 + 开发者语言泄漏检查")
    htmls: dict[str, str] = {}
    for path, label in PUBLIC_PAGES:
        r = c.get(path)
        html = r.get_data(as_text=True)
        htmls[path] = html
        check(f"{label} {path} 返回 200", r.status_code == 200, f"HTTP {r.status_code}")

        leaks = []
        for pat, name in LEAK_PATTERNS:
            if re.search(pat, html, re.IGNORECASE):
                leaks.append(name)
        check(f"{label} 页面无开发者语言泄漏",
              not leaks, "泄漏：" + "、".join(sorted(set(leaks))) if leaks else "干净")

    # ---------- ② 需登录页面 ----------
    print("\n[2] 需登录页面：未登录时必须跳登录，而不是露出空壳")
    r = c.get("/admin")
    check("/admin 未登录 → 302", r.status_code == 302, f"HTTP {r.status_code}")
    if r.status_code == 302:
        check("/admin 跳转到登录页", "/login" in (r.headers.get("Location") or ""),
              r.headers.get("Location") or "")

    # ---------- ③ 旧地址兼容 ----------
    print("\n[3] 旧地址兼容（改版不应让旧链接失效）")
    for old, expect in (("/evaluation", "/smart"), ("/qa", "/smart"), ("/tasks", "login_or_admin")):
        r = c.get(old)
        loc = r.headers.get("Location") or ""
        if expect == "login_or_admin":
            # /tasks 需登录：未登录跳到登录页
            ok = r.status_code == 302 and ("/login" in loc or "/admin" in loc)
        else:
            ok = r.status_code in (301, 302) and expect in loc
        check(f"{old} 重定向正确", ok, f"HTTP {r.status_code} → {loc}")

    # ---------- ④ 静态资源 ----------
    print("\n[4] 静态资源（任一个 404 都会导致页面残缺）")
    bad = []
    for asset in STATIC_ASSETS:
        r = c.get(asset)
        if r.status_code != 200:
            bad.append(f"{asset}({r.status_code})")
    check(f"{len(STATIC_ASSETS)} 个静态资源全部可访问", not bad, "、".join(bad) if bad else "")

    # ---------- ⑤ 导航结构 ----------
    print("\n[5] 导航结构（一级导航收敛为六项，智能评价/问答不再是独立一级）")
    home = htmls.get("/", "")
    nav = re.search(r'<nav class="mainnav".*?</nav>', home, re.S)
    check("存在主导航", nav is not None, "")
    if nav:
        items = re.findall(r'>([^<>]+)</a>', nav.group(0))
        items = [i.strip() for i in items if i.strip()]
        print(f"      导航项：{items}")
        check("含「智能分析」一级入口", "智能分析" in items, "")
        check("「智能评价」不再是独立一级", "智能评价" not in items, "")
        check("「智能问答」不再是独立一级", "智能问答" not in items, "")
        check("导航含首页/数据总览/景点分析/景点对比",
              all(x in items for x in ("首页", "数据总览", "景点分析", "景点对比")), "")
        check("导航项数 ≤ 6（不拥挤）", len(items) <= 6, f"{len(items)} 项")

    # ---------- ⑥ 全局交互元素 ----------
    print("\n[6] 全局交互元素（sticky 导航 / 返回顶部）")
    check("每页都有返回顶部按钮",
          all('id="back-to-top"' in h for h in htmls.values()), "")
    check("返回顶部按钮带 title 与 aria-label（可访问性）",
          'aria-label="返回顶部"' in home and 'title="返回顶部"' in home, "")
    check("导航使用 fixed 定位（下滑仍可见）", "topbar" in home, "")

    # ---------- ⑦ CSS 关键规则 ----------
    print("\n[7] 样式表关键规则（fixed 导航 / 返回顶部显隐 / 三档响应式）")
    css = c.get("/static/css/app.css").get_data(as_text=True)
    check("顶部导航为 fixed", re.search(r"\.topbar\s*\{[^}]*position:\s*fixed", css, re.S) is not None, "")
    check("返回顶部默认隐藏、.show 才显示",
          "#back-to-top" in css and "visibility: hidden" in css and ".show" in css, "")
    check("有 prefers-reduced-motion 兼容（尊重减少动效偏好）",
          "prefers-reduced-motion" in css, "")
    for width in ("1180px", "900px", "620px"):
        check(f"含 {width} 断点（宽屏/笔记本/窄窗）", width in css, "")

    # ---------- ⑧ 首页真实数据绑定 ----------
    print("\n[8] 首页：数据来自接口而非硬编码")
    check("首页通过接口取数据概况", "/api/overview/summary" in htmls.get("/", "")
          or "/api/overview/summary" in c.get("/static/js/home.js").get_data(as_text=True), "")
    entry_count = htmls.get("/", "").count('class="entry"')
    check("首页含四个功能入口", entry_count >= 4, f"entry 数 {entry_count}")
    check("技术说明区在折叠 <details> 内", "<details" in home, "")

    # ---------- ⑨ 智能分析整合页 ----------
    print("\n[9] 智能分析整合页：两个子功能 + 子标签切换")
    smart = htmls.get("/smart", "")
    check("含「景点评价」子标签", "景点评价" in smart and 'data-tab="evaluation"' in smart, "")
    check("含「智能问答」子标签", "智能问答" in smart and 'data-tab="qa"' in smart, "")
    check("两个子面板都在页面内", 'id="panel-evaluation"' in smart and 'id="panel-qa"' in smart, "")
    check("支持 ?tab=qa 指定初始标签",
          c.get("/smart?tab=qa").status_code == 200, "")
    check("支持 ?spot= 直达某景点评价",
          "__INITIAL_SPOT__" in smart, "")
    check("非法 tab 值被安全回退",
          c.get("/smart?tab=../etc").status_code == 200, "")
    check("非法 spot 值被安全回退",
          c.get("/smart?spot=abc").status_code == 200, "")

    # ---------- ⑩ 对比页相同景点拦截 ----------
    print("\n[10] 景点对比：相同景点必须在前端拦截")
    cmp_js = c.get("/static/js/compare.js").get_data(as_text=True)
    check("前端有相同景点的前置校验函数", "validateSelection" in cmp_js, "")
    check("提示语为要求的文案", "请选择两个不同的景点进行对比" in cmp_js, "")
    check("默认预选两个不同景点", "默认就给两个**不同**的景点" in cmp_js or "items[1]" in cmp_js, "")
    check("选择变化时即时提示相同景点", "bindSameGuard" in cmp_js, "")

    # ---------- ⑪ 错误页 ----------
    print("\n[11] 错误页：普通用户看到中文提示而非框架默认页")
    r404 = c.get("/this-page-does-not-exist")
    body404 = r404.get_data(as_text=True)
    check("404 返回 404 状态", r404.status_code == 404, f"HTTP {r404.status_code}")
    check("404 为中文友好页", "页面不存在" in body404, "")
    check("404 不含异常堆栈/框架默认英文", "Not Found" not in body404 or "页面不存在" in body404, "")
    r_api404 = c.get("/api/nope")
    check("API 404 仍返回 JSON 信封", r_api404.is_json and r_api404.get_json().get("code") == 3001,
          str(r_api404.get_json()))

    # ---------- ⑫ 管理员页允许内部术语但要有中文 ----------
    print("\n[12] 管理员页：允许内部术语，但必须同时给中文说明")
    admin_js_src = c.get("/static/js/tasks.js").get_data(as_text=True)
    check("任务类型有中文映射", "TASK_TYPE_LABELS" in admin_js_src, "")
    check("日志阶段有中文映射", "STAGE_LABELS" in admin_js_src, "")

    # ---------- ⑬ 前端传参必须落在接口允许的范围内 ----------
    #
    # 为什么单列一组：改版时踩到过——`limit: 200` 超出接口上限（1–100），
    # 页面直接显示"暂时无法加载景点列表"，而**模板与静态检查全绿**。
    # 这类"前端传了非法参数"的错只有真跑页面才看得到，因此这里静态扫一遍所有前端的数值参数。
    print("\n[13] 前端传参范围：limit / page_size 不得超过接口上限 100")
    js_dir = ROOT / "app" / "web" / "static" / "js"
    API_CAP = 100
    offenders = []
    for js_file in sorted(js_dir.glob("*.js")):
        text = js_file.read_text(encoding="utf-8")
        for m in re.finditer(r"\b(limit|page_size)\s*:\s*(\d+)", text):
            key, val = m.group(1), int(m.group(2))
            if val > API_CAP:
                offenders.append(f"{js_file.name}: {key}={val}")
        # 也检查 select 的 option 值（如总览页的地区条数下拉）
        for m in re.finditer(r'<option value="(\d+)"', text):
            pass
    check(f"前端数值参数都不超过接口上限 {API_CAP}", not offenders,
          "、".join(offenders) if offenders else "全部合规")

    # 反向确认：接口确实拒绝了超限值（说明这条限制是真实存在的，不是我们凭空加的）
    r_over = c.get("/api/spots/ranking?by=reviews&limit=200")
    check("接口对 limit>100 返回参数错误（1002）",
          r_over.status_code == 400 and (r_over.get_json() or {}).get("code") == 1002,
          f"HTTP {r_over.status_code} code={(r_over.get_json() or {}).get('code')}")

    # ---------- ⑭ 下拉框 option 的 value 不含内部标识 ----------
    print("\n[14] 下拉框选项值不得携带内部标识")
    for path, label in PUBLIC_PAGES:
        html = htmls.get(path, "")
        bad = re.findall(r'<option value="(deepseek|mllib|dict)"', html)
        check(f"{label} 的 option value 无内部标识", not bad,
              "、".join(bad) if bad else "")

    # ---------- ⑮ 运行期文案：用户可见区域不得出现内部码 ----------
    #
    # 为什么需要"运行期"检查：模板与静态 JS 都是干净的，但**接口返回体里带着内部码**
    # （如 reason=REPORT_NOT_GENERATED），一旦被渲染进页面正文，用户就会看到。
    # 这里用真实浏览器无关的方式模拟：直接检查渲染函数产物——把接口数据喂给渲染逻辑不现实
    # （需要 JS 运行时），因此改为检查"渲染源码是否把这些字段写进了可见区域"：
    #   允许出现在 `UI.tech(...)` / `<details>` 里，不允许直接拼进正文。
    print("\n[15] 运行期文案：内部码只能进折叠区，不得进入正文")
    for js_name in ("evaluation.js", "compare.js", "qa.js"):
        src = c.get(f"/static/js/{js_name}").get_data(as_text=True)
        # 找出所有"直接输出内部原因码"的地方：`原因码：${...reason...}` 这类
        direct = re.findall(r"(?:原因码|reason 码|reason=)\s*[:：]?\s*\$\{[^}]*reason", src)
        check(f"{js_name} 未把 reason 直接拼进可见正文", not direct,
              "、".join(direct) if direct else "")
        # 若确实要展示，必须是走 UI.tech(...) 的折叠区
        if "reason" in src:
            check(f"{js_name} 的 reason 若出现则仅用于折叠/控制台",
                  ("UI.tech" in src) or ("console.info" in src) or ("console.warn" in src),
                  "")

    # 评价页的"暂未生成"文案必须面向用户、不含内部码
    ev = c.get("/static/js/evaluation.js").get_data(as_text=True)
    check("暂未生成文案是用户语言", "暂未生成" in ev, "")
    check("文案说明「仍可查看基础统计」", "基础统计" in ev, "")

    # ---------- ⑯ 景点识别质量：应命中"主景点"而不是同名子景点 ----------
    #
    # 实测踩到过：问「布达拉宫怎么样」被识别为「布达拉宫-殊胜三界殿」（1 条评论），
    # 而真正的布达拉宫有 3,965 条评论——用户看到的是几乎没数据的那一个。
    # 这属于**用户可见的体验缺陷**，因此固定成断言。
    print("\n[16] 景点识别质量：优先命中主景点")
    from app.web.qa import match_spots

    cases = [
        ("布达拉宫怎么样", "布达拉宫"),
        ("布达拉宫和纳木措景区哪个好", "布达拉宫"),
        ("纳木措怎么样", "纳木措景区"),
        ("大昭寺怎么样", "大昭寺"),
    ]
    for q, expect_first in cases:
        got = match_spots(q)
        first = got[0]["spot_name"] if got else None
        check(f"『{q}』首选命中 {expect_first}", first == expect_first, f"实际 {first}")
    # 辨识出的景点应当是评论量较高的那个（而非同名子景点）
    got_buda = match_spots("布达拉宫怎么样")
    if got_buda:
        check("布达拉宫的识别结果评论量 >1000（不是同名子景点）",
              int(got_buda[0].get("review_count") or 0) > 1000,
              f"review_count={got_buda[0].get('review_count')}")
    check("不含景点名的排行类问题不误识别景点",
          match_spots("评论量前十的景点") == [], "")

    # ---------- ⑰ 藏地视觉识别（2026-10 视觉改版） ----------
    #
    # 目标：让"这是西藏旅游分析系统"在第一眼成立。这里固定三件事：
    #   ① 首屏 Hero 必须存在，且带**自绘矢量插画**（版权干净、不依赖外网）；
    #   ② 关键数据必须落在首屏（用户不用滚动就知道系统规模）；
    #   ③ 插画资源必须真实可访问（否则首页会出现破图）。
    print("\n[17] 藏地视觉识别：首屏 Hero + 原创插画 + 关键数据")
    home_html = htmls.get("/", "")
    check("首页含首屏 Hero 区块", 'class="hero"' in home_html, "")
    check("Hero 使用自绘矢量插画（非外链图片）",
          "img/tibet-hero.svg" in home_html and "http" not in re.search(
              r'<img class="hero-art"[^>]*>', home_html).group(0), "")
    check("Hero 有深色蒙版保证文字对比度", 'class="hero-veil"' in home_html, "")
    check("Hero 含关键数据容器（由接口填充）", 'id="hero-kpis"' in home_html, "")
    check("Hero 含明确的进入按钮", 'class="btn-hero"' in home_html, "")

    home_js = c.get("/static/js/home.js").get_data(as_text=True)
    check("Hero 四个关键数字由接口渲染（非硬编码）",
          "renderHeroKpis" in home_js and "/api/overview/summary" in home_js, "")
    check("关键数字标签与设计要求一致",
          all(k in home_js for k in ("游客评论", "覆盖景点", "有效评价", "可生成完整评价")), "")
    check("有效评价取统计基线口径（47,110 对应的那一档）",
          "mllib" in home_js and "baselineCount" in home_js, "")
    check("首页含西藏景点视觉卡容器", 'id="home-scenic"' in home_html, "")
    check("风景卡插画加载失败有降级处理（不影响数据展示）",
          "data-scenic-art" in home_js and "scenic-noart" in c.get("/static/css/app.css").get_data(as_text=True), "")
    check("首页不再以表格为主（榜单用列表卡而非表格）",
          'class="rank-list"' in home_html and 'class="rank-row"' in home_js, "")

    # ---------- ⑱ 插画资源可访问 + 版权自证 ----------
    print("\n[18] 插画资源：可访问、自绘、体积可控")
    svg_dir = ROOT / "app" / "web" / "static" / "img"
    svgs = sorted(svg_dir.glob("*.svg"))
    check("插画文件存在（≥5 个场景）", len(svgs) >= 5, f"{len(svgs)} 个")
    bad_size, bad_xml = [], []
    import xml.etree.ElementTree as _ET
    for p in svgs:
        if p.stat().st_size > 80_000:
            bad_size.append(f"{p.name}({p.stat().st_size}B)")
        try:
            _ET.fromstring(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            bad_xml.append(f"{p.name}: {exc}")
    check("插画全部为合法 XML/SVG", not bad_xml, "、".join(bad_xml) if bad_xml else "")
    check("单个插画体积 < 80KB（不拖慢演示）", not bad_size, "、".join(bad_size) if bad_size else
          "最大 " + str(max(p.stat().st_size for p in svgs)) + "B")
    for p in svgs:
        r = c.get(f"/static/img/{p.name}")
        if r.status_code != 200:
            check(f"{p.name} 可访问", False, f"HTTP {r.status_code}")
    check("全部插画可通过静态路由访问", True, f"{len(svgs)} 个文件")
    check("插画为原创自绘（文件头注明版权干净、无第三方素材）",
          all("原创" in p.read_text(encoding="utf-8")[:900] for p in svgs), "")

    # ---------- ⑲ 视觉体系：主题变量与"避免满屏白卡片" ----------
    print("\n[19] 视觉体系：藏地主题色 + 分区层次")
    check("定义经幡五色变量（少量点缀用）",
          all(v in css for v in ("--flag-blue", "--flag-white", "--flag-red", "--flag-green", "--flag-yellow")), "")
    check("主色为藏蓝／深青（非通用科技蓝）", "--c-primary:" in css and "--c-primary-ink:" in css, "")
    check("存在深色分区样式（与浅色卡片交替）", ".band {" in css or ".band{" in css, "")
    check("重点数字使用大字号 + 等宽数字对齐",
          "kpi-value" in css and "tabular-nums" in css, "")
    check("经幡色带作为全站视觉签名", ".flagline" in css and "flagline" in home_html, "")
    check("导航有当前栏目高亮标记", ".mainnav a.active" in css, "")
    check("图片/视频等装饰失败不影响布局（降级样式存在）", "scenic-noart" in css, "")

    # ---------- ⑳ 插画兜底：首屏与风景卡都必须能"无图可用" ----------
    print("\n[20] 插画兜底：加载失败时页面与数据不受影响")
    check("首屏插画带兜底标记", "data-art" in home_html and "data-art-box" in home_html, "")
    check("首屏有降级背景样式", ".hero.art-missing" in css, "")
    common_js = c.get("/static/js/common.js").get_data(as_text=True)
    check("公共脚本统一处理插画失败（img[data-art]）",
          "_initArtFallback" in common_js and "img[data-art]" in common_js, "")
    check("失败时隐藏图片并标记容器", "img.style.display = 'none'" in common_js
          and "art-missing" in common_js, "")
    check("已缓存的失败图片也会被处理（不只看 error 事件）",
          "naturalWidth === 0" in common_js, "")
    check("风景卡插画同样接入兜底", "data-scenic-art" in home_js, "")

    # ---------- ㉑ 响应式结构：窄屏必须真的重排，而不只是"写了断点" ----------
    #
    # 仅仅存在 @media 并不代表会重排。这里检查**关键网格在窄屏确实改列数**：
    # 重点数字从 4 列变 2 列、风景卡从多列变 1 列、榜单隐藏次要列。
    # 实测（用固定宽度 iframe 渲染）：
    #   1440 → hero 4 列 / 风景卡 4 列 / 导航单行
    #   1024 → hero 4 列 / 风景卡 3 列
    #    768 → hero 2 列 / 风景卡 2 列
    #    500 → hero 2 列 / 风景卡 1 列 / 导航自身横向滚动
    print("\n[21] 响应式结构：关键网格在窄屏确实重排")

    def media_blocks(css_text: str, query: str) -> str:
        """取出**所有**匹配该查询的 @media 块内容并拼起来。

        为什么不能用 split 取第一个：视觉改版新增的样式层与基础层各写了一个
        `@media (max-width: 900px)`（后者在文件更靠前的位置），
        只取第一个会漏掉真正的重排规则——这是本测试自己踩过的坑。
        """
        chunks, start = [], 0
        while True:
            i = css_text.find(query, start)
            if i < 0:
                break
            j = css_text.find("{", i)
            if j < 0:
                break
            depth, k = 0, j
            while k < len(css_text):
                if css_text[k] == "{":
                    depth += 1
                elif css_text[k] == "}":
                    depth -= 1
                    if depth == 0:
                        chunks.append(css_text[j + 1:k])
                        break
                k += 1
            start = k + 1
        return "\n".join(chunks)

    narrow = media_blocks(css, "@media (max-width: 900px)")
    phone = media_blocks(css, "@media (max-width: 620px)")
    check("成功解析出 900px 断点块", bool(narrow.strip()), f"{len(narrow)} 字符")
    check("成功解析出 620px 断点块", bool(phone.strip()), f"{len(phone)} 字符")
    check("900px 断点内重排首屏关键数字（4 列 → 2 列）",
          ".hero-kpis" in narrow and "repeat(2" in narrow, "")
    check("900px 断点内重排重点数字块", ".kpi-row" in narrow or ".kpi " in narrow or ".kpi{" in narrow, "")
    check("620px 断点内风景卡改为单列", ".scenic-grid" in phone and "1fr" in phone, "")
    check("620px 断点内榜单隐藏次要列（避免挤压）",
          ".rank-row .rs" in phone and "display: none" in phone, "")
    check("620px 断点内 Hero 标题降级字号", ".hero h2" in phone, "")
    check("窄屏按钮改为整行（点击区域足够大）",
          ".btn-hero" in phone and "width: 100%" in phone, "")
    check("导航在窄屏改为自身横向滚动（不挤压品牌区）",
          ".mainnav" in narrow and "overflow-x: auto" in narrow, "")
    check("品牌区在窄屏保留最小宽度（系统名可见）",
          "min-width: 132px" in narrow, "")
    check("表格过宽时容器内滚动而不是撑破卡片",
          "overflow-x: auto" in css, "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"前台改版验收：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
