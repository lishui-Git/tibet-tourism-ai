# -*- coding: utf-8 -*-
"""统一响应封装（详细设计说明书 §6.3 / §6.4）。

响应体固定为 `{code, message, data}`，HTTP 状态码与业务码的对应关系取自详细设计 §6.4。
**关键约定**：`code = 0` 表示成功，其中**包含**「数据不足（available=false）」与
「超范围拒答（OUT_OF_SCOPE）」两种正常业务响应——它们不是错误，前端不得据此报错。
"""

from __future__ import annotations

from typing import Any

from flask import jsonify

# ---------------------------------------------------------------------------
# 业务码（详细设计 §6.4，不得随意增删；如需新增先更新设计文档）
# ---------------------------------------------------------------------------
CODE_OK = 0  # 成功（200）
CODE_PARAM_INVALID = 1001  # 参数缺失或格式错误（400）
CODE_PARAM_OUT_OF_RANGE = 1002  # 参数越界（400）
CODE_NOT_LOGGED_IN = 2001  # 未登录（401）
CODE_FORBIDDEN = 2002  # 权限不足（403）
CODE_USER_EXISTS = 2003  # 用户名已存在（409）
CODE_BAD_CREDENTIALS = 2004  # 用户名或密码错误（401）
CODE_NOT_FOUND = 3001  # 资源不存在（404）
CODE_LLM_FAILED = 4001  # DeepSeek 调用失败或超时（502）
CODE_LLM_BAD_FORMAT = 4002  # 模型返回格式非法（502）
CODE_DB_ERROR = 5001  # 数据库错误（500）
CODE_INTERNAL_ERROR = 5002  # 内部错误（500）
CODE_BATCH_FAILED = 5003  # 批处理失败（500）

# 业务码 → HTTP 状态码
HTTP_STATUS: dict[int, int] = {
    CODE_OK: 200,
    CODE_PARAM_INVALID: 400,
    CODE_PARAM_OUT_OF_RANGE: 400,
    CODE_NOT_LOGGED_IN: 401,
    CODE_BAD_CREDENTIALS: 401,
    CODE_FORBIDDEN: 403,
    CODE_NOT_FOUND: 404,
    CODE_USER_EXISTS: 409,
    CODE_DB_ERROR: 500,
    CODE_INTERNAL_ERROR: 500,
    CODE_BATCH_FAILED: 500,
    CODE_LLM_FAILED: 502,
    CODE_LLM_BAD_FORMAT: 502,
}


def ok(data: Any = None, message: str = "success"):
    """成功响应（HTTP 200，code = 0）。"""
    return jsonify({"code": CODE_OK, "message": message, "data": data}), 200


def fail(code: int, message: str, data: Any = None):
    """失败响应；HTTP 状态码按 §6.4 映射表确定。"""
    status = HTTP_STATUS.get(code, 500)
    return jsonify({"code": code, "message": message, "data": data}), status


__all__ = [
    "CODE_OK",
    "CODE_PARAM_INVALID",
    "CODE_PARAM_OUT_OF_RANGE",
    "CODE_NOT_LOGGED_IN",
    "CODE_FORBIDDEN",
    "CODE_USER_EXISTS",
    "CODE_BAD_CREDENTIALS",
    "CODE_NOT_FOUND",
    "CODE_LLM_FAILED",
    "CODE_LLM_BAD_FORMAT",
    "CODE_DB_ERROR",
    "CODE_INTERNAL_ERROR",
    "CODE_BATCH_FAILED",
    "HTTP_STATUS",
    "ok",
    "fail",
]
