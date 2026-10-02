# -*- coding: utf-8 -*-
"""一键全量验证（答辩前自检入口；**零 API 成本**）。

## 为什么需要这个脚本
项目原先有 6 个测试脚本要分别手动执行（`check_env` / `smoke_api` / `test_auth` /
`test_qa` / `test_report_regen` / `check_api_contract` / `verify_phase4`），
答辩前逐个跑容易漏、也不方便一次性给出"全绿/有失败"的结论。
本脚本把它们串起来，逐项执行并汇总，最后统一打印结论与退出码。

## 用法
    .\\.venv\\Scripts\\python.exe scripts/verify_all.py            # 全部
    .\\.venv\\Scripts\\python.exe scripts/verify_all.py --fast     # 跳过最慢的 verify_phase4

## 设计与纪律
    · **只做只读与自清理测试**：不调用任何模型、不改动分析结果数据；
    · `test_auth` / `test_qa` / `test_report_regen` 会写入少量测试数据，
      但它们**自带精确清理与"清理后无残留"断言**（本脚本会把这一点一并报出来）；
    · **不包含** `verify_cleaned.py`：它会重读上百 MB 的源 CSV，
      属阶段二的一次性复核，不适合放进日常/答辩前自检（也避免大文件读取）。
    · 每个子脚本的"通过数/总数"从它自己的汇总行解析，**不做任何编造**：
      解析不到就如实报"未识别到汇总行"，而不是假定通过。

退出码：全部通过 → 0；任一失败 → 1。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")

# (脚本, 说明, 是否为慢项)
SUITES: tuple[tuple[str, str, bool], ...] = (
    ("check_env.py", "环境自检（Python/依赖/目录/MySQL/17 张表/大数据环境）", False),
    ("smoke_api.py", "接口冒烟（页面、静态资源、接口与错误码）", False),
    ("test_auth.py", "认证与鉴权（口令哈希、注册登录、越权、停用失效）", False),
    ("test_qa.py", "M5 智能问答（分类、检索、三条不调用模型的分支、越权）", False),
    ("test_report_regen.py", "接口 19 重新生成评价（在线只登记 / 离线 MockClient 强制重生成）", False),
    ("test_report_usage.py", "景点评价实际 usage 与费用（含修复轮累加；假客户端）", False),
    ("test_write_recovery.py", "写库失败兜底（注入写库失败 → 只重试写库、落盘待补，不重新调用模型）", False),
    ("test_replay.py", "恢复补写（已付费结果零成本写回库；成功后才删凭证）", False),
    ("test_fact_package.py", "事实包（C-BAT-06 零模型调用 + 幂等跳过 + 清理回基线）", False),
    ("test_chain_handoff.py", "生产链路串联（facts 产物被 report 接住；零模型调用）", False),
    ("test_spot_report_write_failure.py", "景点评价写库失败兜底（注入写库失败 → 批次继续 + 落盘待补）", False),
    ("test_layering.py", "分层自洽性（三层互斥穷尽 + 待处理口径吻合，证明无评论被静默跳过）", False),
    ("test_resume_cursor.py", "断点续跑实证（游标=结果表；插 1 行 → 计划调用恰减 1）", False),
    ("test_cost_projection.py", "计划阶段预估一致性（preflight ↔ dry-run 数字对得上）", False),
    ("test_dry_run_contract.py", "dry-run 契约（只统计不写库、不登记任务）", False),
    ("verify_demo_route.py", "答辩演示动线核对（手册第四节的断言逐行兑现）", False),
    ("check_docs_facts.py", "文档事实核对（手册命令可解析 + 基线数字与库一致）", False),
    ("test_cli_guards.py", "运行安全闸门（规模确认 / mock 上限 / 离线阻断 / 客户端构造）", False),
    ("check_page_bindings.py", "页面绑定（JS 引用的元素 id 在模板/脚本里都存在）", False),
    ("test_readonly_api.py", "只读架构实证（访问全部只读接口后数据逐表未变）", False),
    ("test_server_startup.py", "真实进程启动验证（python run.py；页面/接口/静态资源走真实 HTTP）", False),
    ("test_spark_readonly.py", "阶段三可复现性（Spark 只读跑一遍，库一行未变；缺大数据环境则跳过）", True),
    ("test_source_data_readonly.py", "BR-11 实证（冻结的原始数据文件只读：静态扫描 + 指纹比对）", False),
    ("check_api_contract.py", "接口字段契约（前端依赖的字段确实存在）", False),
    ("verify_phase4.py", "阶段四端到端验证（34 项检查，含快照对比与清理）", True),
)

# 各脚本汇总行的写法不统一，这里用宽松模式匹配"通过数/总数"
SUMMARY_PATTERNS = (
    re.compile(r"(\d+)\s*/\s*(\d+)\s*项通过"),
    re.compile(r"(\d+)\s*/\s*(\d+)\s*通过"),
    re.compile(r"(\d+)\s*/\s*(\d+)\s*个字段存在"),
)


def parse_summary(output: str) -> tuple[int, int] | None:
    """从输出里解析最后一个"x/y 通过"型汇总（取最后一处，避免匹配到中间过程）。"""
    found: tuple[int, int] | None = None
    for pattern in SUMMARY_PATTERNS:
        for match in pattern.finditer(output):
            found = (int(match.group(1)), int(match.group(2)))
    return found


def run_suite(script: str) -> tuple[bool, str, tuple[int, int] | None, float]:
    """执行单个子脚本，返回 (是否成功, 输出, 汇总数, 耗时秒)。"""
    started = time.time()
    proc = subprocess.run(
        [PYTHON, str(PROJECT_ROOT / "scripts" / script)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.time() - started
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output, parse_summary(output), elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一键全量验证（零 API 成本）")
    parser.add_argument("--fast", action="store_true", help="跳过最慢的 verify_phase4.py")
    parser.add_argument("--verbose", action="store_true", help="打印各脚本完整输出")
    args = parser.parse_args(argv)

    print("=" * 88)
    print("一键全量验证（零 API 成本：不调用任何模型，只做只读与自清理测试）")
    print("=" * 88)

    rows: list[tuple[str, str, bool, tuple[int, int] | None, float, str]] = []
    for script, label, slow in SUITES:
        if args.fast and slow:
            print(f"\n[跳过] {script} —— --fast 模式")
            rows.append((script, label, True, None, 0.0, "按 --fast 跳过"))
            continue
        print(f"\n{'-' * 88}\n[执行] {script} —— {label}")
        ok, output, summary, elapsed = run_suite(script)
        if args.verbose:
            print(output)
        else:
            # 只回显失败行与汇总行，保持输出可读
            for line in output.splitlines():
                if "FAIL" in line or "✘" in line or "无法识别" in line:
                    print("   " + line.strip())
        rows.append((script, label, ok, summary, elapsed, output))
        if summary:
            print(f"   → {summary[0]}/{summary[1]} 通过（{elapsed:.1f}s）")
        else:
            print(f"   → {'执行成功' if ok else '执行失败'}（{elapsed:.1f}s，未识别到汇总行）")

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 88)
    print("汇总")
    print("=" * 88)
    print(f"{'脚本':<26}{'结果':<8}{'通过/总数':<14}{'耗时':>8}  说明")
    total_passed = total_checks = 0
    failures: list[str] = []
    for script, label, ok, summary, elapsed, _ in rows:
        if summary:
            total_passed += summary[0]
            total_checks += summary[1]
            ratio = f"{summary[0]}/{summary[1]}"
        else:
            ratio = "—"
        status = "通过" if ok else "失败"
        if not ok:
            failures.append(script)
        print(f"{script:<26}{status:<8}{ratio:<14}{elapsed:>7.1f}s  {label}")

    print("-" * 88)
    print(f"断言合计：{total_passed}/{total_checks} 通过；失败脚本：{len(failures)} 个"
          + (f"（{', '.join(failures)}）" if failures else ""))

    # ---------------- 成本与就绪状态（调 preflight，零 API 调用） ----------------
    print("\n" + "-" * 88)
    print("全量运行就绪状态（调用 preflight；该命令不调用模型）")
    print("-" * 88)
    proc = subprocess.run(
        [PYTHON, "-m", "app.llm", "--stage", "preflight"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    preflight_out = (proc.stdout or "") + (proc.stderr or "")
    for line in preflight_out.splitlines():
        if re.search(r"REAL API CALLS|ESTIMATED API CALLS|STATUS|ESTIMATED COST", line):
            print("  " + line.strip())
    preflight_ok = proc.returncode == 0 and "STATUS: READY" in preflight_out

    print("\n" + "=" * 88)
    if failures or not preflight_ok:
        print(f"结论：存在失败项（脚本失败 {len(failures)} 个；preflight {'READY' if preflight_ok else '非 READY'}）")
        print("=" * 88)
        return 1
    print(f"结论：全部通过（{total_passed}/{total_checks} 断言）+ preflight READY")
    print("      本次验证未产生任何 API 调用与费用（真实 API 调用：0 次）。")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
