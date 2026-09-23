# -*- coding: utf-8 -*-
"""假客户端：小样本链路联调专用（**不产生任何真实 API 调用、不消耗额度**）。

存在的理由：
    API Key 尚未填写时，仍然需要把"调用 → 校验 → 写库 → 幂等 → 断点续跑 → 失败记录"
    这条链路完整验证一遍。`MockClient` 让这条链路可以在零成本下被反复测试。

两条纪律（写进 README 与 CLI 限制）：
    1. `--mock` **只允许在小样本（--limit ≤ 50）下使用**，全量执行会被 CLI 拒绝；
    2. 使用 mock 的结果会在 `analysis_task.task_name` 中标注 `[mock]`，
       避免把假数据误当成真实 DeepSeek 结果（论文与答辩中不得引用 mock 结果）。

它同时实现了 `client.ChatClient` 协议，因此业务代码无需任何分支。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time
from typing import Sequence

from app.llm.client import ChatResult, LlmError
from app.llm.prompts import ASPECT_CANDIDATES

# 从 user 消息里取"评论：xxx"的正文，用于生成"evidence 必为原文子串"的合法结果
_CONTENT_RE = re.compile(r"评论：(.*?)(?:\n\n\[输出 JSON Schema\]|\Z)", re.DOTALL)
_SPOT_RE = re.compile(r"景点：(.*?)\n")


class MockClient:
    """确定性假客户端：同一输入 → 同一输出（便于幂等与重跑验证）。"""

    def __init__(self, *, fail_every: int = 0, seed: int = 42) -> None:
        """
        :param fail_every: 每 N 次调用模拟一次不可重试的失败（0 = 不注入失败）。
                           用于验证"单条失败不会导致整个任务崩溃"。
        """
        self.fail_every = fail_every
        self._counter = 0
        self._lock = threading.Lock()  # 并发调用时保证计数不丢失（否则失败注入次数不可预期）
        self._random = random.Random(seed)

    # -- ChatClient 协议 -----------------------------------------------------
    def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> ChatResult:
        with self._lock:
            self._counter += 1
            counter = self._counter
        time.sleep(0.01)  # 模拟最小时延，便于观察并发与进度输出

        if self.fail_every and counter % self.fail_every == 0:
            # 故意抛"不可重试"错误：验证失败被记录、批次继续
            raise LlmError(f"mock 注入失败（第 {counter} 次调用）", kind="server")

        system_text = "".join(m.get("content", "") for m in messages if m.get("role") == "system")
        prompt_chars = sum(len(m.get("content", "")) for m in messages)

        if "景点评论分析报告" in system_text:
            content = self._report_json()
        else:
            content = self._semantic_json(messages)

        # 粗略 token 估算（中文约 1 token ≈ 1.5 字符），仅用于让成本统计链路可跑通
        return ChatResult(
            content=content,
            model="mock",
            usage={
                "prompt_tokens": int(prompt_chars / 1.5),
                "completion_tokens": int(len(content) / 1.5),
                "total_tokens": int((prompt_chars + len(content)) / 1.5),
            },
            latency_ms=10,
            attempts=1,
        )

    # -- 生成假结果 -----------------------------------------------------------
    def _semantic_json(self, messages: Sequence[dict[str, str]]) -> str:
        user_text = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        match = _CONTENT_RE.search(user_text)
        content = (match.group(1).strip() if match else "") or "内容缺失"

        # 用正文哈希决定极性，保证确定性且分布多样
        digest = int(hashlib.md5(content.encode("utf-8")).hexdigest(), 16)
        polarity = ("positive", "neutral", "negative")[digest % 3]
        intensity = digest % 5 + 1

        # evidence 直接取原文片段 → 必然能通过"原文子串"校验
        evidence = content[:12] if len(content) >= 4 else content
        aspect = ASPECT_CANDIDATES[digest % len(ASPECT_CANDIDATES)]
        summary = content[:40]
        keywords = [content[:4], content[4:8]]
        keywords = [w for w in keywords if w]

        return json.dumps(
            {
                "polarity": polarity,
                "intensity": intensity,
                "aspects": [{"aspect": aspect, "polarity": polarity, "evidence": evidence}],
                "keywords": keywords,
                "summary": summary,
            },
            ensure_ascii=False,
        )

    def _report_json(self) -> str:
        summary = (
            "该景点评论样本显示整体评价以正面为主，游客普遍认可核心景观体验与整体氛围，"
            "同时也有部分游客提到门票与排队相关的问题。以上结论仅依据事实包中的统计数据得出，"
            "用于反映评论数据的整体情况，不构成对景点的绝对推荐意见。"
        )
        return json.dumps(
            {
                "summary": summary,
                "advantages": ["核心景观认可度高", "整体评价偏正面"],
                "issues": ["部分游客提到门票相关意见", "高峰期排队体验一般"],
                "visitor_focus": ["景观体验", "门票与排队"],
            },
            ensure_ascii=False,
        )


__all__ = ["MockClient"]
