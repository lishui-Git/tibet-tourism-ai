# -*- coding: utf-8 -*-
"""Flask 开发服务器入口（阶段一）。

用法：
    python run.py

等价于 `flask --app app.web:create_app run`，但显式读取 .env 中的 FLASK_HOST/PORT/DEBUG，
避免依赖 flask 命令行的环境变量约定（NR-M-01：配置集中）。
"""

from __future__ import annotations

from app.config import settings
from app.web import create_app

app = create_app()


if __name__ == "__main__":
    # 终端横幅面向**开发者**，因此保留完整技术信息（含数据库与自检入口）；
    # 浏览器里的页面标题与导航则使用普通用户语言（见 app/web/templates/base.html）。
    print("=" * 72)
    print(" 西藏旅游景点智能评价与分析系统 · 开发服务器")
    print(" （基于 DeepSeek 的离线语义分析；Web 层只读已落库结果，浏览不产生调用）")
    print(f" 数据库：{settings.db.summary}（口令不打印）")
    print(f" 访问地址：http://{settings.web.host}:{settings.web.port}/")
    print(f" 管理后台：http://{settings.web.host}:{settings.web.port}/admin（需登录）")
    print(" 自检接口：/healthz、/api/db-ping")
    print(" 停止服务：在终端按 Ctrl+C")
    print("=" * 72)
    app.run(host=settings.web.host, port=settings.web.port, debug=settings.web.debug)
