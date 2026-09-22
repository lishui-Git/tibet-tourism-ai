# -*- coding: utf-8 -*-
"""页面路由（阶段一骨架）。

详细设计说明书 §2 明确：展示层（HTML + ECharts + axios）**不直连数据库**，
所有数据经接口层获取；且「不引入前端构建链与框架」（§10）。
因此这里只渲染一个静态骨架页，数据由页面内的 JS 调用 /api/* 取得。
"""

from __future__ import annotations

from flask import Blueprint, render_template

bp = Blueprint("pages", __name__)


@bp.get("/")
def index():
    """骨架首页：证明「Flask 可运行 + 模板可渲染 + 静态资源可加载 + 接口可调用」。"""
    return render_template("index.html")
