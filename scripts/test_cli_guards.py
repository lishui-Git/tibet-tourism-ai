# -*- coding: utf-8 -*-
"""运行安全闸门测试（preflight / --mock / --offline / 规模确认）。

## 为什么单独测"闸门"
成本安全的实现不只在业务代码里，还在**命令行闸门**上：
没有这些闸门，一次手误就能发起 4.7 万次真实调用。闸门逻辑属于"平时不生效、
关键时刻才起作用"的代码，容易被后续改动无声破坏，因此把实际行为固定下来。

## 覆盖
    A. 规模闸门：`--stage semantic` 无 `--limit`/`--yes` → 拒绝执行；`--yes` 放行
    B. 定向补跑：`--only-ids` / `--spot-ids` 绕过规模闸门（失败项要能单独重试）
    C. mock 闸门：`--mock` 必须配 `--limit ≤ 50`
    D. 离线闸门：`--offline` 阻断真实调用；放行 preflight/check/--dry-run/--mock
    E. 客户端构造：离线时**绝不**构造真实客户端（第二道保险）
    F. mock 结果自带标记：`raw_json.mode='mock'`，可被 preflight 检出

**本测试不发起任何真实调用**：只调用闸门函数与纯构造逻辑。
"""

from __future__ import annotations

import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.batch.semantic_analysis import _build_api_record
from app.llm.__main__ import _build_client, _guard_mock, _guard_offline, _guard_scale, build_parser
from app.llm.validators import SemanticResult

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def guard_blocks(func, args) -> tuple[bool, str]:
    """执行闸门函数：被 SystemExit 拦下返回 (True, 提示语)，否则 (False, '')。"""
    try:
        func(args)
        return False, ""
    except SystemExit as exc:
        return True, str(exc)


def main() -> int:
    parser = build_parser()

    def args_of(*argv: str):
        return parser.parse_args(["--stage", "semantic", *argv])

    print("=" * 84)
    print("运行安全闸门测试（不发起任何真实调用）")
    print("=" * 84)

    # ---------- A. 规模闸门 ----------
    print("\n[A] 规模闸门（无 --limit/--yes 时拒绝全量）")
    blocked, msg = guard_blocks(_guard_scale, args_of())
    check("无 --limit/--yes → 阻断", blocked, msg.splitlines()[0] if msg else "")
    check("阻断提示里给出了下一步做法（--yes 或 --limit）",
          "--yes" in msg and "--limit" in msg, "")
    blocked, _ = guard_blocks(_guard_scale, args_of("--yes"))
    check("加 --yes → 放行", not blocked, "")
    blocked, _ = guard_blocks(_guard_scale, args_of("--limit", "8"))
    check("加 --limit 8 → 放行（小样本）", not blocked, "")

    # ---------- B. 定向补跑绕过规模闸门 ----------
    print("\n[B] 定向补跑（--only-ids / --spot-ids 绕过规模闸门）")
    blocked, _ = guard_blocks(_guard_scale, args_of("--only-ids", "73603914"))
    check("--only-ids → 放行（失败项必须能单独重试）", not blocked, "")
    blocked, _ = guard_blocks(_guard_scale, args_of("--spot-ids", "5"))
    check("--spot-ids → 放行", not blocked, "")

    # ---------- C. mock 闸门 ----------
    print("\n[C] mock 闸门（必须配小样本）")
    blocked, msg = guard_blocks(_guard_mock, args_of("--mock"))
    check("--mock 无 --limit → 阻断", blocked, msg.splitlines()[0] if msg else "")
    blocked, _ = guard_blocks(_guard_mock, args_of("--mock", "--limit", "50"))
    check("--mock --limit 50 → 放行（上限）", not blocked, "")
    blocked, _ = guard_blocks(_guard_mock, args_of("--mock", "--limit", "51"))
    check("--mock --limit 51 → 阻断（超过上限）", blocked, "")

    # ---------- D. 离线闸门 ----------
    print("\n[D] 离线闸门（--offline 阻断真实调用）")
    blocked, msg = guard_blocks(_guard_offline, args_of("--offline"))
    check("--offline + semantic → 阻断", blocked, msg.splitlines()[0] if msg else "")
    blocked, msg = guard_blocks(_guard_offline, args_of("--offline", "--only-ids", "73603914"))
    check("--offline + --only-ids → 也阻断（不得同时绕过两道闸门）", blocked, "")
    check("阻断提示对 --only-ids 给出可照做的替代做法",
          "--dry-run" in msg and "--mock" in msg, "")
    blocked, _ = guard_blocks(_guard_offline, args_of("--offline", "--dry-run"))
    check("--offline + --dry-run → 放行（零调用）", not blocked, "")
    blocked, _ = guard_blocks(_guard_offline, args_of("--offline", "--mock", "--limit", "1"))
    check("--offline + --mock → 放行（假客户端，不可能真实调用）", not blocked, "")

    offline_args = parser.parse_args(["--stage", "preflight", "--offline"])
    blocked, _ = guard_blocks(_guard_offline, offline_args)
    check("--offline + preflight → 放行（只读）", not blocked, "")

    # 环境变量形式：APP_LLM_OFFLINE=1
    os.environ["APP_LLM_OFFLINE"] = "1"
    try:
        blocked, _ = guard_blocks(_guard_offline, args_of())
        check("环境变量 APP_LLM_OFFLINE=1 同样阻断", blocked, "")
    finally:
        os.environ.pop("APP_LLM_OFFLINE", None)

    # ---------- E. 第二道保险：离线时不构造真实客户端 ----------
    print("\n[E] 客户端构造（离线时绝不构造真实客户端）")
    blocked, _ = guard_blocks(_build_client, args_of("--offline"))
    check("--offline → 构造真实客户端被阻断（第二道保险）", blocked, "")
    mock_client = _build_client(args_of("--mock", "--limit", "1"))
    check("--mock → 返回 MockClient（不会真实调用）",
          type(mock_client).__name__ == "MockClient", type(mock_client).__name__)

    # ---------- F. mock 结果自带标记 ----------
    print("\n[F] mock 结果必须自带标记（否则与真实结果无法区分）")
    result = SemanticResult(
        polarity="neutral", intensity=3, is_valid=1, aspects=[], keywords="a", summary="b"
    )
    row = {"comment_id": 1, "spot_id": 1}
    mock_raw = _build_api_record(row, result, {"total_tokens": 10}, mode="mock")["raw"]
    real_raw = _build_api_record(row, result, {"total_tokens": 10})["raw"]
    check("mock 结果写入 mode='mock'", mock_raw.get("mode") == "mock", str(mock_raw.get("mode")))
    check("默认为真实调用 mode='real'", real_raw.get("mode") == "real", str(real_raw.get("mode")))
    check("mock 与真实结果在 raw_json 上可区分", mock_raw.get("mode") != real_raw.get("mode"), "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 84)
    print(f"安全闸门：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 84)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
