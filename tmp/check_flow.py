# -*- coding: utf-8 -*-
"""校验景点对比流程图的节点引用完整性"""
import sys, re, glob
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def check(path):
    t = open(path, encoding="utf-8").read()
    print("=" * 70)
    print(path.split("\\")[-1])
    print("=" * 70)
    blocks = re.findall(r"```mermaid\n(.*?)```", t, re.S)
    for bi, body in enumerate(blocks, 1):
        # 节点定义：ID 后面紧跟 [ { ( > 等形状
        ids = re.findall(r"\b([A-Z][A-Za-z0-9_]*)\s*[\[\{\(]", body)
        # 去掉 classDef / class / style 行
        code_lines = [l for l in body.split("\n")
                      if not re.match(r"\s*(classDef|class|style|linkStyle)\b", l)]
        code = "\n".join(code_lines)
        ids = re.findall(r"\b([A-Z][A-Za-z0-9_]*)\s*[\[\{\(]", code)
        defined = list(dict.fromkeys(ids))

        # 引用：出现在箭头两侧的标识符
        refs = set()
        for m in re.finditer(r"([A-Z][A-Za-z0-9_]*)\s*-->", code):
            refs.add(m.group(1))
        for m in re.finditer(r"-->\s*(?:\|[^|]*\|\s*)?([A-Z][A-Za-z0-9_]*)", code):
            refs.add(m.group(1))
        # `A & B -->` 形式
        for m in re.finditer(r"^\s*([A-Z][A-Za-z0-9_]*(?:\s*&\s*[A-Z][A-Za-z0-9_]*)+)\s*-->", code, re.M):
            for x in re.split(r"\s*&\s*", m.group(1)):
                refs.add(x)

        d, r = set(defined), refs
        print(f"  图{bi}: 定义 {len(d)} 个节点")
        undef = sorted(r - d)
        unused = sorted(d - r)
        print(f"    被引用但未定义: {undef if undef else '无'}")
        print(f"    定义但未被引用: {unused if unused else '无'}")

    # 已删除表名检查
    for name in ["spot_comparison", "ml_model", "llm_failure", "`clean_log`"]:
        if name in t:
            for i, ln in enumerate(t.split("\n"), 1):
                if name in ln:
                    print(f"  ⚠ 行{i} 仍含 {name}: {ln.strip()[:100]}")


for p in sorted(glob.glob(r"E:\tibet-tourism-ai\docs\diagrams\*.md")) + [
        r"E:\tibet-tourism-ai\docs\design\详细设计说明书.md"]:
    check(p)
