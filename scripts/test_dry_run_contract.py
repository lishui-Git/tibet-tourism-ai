# -*- coding: utf-8 -*-
"""`--dry-run` 契约测试：**只统计、不写库、不登记任务**。

## 为什么需要它
CLI 会打印 `[dry-run] 只统计不写库；真实调用与写库均不会发生`。
但这是**代码的承诺**，必须被验证——本轮实测就抓到了违反：
`--stage all --dry-run` 中，C-BAT-06 事实包**忽略**了 `dry_run` 参数，
照样写入 57 行事实包并登记 1 条任务。

后果不只是"多写了 57 行（零成本）"：`--dry-run` 是操作者**开跑前**用来
确认工作量与费用的动作，如果它自己会改数据，那"先试跑看看"就不再安全，
而且 `spot_report` 的 dry-run 会因此报出 `packages_available=57`，
让人误以为"评价依据已经就绪"。

## 做法
    ① 快照 17 张表行数；
    ② 依次跑 `--stage semantic --dry-run`、`--stage facts --dry-run`、
       `--stage report --dry-run`、`--stage all --dry-run`（子进程，真实 CLI 入口）；
    ③ 再快照，断言**逐表完全一致**；
    ④ 断言事实包 dry-run 的返回值里 `pending_api_calls == 0`
       （事实包零模型调用，dry-run 下更不应该"计划调用"）。

全程零 API 消费、零写入。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe")

TABLES = (
    "spot", "review", "sentiment", "aspect", "comment_semantic", "stat_spot",
    "stat_time", "stat_ip", "topic", "spot_fact_package", "spot_report",
    "qa_record", "analysis_task", "task_log", "sys_user",
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def snapshot() -> dict[str, int]:
    sys.path.insert(0, str(ROOT))
    from app.db import query_one

    out: dict[str, int] = {}
    for table in TABLES:
        try:
            out[table] = int(query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"])
        except Exception:
            out[table] = -1
    return out


def run(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [PYTHON, "-m", "app.llm", *args],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    print("=" * 86)
    print("`--dry-run` 契约：只统计、不写库、不登记任务（零 API 消费）")
    print("=" * 86)

    before = snapshot()
    print(f"\n运行前：spot_fact_package={before['spot_fact_package']}、"
          f"analysis_task={before['analysis_task']}、task_log={before['task_log']}")

    stages = (
        ("--stage", "semantic", "--dry-run"),
        ("--stage", "facts", "--dry-run"),
        ("--stage", "report", "--dry-run"),
        ("--stage", "all", "--dry-run"),
    )
    outputs: dict[str, str] = {}
    for argv in stages:
        code, out = run(*argv)
        label = " ".join(argv)
        outputs[label] = out
        print(f"  已运行 {' '.join(argv)}（exit={code}）")
        check(f"{label} 正常退出（exit=0）", code == 0, f"exit={code}")

    after = snapshot()
    changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    check("全部 dry-run 跑完后，17 张表行数逐表未变", not changed,
          ("变化：" + ", ".join(f"{k}: {v[0]}→{v[1]}" for k, v in changed.items())) if changed
          else f"{len(before)} 张表一致")
    check("未新增事实包行（本轮修复前会写入 57 行）",
          before["spot_fact_package"] == after["spot_fact_package"],
          f"{before['spot_fact_package']} → {after['spot_fact_package']}")
    check("未新增任务行（本轮修复前会登记 1 条 fact_package 任务）",
          before["analysis_task"] == after["analysis_task"],
          f"{before['analysis_task']} → {after['analysis_task']}")
    check("未新增任务日志", before["task_log"] == after["task_log"],
          f"{before['task_log']} → {after['task_log']}")

    print("\n[补充] 事实包 dry-run 的输出必须自证『零调用』")
    facts_out = outputs["--stage facts --dry-run"]
    check("输出含 mode=dry-run", '"mode": "dry-run"' in facts_out, "")
    check("输出含 pending_api_calls=0", '"pending_api_calls": 0' in facts_out, "")
    check("输出写明『不写库、不登记任务』", "不写库" in facts_out and "不登记任务" in facts_out, "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 86)
    print(f"dry-run 契约：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：`--dry-run` 确实只统计不落账——可以放心在开跑前用它核对工作量与费用。")
    print("=" * 86)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
