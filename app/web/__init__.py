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

import secrets

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

    # 会话签名密钥：优先取 .env 的 APP_SECRET_KEY；未配置时生成一次性随机密钥
    # （开发可跑，重启后旧登录态失效；生产/答辩演示请在 .env 配置固定值）
    app.secret_key = settings.web.secret_key or secrets.token_hex(32)
    # 会话 Cookie 的安全属性（§13.1）：
    #   HttpOnly 防脚本读取；SameSite=Lax 缓解 CSRF；本地 http 环境不能强制 Secure
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_NAME="tibet_session",
    )

    # 中文 JSON：关闭 ASCII 转义，便于直接阅读接口返回
    app.json.ensure_ascii = False
    app.config["JSON_AS_ASCII"] = False

    # 注册蓝图：自检 + 页面
    from app.web.routes.health import bp as health_bp

    app.register_blueprint(health_bp)

    from app.web.routes.pages import bp as pages_bp

    app.register_blueprint(pages_bp)

    # 注册蓝图：业务只读接口 + 认证（M1 总览 / M2 景点 / M3 评价 / M4 对比 / M5 问答 / M6 管理）
    from app.web.routes.admin import bp as admin_bp
    from app.web.routes.auth import bp as auth_bp
    from app.web.routes.compare import bp as compare_bp
    from app.web.routes.overview import bp as overview_bp
    from app.web.routes.qa import bp as qa_bp
    from app.web.routes.spots import bp as spots_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(overview_bp)
    app.register_blueprint(spots_bp)
    app.register_blueprint(compare_bp)
    app.register_blueprint(qa_bp)
    app.register_blueprint(admin_bp)

    return app


__all__ = ["create_app"]
