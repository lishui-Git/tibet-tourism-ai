# -*- coding: utf-8 -*-
"""DeepSeek 统一调用封装（详细设计说明书 §7.2）。

本文件属于 C4 组件 `C-API-12`「DeepSeek 调用封装」的 Python 实现，
被 `C-BAT-05`（评论语义分析）与 `C-BAT-07`（景点智能评价）共同使用。

输入：`messages`（system + user）、`temperature`、`max_tokens`
输出：`ChatResult(content, usage, latency_ms, attempts)` 或抛出 `LlmError`

统一处理的异常（§7.5）：
    网络异常 / HTTP 错误(4xx,5xx) / API 返回体错误 / 空响应 / 超时 / 限流(429)

三条硬约束：
    1. **API Key 只来自 `app.config.settings`（即 .env）**，本文件不读环境变量；
    2. **任何日志、异常文本都不得包含 API Key**——错误信息只带状态码与 URL 路径；
    3. 参数类错误（400/401/403/404/422）**不重试**，避免无意义计费与等待。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

import requests

from app.config import settings


# ---------------------------------------------------------------------------
# 一、数据结构
# ---------------------------------------------------------------------------


@dataclass
class ChatResult:
    """一次成功调用的结果。

    `usage` 用于成本核算（阶段四要求记录真实 token 用量）。
    """

    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)  # prompt/completion/total_tokens
    latency_ms: int = 0
    attempts: int = 1  # 实际尝试次数（1 表示一次成功）

    @property
    def total_tokens(self) -> int:
        return int(self.usage.get("total_tokens") or 0)


class LlmError(RuntimeError):
    """模型调用失败的统一异常。

    分类（`kind`）便于上层决定"是否重试/是否记失败"：
        network      网络不可达、连接重置、超时
        rate_limit   429 限流
        server       5xx 服务端错误
        client       4xx 参数/鉴权错误（**不重试**）
        bad_response 返回体不是预期结构或内容为空
    """

    def __init__(self, message: str, kind: str = "unknown", status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status

    @property
    def retryable(self) -> bool:
        """是否属于"可重试"类别（§7.2：仅网络错误、5xx、限流重试）。"""
        return self.kind in {"network", "rate_limit", "server", "bad_response"}


@dataclass
class CallStats:
    """进程内调用统计（小样本阶段要如实汇报请求数、token、耗时、重试数）。"""

    requests: int = 0          # 逻辑调用次数（不含重试）
    attempts: int = 0          # 实际 HTTP 请求次数（含重试）
    success: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_ms: int = 0
    failures_by_kind: dict[str, int] = field(default_factory=dict)

    def record_success(self, result: ChatResult) -> None:
        self.requests += 1
        self.attempts += result.attempts
        self.success += 1
        self.prompt_tokens += int(result.usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(result.usage.get("completion_tokens") or 0)
        self.elapsed_ms += result.latency_ms

    def record_failure(self, attempts: int, kind: str) -> None:
        self.requests += 1
        self.attempts += max(attempts, 1)
        self.failed += 1
        self.failures_by_kind[kind] = self.failures_by_kind.get(kind, 0) + 1

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def retry_count(self) -> int:
        """重试次数 = 实际请求数 − 逻辑调用数。"""
        return max(self.attempts - self.requests, 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "attempts": self.attempts,
            "retry_count": self.retry_count,
            "success": self.success,
            "failed": self.failed,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "elapsed_ms": self.elapsed_ms,
            "failures_by_kind": dict(self.failures_by_kind),
        }


class ChatClient(Protocol):
    """调用方依赖的最小接口：便于用 `MockClient` 做不消耗额度的链路联调。"""

    def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> ChatResult:  # pragma: no cover - 协议声明
        ...


# ---------------------------------------------------------------------------
# 二、真实客户端
# ---------------------------------------------------------------------------


class DeepSeekClient:
    """DeepSeek 官方 API 客户端（`/chat/completions`）。

    重试策略（§7.2）：最多 `settings.deepseek.max_retry` 次（默认 2），
    退避间隔 2s、5s；429 若带 `Retry-After` 则优先采用。
    """

    # 重试退避基线（秒），与设计文档 §7.2「间隔 2s / 5s」一致
    BACKOFF_SECONDS: tuple[float, ...] = (2.0, 5.0)
    # 429 限流时额外拉长等待，避免继续触发限流
    RATE_LIMIT_BACKOFF: tuple[float, ...] = (5.0, 15.0)

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        max_retry: int | None = None,
        session: requests.Session | None = None,
    ) -> None:
        cfg = settings.deepseek
        self.api_key = api_key if api_key is not None else cfg.api_key
        self.base_url = (base_url or cfg.base_url).rstrip("/")
        self.model = model or cfg.model
        self.timeout = timeout or cfg.timeout
        self.max_retry = cfg.max_retry if max_retry is None else max_retry
        self._session = session or requests.Session()

    # -- 对外只读属性，便于自检与日志（**不含 Key 本身**） --------------------
    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def summary(self) -> str:
        """可安全打印的配置摘要（**绝不包含 Key**）。"""
        return (
            f"model={self.model} endpoint={self.endpoint} "
            f"timeout={self.timeout}s max_retry={self.max_retry} key={'已配置' if self.is_configured else '未配置'}"
        )

    # -- 主入口 ---------------------------------------------------------------
    def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> ChatResult:
        """发起一次对话补全请求。失败时抛 `LlmError`（已重试到上限）。"""
        if not self.is_configured:
            # 属于配置类错误，不重试：直接抛出，由调用方降级处理（NR-R-05）
            raise LlmError(
                "APP_DEEPSEEK_API_KEY 未配置：请在项目根目录 .env 中填写后再执行真实调用",
                kind="client",
            )

        payload = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_error: LlmError | None = None
        for attempt in range(self.max_retry + 1):
            started = time.perf_counter()
            try:
                response = self._session.post(
                    self.endpoint,
                    headers=headers,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=self.timeout,
                )
                latency_ms = int((time.perf_counter() - started) * 1000)
                return self._parse_response(response, latency_ms, attempt + 1)
            except LlmError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
                wait = self._backoff_seconds(attempt, exc)
            except requests.exceptions.Timeout as exc:
                last_error = LlmError(f"请求超时（{self.timeout}s）", kind="network")
                wait = self._backoff_seconds(attempt, last_error)
            except requests.exceptions.RequestException as exc:
                # 只保留异常类型与简要原因，绝不带上 headers（含 Key）
                last_error = LlmError(f"网络异常：{type(exc).__name__}: {exc}", kind="network")
                wait = self._backoff_seconds(attempt, last_error)

            if attempt >= self.max_retry:
                break
            time.sleep(wait)

        assert last_error is not None  # 循环必然赋值
        raise last_error

    # -- 内部工具 -------------------------------------------------------------
    def _backoff_seconds(self, attempt: int, error: LlmError) -> float:
        """按错误类型取退避时长（限流更久；网络/服务端 2s、5s）。"""
        base = self.RATE_LIMIT_BACKOFF if error.kind == "rate_limit" else self.BACKOFF_SECONDS
        index = min(attempt, len(base) - 1)
        return base[index]

    def _parse_response(self, response: "requests.Response", latency_ms: int, attempts: int) -> ChatResult:
        """把 HTTP 响应解析为 `ChatResult`，异常统一转换为 `LlmError`。

        注意：错误信息里**只放状态码与响应体片段**（DeepSeek 的报错不含 Key），
        不放请求头，避免 Key 泄漏到日志。
        """
        status = response.status_code
        if status == 429:
            raise LlmError(f"限流(429)：{self._short(response.text)}", kind="rate_limit", status=status)
        if status >= 500:
            raise LlmError(f"服务端错误({status})：{self._short(response.text)}", kind="server", status=status)
        if status >= 400:
            raise LlmError(f"请求被拒绝({status})：{self._short(response.text)}", kind="client", status=status)

        try:
            body = response.json()
        except ValueError as exc:
            raise LlmError(f"响应不是合法 JSON：{self._short(response.text)}", kind="bad_response") from exc

        if not isinstance(body, dict):
            raise LlmError("响应结构异常：顶层不是对象", kind="bad_response")

        choices = body.get("choices") or []
        if not choices:
            raise LlmError(f"响应缺少 choices：{self._short(response.text)}", kind="bad_response")

        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            raise LlmError("模型返回内容为空", kind="bad_response")

        usage_raw = body.get("usage") or {}
        usage = {
            "prompt_tokens": int(usage_raw.get("prompt_tokens") or 0),
            "completion_tokens": int(usage_raw.get("completion_tokens") or 0),
            "total_tokens": int(usage_raw.get("total_tokens") or 0),
        }
        return ChatResult(
            content=content,
            model=str(body.get("model") or self.model),
            usage=usage,
            latency_ms=latency_ms,
            attempts=attempts,
        )

    @staticmethod
    def _short(text: str, limit: int = 200) -> str:
        """截断响应文本，避免把整段返回写进日志。"""
        text = (text or "").replace("\n", " ").strip()
        return text[:limit] + ("…" if len(text) > limit else "")


__all__ = ["ChatResult", "LlmError", "CallStats", "ChatClient", "DeepSeekClient"]
