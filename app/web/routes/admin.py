# -*- coding: utf-8 -*-
"""M6 系统管理接口（C4 组件 `C-API-14`，详细设计 §6.2 第 23、24、26 项）。

对应页面：后台管理页（M6）。本阶段实现**只读**部分：任务列表、任务日志、口径配置。
需要登录与角色的接口（用户管理、重新生成评价）留待用户体系落地后再补，
**不做假的鉴权**——宁可不实现，也不留一个"看起来有权限校验其实没有"的接口。

注：接口 25 `/api/admin/users` 未实现（依赖 `sys_user` 与登录态）；
本文件不注册该路由，调用会得到 Flask 的 404，属预期。
"""

from __future__ import annotations

from flask import Blueprint, request

from app.web.data_access import parse_int_arg
from app.web.routes._helpers import respond, respond_one
from app.web.services import admin_caliber, admin_task_detail, admin_tasks

bp = Blueprint("admin", __name__, url_prefix="/api/admin")


@bp.get("/tasks")
def tasks():
    """批处理任务列表与进度（接口 23）：`limit`、`task_type`。"""
    task_type = request.args.get("task_type") or None

    def action():
        limit = parse_int_arg(request.args, "limit", required=False, default=20)
        return admin_tasks(limit or 20, task_type)

    return respond(action)


@bp.get("/tasks/<int:task_id>/logs")
def task_logs(task_id: int):
    """任务日志明细（接口 24）。"""
    return respond_one(lambda: admin_task_detail(task_id), f"任务 {task_id} 不存在")


@bp.get("/caliber")
def caliber():
    """系统数据口径配置（接口 26）。"""
    return respond(admin_caliber)


__all__ = ["bp"]
