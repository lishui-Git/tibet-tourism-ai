# -*- coding: utf-8 -*-
"""页面路由（展示层）。

详细设计说明书 §2 明确：展示层（HTML + ECharts + axios）**不直连数据库**，
所有数据经接口层（`/api/*`）获取；且「不引入前端构建链与框架」（§10）。

页面清单（对应系统六个模块中的业务页面）：
    `/`            首页（系统说明 + 数据概况 + 环境自检）
    `/overview`    M1 数据总览
    `/spots`       M2 景点分析
    `/evaluation`  M3 景点智能评价
    `/compare`     M4 景点对比（指标由后端算；解读默认关闭，零 API 消费）
    `/tasks`       M6 系统管理（任务与口径）

未实现的页面：M5 智能问答 —— 它必须在请求时调用模型（依赖用户当次提问），
需先设计好限额、缓存与降级策略再落地（见 `app/web/README.md` §4）。
"""

from __future__ import annotations

from flask import Blueprint, render_template

bp = Blueprint("pages", __name__)


@bp.get("/")
def index():
    """首页：系统说明、数据概况与运行自检。"""
    return render_template("home.html")


@bp.get("/overview")
def overview_page():
    """M1 数据总览页。"""
    return render_template("overview.html")


@bp.get("/spots")
def spots_page():
    """M2 景点分析页。"""
    return render_template("spots.html")


@bp.get("/evaluation")
def evaluation_page():
    """M3 景点智能评价页。"""
    return render_template("evaluation.html")


@bp.get("/compare")
def compare_page():
    """M4 景点对比页。"""
    return render_template("compare.html")


@bp.get("/tasks")
def tasks_page():
    """M6 任务与口径页。"""
    return render_template("tasks.html")
