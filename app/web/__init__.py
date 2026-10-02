# -*- coding: utf-8 -*-
"""Flask Web 应用（C4 容器「Flask Web 应用」）。

本包承载 C4 组件 `C-API-01` ~ `C-API-14`，采用应用工厂模式（create_app），
便于后续按模块拆分路由蓝图、也便于自检脚本在不启动服务器的情况下获取 app 对象。

当前进度（截至阶段四 + 只读业务层）：
    页面：GET /                → 骨架首页（证明模板与静态资源可加载）
    自检：GET /healthz         → 进程存活（不查库）
          GET /api/db-ping     → MySQL 连通性 + 已建表数量（查库）

    业务（**只读**，数据来自已落库的离线分析结果）：
      M1 数据总览  GET /api/overview/summary | /trend | /distribution | /provinces | /data-note
      M2 景点分析  GET /api/spots | /spots/ranking | /spots/{id} | /trend | /sentiment
                       | /aspects | /topics | /reviews
      M3 智能评价  GET /api/spots/{id}/report
      M6 系统管理  GET /api/admin/tasks | /tasks/{id}/logs | /caliber

【架构原则】详细设计 §6.2 的其余接口（认证 4 个、对比 1 个、问答 2 个、
重新生成评价 1 个、用户列表 1 个）尚未实现，因为它们依赖用户体系或需要在线模型调用；
在实现前不注册路由，避免出现"有接口无实现"或"有权限校验但形同虚设"的情况。
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

    # 注册蓝图：自检 + 页面
    from app.web.routes.health import bp as health_bp

    app.register_blueprint(health_bp)

    from app.web.routes.pages import bp as pages_bp

    app.register_blueprint(pages_bp)

    # 注册蓝图：业务只读接口（M1 数据总览 / M2 景点分析 / M3 智能评价 / M6 系统管理）
    from app.web.routes.admin import bp as admin_bp
    from app.web.routes.overview import bp as overview_bp
    from app.web.routes.spots import bp as spots_bp

    app.register_blueprint(overview_bp)
    app.register_blueprint(spots_bp)
    app.register_blueprint(admin_bp)

    return app


__all__ = ["create_app"]
