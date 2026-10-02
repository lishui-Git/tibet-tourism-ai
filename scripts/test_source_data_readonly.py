# -*- coding: utf-8 -*-
"""BR-11 实证：`数据采集/` 下的冻结件**只读**，任何运行都不得改动它们。

## 为什么需要它
设计纪律 BR-11 规定：原始数据文件是**冻结件**，禁止覆盖或就地修改；
所有清洗/导入产物必须写到 `data/`。此前这条只有"代码里没写 data_dir"的推断，
本测试把它变成**可执行的证据**，从两个角度验证：

    A. **静态**：检查代码中是否存在"对冻结路径的写操作"
       （例如 `settings.paths.final_dataset.write_text(...)`）。
    B. **实证**：记录三份冻结件的**大小 + 修改时间**，然后实际运行
       只读命令（`--stage preflight` / `--stage check`）与配置装配，
       结束后断言**大小与 mtime 一个都没变**。

## 边界
本测试**不运行**会写库/写产物的阶段（如 `--stage semantic`、`clean_dataset`），
因为那需要真实调用或长耗时；写产物的阶段其输出目录由配置固定在 `data/`
（见 `app/config.py` 的三个只读 property 与 `output_dir`）。

全程不调用任何模型。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def frozen_files() -> list[Path]:
    sys.path.insert(0, str(ROOT))
    from app.config import settings

    return [settings.paths.final_dataset, settings.paths.spot_source, settings.paths.dup_list]


def fingerprint(paths: list[Path]) -> dict[str, tuple[int, float]]:
    """(大小, 修改时间) 指纹；文件缺失记为 (-1, -1)（如实反映）。"""
    out: dict[str, tuple[int, float]] = {}
    for p in paths:
        if p.exists():
            st = p.stat()
            out[p.name] = (st.st_size, round(st.st_mtime, 3))
        else:
            out[p.name] = (-1, -1.0)
    return out


def static_scan() -> list[str]:
    """扫描代码里是否存在对冻结路径的写操作。"""
    suspicious: list[str] = []
    write_ops = (".write_text(", ".write_bytes(", ".unlink(", ".rename(", ".replace(")
    # 会打开文件写入的模式
    open_write = ('open("w', "open('w", 'open("a', "open('a", '"wb"', "'wb'")
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # 只看与冻结数据路径相关的行
            if not any(tok in line for tok in ("data_dir", "final_dataset", "spot_source", "dup_list")):
                continue
            if any(op in line for op in write_ops) or any(op in line for op in open_write):
                suspicious.append(f"{path.relative_to(ROOT)}:{lineno}: {stripped[:100]}")
    return suspicious


def main() -> int:
    print("=" * 86)
    print("BR-11 实证：冻结的原始数据文件只读（静态扫描 + 运行前后指纹比对）")
    print("=" * 86)

    paths = frozen_files()
    print("\n冻结件：")
    for p in paths:
        print(f"  {p.name}  （{'存在' if p.exists() else '缺失'}）")

    print("\n[A] 静态扫描：代码中是否存在对冻结路径的写操作")
    suspicious = static_scan()
    check("未发现对冻结路径的写操作（write_text/unlink/open('w') 等）",
          not suspicious, "\n      ".join(suspicious) if suspicious else "扫描 app/ 下全部 .py")

    print("\n[B] 实证：运行只读命令前后，冻结件指纹必须一致")
    before = fingerprint(paths)
    for name, (size, mtime) in before.items():
        print(f"  运行前 {name}: size={size} mtime={mtime}")

    for stage in ("preflight", "check"):
        proc = subprocess.run(
            [PYTHON, "-m", "app.llm", "--stage", stage],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        print(f"  已运行 --stage {stage}（exit={proc.returncode}）")

    after = fingerprint(paths)
    for name, (size, mtime) in after.items():
        print(f"  运行后 {name}: size={size} mtime={mtime}")

    unchanged = {n: (before[n], after[n]) for n in before if before[n] != after[n]}
    check("三份冻结件的大小与修改时间均未变化", not unchanged,
          ("变化：" + ", ".join(f"{k}: {v[0]}→{v[1]}" for k, v in unchanged.items())) if unchanged
          else f"{len(before)} 份文件指纹一致")

    print("\n[C] 产物目录与冻结目录必须分离")
    sys.path.insert(0, str(ROOT))
    from app.config import settings

    check("data_dir 与 output_dir 是两个不同目录",
          settings.paths.data_dir != settings.paths.output_dir,
          f"{settings.paths.data_dir.name} vs {settings.paths.output_dir.name}")
    check("log_dir 与冻结目录分离",
          settings.paths.log_dir != settings.paths.data_dir,
          f"{settings.paths.log_dir.name} vs {settings.paths.data_dir.name}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 86)
    print(f"BR-11 实证：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    if passed == total:
        print("结论：冻结的原始数据只读这一纪律，已由静态扫描 + 运行前后指纹比对证明。")
    print("=" * 86)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
