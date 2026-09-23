# -*- coding: utf-8 -*-
"""DeepSeek 调用封装包（阶段四：C-BAT-05～07 的公共基础）。

本包**只负责**"把 Prompt 发给 DeepSeek、把返回内容安全地取回来"，
不含任何业务判断；业务校验与写库在 `app/batch/` 的语义分析与评价模块中完成。

对应设计：
    · 详细设计说明书 §7.2 统一调用封装（C-API-12 的 Python 实现）
    · §7.4 调用量控制、§7.5 失败兜底
    · 非功能需求 NR-M-01（配置集中）、NR-R-05（缺 Key 降级而非报错）

文件职责：
    `client.py`     HTTP 调用、重试、限流、超时、错误分类、用量统计
    `mock.py`       小样本联调用的**假客户端**（不产生任何真实调用）
    `prompts.py`    Prompt 模板集中管理与版本号（prompt_version 可追溯）
    `validators.py` 模型输出校验（枚举/钳制/evidence 原文子串/长度截断）
"""

from __future__ import annotations

__all__ = ["client", "mock", "prompts", "validators"]
