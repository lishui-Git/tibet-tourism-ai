# -*- coding: utf-8 -*-
"""页面路由（展示层）。

详细设计说明书 §2 明确：展示层（HTML + ECharts + axios）**不直连数据库**，
所有数据经接口层（`/api/*`）获取；且「不引入前端构建链与框架」（§10）。

一级导航（2026-10 前台改版后收敛为五栏 + 管理员）：

    `/`            首页          系统能做什么 + 真实数据概况 + 四个核心入口
    `/overview`    数据总览      规模、评分分布、时间趋势、分档、客源地
    `/spots`       景点分析      排行榜、检索、景点详情（情感／趋势／方面／主题／评论）
    `/smart`       智能分析      **整合页**：景点评价 + 智能问答（子标签切换）
    `/compare`     景点对比      指标差异由后端计算；解读默认关闭
    `/my-qa`       我的问答      **需登录**：只显示当前用户自己的问答历史
    `/admin`       管理后台      **需管理员**：任务状态、系统日志、数据口径、系统自检
    `/login`       统一登录      普通用户与管理员共用入口

【为什么把「智能评价」与「智能问答」合并】
    两者都是"针对某个景点的智能问答式输出"，原来是两个一级入口，
    对普通用户来说导航过碎、也不清楚该进哪个。合并为「智能分析」后，
    一级导航只表达**用户想做的事**，内部再用子标签区分两种形态。

【旧地址兼容】`/evaluation`、`/qa`、`/tasks` 保留为**重定向**（分别指向
    `/smart?tab=evaluation`、`/smart?tab=qa`、`/admin`），使已存在的链接与
    书签不会 404——改版不该让旧地址失效。
"""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from app.web.routes._auth_helpers import current_user

bp = Blueprint("pages", __name__)


@bp.get("/")
def index():
    """首页：系统能做什么、真实数据概况、四个核心功能入口。"""
    return render_template("home.html")


@bp.get("/overview")
def overview_page():
    """数据总览。"""
    return render_template("overview.html")


@bp.get("/spots")
def spots_page():
    """景点分析。

    支持 `?spot=<id>` 直达某景点的详情（详情在**页内**展开，不跳页）：
    「智能分析」页在某个景点的智能评价不可用时，会用它把用户送回该景点的
    基础统计与代表评论——因此这个参数必须真的能用，否则那条动线是断的。
    非法值（非数字）按"未指定"处理，不报错。
    """
    spot = (request.args.get("spot") or "").strip()
    if spot and not spot.isdigit():
        spot = ""
    return render_template("spots.html", initial_spot=spot)


@bp.get("/smart")
def smart_page():
    """智能分析（景点评价 + 智能问答）。

    `?tab=evaluation|qa` 决定初始子标签；支持 `?spot=<id>` 直达某景点的评价，
    便于从「景点分析」页一键跳过来看该景点的智能评价。
    `?q=<问题>` 用于首页 AI 入口/业务页联动：默认切到问答标签并预填问题。
    """
    tab = (request.args.get("tab") or "").strip().lower()
    question = (request.args.get("q") or "").strip()[:200]
    if tab not in ("evaluation", "qa"):
        tab = "qa" if question else "evaluation"
    spot = (request.args.get("spot") or "").strip()
    if spot and not spot.isdigit():
        spot = ""
    return render_template(
        "smart.html",
        initial_tab=tab,
        initial_spot=spot,
        initial_question=question,
    )


@bp.get("/compare")
def compare_page():
    """景点对比。"""
    return render_template("compare.html")


@bp.get("/admin")
def admin_page():
    """管理后台（**需管理员**）。

    服务端在渲染前检查会话与角色：
    · 未登录 → 登录页（带 next，登录后回到 /admin）；
    · 已登录但非管理员 → 403 友好页，而不是"页面能打开、里面每个接口都 403"的空壳。
    （真正的权限边界仍在 API 层：`admin_required`，页面检查只是体验优化。）
    """
    user = current_user()
    if user is None:
        return redirect(url_for("pages.login_page", next=request.path))
    if not user.is_admin:
        return render_template(
            "error.html",
            code=403,
            title="无权访问管理后台",
            detail="当前账号是普通用户，不能进入管理后台。普通用户可以在「我的问答」查看自己的提问记录。",
        ), 403
    return render_template("admin.html")


@bp.get("/my-qa")
def my_qa_page():
    """我的问答（需登录）：只展示当前用户自己的问答历史。"""
    if current_user() is None:
        return redirect(url_for("pages.login_page", next=request.path))
    return render_template("my_qa.html")


@bp.get("/login")
def login_page():
    """统一登录页（普通用户与管理员共用入口）。"""
    return render_template("login.html")


# ---------------------------------------------------------------------------
# 旧地址兼容（改版前的链接与书签不应失效）
# ---------------------------------------------------------------------------


@bp.get("/evaluation")
def evaluation_page():
    """旧地址 → 智能分析（景点评价）。"""
    return redirect(url_for("pages.smart_page", tab="evaluation"), code=301)


@bp.get("/qa")
def qa_page():
    """旧地址 → 智能分析（智能问答）。"""
    return redirect(url_for("pages.smart_page", tab="qa"), code=301)


@bp.get("/tasks")
def tasks_page():
    """旧地址 → 管理后台（仍需登录）。"""
    if current_user() is None:
        return redirect(url_for("pages.login_page"))
    return redirect(url_for("pages.admin_page"), code=301)
