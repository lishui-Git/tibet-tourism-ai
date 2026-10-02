# -*- coding: utf-8 -*-
"""创建管理员账号（首次部署/答辩演示前的引导脚本）。

背景：设计规定"注册一律为普通用户"，管理员只能由引导脚本创建，
避免任何自助提权路径。

用法：
    # 交互式（推荐：口令不回显、不进命令历史）
    .\\.venv\\Scripts\\python.exe scripts\\create_admin.py --username admin

    # 非交互（口令从环境变量读取，同样不落命令行历史）
    $env:APP_ADMIN_PASSWORD = "……"; .\\.venv\\Scripts\\python.exe scripts\\create_admin.py --username admin

安全：
    · 口令加盐哈希后入库，脚本**不打印口令**；
    · 已存在同名用户时默认拒绝（`--reset-password` 可显式改密）。
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
    parser = argparse.ArgumentParser(description="创建/更新管理员账号（口令加盐哈希）")
    parser.add_argument("--username", required=True, help="管理员登录名")
    parser.add_argument("--nickname", default=None, help="显示名（默认与登录名相同）")
    parser.add_argument("--reset-password", action="store_true", help="用户已存在时重置口令与角色")
    args = parser.parse_args(argv)

    password = os.environ.get("APP_ADMIN_PASSWORD") or getpass.getpass("请输入管理员口令（不回显）：")
    if not password:
        print("✘ 口令不能为空")
        return 2
    if len(password) < 6:
        print("✘ 口令长度至少 6 位")
        return 2
    if os.environ.get("APP_ADMIN_PASSWORD") is None:
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
                        "UPDATE sys_user SET password_hash=%s, role='admin', status=1, nickname=%s WHERE user_id=%s",
                        (hash_password(password), args.nickname or existing.nickname or args.username, existing.user_id),
                    )
            print(f"✔ 已重置 {args.username} 的口令并把角色设为 admin（口令未打印、未落库为明文）")
            return 0

        user = create_user(args.username, password, args.nickname, role="admin")
    except DatabaseError as exc:
        print(f"✘ 数据库访问失败：{exc}")
        return 1

    print(f"✔ 已创建管理员：user_id={user.user_id} username={user.username} role={user.role}")
    print("  口令已加盐哈希入库（PBKDF2-HMAC-SHA256）；请妥善保管，本脚本不回显口令。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
