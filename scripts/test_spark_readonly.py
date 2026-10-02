# -*- coding: utf-8 -*-
"""阶段三（Spark）可复现性验证：**只读**跑一遍统计作业，证明它仍能跑且不动数据库。

## 为什么需要它
阶段三早就完成并冻结了，我此前对它只有"没去改过"这种**被动**信心，
却从来没有**主动验证**过：
    · `spark-submit` 现在还能跑起来吗（JDK/Spark/连接器路径是否仍在）？
    · `--skip-db-write` 是否真的不写库（这是"安全重跑"的前提）？
    · 核心表会不会被作业顺手改掉？

第二条尤其关键：答辩时若被问"能不能现场重跑一遍统计"，答案必须是
**"能，而且不会破坏已有结果"**——这需要证据，不是感觉。

## 做法
    ① 快照核心表行数（spot / review / stat_* / sentiment(mllib) / topic* / analysis_task）；
    ② 用 `--jars <mysql 连接器> --stage 2 --limit 100 --skip-db-write` 跑 Spark 作业；
    ③ 断言输出含"执行结束：成功"且**确实打印了 "--skip-db-write 已启用"**；
    ④ 再快照，断言**逐表未变**。

## 环境说明（缺一即跳过，并明确说明原因）
需要 `spark-submit` 在 PATH 上、且 MySQL 连接器 jar 存在。
路径可用环境变量覆盖：`SPARK_SUBMIT`、`SPARK_MYSQL_JAR`。

全程不动数据库、不调用任何模型。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JAR = ROOT / "spark" / "target" / "spark-offline-analysis-1.0.0.jar"
MYSQL_JAR = Path(os.environ.get("SPARK_MYSQL_JAR", r"D:\bigdata\jars\mysql-connector-j-8.0.33.jar"))
SPARK_SUBMIT = os.environ.get("SPARK_SUBMIT") or shutil.which("spark-submit") or ""
TIMEOUT_SEC = 480

TABLES = ("spot", "review", "stat_spot", "stat_time", "stat_ip", "topic", "topic_word", "analysis_task")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def snapshot() -> dict[str, int]:
    from app.db import query_one

    out = {t: int(query_one(f"SELECT COUNT(*) AS n FROM {t}")["n"]) for t in TABLES}
    out["sentiment_mllib"] = int(
        query_one("SELECT COUNT(*) AS n FROM sentiment WHERE method='mllib'")["n"]
    )
    return out


def main() -> int:
    print("=" * 88)
    print("阶段三（Spark）可复现性验证：只读跑一遍，数据库必须一行未变")
    print("=" * 88)

    # ---- 环境检查：缺依赖就明确跳过，而不是假装通过 ----
    missing: list[str] = []
    if not SPARK_SUBMIT or not Path(SPARK_SUBMIT).exists():
        missing.append("spark-submit 不在 PATH（可用环境变量 SPARK_SUBMIT 指定）")
    if not JAR.exists():
        missing.append(f"作业 jar 不存在：{JAR.relative_to(ROOT)}")
    if not MYSQL_JAR.exists():
        missing.append(f"MySQL 连接器不存在：{MYSQL_JAR}（可用 SPARK_MYSQL_JAR 指定）")
    if missing:
        print("\n[跳过] 缺少运行环境，未执行 Spark 作业：")
        for m in missing:
            print(f"  · {m}")
        print("  说明：本脚本在缺少大数据环境时会**明确跳过**，不会伪装成通过。")
        print("=" * 88)
        return 0
    check("Spark 运行环境齐备（spark-submit / 作业 jar / MySQL 连接器）", True,
          f"{Path(SPARK_SUBMIT).name} + {MYSQL_JAR.name}")

    before = snapshot()
    print(f"\n运行前：{json.dumps(before, ensure_ascii=False)}")
    print(f"\n执行：--stage 2 --limit 100 --skip-db-write（只计算、不写库）…")

    cmd = [
        SPARK_SUBMIT, "--class", "com.tibet.tourism.spark.Main", "--master", "local[*]",
        "--jars", str(MYSQL_JAR), str(JAR),
        "--source", "mysql", "--stage", "2", "--limit", "100", "--skip-db-write",
    ]
    # 【必须】强制子 JVM 用 UTF-8 输出。
    # 默认 Windows 上 JVM 用 GBK 写控制台，Python 侧按 UTF-8 解码就会得到乱码，
    # 于是"执行结束：成功"这类**中文断言会假失败**（首版实测：ASCII 断言通过、中文断言全挂）。
    env = {**os.environ, "JAVA_TOOL_OPTIONS": "-Dfile.encoding=UTF-8"}
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT_SEC, env=env,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes)
                  else (exc.stdout or ""))
        timed_out = True

    check(f"作业在 {TIMEOUT_SEC} 秒内结束", not timed_out, "")
    if timed_out:
        return 1

    # ---- 断言：成功 + 确实启用了不写库 ----
    check("作业输出『执行结束：成功』", "执行结束：成功" in output,
          next((ln.strip() for ln in output.splitlines() if "执行结束" in ln), "(未找到该行)"))
    check("作业**确实**启用了 --skip-db-write（不是我们以为它启用了）",
          "--skip-db-write 已启用" in output, "")
    check("读到了全部 59,033 条评论（数据源连通）", "59033" in output,
          next((ln.strip() for ln in output.splitlines() if "评论明细行数" in ln), ""))
    check("读到 837 个景点维表", "837" in output,
          next((ln.strip() for ln in output.splitlines() if "景点维表" in ln), ""))

    after = snapshot()
    print(f"\n运行后：{json.dumps(after, ensure_ascii=False)}")
    changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    check("核心表逐表未变（--skip-db-write 名副其实）", not changed,
          ("变化：" + ", ".join(f"{k}: {v[0]}→{v[1]}" for k, v in changed.items())) if changed
          else f"{len(before)} 张表一致")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 88)
    print(f"阶段三可复现性验证：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：Spark 统计作业现在仍能跑通，且 `--skip-db-write` 确实不写库——")
        print("      可以放心地『现场重跑一遍』来证明阶段三可复现。")
    print("=" * 88)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
