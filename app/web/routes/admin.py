# -*- coding: utf-8 -*-
"""M6 系统管理接口（C4 组件 `C-API-14`，详细设计 §6.2 第 23–26 项）。

    GET /api/admin/tasks                批处理任务列表与进度（23）
    GET /api/admin/tasks/{id}/logs      任务日志明细（24）
    GET /api/admin/users                用户列表（25）
    GET /api/admin/caliber              数据口径配置（26）

**鉴权（§13.1 / NR-S-02）**：四个接口都要求**管理员**（`admin_required`）：
未登录 → 2001（HTTP 401）；已登录但非管理员 → 2002（HTTP 403）。
接口 26（口径配置）本可公开——口径说明在每个业务接口的 `caliber_note` 里已经回显，
这里保持一致归入管理端，避免出现"同一份口径两处不同权限"的混乱。

**用户列表**（接口 25）只返回 `user_id/username/nickname/role/status/时间`，
**绝不返回 `password_hash`**（§13.1 NR-S-01）。
"""

from __future__ import annotations

from flask import Blueprint, request

from app.web.auth import list_users
from app.web.data_access import parse_int_arg, parse_pagination
from app.web.routes._auth_helpers import admin_required
from app.web.routes._helpers import respond, respond_one
from app.web.services import admin_caliber, admin_task_detail, admin_tasks

bp = Blueprint("admin", __name__, url_prefix="/api/admin")


@bp.get("/tasks")
@admin_required
def tasks():
    """批处理任务列表与进度（接口 23）：`limit`、`task_type`。"""
    task_type = request.args.get("task_type") or None

    def action():
        limit = parse_int_arg(request.args, "limit", required=False, default=20)
        return admin_tasks(limit or 20, task_type)

    return respond(action)


@bp.get("/tasks/<int:task_id>/logs")
@admin_required
def task_logs(task_id: int):
    """任务日志明细（接口 24）。"""
    return respond_one(lambda: admin_task_detail(task_id), f"任务 {task_id} 不存在")


@bp.get("/users")
@admin_required
def users():
    """用户列表（接口 25）：`page`、`page_size`；不含口令哈希。"""

    def action():
        page, page_size = parse_pagination(request.args)
        data = list_users(page, page_size)
        data["caliber_note"] = "用户列表仅返回账号基本信息，不包含口令或口令哈希（NR-S-01）。"
        data["sample_size"] = data["total"]
        return data

    return respond(action)


@bp.get("/caliber")
@admin_required
def caliber():
    """系统数据口径配置（接口 26）。"""
    return respond(admin_caliber)


__all__ = ["bp"]
