# -*- coding: utf-8 -*-
"""自检接口（阶段一新增，属 `C-API-13` 数据访问组件的联调入口）。

注意：本文件的两个接口**不在**详细设计 §6.2 的 26 个业务接口清单内，
它们是阶段一的环境自检口，用于确认「Flask 能起来、MySQL 能连上、17 张表在位」。
待 §6.2 的正式接口在阶段六落地后，本文件可整体删除，不影响业务。
"""

from __future__ import annotations

from flask import Blueprint

from app import __version__
from app.config import settings
from app.db import DatabaseError, ping
from app.web.response import CODE_DB_ERROR, ok, fail

bp = Blueprint("health", __name__)


@bp.get("/healthz")
def healthz():
    """进程存活探测。**不查库**，用于区分「服务没起来」与「数据库连不上」。"""
    return ok(
        {
            "app": "tibet-review-analysis",
            "version": __version__,
            "stage": "阶段一：开发环境与代码骨架初始化",
            "db_target": settings.db.summary,  # 不含口令
        }
    )


@bp.get("/api/db-ping")
def db_ping():
    """MySQL 连通性自检，返回版本、字符集、库名与已建表/外键数量。

    预期结果（项目现状分析.md §8.3、ReadMe.md「数据库连接与初始化」）：
        version = 8.0.32、charset = utf8mb4、database_name = tibet_review、
        table_count = 17、foreign_key_count = 14
    """
    try:
        info = ping()
    except DatabaseError as exc:
        return fail(CODE_DB_ERROR, str(exc))
    return ok(info)
