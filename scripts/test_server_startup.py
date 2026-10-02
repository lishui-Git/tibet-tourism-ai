# -*- coding: utf-8 -*-
"""真实进程启动验证：`python run.py` 起得来、静态资源与接口都能通过**真实 HTTP** 访问。

## 为什么需要它
其余所有测试都用 Flask 的 `test_client()`——它在**进程内**直接调用 WSGI 应用，
不经过 socket、不经过 Werkzeug 开发服务器、也不真正读取 `static/` 目录。
以下问题**只有真的起一个服务器才能发现**：
    · `run.py` 启动参数写错（host/port/debug）；
    · 静态文件路径或 `static_url_path` 配错 → 页面样式/图表全挂（test_client 也会 200，但内容不对）；
    · 模板里引用的静态资源根本不存在；
    · 真实 HTTP 下的错误响应不是 JSON 信封。

演示当天用的是**真实服务器**，所以这个边界值得单独验证一次。

## 做法（零 API 消费）
    ① 以子进程启动 `run.py`（stdout/stderr 重定向到文件，避免管道限制）；
    ② 轮询 `/healthz` 直到就绪（最多约 30 秒）；
    ③ 逐个请求 8 个页面 + 关键接口 + 3 个静态资源，断言状态码与 Content-Type；
    ④ 断言错误路径在真实 HTTP 下仍是 JSON 信封；
    ⑤ 关停子进程，并断言日志里**没有任何异常/traceback**。

## 说明
本脚本会在 `127.0.0.1:5000` 起服务（与手册一致）。若该端口已被占用，
脚本会明确报错而不是静默连到别人的服务上。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe")

# 用一个**专用端口**，避免与"正在给演示开的服务"抢 5000。
# 为什么不用 run.py 的默认端口：本脚本要能随时运行，不能要求"先把演示服务关掉"。
TEST_PORT = 5011
BASE = f"http://127.0.0.1:{TEST_PORT}"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def fetch(path: str, timeout: float = 20.0):
    """返回 (status, content_type, body)；HTTP 错误也正常返回而不抛异常。"""
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, (resp.headers.get("Content-Type") or ""), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.headers.get("Content-Type") or "" if exc.headers else ""), exc.read()


def main() -> int:
    print("=" * 88)
    print("真实进程启动验证（python run.py；零 API 消费）")
    print("=" * 88)

    # 端口占用检查：避免"连到了别人的服务"这种假通过
    import socket

    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", TEST_PORT)) == 0:
            check(f"端口 {TEST_PORT} 未被占用（否则无法区分是本脚本起的服务）", False, "已被占用")
            return 1
    check(f"端口 {TEST_PORT} 未被占用", True, "")

    out_path = ROOT / "tmp" / "_server_out.txt"
    err_path = ROOT / "tmp" / "_server_err.txt"
    for p in (out_path, err_path):
        p.unlink(missing_ok=True)

    # 【必须关掉 debug】Flask 在 debug 下会启动 reloader，拉起**子进程**；
    # 只 terminate 父进程会留下子进程继续占着端口（实测踩到：脚本结束后 5000 仍被占用）。
    # 注意项目的配置变量名是 `APP_FLASK_DEBUG`（config.py 用 _get_app_bool("FLASK_DEBUG")），
    # 因此这里设 APP_ 前缀那个才生效。
    env = {
        **os.environ,
        "APP_FLASK_DEBUG": "0",
        "APP_FLASK_PORT": str(TEST_PORT),
        "APP_FLASK_HOST": "127.0.0.1",
    }
    # 用 with 打开：子进程只继承文件描述符副本，Python 侧句柄必须及时关闭，
    # 否则 Windows 上删除临时文件会报 WinError 32（"另一个程序正在使用此文件"）。
    with out_path.open("w", encoding="utf-8") as out_f, err_path.open("w", encoding="utf-8") as err_f:
        proc = subprocess.Popen(
            [PYTHON, "run.py"],
            cwd=str(ROOT),
            stdout=out_f,
            stderr=err_f,
            env=env,
        )
    print(f"  已启动 run.py（pid={proc.pid}，端口 {TEST_PORT}，debug 关闭），等待就绪…")

    ready = False
    for _ in range(30):
        time.sleep(1)
        if proc.poll() is not None:
            break
        try:
            status, _, _ = fetch("/healthz", timeout=5)
            if status == 200:
                ready = True
                break
        except Exception:
            continue

    check("服务器在 30 秒内就绪（/healthz 返回 200）", ready, f"pid={proc.pid}")

    try:
        if not ready:
            print("  服务未就绪，输出如下：")
            print(err_path.read_text(encoding="utf-8", errors="replace")[:800])
            return 1

        print("\n[1] 8 个页面均返回 HTML 200")
        for path in ("/", "/overview", "/spots", "/evaluation", "/compare", "/qa", "/tasks", "/login"):
            status, ctype, body = fetch(path)
            # /tasks 未登录会 302→200（urllib 自动跟随到 /login）
            ok = status == 200 and "text/html" in ctype and len(body) > 500
            check(f"{path} → HTML 200（{len(body)} 字节）", ok, f"HTTP {status} {ctype.split(';')[0]}")

        print("\n[2] 关键接口返回 JSON 且 code=0")
        for path in (
            "/healthz", "/api/db-ping", "/api/overview/summary",
            "/api/spots?page=1&page_size=3", "/api/spots/564/report",
        ):
            status, ctype, body = fetch(path)
            payload = json.loads(body.decode("utf-8"))
            check(f"{path} → JSON code={payload.get('code')}",
                  status == 200 and "application/json" in ctype and payload.get("code") == 0,
                  f"HTTP {status}")

        print("\n[3] 静态资源真的能取到（否则页面样式/图表全挂）")
        for path in ("/static/js/common.js", "/static/vendor/echarts.min.js", "/static/vendor/axios.min.js"):
            status, ctype, body = fetch(path)
            check(f"{path} → 200 且内容非空（{len(body)} 字节）",
                  status == 200 and len(body) > 1000, f"HTTP {status} {ctype.split(';')[0]}")

        print("\n[4] 真实 HTTP 下的错误响应仍是统一信封（不是 HTML）")
        for path, want in (("/api/nope", 404), ("/api/spots/abc", 404), ("/api/admin/caliber", 401)):
            status, ctype, body = fetch(path)
            try:
                payload = json.loads(body.decode("utf-8"))
                is_env = set(["code", "message", "data"]).issubset(payload.keys())
            except Exception:
                payload, is_env = {}, False
            check(f"{path} → HTTP {want} 且为 JSON 信封（code={payload.get('code')}）",
                  status == want and "application/json" in ctype and is_env, f"HTTP {status}")

        print("\n[5] 服务器日志不得出现异常")
        time.sleep(0.5)
        log = err_path.read_text(encoding="utf-8", errors="replace") + out_path.read_text(
            encoding="utf-8", errors="replace"
        )
        check("日志无 Traceback", "Traceback" not in log, "")
        check("日志无 500 响应", " 500 -" not in log, "")
        check("日志记录了本次请求（证明确实是这个进程在服务）",
              "GET /healthz" in log, "")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        # 等端口真正释放（Windows 上关闭监听后可能短暂处于 TIME_WAIT/仍被占）
        released = False
        for _ in range(15):
            time.sleep(1)
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", TEST_PORT)) != 0:
                    released = True
                    break
        check("子进程已停止", proc.poll() is not None, f"exit={proc.poll()}")
        # 这条是"防漏子进程"的关键断言：debug 模式曾导致 reloader 子进程残留并占住端口
        check(f"端口 {TEST_PORT} 已释放（没有残留子进程占着）", released, "")

        for p in (out_path, err_path):
            try:
                p.unlink(missing_ok=True)
            except PermissionError:
                pass   # 句柄刚释放时偶发；临时文件不参与断言
        check("临时日志文件已清理",
              not out_path.exists() and not err_path.exists(), "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"真实进程启动验证：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：`python run.py` 起得来，页面/接口/静态资源在真实 HTTP 下都正常——")
        print("      演示前执行这一条即可确认『服务能跑』，而不只是『应用对象能构造』。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
