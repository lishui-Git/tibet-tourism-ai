# -*- coding: utf-8 -*-
"""基于 DeepSeek 的西藏旅游景点智能评价与分析系统 · 后端应用包。

分层对应关系（详见 docs/design/详细设计说明书.md §2）：
    app/web/    Flask Web 应用      —— C4 容器「Flask Web 应用」，组件 C-API-01 ~ C-API-14
    app/batch/  Python 批处理服务    —— C4 容器「Python 批处理服务」，组件 C-BAT-01 ~ C-BAT-07
    app/llm/    DeepSeek 调用封装    —— 组件 C-API-12
    app/config.py  集中配置（唯一配置读取入口，NR-M-01）
    app/db.py      数据访问组件 C-API-13

约束（来自 ReadMe.md 与设计文档）：
    · 本包不实现设计中不存在的模块；新增模块前先更新 C4 与详细设计。
    · 数据库以 docs/database/建表脚本.sql 为准，字段名与类型不得改动。
"""

__all__ = ["__version__"]

# 项目内部版本号（与 docs/database/建表脚本.sql 的 V1.0 基线对应）
__version__ = "0.1.0"
