# -*- coding: utf-8 -*-
"""接口 19「重新生成评价」测试（详细设计 §6.2 第 19 项、§4.3 FR-IE-08）。

覆盖两端：
    A. **在线侧**：POST `/api/spots/{id}/report/regenerate`
       —— 鉴权（未登录 2001 / 非管理员 2002）、景点不存在 3001、
          <100 条评论按 BR-02/BR-03 返回正常业务结果、合格景点登记 pending 任务（202）。
       **该接口不调用模型**（只登记请求），因此本段是确定性的。
    B. **离线侧**：真正强制重生成（`--force`）的效果
       —— 用 **MockClient** 驱动 `run_spot_report(force=True)`，
          验证"带 --force 会重新生成并覆盖；不带则跳过"，全程零 API 消费。

【数据卫生】测试只处理自己构造事实包的那 1 个景点；
结束时按 spot_id 精确删除该景点的 `spot_fact_package` / `spot_report`，
并删除自己登记的 pending 任务与测试用户。**不触碰其它景点的既有数据**。
"""

from __future__ import annotations

import secrets
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.batch.fact_package import run_fact_package
from app.batch.spot_report import run_spot_report
from app.db import connection, query_one
from app.llm.mock import MockClient
from app.web import create_app
from app.web.auth import create_user
from scripts.result_isolation import isolated_spot_results, snapshot

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def max_task_id() -> int:
    """当前最大 task_id（用于界定"本次新建的任务"，比时间戳更可靠）。"""
    row = query_one("SELECT COALESCE(MAX(task_id), 0) AS m FROM analysis_task")
    return int(row["m"])


