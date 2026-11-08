# -*- coding: utf-8 -*-
"""创建普通用户账号（演示/测试用；不创建管理员）。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\create_user.py --username demo_user

安全：
    · 口令交互式输入，不回显、不进入命令历史；
    · 角色固定为 user；管理员仍只能由 scripts/create_admin.py 创建；
    · 已存在同名用户时默认拒绝，`--reset-password` 仅重置口令，不改变角色。
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.db import DatabaseError, connection
from app.web.auth import create_user, get_by_username, hash_password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="创建普通用户账号（role 固定为 user）")
    parser.add_argument("--username", required=True, help="登录名")
    parser.add_argument("--nickname", default=None, help="显示名（默认与登录名相同）")
    parser.add_argument("--reset-password", action="store_true", help="用户已存在时仅重置口令")
    args = parser.parse_args(argv)

    password = os.environ.get("APP_USER_PASSWORD") or getpass.getpass("请输入用户口令（不回显）：")
    if not password:
        print("✘ 口令不能为空")
        return 2
    if len(password) < 6:
        print("✘ 口令长度至少 6 位")
        return 2
    if os.environ.get("APP_USER_PASSWORD") is None:
        again = getpass.getpass("请再次输入以确认：")
        if again != password:
            print("✘ 两次输入不一致")
            return 2

    try:
        existing = get_by_username(args.username)
        if existing and not args.reset_password:
            print(f"✘ 用户 {args.username} 已存在（角色 {existing.role}）。如需重置口令请加 --reset-password")
            return 3
        if existing:
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE sys_user SET password_hash=%s, status=1, nickname=%s WHERE user_id=%s",
                        (hash_password(password), args.nickname or existing.nickname or args.username, existing.user_id),
                    )
            print(f"✔ 已重置 {args.username} 的口令（角色保持 {existing.role}；口令未打印）")
            return 0

        user = create_user(args.username, password, args.nickname, role="user")
    except DatabaseError as exc:
        print(f"✘ 数据库访问失败：{exc}")
        return 1

    print(f"✔ 已创建普通用户：user_id={user.user_id} username={user.username} role={user.role}")
    print("  口令已加盐哈希入库（PBKDF2-HMAC-SHA256）；本脚本不回显口令。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
