# -*- coding: utf-8 -*-
"""Flask Web 应用（C4 容器「Flask Web 应用」）。

本包承载 C4 组件 `C-API-01` ~ `C-API-14`，采用应用工厂模式（create_app），
便于后续按模块拆分路由蓝图、也便于自检脚本在不启动服务器的情况下获取 app 对象。

当前进度（截至阶段四）：
    GET /                    → 骨架首页（证明模板与静态资源可加载）
    GET /healthz             → 进程存活（不查库）
    GET /api/db-ping         → MySQL 连通性 + 已建表数量（查库）

详细设计 §6.2 的 26 个业务接口与 5 个业务页面尚未实现——
它们的**数据来源已经就绪**（阶段三 Spark 全量统计 + 阶段四 DeepSeek 语义结果），
下一阶段按"只读数据库、不在线调用模型"的原则落地。
"""

from __future__ import annotations

from flask import Flask

from app.config import settings


def create_app() -> Flask:
    """创建并配置 Flask 应用实例。"""
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/static",
    )

    # 中文 JSON：关闭 ASCII 转义，便于直接阅读接口返回
    app.json.ensure_ascii = False
    app.config["JSON_AS_ASCII"] = False

    # 注册蓝图（阶段一仅自检类接口）
    from app.web.routes.health import bp as health_bp

    app.register_blueprint(health_bp)

    from app.web.routes.pages import bp as pages_bp

    app.register_blueprint(pages_bp)

    return app


__all__ = ["create_app"]