def cleanup(
    spot_id: int, task_id_floor: int, user_ids: list[int]
) -> tuple[int, int, int]:
    """清理本次测试写入的**任务与用户**。

    清理依据是 **task_id_floor**（测试开始前的最大 task_id）而不是"手工收集的 id"：
    因为 `run_fact_package()` / `run_spot_report()` 会在内部**自己登记任务**，
    只删接口层收集到的任务 id 会漏掉它们（曾因此先后泄漏了 6 条 fact_package 任务行）。
    凡是 `task_id > task_id_floor` 的任务，一定是本次测试产生的 → 一并删除。

    【重要】本函数**不再删除 `spot_report` / `spot_fact_package`**。
    旧实现按 `spot_id` 无条件删这两张表，Stage 5 生成生产评价后会把**真实结果删掉**。
    景点级结果的"临时清空 + 精确还原"统一交给 `result_isolation.isolated_spot_results`。

    返回 `(sys_user 剩余, 删除用户数, analysis_task 剩余)`。
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM task_log WHERE task_id > %s", (task_id_floor,))
            cur.execute("DELETE FROM analysis_task WHERE task_id > %s", (task_id_floor,))
            deleted_users = 0
            if user_ids:
                ph = ",".join(["%s"] * len(user_ids))
                cur.execute(f"DELETE FROM sys_user WHERE user_id IN ({ph})", tuple(user_ids))
                deleted_users = cur.rowcount
            cur.execute("SELECT COUNT(*) AS n FROM sys_user")
            left_users = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM analysis_task")
            left_tasks = int(cur.fetchone()["n"])
    return left_users, deleted_users, left_tasks


def _target_spot_id() -> int:
    """本测试使用的合格景点（与 `_run_body` 内的选点口径**完全一致**）。"""
    row = query_one(
        "SELECT spot_id FROM spot WHERE has_full_evaluation=1 ORDER BY spot_id LIMIT 1"
    )
    return int(row["spot_id"])


def main() -> int:
    """【测试隔离】包住整个测试：快照该景点的景点级结果 → 临时清空 → 跑主体 → **精确还原**。

    为什么必须隔离：本测试会对该景点 `--force` 重新生成评价，并在中间用 `MockClient` 写入
    **mock 评价**。若该景点已有生产评价（Stage 5 之后），不隔离就会**覆盖/删除生产结果**。
    隔离层保证：测试跑在干净状态上，结束后把生产行原样写回。
    """
    target = _target_spot_id()
    with isolated_spot_results([target]) as snap:
        rc = _run_body()

    after = snapshot([target])
    print("\n[隔离收尾] 该景点的景点级结果应精确回到测试前状态：")
    print(f"  fact_package: {len(snap['fact_package'])} → {len(after['fact_package'])}")
    print(f"  spot_report : {len(snap['spot_report'])} → {len(after['spot_report'])}")
    check("隔离层已精确还原（生产 spot_report / fact_package 未被删除或覆盖）",
          len(after["fact_package"]) == len(snap["fact_package"])
          and len(after["spot_report"]) == len(snap["spot_report"]), "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 84)
    print(f"接口 19 测试：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 84)
    return 0 if (rc == 0 and passed == total) else 1


def _run_body() -> int:
    app = create_app()
    admin_id: int | None = None

    print("=" * 84)
    print("接口 19 重新生成评价测试（在线侧零模型调用；离线侧用 MockClient）")
    print("=" * 84)

    # 记录"测试开始前的最大 task_id"：本次测试产生的所有任务（含 run_fact_package /
    # run_spot_report 内部自行登记的任务）都大于它，清理时按此界定，避免漏删。
    task_floor = max_task_id()
    tasks_before = query_one("SELECT COUNT(*) AS n FROM analysis_task")["n"]
    print(f"\n测试前基线：最大 task_id={task_floor}，analysis_task 共 {tasks_before} 行")

    # 选一个"评论量 ≥100"的景点（合格），以及一个"<100"的景点（不合格）
    eligible = query_one(
        "SELECT spot_id, spot_name, review_count FROM spot "
        "WHERE has_full_evaluation=1 ORDER BY spot_id LIMIT 1"
    )
    ineligible = query_one(
        "SELECT spot_id, spot_name, review_count FROM spot "
        "WHERE has_full_evaluation=0 AND review_count > 0 ORDER BY spot_id LIMIT 1"
    )
    spot_id = int(eligible["spot_id"])
    low_id = int(ineligible["spot_id"])
    print(f"\n测试景点：合格 {eligible['spot_name']}({spot_id}, {eligible['review_count']} 条)；"
          f"不合格 {ineligible['spot_name']}({low_id}, {ineligible['review_count']} 条)")

    # ---------- A. 在线侧 ----------
    print("\n[A] 在线接口（只登记请求，不调用模型）")
    client = app.test_client()
    resp = client.post(f"/api/spots/{spot_id}/report/regenerate")
    check("未登录 → 2001（HTTP 401）", resp.status_code == 401 and resp.get_json()["code"] == 2001,
          f"HTTP {resp.status_code} code={resp.get_json()['code']}")

    # 普通用户 → 2002
    uname = f"regen_user_{secrets.token_hex(4)}"
    normal = create_user(uname, "Regen@Test1", "重生成测试", role="user")
    uclient = app.test_client()
    uclient.post("/api/auth/login", json={"username": uname, "password": "Regen@Test1"})
    resp = uclient.post(f"/api/spots/{spot_id}/report/regenerate")
    check("普通用户 → 2002（HTTP 403）", resp.status_code == 403 and resp.get_json()["code"] == 2002,
          f"HTTP {resp.status_code} code={resp.get_json()['code']}")

    # 管理员
    aname = f"regen_admin_{secrets.token_hex(4)}"
    admin = create_user(aname, "Regen@Test2", "重生成管理员", role="admin")
    admin_id = admin.user_id
    aclient = app.test_client()
    aclient.post("/api/auth/login", json={"username": aname, "password": "Regen@Test2"})

    resp = aclient.post("/api/spots/999999999/report/regenerate")
    check("景点不存在 → 3001（HTTP 404）", resp.status_code == 404 and resp.get_json()["code"] == 3001,
          f"HTTP {resp.status_code} code={resp.get_json()['code']}")

    resp = aclient.post(f"/api/spots/{low_id}/report/regenerate")
    body = resp.get_json()
    check("评论量 <100 → 正常业务结果 available=false（HTTP 200 / code=0）",
          resp.status_code == 200 and body["code"] == 0 and body["data"]["available"] is False,
          f"HTTP {resp.status_code} code={body['code']} reason={body['data'].get('reason')}")
    check("不合格景点不登记任务（submitted=false）", body["data"]["submitted"] is False, "")

    resp = aclient.post(f"/api/spots/{spot_id}/report/regenerate")
    body = resp.get_json()
    check("合格景点 → 202 且登记 pending 任务",
          resp.status_code == 202 and body["code"] == 0 and body["data"]["submitted"] is True,
          f"HTTP {resp.status_code} task_id={body['data'].get('task_id')}")
    api_task_id = body["data"].get("task_id")
    if api_task_id:
        row = query_one("SELECT status, task_type FROM analysis_task WHERE task_id=%s", (api_task_id,))
        check("任务行状态为 pending、类型为 spot_report",
              row and row["status"] == "pending" and row["task_type"] == "spot_report",
              f"status={row['status']} type={row['task_type']}")

    # ---------- B. 离线侧：强制重生成（MockClient，零消费） ----------
    print("\n[B] 离线侧：--force 强制重生成（MockClient，零 API 消费）")
    pkg = run_fact_package(spot_ids=[spot_id])
    check("已构造该景点的事实包", pkg["generated"] >= 1 or pkg["skipped"] >= 1, f"{pkg}")

    first = run_spot_report(client=MockClient(), mock=True, spot_ids=[spot_id], force=True)
    check("首次 force 生成成功", first["generated"] == 1 and first["failed"] == 0, f"{first['generated']} 生成 / {first['failed']} 失败")

    again = run_spot_report(client=MockClient(), mock=True, spot_ids=[spot_id])
    check("不带 force 再跑 → 幂等跳过（不重复调用模型）", again["generated"] == 0 and again["skipped"] == 1,
          f"generated={again['generated']} skipped={again['skipped']}")

    forced = run_spot_report(client=MockClient(), mock=True, spot_ids=[spot_id], force=True)
    check("带 force 再跑 → 重新生成（覆盖）", forced["generated"] == 1, f"generated={forced['generated']}")

    row = query_one("SELECT spot_id, prompt_version, model FROM spot_report WHERE spot_id=%s", (spot_id,))
    check("spot_report 已写入该景点（一景一行）", row is not None and int(row["spot_id"]) == spot_id,
          f"prompt={row['prompt_version'] if row else None} model={row['model'] if row else None}")

    # 接口 18 现在应能读到刚生成的评价
    resp = aclient.get(f"/api/spots/{spot_id}/report")
    body = resp.get_json()
    check("接口 18 能读到刚生成的评价（available=true）",
          resp.status_code == 200 and body["data"]["available"] is True,
          f"available={body['data'].get('available')}")

    # ---------- 清理（只清用户与任务；景点级结果由隔离层精确还原） ----------
    left_users, deleted_users, left_tasks = cleanup(
        spot_id, task_floor, [u for u in (admin_id, normal.user_id) if u]
    )
    print(f"\n已清理：删除用户 {deleted_users} 个；sys_user 剩余 {left_users} 行、"
          f"analysis_task 剩余 {left_tasks} 行（spot_report / fact_package 由隔离层还原）")
    check("清理时确实删除了本次创建的用户", deleted_users == 2, f"影响行数 {deleted_users}")
    check("清理后无测试用户残留", left_users == 0, f"剩余 {left_users}")
    # 这条断言是本轮补的：run_fact_package / run_spot_report 会在内部自行登记任务，
    # 早先只删"接口层收集到的 id"导致先后泄漏了 6 条 fact_package 任务行。
    check("清理后 analysis_task 行数回到测试前基线（无任务泄漏）",
          left_tasks == tasks_before, f"前 {tasks_before} → 后 {left_tasks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
