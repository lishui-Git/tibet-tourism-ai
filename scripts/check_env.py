# -*- coding: utf-8 -*-
"""环境自检脚本（对应非功能需求 NR-M-04「环境自检脚本」）。

检查项（顺序执行，任一项失败都给出可操作的排查建议）：
    1. Python 版本与项目依赖
    2. 项目目录与配置（.env / 数据源文件）
    3. MySQL 服务与库 `tibet_review`
    4. 17 张表与约束数量（对照 docs/database/建表脚本.sql 基线）
    5. 数据导入现状（spot / review 行数）
    6. 大数据环境（JDK / Spark / Scala）——阶段四才需要，未就绪只提示不判失败

用法：
    python scripts/check_env.py
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import sys
from pathlib import Path

# Windows 默认控制台代码页为 GBK，直接输出 ✔ / ✘ / ⚠ 会抛 UnicodeEncodeError；
# 统一把 stdout 切到 UTF-8（与 tmp/ 下的校验脚本保持一致）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 允许以 `python scripts/check_env.py` 方式直接运行（把项目根加入 sys.path）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PASS = "  ✔"
FAIL = "  ✘"
WARN = "  ⚠"
INFO = "  ·"

#: 设计基线：17 张表（已冻结，不得增删）
EXPECTED_TABLES = [
    "analysis_task", "aspect", "comment_semantic", "qa_record", "review",
    "sentiment", "spot", "spot_fact_package", "spot_report", "stat_ip",
    "stat_overview", "stat_spot", "stat_time", "sys_user", "task_log",
    "topic", "topic_word",
]
EXPECTED_TABLE_COUNT = 17
EXPECTED_PK_COUNT = 17
EXPECTED_UNIQUE_COUNT = 7
EXPECTED_FK_COUNT = 14
EXPECTED_SPOT_ROWS = 837
EXPECTED_REVIEW_ROWS = 59033

#: requirements.txt 中阶段一必需的核心依赖（名称 → 说明）
CORE_PACKAGES = {
    "flask": "Flask Web 框架",
    "pymysql": "MySQL 驱动",
    "cryptography": "MySQL 8 caching_sha2_password 认证支持",
    "dotenv": "python-dotenv（.env 读取）",
}


def section(title: str) -> None:
    print("\n" + "=" * 74)
    print(f" {title}")
    print("=" * 74)


def check_python() -> bool:
    section("1. Python 运行时与依赖")
    major, minor = sys.version_info[:2]
    print(f"{INFO} Python 版本：{sys.version.split()[0]}  路径：{sys.executable}")
    if (major, minor) < (3, 9):
        print(f"{FAIL} Python 版本过低（需要 3.9+），请升级。")
        return False
    print(f"{PASS} Python 版本可用")

    ok = True
    for name, desc in CORE_PACKAGES.items():
        try:
            importlib.import_module(name)
            try:
                version = md.version(name)
            except md.PackageNotFoundError:
                version = "已导入（未能读取版本）"
            print(f"{PASS} {desc}：{name} {version}")
        except ImportError:
            print(f"{FAIL} {desc}：{name} 未安装 → 执行 python -m pip install -r requirements.txt")
            ok = False
    return ok


def check_config() -> bool:
    section("2. 项目配置与数据源文件")
    ok = True

    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        print(f"{PASS} 配置文件存在：{env_file.name}")
    else:
        print(f"{FAIL} 缺少 .env → 执行 copy .env.example .env 并填写 DB_PASSWORD")
        ok = False

    try:
        from app.config import settings
    except Exception as exc:  # 配置本身出错时直接暴露
        print(f"{FAIL} 配置加载失败：{exc}")
        return False

    print(f"{INFO} 数据库目标：{settings.db.summary}（口令不打印）")
    if not settings.db.is_configured:
        print(f"{FAIL} DB_PASSWORD 为空 → 请在 .env 中填写")
        ok = False

    for label, path in (
        ("最终数据集", settings.paths.final_dataset),
        ("景点来源标注", settings.paths.spot_source),
        ("内容重复清单", settings.paths.dup_list),
    ):
        if path.exists():
            size_mb = path.stat().st_size / 1024 / 1024
            print(f"{PASS} {label}：{path.name}（{size_mb:.1f} MB）")
        else:
            print(f"{FAIL} {label} 缺失：{path}")
            ok = False

    key_state = "已填写" if settings.deepseek.is_configured else "未填写（阶段一不需要）"
    print(f"{INFO} DeepSeek API Key：{key_state}")
    return ok


def check_mysql() -> bool:
    section("3. MySQL 服务与数据库")
    try:
        from app.db import DatabaseError, query_all
    except Exception as exc:
        print(f"{FAIL} 无法导入数据访问模块：{exc}")
        return False

    try:
        info = query_all(
            """
            SELECT VERSION() AS version,
                   @@character_set_database AS charset,
                   DATABASE() AS db_name
            """
        )[0]
    except DatabaseError as exc:
        print(f"{FAIL} {exc}")
        print(f"{INFO} 排查：① MySQL 服务是否启动（services.msc 找 MySQL80）；"
              f"② .env 中 DB_PASSWORD 是否正确；③ 库 tibet_review 是否已创建。")
        return False

    print(f"{PASS} 连接成功：MySQL {info['version']} · 字符集 {info['charset']} · 库 {info['db_name']}")
    if info["charset"] != "utf8mb4":
        print(f"{WARN} 库字符集为 {info['charset']}，设计基线为 utf8mb4，中文可能出现乱码。")
    return True


def check_tables() -> bool:
    section("4. 表结构与约束（基线：17 张表 / 17 主键 / 7 唯一 / 14 外键）")
    from app.db import query_all

    rows = query_all(
        """
        SELECT TABLE_NAME FROM information_schema.TABLES
         WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME
        """
    )
    actual = sorted(r["TABLE_NAME"] for r in rows)
    print(f"{INFO} 实际表数量：{len(actual)}（期望 {EXPECTED_TABLE_COUNT}）")

    missing = sorted(set(EXPECTED_TABLES) - set(actual))
    extra = sorted(set(actual) - set(EXPECTED_TABLES))
    if missing:
        print(f"{FAIL} 缺失的表：{missing}")
    if extra:
        print(f"{WARN} 多出的表（设计中不存在）：{extra}")

    counts = query_all(
        """
        SELECT CONSTRAINT_TYPE, COUNT(*) AS c
          FROM information_schema.TABLE_CONSTRAINTS
         WHERE TABLE_SCHEMA = DATABASE()
         GROUP BY CONSTRAINT_TYPE
        """
    )
    by_type = {r["CONSTRAINT_TYPE"]: int(r["c"]) for r in counts}
    expected = {
        "PRIMARY KEY": EXPECTED_PK_COUNT,
        "UNIQUE": EXPECTED_UNIQUE_COUNT,
        "FOREIGN KEY": EXPECTED_FK_COUNT,
    }
    ok = not missing and len(actual) == EXPECTED_TABLE_COUNT
    for ctype, want in expected.items():
        got = by_type.get(ctype, 0)
        mark = PASS if got == want else FAIL
        if got != want:
            ok = False
        print(f"{mark} {ctype}：{got}（期望 {want}）")

    if ok:
        print(f"{PASS} 表结构与设计基线一致（17 张表已冻结，后续不得增删）")
    else:
        print(f"{INFO} 重建方式（会先 DROP 再建，**仅在确认无数据时执行**）：")
        print(f"       mysql -u root -p --default-character-set=utf8mb4 "
              f"--database=tibet_review -e \"source docs/database/建表脚本.sql\"")
        print(f"       注意：mysql.exe 读不了含中文的路径，需先把脚本复制到纯 ASCII 路径。")
    return ok


def check_data() -> bool:
    section("5. 数据导入现状")
    from app.db import query_all

    spot_rows = int(query_all("SELECT COUNT(*) AS c FROM spot")[0]["c"])
    review_rows = int(query_all("SELECT COUNT(*) AS c FROM review")[0]["c"])
    print(f"{INFO} spot   ：{spot_rows} 行（全量期望 {EXPECTED_SPOT_ROWS}）")
    print(f"{INFO} review ：{review_rows} 行（全量期望 {EXPECTED_REVIEW_ROWS}）")

    if review_rows == 0:
        print(f"{WARN} 尚未导入数据。下一步：")
        print(f"       python -m app.batch.import_dataset --mode sample --limit 200 --dry-run")
        print(f"       python -m app.batch.import_dataset --mode sample --limit 200")
        return True
    if review_rows < EXPECTED_REVIEW_ROWS:
        print(f"{WARN} 当前为抽样导入（{review_rows} 行），全量导入请执行：")
        print(f"       python -m app.batch.import_dataset --mode full")
        return True
    print(f"{PASS} 数据已全量导入")
    return True


def check_bigdata() -> None:
    section("6. 大数据环境（阶段四才需要，未就绪不影响当前阶段）")
    import shutil
    import subprocess

    java = shutil.which("java")
    if java:
        try:
            out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=20)
            print(f"{PASS} JDK：{(out.stderr or out.stdout).splitlines()[0].strip()}")
        except Exception as exc:
            print(f"{WARN} JDK 存在但无法读取版本：{exc}")
    else:
        print(f"{WARN} 未找到 java（阶段四 Spark 需要 JDK 1.8）")

    if shutil.which("spark-submit"):
        print(f"{PASS} spark-submit 已在 PATH 中")
    else:
        print(f"{WARN} 未找到 spark-submit（阶段四安装 Spark 3.3.1，运行在 JDK 1.8）")

    hadoop_home = __import__("os").environ.get("HADOOP_HOME")
    if hadoop_home:
        print(f"{PASS} HADOOP_HOME={hadoop_home}")
    else:
        print(f"{WARN} 未设置 HADOOP_HOME（Windows 上 Spark 需要 winutils.exe，见 项目现状分析.md §8.4 K1）")


def main() -> int:
    print("=" * 74)
    print(" 基于 DeepSeek 的西藏旅游景点智能评价与分析系统 · 环境自检")
    print("=" * 74)
    print(f" 项目根目录：{PROJECT_ROOT}")

    results = [
        ("Python 与依赖", check_python()),
        ("配置与数据源", check_config()),
    ]

    mysql_ok = check_mysql()
    results.append(("MySQL 连接", mysql_ok))
    if mysql_ok:
        results.append(("表结构", check_tables()))
        results.append(("数据导入现状", check_data()))

    check_bigdata()

    section("自检结论")
    for name, ok in results:
        print(f"{PASS if ok else FAIL} {name}")
    all_ok = all(ok for _, ok in results)
    print("\n" + (" 环境就绪，可以继续开发。" if all_ok else " 存在未通过项，请按上面的提示处理后重跑本脚本。"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
