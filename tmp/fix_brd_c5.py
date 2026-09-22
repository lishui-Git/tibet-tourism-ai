# -*- coding: utf-8 -*-
"""最小改动修补 `开题/业务需求文档.docx` 中与 `业务需求文档.md` 不一致的事实表述。

依据 `ReadMe.md` 工作守则第 10 条「已交付文档用手工改过时，改用"最小改动"方式」：
**不整篇重新生成**（会重建文档结构、冲掉手工格式），只替换 document.xml 中的
指定文本串；其余 zip 条目（[Content_Types].xml、rels、media、styles …）逐字节原样复制。

当前待修补项（`业务需求文档.md` 是唯一内容源，以 .md 为准）：

  ① 版本号：.md 头部已是 V1.1，而 docx 正文页眉块仍为 V1.0（docx 封面块此前已被单独
     改为 V1.1，导致同一文档出现两个不同版本号）→ 统一为 V1.1。

  ② C5 表述：.md 已把"17.33% 不足 10 字"改为"17.33% 为 ≤10 字"。按最终数据集全量核验，
     **正文长度 ≤ 10 字**才是 10,228 条（占 17.33%）；严格"< 10 字"仅 8,747 条，与 17.33% 不符。
     （本项在 2026-09-22 已修补完成，脚本可重复运行，已应用项会自动跳过。）

安全检查：
  · 改前先把原文件备份到 tmp/（重要数据变更前备份）；
  · 每条替换的旧串必须**恰好出现 1 次**，否则中止，避免误改；
  · 涉事文本均位于**单个 `<w:t>` 运行内**，不跨 run、不涉及图片与关系 Id（rId），
    因此无需处理 media/rels；
  · 改后重新解析 docx，确认旧串归零、新串各出现 1 次，其余条目逐字节未变。

用法：
    python tmp/fix_brd_c5.py --dry-run   # 只检查不写入
    python tmp/fix_brd_c5.py             # 执行
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DOCX = PROJECT_ROOT / "开题" / "业务需求文档.docx"
BACKUP_DIR = PROJECT_ROOT / "tmp"
TARGET_ENTRY = "word/document.xml"

#: (说明, 旧串, 新串) —— 旧串在 document.xml 中必须恰好出现 1 次
REPLACEMENTS: list[tuple[str, str, str]] = [
    (
        "版本号统一为 V1.1（与 .md 一致）",
        "：V1.0",
        "：V1.1",
    ),
    (
        "C5 表述改为 ≤10 字（与 .md 及全量核验结果一致）",
        "评论正文短小，中位数 31 字，17.33% 不足 10 字",
        "评论正文短小，中位数 31 字，17.33% 为 ≤10 字",
    ),
]


def read_entry(zf: zipfile.ZipFile, name: str) -> str:
    with zf.open(name) as fp:
        return fp.read().decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="最小改动修补 BRD docx 中的事实表述")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不写入")
    args = parser.parse_args(argv)

    if not DOCX.exists():
        print(f"✘ 目标文件不存在：{DOCX}")
        return 2

    # 1) 独占打开探测（守则第 9 条：写入前先探测是否被 WPS／Word 占用）
    try:
        with DOCX.open("r+b"):
            pass
    except PermissionError:
        print(f"✘ 文件被占用（WPS／Word 已打开）：{DOCX}")
        print("  请先关闭该文档后重试。本脚本不会强制关闭 Office，也不会删除 ~$ 锁文件。")
        return 3

    # 2) 读取并逐条检查
    with zipfile.ZipFile(DOCX, "r") as zf:
        names = zf.namelist()
        xml = read_entry(zf, TARGET_ENTRY)

    pending: list[tuple[str, str, str, int, int]] = []
    for label, old, new in REPLACEMENTS:
        old_n, new_n = xml.count(old), xml.count(new)
        if old_n == 0 and new_n >= 1:
            print(f"· 已应用，跳过：{label}")
            continue
        if old_n != 1:
            print(f"✘ 旧串出现 {old_n} 次（要求恰好 1 次），已中止：{label}")
            print(f"    旧串：{old!r}")
            return 4
        pending.append((label, old, new, old_n, new_n))
        print(f"· 待应用：{label}  （旧串 ×{old_n} → 新串 ×{new_n + old_n}）")

    if not pending:
        print("\n✔ 无需修补，文档已与 .md 一致。")
        return 0

    if args.dry_run:
        print(f"\n--dry-run：将应用 {len(pending)} 条替换，但未写入任何内容。")
        return 0

    # 3) 备份
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / f"业务需求文档_备份_{stamp}.docx"
    shutil.copy2(DOCX, backup)
    print(f"\n· 已备份原文件 → {backup.relative_to(PROJECT_ROOT)}")

    # 4) 重建 zip：仅替换 document.xml
    new_xml = xml
    for _, old, new, _, _ in pending:
        new_xml = new_xml.replace(old, new)

    tmp_out = DOCX.with_suffix(".docx.tmp")
    with zipfile.ZipFile(DOCX, "r") as src, zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == TARGET_ENTRY:
                data = new_xml.encode("utf-8")
            dst.writestr(info, data)  # 保留原压缩方式与时间戳

    # 5) 校验：非目标条目逐字节未变；每条替换的旧串归零、新串恰好增加 1 次
    #    注意：新串可能原本就存在（例如封面块已是 V1.1），故按"增量"校验而非"必须为 1"。
    ok = True
    with zipfile.ZipFile(DOCX, "r") as orig_zf, zipfile.ZipFile(tmp_out, "r") as zf:
        if zf.namelist() != names:
            print("✘ 条目列表发生变化！")
            ok = False
        for name in names:
            if name != TARGET_ENTRY and zf.read(name) != orig_zf.read(name):
                print(f"✘ 非目标条目被改动：{name}")
                ok = False
        check = read_entry(zf, TARGET_ENTRY)

    for label, old, new, old_before, new_before in pending:
        got_old, got_new = check.count(old), check.count(new)
        if got_old != old_before - 1:
            print(f"✘ 旧串剩余 {got_old} 次（期望 {old_before - 1} 次）：{label}")
            ok = False
        if got_new != new_before + 1:
            print(f"✘ 新串出现 {got_new} 次（期望 {new_before + 1} 次）：{label}")
            ok = False

    if not ok:
        tmp_out.unlink(missing_ok=True)
        print("✘ 校验未通过，已放弃写入，原文件未被改动。")
        return 5

    tmp_out.replace(DOCX)
    print(f"· 校验通过：其余 {len(names) - 1} 个 zip 条目逐字节未变")
    for label, old, new, _, _ in pending:
        print(f"  - {label}\n      旧：{old}\n      新：{new}")
    print(f"\n✔ 已更新：{DOCX.relative_to(PROJECT_ROOT)}")
    print("  提示：本文档含 Word 目录域，若目录中相关文字需同步，请在 Word 中右键「更新域」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
