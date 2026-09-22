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
    print("=" * 72)
    print(" 基于 DeepSeek 的西藏旅游景点智能评价与分析系统 · 开发服务器")
    print(f" 数据库：{settings.db.summary}（口令不打印）")
    print(f" 访问地址：http://{settings.web.host}:{settings.web.port}/")
    print(" 自检接口：/healthz、/api/db-ping")
    print(" 停止服务：在终端按 Ctrl+C")
    print("=" * 72)
    app.run(host=settings.web.host, port=settings.web.port, debug=settings.web.debug)
