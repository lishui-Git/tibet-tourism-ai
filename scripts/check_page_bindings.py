# -*- coding: utf-8 -*-
"""页面绑定检查：**JS 引用的元素 id 必须在对应模板里存在**（静态、零成本）。

## 为什么需要它
本轮之前，每个页面的"JS ↔ HTML 绑定"只在**真机浏览器**里人工点过一遍。
这类问题（JS 取了一个打错字的 id）**不会**被现有任何脚本发现：

    · `smoke_api.py` 只断言路由与静态资源返回 200；
    · `check_api_contract.py` 只校验接口返回字段；
    · 两者都不看"模板里有没有这个 id"。

而它的表现是**静默失效**：`document.getElementById('x')` 返回 null，
JS 在那一行抛错、后续逻辑全不执行，页面看起来"能打开但有些功能没反应"。
本脚本把这一类缺陷变成可自动检查的断言。

## 做法
逐页配对「模板 ↔ 脚本」，从 JS 中提取所有 `getElementById('…')` 的 id，
断言它们都能在对应模板（以及 `base.html` 的公共骨架）中找到。

## 零成本与零副作用
只读文件；不启动服务器、不连数据库、不调用模型。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "app" / "web" / "templates"
STATIC_JS = ROOT / "app" / "web" / "static" / "js"

# 页面 → (模板, 该页的脚本)
#
# 【2026-10 前台改版】一级导航收敛为「首页｜数据总览｜景点分析｜智能分析｜景点对比｜管理员」，
# 原先独立的「智能评价」「智能问答」「任务与口径」三个页面合并为：
#   · evaluation.js + qa.js  → 共用 `smart.html`（子标签切换）
#   · tasks.js + admin.js    → 共用 `admin.html`
# 因此这里按**合并后的真实结构**声明；若仍按旧结构检查，会因为"模板缺失"而误报。
PAGES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("首页", "home.html", ("home.js",)),
    ("数据总览", "overview.html", ("overview.js",)),
    ("景点分析", "spots.html", ("spots.js",)),
    # 整合页：一个模板承载两个子功能的脚本，因此都要检查
    ("智能分析（景点评价 + 智能问答）", "smart.html", ("smart.js", "evaluation.js", "qa.js")),
    ("景点对比", "compare.html", ("compare.js",)),
    # 管理后台：任务与口径 + 登录信息 + 自检
    ("管理后台", "admin.html", ("tasks.js", "admin.js")),
    ("管理员登录", "login.html", ("login.js",)),
)

# 公共脚本（被各页共用），其引用的 id 可能来自 base.html 或各页模板
COMMON_JS = "common.js"

ID_RE = re.compile(r"getElementById\(\s*['\"]([A-Za-z0-9_\-]+)['\"]\s*\)")
HTML_ID_RE = re.compile(r'id="([A-Za-z0-9_\-]+)"')
# 以 **字符串 id** 传给图表初始化的写法（`UI.initChart('chart-score', …)`），
# 这类控件不是用 getElementById 取的，若只认 getElementById 会误报"模板里没人用"。
INIT_CHART_RE = re.compile(r"(?:initChart|UI\.initChart)\(\s*['\"]([A-Za-z0-9_\-]+)['\"]")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def ids_in_html(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    found = set(HTML_ID_RE.findall(text))
    # 有些元素由 JS 动态插入（例如 UI.statCards 生成的结构），
    # 或来自其它模板的 include；这里只收集静态可见的 id。
    return found


def ids_in_js(path: Path) -> set[str]:
    """脚本"取用"的元素 id：既包括 `getElementById`，也包括以字符串传入的图表挂载点。"""
    text = path.read_text(encoding="utf-8")
    # 排除注释行里的示例，避免误报（注释里写 getElementById 是文档行为）
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith(("//", "*", "/*"))]
    body = "\n".join(lines)
    return set(ID_RE.findall(body)) | set(INIT_CHART_RE.findall(body))


def ids_defined_in_js(path: Path) -> set[str]:
    """脚本**动态生成**的元素 id（模板字符串里 `id="xxx"`）。

    为什么必须算进来：像分页按钮（`id="pg-prev"` / `id="pg-next"`）是 JS 拼进
    容器的，静态模板里当然找不到；若不算，检查会误报"JS 引用了不存在的 id"。
    """
    text = path.read_text(encoding="utf-8")
    return set(HTML_ID_RE.findall(text))


def main() -> int:
    print("=" * 84)
    print("页面绑定检查：JS 引用的元素 id 是否都存在于对应模板（只读、零成本）")
    print("=" * 84)

    base_ids = ids_in_html(TEMPLATES / "base.html")
    common_ids = ids_in_js(STATIC_JS / COMMON_JS)
    print(f"\n公共骨架 base.html 提供 {len(base_ids)} 个 id；{COMMON_JS} 引用 {len(common_ids)} 个 id")

    # 整合页会有多个脚本共用一个模板：逐个脚本检查其引用的 id 是否都能在该模板/公共骨架里找到
    for label, template_name, js_names in PAGES:
        template_path = TEMPLATES / template_name
        print(f"\n[{label}] {template_name} ↔ {' + '.join(js_names)}")
        if not template_path.exists():
            check(f"{template_name} 存在", False, "模板缺失")
            continue

        page_ids = ids_in_html(template_path)
        for js_name in js_names:
            js_path = STATIC_JS / js_name
            if not js_path.exists():
                check(f"{js_name} 存在", False, "脚本缺失")
                continue
            js_generated = ids_defined_in_js(js_path)
            referenced = ids_in_js(js_path)
            missing = sorted(
                rid for rid in referenced
                if rid not in page_ids and rid not in base_ids and rid not in common_ids
                and rid not in js_generated
            )
            detail = f"模板 {len(page_ids)} 个 + 脚本动态生成 {len(js_generated)} 个"
            check(f"{js_name} 引用的 {len(referenced)} 个 id 都能找到", not missing,
                  ("缺失：" + ", ".join(missing)) if missing else detail)

    # 反向检查：模板里声明了 id，但没有任何脚本引用它——可能是废弃控件（提示级，不算失败）
    print("\n[提示] 模板中声明但脚本未引用的 id（可能是废弃控件，供人工确认）")
    all_js_ids: set[str] = set(common_ids)
    for _, _, js_names in PAGES:
        for js_name in js_names:
            p = STATIC_JS / js_name
            if p.exists():
                all_js_ids |= ids_in_js(p)
    for _, template_name, _ in PAGES:
        p = TEMPLATES / template_name
        if not p.exists():
            continue
        unused = sorted(i for i in ids_in_html(p) if i not in all_js_ids)
        if unused:
            print(f"  · {template_name}: {', '.join(unused)}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 84)
    print(f"页面绑定检查：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 84)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
