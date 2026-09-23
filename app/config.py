# -*- coding: utf-8 -*-
"""集中配置模块（对应非功能需求 NR-M-01「连接/Key/限流集中配置」）。

设计约束：
    · 本模块是**唯一**读取 .env / 环境变量的入口，其他模块一律 `from app.config import settings`，
      不得自行调用 os.environ，以保证口径与默认值集中、可审计。
    · 真实口令与 API Key 只存在于 .env（已被 .gitignore 排除），代码与文档中不得出现明文。
    · API Key 未填写时，生成类功能必须降级（NR-R-05），不得因缺 Key 而启动失败——
      阶段一完全没有模型调用，因此此处仅做占位与校验方法，不强制要求。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录：app/config.py -> app/ -> 项目根
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# 载入 .env（不存在时静默跳过，使用下面的默认值）
load_dotenv(PROJECT_ROOT / ".env")


def _get(name: str, default: str = "") -> str:
    """读取环境变量并去除首尾空白；未设置时返回默认值。"""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def _get_app(name: str, default: str = "") -> str:
    """读取「应用自有」环境变量：**优先带 APP_ 前缀的名字，兼容无前缀旧名**。

    背景（属于部署约束，不是代码风格问题）：
        本项目根目录下的 .env 会被**开发工具**（如 DSH）与 Flask 一并读取。
        其中部分变量名（例如 DEEPSEEK_BASE_URL）被工具保留给「启动环境」独占，
        工具会拒绝从 .env 读取它们并直接终止启动。为彻底避开命名空间冲突，
        应用自有配置统一改用 APP_ 前缀；同时保留对无前缀旧名的兼容，
        这样也可以把值放到真正的进程环境变量中（导出环境变量是被允许的）。

    :param name: 无前缀变量名，如 "DEEPSEEK_BASE_URL"
    """
    value = os.environ.get("APP_" + name)
    if value is not None:
        return value.strip()
    value = os.environ.get(name)
    if value is not None:
        return value.strip()
    return default


def _get_int(name: str, default: int) -> int:
    """读取整型环境变量；非法值回落默认值（避免因配置笔误导致启动失败）。"""
    raw = _get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_app_int(name: str, default: int) -> int:
    """读取「应用自有」整型环境变量（APP_ 前缀优先，兼容旧名；非法值回落默认值）。"""
    raw = _get_app(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_app_float(name: str, default: float) -> float:
    """读取「应用自有」浮点型环境变量（APP_ 前缀优先，兼容旧名；非法值回落默认值）。"""
    raw = _get_app(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_bool(name: str, default: bool = False) -> bool:
    """读取布尔型环境变量，接受 1/true/yes/on（不区分大小写）。"""
    raw = _get(name, "")
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _get_app_bool(name: str, default: bool = False) -> bool:
    """读取「应用自有」布尔型环境变量（APP_ 前缀优先，兼容旧名）。"""
    raw = _get_app(name, "")
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class DatabaseSettings:
    """MySQL 连接配置。

    对应 docs/database/建表脚本.sql：库 `tibet_review`，字符集 utf8mb4。
    注意（项目现状分析.md §8.4 K5/K6/K7）：MySQL 8 连接必须允许公钥检索、指定时区与字符集。
    """

    host: str = _get("DB_HOST", "127.0.0.1")
    port: int = _get_int("DB_PORT", 3306)
    user: str = _get("DB_USER", "root")
    password: str = _get("DB_PASSWORD", "")
    database: str = _get("DB_NAME", "tibet_review")
    charset: str = _get("DB_CHARSET", "utf8mb4")

    @property
    def is_configured(self) -> bool:
        """口令是否已填写（未填写时给出可操作的报错，而不是让 PyMySQL 抛认证异常）。"""
        return bool(self.password)

    @property
    def summary(self) -> str:
        """可安全打印的连接摘要（**绝不包含口令**）。"""
        return f"{self.user}@{self.host}:{self.port}/{self.database}?charset={self.charset}"

    def connect_kwargs(self) -> dict:
        """转换为 PyMySQL.connect() 的关键字参数（cursorclass 由 app/db.py 指定）。"""
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "charset": self.charset,
            "autocommit": False,
        }


@dataclass(frozen=True)
class DeepSeekSettings:
    """DeepSeek 官方 API 配置（阶段一不调用；阶段五/六启用）。

    模型名与调用参数依据 docs/design/详细设计说明书.md §7.2：
        timeout 60s、并发 3–5、重试最多 2 次。
    temperature / max_tokens 属「按场景」参数，在 app/llm/client.py 中按场景定义，
    不放在这里（避免把场景细节混进全局配置）。
    """

    # 统一使用 _get_app：APP_ 前缀优先，兼容无前缀旧名（避免与开发工具保留变量冲突）
    api_key: str = _get_app("DEEPSEEK_API_KEY", "")
    base_url: str = _get_app("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model: str = _get_app("DEEPSEEK_MODEL", "deepseek-chat")
    timeout: int = _get_app_int("DEEPSEEK_TIMEOUT", 60)
    max_concurrency: int = _get_app_int("DEEPSEEK_MAX_CONCURRENCY", 3)
    max_retry: int = _get_app_int("DEEPSEEK_MAX_RETRY", 2)
    # 成本核算用的参考单价（元 / 百万 tokens）。属"可配置的估算参数"，
    # 真实费用一律以 API 返回的 usage 为准；单价如与官网不符可用 .env 覆盖。
    price_input: float = _get_app_float("DEEPSEEK_PRICE_INPUT", 2.0)
    price_output: float = _get_app_float("DEEPSEEK_PRICE_OUTPUT", 8.0)

    @property
    def is_configured(self) -> bool:
        """API Key 是否已申请并填写。"""
        return bool(self.api_key)


@dataclass(frozen=True)
class PathSettings:
    """数据与产物路径配置。

    纪律（BR-11）：`数据采集/` 下的文件是**冻结件、只读**，禁止覆盖或就地修改；
    任何清洗/导入产物一律写入 `data/`（新版本文件）。
    """

    project_root: Path = PROJECT_ROOT
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / _get_app("DATA_DIR", "数据采集"))
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / _get_app("OUTPUT_DIR", "data"))
    log_dir: Path = field(default_factory=lambda: PROJECT_ROOT / _get_app("LOG_DIR", "logs"))

    final_dataset_file: str = _get_app("FINAL_DATASET", "旅游评论数据集_最终版.csv")
    spot_source_file: str = _get_app("SPOT_SOURCE_FILE", "景点来源标注.csv")
    dup_list_file: str = _get_app("DUP_LIST_FILE", "内容重复清单.csv")

    @property
    def final_dataset(self) -> Path:
        """最终数据集（59,033 条 / 837 景点 / 15 字段）的绝对路径。"""
        return self.data_dir / self.final_dataset_file

    @property
    def spot_source(self) -> Path:
        """景点来源标注（景点名称 / 来源口径 / 条数）的绝对路径。"""
        return self.data_dir / self.spot_source_file

    @property
    def dup_list(self) -> Path:
        """内容重复清单（1,285 组 / 4,239 条）的绝对路径。"""
        return self.data_dir / self.dup_list_file


@dataclass(frozen=True)
class WebSettings:
    """Flask 运行配置（APP_ 前缀优先，兼容无前缀旧名）。"""

    host: str = _get_app("FLASK_HOST", "127.0.0.1")
    port: int = _get_app_int("FLASK_PORT", 5000)
    debug: bool = _get_app_bool("FLASK_DEBUG", True)


@dataclass(frozen=True)
class Settings:
    """配置总入口。用法：`from app.config import settings`。"""

    db: DatabaseSettings = field(default_factory=DatabaseSettings)
    deepseek: DeepSeekSettings = field(default_factory=DeepSeekSettings)
    paths: PathSettings = field(default_factory=PathSettings)
    web: WebSettings = field(default_factory=WebSettings)


# 全局唯一配置实例
settings = Settings()


__all__ = [
    "PROJECT_ROOT",
    "DatabaseSettings",
    "DeepSeekSettings",
    "PathSettings",
    "WebSettings",
    "Settings",
    "settings",
]
