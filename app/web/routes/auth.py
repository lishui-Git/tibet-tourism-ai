# -*- coding: utf-8 -*-
"""认证接口（C4 组件 `C-API-01`，详细设计 §6.2 第 1–4 项）。

    POST /api/auth/register   用户注册（否）
    POST /api/auth/login      登录（否）
    POST /api/auth/logout     注销（是）
    GET  /api/auth/me         当前用户与角色（是）

安全要点（§13.1 / §13.2）：
    · 口令加盐哈希后入库，**响应与日志都不含口令与哈希**（NR-S-01）；
    · 登录失败统一返回 2004「用户名或密码错误」，**不区分**"用户不存在"（避免用户名枚举）；
    · 注册重复 → 2003；参数问题 → 1001；
    · 会话只存 `user_id`，其余每次回查数据库（帐号停用即失效）。
"""

from __future__ import annotations

from flask import Blueprint, request

from app.db import DatabaseError
from app.web.auth import authenticate, create_user, get_by_username, touch_login, username_exists
from app.web.response import (
    CODE_BAD_CREDENTIALS,
    CODE_DB_ERROR,
    CODE_INTERNAL_ERROR,
    CODE_PARAM_INVALID,
    CODE_USER_EXISTS,
    fail,
    ok,
)
from app.web.routes._auth_helpers import current_user, login_required, login_session, logout_session

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

# 口令与用户名长度约束（输入校验，§13.2）
USERNAME_MIN, USERNAME_MAX = 3, 32
PASSWORD_MIN, PASSWORD_MAX = 6, 64
NICKNAME_MAX = 32


def _payload() -> dict:
    """兼容 JSON 与表单两种提交方式（前端用 axios 默认 JSON）。"""
    return request.get_json(silent=True) or request.form.to_dict() or {}


@bp.post("/register")
def register():
    """用户注册（接口 1）。"""
    data = _payload()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    nickname = str(data.get("nickname") or "").strip() or None

    if not username or not password:
        return fail(CODE_PARAM_INVALID, "用户名与口令为必填项")
    if not (USERNAME_MIN <= len(username) <= USERNAME_MAX):
        return fail(CODE_PARAM_INVALID, f"用户名长度需在 {USERNAME_MIN}–{USERNAME_MAX} 之间")
    if not (PASSWORD_MIN <= len(password) <= PASSWORD_MAX):
        return fail(CODE_PARAM_INVALID, f"口令长度需在 {PASSWORD_MIN}–{PASSWORD_MAX} 之间")
    if nickname and len(nickname) > NICKNAME_MAX:
        return fail(CODE_PARAM_INVALID, f"昵称长度不得超过 {NICKNAME_MAX}")

    try:
        if username_exists(username):
            return fail(CODE_USER_EXISTS, "用户名已存在")
        # 注册一律为普通用户；管理员只能由 scripts/create_admin.py 创建（避免自助提权）
        user = create_user(username, password, nickname, role="user")
    except DatabaseError as exc:
        return fail(CODE_DB_ERROR, f"数据库访问失败：{exc}")
    except Exception as exc:
        return fail(CODE_INTERNAL_ERROR, f"服务内部错误：{type(exc).__name__}")

    return ok(user.as_dict(), message="注册成功")


@bp.post("/login")
def login():
    """登录（接口 2）。"""
    data = _payload()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not username or not password:
        return fail(CODE_PARAM_INVALID, "用户名与口令为必填项")

    try:
        user = authenticate(username, password)
    except DatabaseError as exc:
        return fail(CODE_DB_ERROR, f"数据库访问失败：{exc}")

    if user is None:
        # 统一提示，不暴露"用户是否存在"
        return fail(CODE_BAD_CREDENTIALS, "用户名或口令错误")

    login_session(user)
    try:
        touch_login(user.user_id)
    except DatabaseError:
        # 登录时间更新失败不影响登录本身（属可容忍的辅助写入）
        pass
    return ok(user.as_dict(), message="登录成功")


@bp.post("/logout")
@login_required
def logout():
    """注销（接口 3）：清除会话，即刻失效。"""
    logout_session()
    return ok({"logged_out": True}, message="已注销")


@bp.get("/me")
@login_required
def me():
    """当前用户与角色（接口 4）。"""
    user = current_user()
    if user is None:  # 理论上 login_required 已拦截，这里做二次保险
        return fail(CODE_BAD_CREDENTIALS, "未登录或登录已失效，请先登录")
    return ok(user.as_dict())


__all__ = ["bp"]
