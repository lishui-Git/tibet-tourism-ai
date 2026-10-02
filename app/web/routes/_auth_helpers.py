# -*- coding: utf-8 -*-
"""接口级鉴权装饰器（详细设计 §13.1）。

设计约定：
    · `visitor` = 未登录（无会话记录）
    · `user`    = 已登录
    · `admin`   = 已登录且 `role == 'admin'`

两个装饰器：
    `login_required`  未登录 → 2001（HTTP 401）
    `admin_required`  未登录 → 2001；已登录但非管理员 → 2002（HTTP 403）

**为什么检查数据库而不是只信 Cookie**：会话里只存 `user_id`，
每次请求都回查 `sys_user` 以确认"用户仍然存在且未被停用"——
否则把用户停用后，旧 Cookie 仍能继续访问（越权）。
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from flask import session

from app.web.auth import User, get_by_id
from app.web.response import CODE_FORBIDDEN, CODE_NOT_LOGGED_IN, fail

SESSION_USER_KEY = "user_id"


def current_user() -> User | None:
    """取当前登录用户（每次请求回查数据库，确保帐号仍然有效）。"""
    user_id = session.get(SESSION_USER_KEY)
    if not user_id:
        return None
    try:
        user = get_by_id(int(user_id))
    except (TypeError, ValueError):
        return None
    if user is None or not user.is_active:
        # 用户被删除或停用：清掉会话，避免"幽灵登录"
        session.pop(SESSION_USER_KEY, None)
        return None
    return user


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """要求已登录。"""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if current_user() is None:
            return fail(CODE_NOT_LOGGED_IN, "未登录或登录已失效，请先登录")
        return view(*args, **kwargs)

    return wrapper


def admin_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """要求管理员角色（未登录 2001；非管理员 2002）。"""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        user = current_user()
        if user is None:
            return fail(CODE_NOT_LOGGED_IN, "未登录或登录已失效，请先登录")
        if not user.is_admin:
            return fail(CODE_FORBIDDEN, "需要管理员权限")
        return view(*args, **kwargs)

    return wrapper


def login_session(user: User) -> None:
    """写入会话（只存 user_id，其余每次回查数据库）。"""
    session[SESSION_USER_KEY] = user.user_id


def logout_session() -> None:
    """清除会话。"""
    session.pop(SESSION_USER_KEY, None)


__all__ = [
    "SESSION_USER_KEY",
    "current_user",
    "login_required",
    "admin_required",
    "login_session",
    "logout_session",
]
