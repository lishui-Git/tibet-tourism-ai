# -*- coding: utf-8 -*-
"""Flask Web 应用（C4 容器「Flask Web 应用」）。

本包承载 C4 组件 `C-API-01` ~ `C-API-14`，采用应用工厂模式（create_app），
便于后续按模块拆分路由蓝图、也便于自检脚本在不启动服务器的情况下获取 app 对象。

当前进度（截至阶段四）：详细设计 §6.2 的 **26 个接口全部实现**，
外加 3 个自检接口与 8 个页面路由；清单与说明见 `app/web/README.md`。

    页面：`/`、`/overview`、`/spots`、`/evaluation`、`/compare`、`/qa`、`/tasks`、`/login`
    自检：`GET /healthz`（不查库）、`GET /api/db-ping`（查库）
    业务（**只读 + 离线结果**）：M1 总览 / M2 景点 / M3 评价 / M4 对比 / M5 问答 / M6 管理

【架构原则】Web 层**只读数据库**：`app/web/**` 内不导入 `app.llm`、不发 HTTP 请求，
因此"打开页面"永远不会产生模型调用；需要模型的场景（对比解读、问答回答）
由 `APP_COMPARE_LIVE` / `APP_QA_LIVE` 显式开关控制，**默认关闭**。
"""

from __future__ import annotations

import secrets

from flask import Flask, render_template, request

from app.config import settings


def _wants_json_envelope() -> bool:
    """请求是否属于 `/api/**`（决定错误响应用 JSON 信封还是 HTML 页面）。"""
    return request.path.startswith("/api/")


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

    _register_template_context(app)
    _register_error_handlers(app)
    return app


# 路径前缀 → 一级导航的 data-nav 标识（用于高亮当前栏目）
_NAV_BY_PREFIX = (
    ("/overview", "overview"),
    ("/spots", "spots"),
    ("/smart", "smart"),
    ("/evaluation", "smart"),   # 兼容旧地址
    ("/qa", "smart"),           # 兼容旧地址
    ("/compare", "compare"),
    ("/admin", "admin"),
    ("/tasks", "admin"),        # 兼容旧地址
    ("/login", "login"),
)


def _register_template_context(app: Flask) -> None:
    """把模板需要的公共变量注入 Jinja 上下文。

    为什么需要：`base.html` 要在**每个页面**上判断"当前是否已登录"（决定导航右侧显示
    『管理员』还是『管理后台』）并高亮当前栏目。逐个视图传参既啰嗦又容易漏，
    因此统一在这里注入。
    """

    @app.context_processor
    def _inject():  # noqa: ANN202
        from app.web.routes._auth_helpers import current_user

        path = request.path or "/"
        active = "home" if path == "/" else ""
        for prefix, nav in _NAV_BY_PREFIX:
            if path.startswith(prefix):
                active = nav
                break
        return {"current_user": current_user(), "active_nav": active}



def _register_error_handlers(app: Flask) -> None:
    """让 `/api/**` 的 404/405 也返回统一信封，而不是 Flask 默认的 HTML 页面。

    为什么必须做（本轮实测发现的缺口）：
        `/api/spots/abc` 这类路径会被路由转换器 `<int:spot_id>` **在进入视图前**就拒绝，
        因此项目自己的 `respond()` 根本没机会执行，客户端拿到的是
        `text/html` 的 "404 Not Found" 页面——**不符合 §6.3 声明的统一响应体**，
        前端按 `body.code` 取错误码时会拿到 `undefined`。

    口径（严格按 §6.4 既有码，不新增）：
        · 路径不存在          → 3001（资源不存在，HTTP 404）
        · 路径存在但方法不对  → 1001（参数/请求格式错误，HTTP 400）

    页面路由（非 `/api/`）**不受影响**，仍返回 Flask 默认的 HTML 404。
    """
    from app.web.response import (
        CODE_NOT_FOUND,
        CODE_PARAM_INVALID,
        fail,
    )

    @app.errorhandler(404)
    def _api_404(error):  # noqa: ANN001, ARG001
        if not _wants_json_envelope():
            # 页面路径：渲染友好的 404 页，而不是 Flask 默认的英文 "Not Found"。
            # 普通用户不该看到框架默认页；技术信息保留在开发日志里。
            return render_template("error.html",
                                   code=404, title="页面不存在",
                                   detail="你访问的地址不存在，可能链接有误或页面已调整。"), 404
        return fail(CODE_NOT_FOUND, "请求的接口路径不存在")

    @app.errorhandler(405)
    def _api_405(error):  # noqa: ANN001, ARG001
        if not _wants_json_envelope():
            return render_template("error.html",
                                   code=405, title="请求方式不正确",
                                   detail="请通过页面上的正常入口访问该功能。"), 405
        return fail(
            CODE_PARAM_INVALID,
            f"请求方法不被允许：{request.method} {request.path}",
        )

    @app.errorhandler(500)
    def _api_500(error):  # noqa: ANN001, ARG001
        # 500 一律不向前台暴露异常内容（堆栈只进日志），避免把内部实现泄给用户。
        if not _wants_json_envelope():
            return render_template("error.html",
                                   code=500, title="服务器内部错误",
                                   detail="系统处理该请求时出现异常，请稍后重试。"), 500
        return fail(5003, "服务器内部错误，请稍后重试")


__all__ = ["create_app"]
