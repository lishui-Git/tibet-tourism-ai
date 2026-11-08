# -*- coding: utf-8 -*-
"""答辩演示账号与权限回归（不打印口令）。

读取本地 `tmp/test_accounts.txt`（已被 .gitignore 排除），验证：
    1. demo_admin / demo_user 存在，角色与状态正确，口令为 PBKDF2 哈希；
    2. 两个账号都能登录，且分别能进入 /admin、/my-qa；
    3. 普通用户访问 /admin 被页面级拒绝（403），调用管理员接口被 API 级拒绝（2002）；
    4. 管理员可以调用管理员接口；
    5. 注册参数注入 role=admin 不会提权（注册一律为 user）；
    6. 退出后会话失效，受保护页面重新要求登录；
    7. 错误口令返回 2004。

安全：只从本地账号文件读取口令，**不打印口令**；临时注册用户会自动删除。
"""

from __future__ import annotations

import io
import re
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import connection, query_one
from app.web import create_app

ACCOUNT_FILE = ROOT / "tmp" / "test_accounts.txt"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def read_accounts() -> dict[str, dict[str, str]]:
    """从本地文件解析账号；只返回用户名/口令/角色，绝不打印。"""
    text = io.open(ACCOUNT_FILE, encoding="utf-8").read()
    accounts: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}

    def flush() -> None:
        username = current.get("username")
        password = current.get("password")
        if username and password:
            accounts[username] = {"password": password, "role": current.get("role", "")}
        current.clear()

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            flush()
            continue
        m = re.match(r"(\w+)=(.+)", line)
        if m:
            current[m.group(1)] = m.group(2).strip()
    flush()
    return accounts


def db_user(username: str) -> dict | None:
    return query_one(
        "SELECT user_id, username, role, status, password_hash FROM sys_user WHERE username=%s",
        (username,),
    )


def main() -> int:
    app = create_app()
    app.config["TESTING"] = True

    print("=" * 78)
    print("答辩演示账号与权限回归（不打印口令）")
    print("=" * 78)

    if not ACCOUNT_FILE.exists():
        print(f"✘ 找不到本地账号文件：{ACCOUNT_FILE}")
        return 2
    accounts = read_accounts()
    admin = accounts.get("demo_admin")
    user = accounts.get("demo_user")
    check("本地账号文件包含 demo_admin", bool(admin), str(ACCOUNT_FILE))
    check("本地账号文件包含 demo_user", bool(user), str(ACCOUNT_FILE))
    if not admin or not user:
        return 2

    print("\n[1] 数据库账号、角色、状态与口令存储")
    for name, expected in (("demo_admin", "admin"), ("demo_user", "user")):
        row = db_user(name)
        check(f"{name} 存在", bool(row), f"user_id={row['user_id'] if row else '—'}")
        if not row:
            continue
        check(f"{name} 角色为 {expected}", row["role"] == expected, f"实际 {row['role']}")
        check(f"{name} 状态启用", int(row["status"]) == 1, f"status={row['status']}")
        stored = str(row["password_hash"] or "")
        check(f"{name} 口令为 PBKDF2 哈希（非明文）",
              stored.startswith("pbkdf2_sha256$") and len(stored) > 80,
              f"前缀 {stored.split('$')[0]}，长度 {len(stored)}")

    print("\n[2] 管理员登录与后台访问")
    c_admin = app.test_client()
    r = c_admin.post("/api/auth/login", json={"username": "demo_admin", "password": admin["password"]})
    body = r.get_json() or {}
    check("管理员登录返回 code=0", body.get("code") == 0, f"HTTP {r.status_code}")
    d = body.get("data") or {}
    check("管理员身份 is_admin=true", d.get("is_admin") is True, f"role={d.get('role')}")
    check("管理员访问 /admin 返回 200", c_admin.get("/admin").status_code == 200, "")
    r = c_admin.get("/api/admin/caliber")
    check("管理员调用 /api/admin/caliber 成功",
          r.status_code == 200 and (r.get_json() or {}).get("code") == 0,
          f"HTTP {r.status_code}")

    print("\n[3] 普通用户登录、我的问答与越权拦截")
    c_user = app.test_client()
    r = c_user.post("/api/auth/login", json={"username": "demo_user", "password": user["password"]})
    body = r.get_json() or {}
    check("普通用户登录返回 code=0", body.get("code") == 0, f"HTTP {r.status_code}")
    d = body.get("data") or {}
    check("普通用户 is_admin=false", d.get("is_admin") is False, f"role={d.get('role')}")
    check("普通用户访问 /my-qa 返回 200", c_user.get("/my-qa").status_code == 200, "")
    check("普通用户访问 /admin 被拒绝（403）", c_user.get("/admin").status_code == 403, "")
    r = c_user.get("/api/admin/caliber")
    check("普通用户调用管理员接口被拒绝（2002）",
          (r.get_json() or {}).get("code") == 2002, f"HTTP {r.status_code}")

    print("\n[4] 注册参数注入 role=admin 不得提权")
    probe = f"qa_role_probe_{int(time.time())}"
    c_probe = app.test_client()
    r = c_probe.post("/api/auth/register", json={
        "username": probe, "password": "Temp#Pass123", "nickname": "权限探针", "role": "admin",
    })
    body = r.get_json() or {}
    check("注册请求返回 code=0", body.get("code") == 0, f"HTTP {r.status_code}")
    row = query_one("SELECT role FROM sys_user WHERE username=%s", (probe,))
    check("注册用户角色实际为 user（role 参数被忽略）",
          bool(row) and row["role"] == "user", f"实际 {row['role'] if row else '未找到'}")
    if row:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sys_user WHERE username=%s", (probe,))
        check("临时注册用户已清理", query_one("SELECT user_id FROM sys_user WHERE username=%s", (probe,)) is None, "")

    print("\n[5] 错误口令与退出")
    c_wrong = app.test_client()
    r = c_wrong.post("/api/auth/login", json={"username": "demo_user", "password": "definitely-wrong"})
    check("错误口令返回 2004", (r.get_json() or {}).get("code") == 2004, f"HTTP {r.status_code}")

    r = c_user.post("/api/auth/logout")
    check("普通用户退出成功", r.status_code == 200 and (r.get_json() or {}).get("code") == 0, "")
    check("退出后 /my-qa 重新要求登录（302）", c_user.get("/my-qa").status_code == 302, "")
    check("退出后 /api/auth/me 返回 2001", (c_user.get("/api/auth/me").get_json() or {}).get("code") == 2001, "")

    r = c_admin.post("/api/auth/logout")
    check("管理员退出成功", r.status_code == 200 and (r.get_json() or {}).get("code") == 0, "")
    check("退出后 /admin 重新要求登录（302）", c_admin.get("/admin").status_code == 302, "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 78)
    print(f"演示账号与权限测试：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 78)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
