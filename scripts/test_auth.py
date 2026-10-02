# -*- coding: utf-8 -*-
"""认证与鉴权测试（C4 组件 `C-API-01`，详细设计 §13.1 / §6.2 第 1–4 项）。

覆盖：
    1. 口令哈希：加盐、不同口令不同哈希、校验通过/失败、格式异常不抛异常
    2. 注册：成功 / 重复 2003 / 参数问题 1001
    3. 登录：成功 / 口令错误 2004 / 不存在 2004（不区分，防用户名枚举）
    4. 会话：/me 前后状态；注销后失效
    5. 鉴权：未登录 2001；普通用户访问管理接口 2002；管理员可访问
    6. 接口 25 用户列表**不含口令哈希**

【数据卫生】测试用户使用随机用户名，测试结束**按 user_id 精确删除**自己创建的用户，
不触碰其它数据，也不写任何分析结果表。
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
from app.web.auth import hash_password, verify_password

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def post(client, path: str, payload: dict):
    resp = client.post(path, data=json.dumps(payload), content_type="application/json")
    return resp.status_code, resp.get_json()


def cleanup(user_ids: list[int]) -> None:
    """精确删除本次创建的测试用户（只按 user_id，不用 LIKE，避免误删）。"""
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
    created: list[int] = []
    suffix = secrets.token_hex(4)
    normal_user = f"test_user_{suffix}"
    admin_user = f"test_admin_{suffix}"
    password = "Test@123456"

    print("=" * 80)
    print("认证与鉴权测试（本地 test_client；不调用任何模型）")
    print("=" * 80)

    # ---------- 1. 口令哈希 ----------
    print("\n[1] 口令加盐哈希")
    h1 = hash_password(password)
    h2 = hash_password(password)
    check("哈希串格式正确（pbkdf2_sha256$迭代$盐$摘要）", h1.startswith("pbkdf2_sha256$") and len(h1.split("$")) == 4, h1[:32] + "…")
    check("同一口令两次哈希不同（盐随机）", h1 != h2)
    check("正确口令校验通过", verify_password(password, h1))
    check("错误口令校验失败", not verify_password(password + "x", h1))
    check("存储串非法时不抛异常", verify_password(password, "garbage") is False)
    check("空口令/空存储串返回 False", not verify_password("", h1) and not verify_password(password, ""))

    # ---------- 2. 注册 ----------
    print("\n[2] 注册（接口 1）")
    client = app.test_client()
    status, body = post(client, "/api/auth/register", {"username": normal_user, "password": password, "nickname": "测试用户"})
    check("注册成功 code=0", status == 200 and body["code"] == 0, f"HTTP {status} code={body['code']}")
    if status == 200 and body["code"] == 0:
        created.append(int(body["data"]["user_id"]))
        check("响应不含口令/哈希字段", "password" not in json.dumps(body) and "pbkdf2" not in json.dumps(body), json.dumps(body["data"], ensure_ascii=False))
        check("注册角色为普通用户 user", body["data"]["role"] == "user", body["data"]["role"])

    status, body = post(client, "/api/auth/register", {"username": normal_user, "password": password})
    check("重复用户名 → 2003（HTTP 409）", status == 409 and body["code"] == 2003, f"HTTP {status} code={body['code']}")

    status, body = post(client, "/api/auth/register", {"username": "ab", "password": password})
    check("用户名过短 → 1001", status == 400 and body["code"] == 1001, f"HTTP {status} code={body['code']}")
    status, body = post(client, "/api/auth/register", {"username": f"x_{suffix}", "password": "123"})
    check("口令过短 → 1001", status == 400 and body["code"] == 1001, f"HTTP {status} code={body['code']}")

    # 注册接口"不能自助创建管理员"：尝试传 role 应被忽略
    role_user = f"test_role_{suffix}"
    status, body = post(client, "/api/auth/register", {"username": role_user, "password": password, "role": "admin"})
    if status == 200 and body["code"] == 0:
        created.append(int(body["data"]["user_id"]))
        check("注册时传入 role=admin 被忽略（不可自助提权）", body["data"]["role"] == "user", body["data"]["role"])

    # ---------- 3. 登录 / 会话 ----------
    print("\n[3] 登录与会话（接口 2/3/4）")
    status, body = post(client, "/api/auth/login", {"username": normal_user, "password": password + "bad"})
    check("口令错误 → 2004（HTTP 401）", status == 401 and body["code"] == 2004, f"HTTP {status} code={body['code']}")

    status, body = post(client, "/api/auth/login", {"username": f"no_such_{suffix}", "password": password})
    check("用户不存在 → 同样 2004（不暴露用户名是否存在）", status == 401 and body["code"] == 2004, f"HTTP {status} code={body['code']}")

    status, body = post(client, "/api/auth/login", {"username": normal_user, "password": password})
    check("登录成功 code=0", status == 200 and body["code"] == 0, f"HTTP {status} code={body['code']}")

    resp = client.get("/api/auth/me")
    check("登录后 /me 返回当前用户", resp.status_code == 200 and resp.get_json()["data"]["username"] == normal_user, json.dumps(resp.get_json()["data"], ensure_ascii=False))

    # ---------- 4. 鉴权 ----------
    print("\n[4] 鉴权（§13.1）")
    resp = client.get("/api/admin/tasks")
    check("普通用户访问管理接口 → 2002（HTTP 403）", resp.status_code == 403 and resp.get_json()["code"] == 2002, f"HTTP {resp.status_code} code={resp.get_json()['code']}")

    # 管理员：用脚本同一套函数创建（不经过注册接口）
    from app.web.auth import create_user

    admin = create_user(admin_user, password, "测试管理员", role="admin")
    created.append(admin.user_id)
    admin_client = app.test_client()
    status, body = post(admin_client, "/api/auth/login", {"username": admin_user, "password": password})
    check("管理员登录成功", status == 200 and body["code"] == 0, f"HTTP {status} code={body['code']}")

    resp = admin_client.get("/api/admin/tasks")
    check("管理员访问任务列表 → 200 code=0", resp.status_code == 200 and resp.get_json()["code"] == 0, f"HTTP {resp.status_code}")

    resp = admin_client.get("/api/admin/users")
    users_body = resp.get_json()
    check("管理员访问用户列表 → 200 code=0", resp.status_code == 200 and users_body["code"] == 0, f"HTTP {resp.status_code}")
    raw = json.dumps(users_body, ensure_ascii=False)
    check("用户列表不含 password_hash / pbkdf2", "password_hash" not in raw and "pbkdf2" not in raw, "已检查响应全文")

    resp = admin_client.get("/api/admin/caliber")
    check("管理员访问口径配置 → 200 code=0", resp.status_code == 200 and resp.get_json()["code"] == 0, f"HTTP {resp.status_code}")

    # limit 边界一致性：0 与负数必须同样被拒绝，而不是被静默吞掉回落到默认值。
    # （实测发现的缺口：limit=-1 与 limit=99999 会被 1002 拒绝，只有 0 被当作"没传"。）
    resp = admin_client.get("/api/admin/tasks?limit=0")
    check("limit=0 → 400/1002（不静默回落到默认 20）",
          resp.status_code == 400 and resp.get_json()["code"] == 1002,
          f"HTTP {resp.status_code} code={resp.get_json().get('code')}")
    resp = admin_client.get("/api/admin/tasks?limit=-1")
    check("limit=-1 → 400/1002",
          resp.status_code == 400 and resp.get_json()["code"] == 1002,
          f"HTTP {resp.status_code} code={resp.get_json().get('code')}")
    resp = admin_client.get("/api/admin/tasks?limit=1")
    body1 = resp.get_json()
    check("limit=1 → 只返回 1 条（参数确实生效）",
          resp.status_code == 200 and len((body1.get("data") or {}).get("items") or []) <= 1,
          f"items={len((body1.get('data') or {}).get('items') or [])}")

    # ---------- 5. 注销 ----------
    print("\n[5] 注销（接口 3）")
    resp = client.post("/api/auth/logout")
    check("注销成功", resp.status_code == 200 and resp.get_json()["code"] == 0, f"HTTP {resp.status_code}")
    resp = client.get("/api/auth/me")
    check("注销后 /me → 2001（HTTP 401）", resp.status_code == 401 and resp.get_json()["code"] == 2001, f"HTTP {resp.status_code} code={resp.get_json()['code']}")

    # ---------- 6. 停用帐号后旧会话立即失效 ----------
    print("\n[6] 停用帐号后旧会话失效（越权防护）")
    # 重新登录一个普通用户并取得有效会话，然后停用该帐号，再用同一个 client 访问受保护接口
    session_client = app.test_client()
    status, body = post(session_client, "/api/auth/login", {"username": role_user, "password": password})
    check("（准备）普通用户登录并持有会话", status == 200 and body["code"] == 0, f"HTTP {status}")
    resp = session_client.get("/api/auth/me")
    check("（准备）会话有效", resp.status_code == 200, f"HTTP {resp.status_code}")

    role_user_id = int(body["data"]["user_id"]) if status == 200 else None
    if role_user_id:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE sys_user SET status=0 WHERE user_id=%s", (role_user_id,))
        resp = session_client.get("/api/auth/me")
        check(
            "帐号停用后旧会话立即失效 → 2001（HTTP 401）",
            resp.status_code == 401 and resp.get_json()["code"] == 2001,
            f"HTTP {resp.status_code} code={resp.get_json()['code']}",
        )
        resp = session_client.get("/api/admin/tasks")
        check(
            "停用帐号无法访问受保护接口",
            resp.status_code == 401,
            f"HTTP {resp.status_code}",
        )
    else:
        check("帐号停用后旧会话立即失效 → 2001（HTTP 401）", False, "准备阶段未取得会话")

    cleanup(created)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 80)
    print(f"认证测试：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 80)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
