# -*- coding: utf-8 -*-
"""API 路由公共工具：把业务层异常统一翻译成统一响应体（§6.3 / §6.4）。

**为什么要单独一层**：详细设计 §6.4 规定了业务码与 HTTP 状态码的映射，
若每个路由各写一遍 `try/except`，很容易出现"同样的错误返回不同码"的不一致。
这里统一处理三类异常：

    ParamError(1001/1002)  → 参数问题
    DatabaseError(5001)    → 数据库问题
    其他 Exception(5002)   → 兜底，避免把堆栈直接暴露给前端

注意：`service` 返回 `None` 表示"资源不存在"，由各路由显式调用 `fail(3001, ...)`，
**不要**在这里把 None 一律当成 404——有些接口的 None 有别的语义。
"""

from __future__ import annotations

from typing import Any, Callable

from app.db import DatabaseError
from app.web.data_access import ParamError
from app.web.response import (
    CODE_DB_ERROR,
    CODE_INTERNAL_ERROR,
    CODE_NOT_FOUND,
    CODE_PARAM_INVALID,
    CODE_PARAM_OUT_OF_RANGE,
    fail,
    ok,
)


def respond(action: Callable[[], Any]) -> Any:
    """执行一次业务读取，把结果或异常翻译成统一响应。"""
    try:
        data = action()
    except ParamError as exc:
        # ParamError 自带业务码：1001 参数格式错误 / 1002 参数越界
        code = CODE_PARAM_OUT_OF_RANGE if getattr(exc, "code", 1001) == CODE_PARAM_OUT_OF_RANGE else CODE_PARAM_INVALID
        return fail(code, str(exc))
    except DatabaseError as exc:
        return fail(CODE_DB_ERROR, f"数据库访问失败：{exc}")
    except Exception as exc:  # 兜底：不把内部细节暴露给前端
        return fail(CODE_INTERNAL_ERROR, f"服务内部错误：{type(exc).__name__}")
    return ok(data)


def respond_one(action: Callable[[], Any], not_found_message: str) -> Any:
    """执行"按 ID 取单个资源"的读取：`None` 表示资源不存在 → 3001。

    与 `respond` 的区别：这里把 `None` 明确当作 404。
    只用于"路径参数指定唯一资源"的接口（景点详情、任务详情），
    不用于列表接口——列表查不到结果是空数组而非 404。
    """
    try:
        data = action()
    except ParamError as exc:
        code = CODE_PARAM_OUT_OF_RANGE if getattr(exc, "code", 1001) == CODE_PARAM_OUT_OF_RANGE else CODE_PARAM_INVALID
        return fail(code, str(exc))
    except DatabaseError as exc:
        return fail(CODE_DB_ERROR, f"数据库访问失败：{exc}")
    except Exception as exc:
        return fail(CODE_INTERNAL_ERROR, f"服务内部错误：{type(exc).__name__}")
    if data is None:
        return fail(CODE_NOT_FOUND, not_found_message)
    return ok(data)


__all__ = ["respond", "respond_one"]
