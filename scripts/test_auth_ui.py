# -*- coding: utf-8 -*-
"""登录页与页面级角色权限测试（阶段三）。

覆盖：
    1. 统一登录页结构（左右分栏、密码显示切换、统一入口文案）
    2. 未登录访问 /admin、/my-qa → 302 登录页，并带 next
    3. 普通用户：/my-qa 200、/admin 403、管理 API 403、自己的问答历史 200
    4. 管理员：/admin 200、/my-qa 200、管理 API 200
    5. 注销后受保护页面重新要求登录

【数据卫生】测试用户使用随机用户名，结束按 user_id 精确删除；不触碰其它用户。
"""

from __future__ import annotations

import json
import secrets
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.db import connection
from app.web import create_app
from app.web.auth import create_user

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def post(client, path: str, payload: dict):
    resp = client.post(path, data=json.dumps(payload), content_type="application/json")
    return resp.status_code, resp.get_json()


def cleanup(user_ids: list[int]) -> None:
    if not user_ids:
        return
    placeholders = ",".join(["%s"] * len(user_ids))
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM sys_user WHERE user_id IN ({placeholders})", tuple(user_ids))
            cur.execute(f"SELECT COUNT(*) AS n FROM sys_user WHERE user_id IN ({placeholders})", tuple(user_ids))
            left = int(cur.fetchone()["n"])
    print(f"\n已清理测试用户 {user_ids}（剩余 {left} 个，应为 0）")


def main() -> int:
    app = create_app()
    app.config["TESTING"] = True
    suffix = secrets.token_hex(4)
    normal_user = f"ui_user_{suffix}"
    admin_user = f"ui_admin_{suffix}"
    password = "UiTest@2026"
    created: list[int] = []

    print("=" * 84)
    print("登录页与页面级角色权限测试（test_client；不调用模型）")
    print("=" * 84)

    # ---------- 1. 登录页结构 ----------
    print("\n[1] 统一登录页结构")
    c = app.test_client()
    r = c.get("/login")
    html = r.get_data(as_text=True)
    check("登录页返回 200", r.status_code == 200, f"HTTP {r.status_code}")
    check("左右分栏容器存在", "login-split" in html and "login-visual" in html and "login-panel" in html)
    check("统一入口文案存在", "普通用户与管理员统一入口" in html)
    check("密码显示/隐藏按钮存在", 'id="toggle-pass"' in html)
    check("表单为 submit 结构（Enter 可提交）", 'id="login-form"' in html and 'type="submit"' in html)

    # ---------- 2. 未登录访问受保护页面 ----------
    print("\n[2] 未登录访问受保护页面")
    for path, next_value in (("/admin", "/admin"), ("/my-qa", "/my-qa")):
        r = c.get(path)
        loc = r.headers.get("Location") or ""
        check(f"{path} 未登录 → 302", r.status_code == 302, f"HTTP {r.status_code} → {loc}")
        check(f"{path} 跳登录页且带 next", "/login" in loc and next_value in loc, loc)

    # ---------- 3. 普通用户 ----------
    print("\n[3] 普通用户")
    user = create_user(normal_user, password, "UI 测试用户", role="user")
    created.append(user.user_id)
    user_client = app.test_client()
    status, body = post(user_client, "/api/auth/login", {"username": normal_user, "password": password})
    check("普通用户登录成功", status == 200 and body["code"] == 0, f"HTTP {status} code={body['code']}")

    r = user_client.get("/my-qa")
    check("普通用户 /my-qa → 200", r.status_code == 200, f"HTTP {r.status_code}")
    r = user_client.get("/admin")
    check("普通用户 /admin → 403", r.status_code == 403, f"HTTP {r.status_code}")
    check("403 页有中文说明", "无权访问管理后台" in r.get_data(as_text=True))
    r = user_client.get("/api/admin/users")
    check("普通用户管理 API → 403/2002",
          r.status_code == 403 and r.get_json()["code"] == 2002,
          f"HTTP {r.status_code} code={r.get_json().get('code')}")
    r = user_client.get("/api/qa/history?page=1&page_size=10")
    check("普通用户自己的问答历史 → 200/0",
          r.status_code == 200 and r.get_json()["code"] == 0,
          f"HTTP {r.status_code} code={r.get_json().get('code')}")

    # ---------- 4. 管理员 ----------
    print("\n[4] 管理员")
    admin = create_user(admin_user, password, "UI 测试管理员", role="admin")
    created.append(admin.user_id)
    admin_client = app.test_client()
    status, body = post(admin_client, "/api/auth/login", {"username": admin_user, "password": password})
    check("管理员登录成功", status == 200 and body["code"] == 0, f"HTTP {status} code={body['code']}")
    r = admin_client.get("/admin")
    check("管理员 /admin → 200", r.status_code == 200, f"HTTP {r.status_code}")
    r = admin_client.get("/my-qa")
    check("管理员 /my-qa → 200", r.status_code == 200, f"HTTP {r.status_code}")
    r = admin_client.get("/api/admin/users?page=1&page_size=5")
    raw = json.dumps(r.get_json(), ensure_ascii=False)
    check("管理员用户列表 → 200 且不含口令哈希",
          r.status_code == 200 and r.get_json()["code"] == 0
          and "password_hash" not in raw and "pbkdf2" not in raw,
          f"HTTP {r.status_code}")

    # ---------- 5. 注销 ----------
    print("\n[5] 注销")
    r = user_client.post("/api/auth/logout")
    check("注销成功", r.status_code == 200 and r.get_json()["code"] == 0)
    r = user_client.get("/my-qa")
    check("注销后 /my-qa → 302", r.status_code == 302, f"HTTP {r.status_code}")
    r = user_client.get("/api/auth/me")
    check("注销后 /api/auth/me → 2001", r.status_code == 401 and r.get_json()["code"] == 2001,
          f"HTTP {r.status_code} code={r.get_json().get('code')}")

    cleanup(created)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 84)
    print(f"登录与页面权限测试：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 84)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
