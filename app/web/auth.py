# -*- coding: utf-8 -*-
"""用户与会话（C4 组件 `C-API-01`，详细设计 §13.1）。

【职责】
    · 口令的加盐哈希与校验（NR-S-01：**不存明文**）
    · 用户的注册 / 查询 / 登录时间更新（全部参数化 SQL）
    · 与 HTTP 层无关的纯业务逻辑，便于单测与复用（`routes/auth.py` 只做参数与响应）

【口令方案】PBKDF2-HMAC-SHA256（Python 标准库 `hashlib`），
    存储格式：`pbkdf2_sha256$<迭代次数>$<十六进制盐>$<十六进制摘要>`
    · 自带算法名与迭代次数，将来提高迭代次数不会让旧口令失效（校验时按存储值计算）；
    · **不引入第三方依赖**（`requirements.txt` 保持冻结，不加 bcrypt/passlib）。

【为什么用 Flask 签名 Cookie 会话】设计 §13.1 要求"登录成功写入服务端会话，前端携带 Cookie"。
    Flask 的 `session` 是**签名后的客户端 Cookie**（密钥来自 `.env`），
    对本项目"权限从简、不引入会话存储中间件"的约束是合适的折中；
    退出即清除 Cookie 中的会话内容。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.db import connection, query_one

# ---------------------------------------------------------------------------
# 一、口令哈希
# ---------------------------------------------------------------------------

ALGORITHM = "pbkdf2_sha256"
# 迭代次数：本地单机部署下的折中值（约 0.1s 量级）；写入存储串，将来可提高而不影响旧口令
ITERATIONS = 200_000
SALT_BYTES = 16


def hash_password(password: str) -> str:
    """生成加盐哈希串（明文口令不落库、不写日志）。"""
    if not password:
        raise ValueError("口令不能为空")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"{ALGORITHM}${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令是否与存储的哈希串匹配。

    用 `hmac.compare_digest` 做定长比较，避免时序侧信道；
    存储串格式异常时返回 False（不抛异常，避免把格式问题暴露成 500）。
    """
    if not password or not stored:
        return False
    try:
        algorithm, iterations_text, salt_hex, digest_hex = stored.split("$")
        if algorithm != ALGORITHM:
            return False
        iterations = int(iterations_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# 二、用户数据访问
# ---------------------------------------------------------------------------


@dataclass
class User:
    """对外暴露的用户信息（**不含 password_hash**）。"""

    user_id: int
    username: str
    nickname: str | None
    role: str
    status: int

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_active(self) -> bool:
        return int(self.status) == 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "nickname": self.nickname,
            "role": self.role,
            "is_admin": self.is_admin,
        }


def _to_user(row: dict | None) -> User | None:
    if not row:
        return None
    return User(
        user_id=int(row["user_id"]),
        username=row["username"],
        nickname=row["nickname"],
        role=row["role"],
        status=int(row["status"]),
    )


def get_by_username(username: str) -> User | None:
    return _to_user(
        query_one(
            "SELECT user_id, username, nickname, role, status FROM sys_user WHERE username = %s",
            (username,),
        )
    )


def get_by_id(user_id: int) -> User | None:
    return _to_user(
        query_one(
            "SELECT user_id, username, nickname, role, status FROM sys_user WHERE user_id = %s",
            (user_id,),
        )
    )


def username_exists(username: str) -> bool:
    row = query_one("SELECT 1 AS x FROM sys_user WHERE username = %s", (username,))
    return bool(row)


def create_user(username: str, password: str, nickname: str | None = None, role: str = "user") -> User:
    """创建用户（口令加盐哈希后入库）。

    :raises ValueError: 口令为空
    """
    password_hash = hash_password(password)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sys_user (username, password_hash, nickname, role, status)
                VALUES (%s, %s, %s, %s, 1)
                """,
                (username, password_hash, nickname or username, role),
            )
            user_id = int(cur.lastrowid)
    return User(user_id=user_id, username=username, nickname=nickname or username, role=role, status=1)


def authenticate(username: str, password: str) -> User | None:
    """校验用户名口令。成功返回用户，失败返回 None（不区分"用户不存在"与"口令错误"）。"""
    row = query_one(
        "SELECT user_id, username, nickname, role, status, password_hash FROM sys_user WHERE username = %s",
        (username,),
    )
    if not row:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    user = _to_user(row)
    if user is None or not user.is_active:
        return None
    return user


def touch_login(user_id: int) -> None:
    """更新最近登录时间（`sys_user.last_login_at`）。"""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sys_user SET last_login_at = %s WHERE user_id = %s",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id),
            )


def list_users(page: int, page_size: int) -> dict[str, Any]:
    """用户列表（管理端，接口 25）。**不返回 password_hash**。"""
    from app.web.data_access import fetch_all, scalar

    total = scalar("SELECT COUNT(*) FROM sys_user")
    items = fetch_all(
        """
        SELECT user_id, username, nickname, role, status, created_at, last_login_at
          FROM sys_user
         ORDER BY user_id
         LIMIT %s OFFSET %s
        """,
        (page_size, (page - 1) * page_size),
    )
    return {"items": items, "page": page, "page_size": page_size, "total": total}


__all__ = [
    "User",
    "hash_password",
    "verify_password",
    "get_by_username",
    "get_by_id",
    "username_exists",
    "create_user",
    "authenticate",
    "touch_login",
    "list_users",
    "ALGORITHM",
    "ITERATIONS",
]
