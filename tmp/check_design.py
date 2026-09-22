# -*- coding: utf-8 -*-
"""
设计阶段文档一致性交叉检查
检查项：Mermaid 块完整性、禁用技术、模块编号一致性、关键数据一致性、接口一致性
"""
import sys, os, re, glob, json
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = r"E:\tibet-tourism-ai"
DOCS = os.path.join(ROOT, "docs")
files = []
for sub in ("architecture", "design", "database", "diagrams"):
    files += sorted(glob.glob(os.path.join(DOCS, sub, "*.md")))
files += [os.path.join(ROOT, "开题", "业务需求文档.md")]
files = [f for f in files if os.path.exists(f)]

print("=" * 78)
print("被检查文档：")
for f in files:
    print("  ", os.path.relpath(f, ROOT), f"({os.path.getsize(f)} 字节)")
print("=" * 78)

contents = {}
for f in files:
    contents[os.path.relpath(f, ROOT)] = open(f, encoding="utf-8").read()

# ---------- 1. Mermaid 代码块完整性 ----------
print("\n【检查 1】Mermaid 代码块完整性")
total_blocks = 0
for name, txt in contents.items():
    opens = len(re.findall(r"^```mermaid\s*$", txt, re.M))
    total_blocks += opens
    # 围栏配对
    fences = len(re.findall(r"^```", txt, re.M))
    status = "OK " if fences % 2 == 0 else "异常"
    print(f"  {status} {name:44s} mermaid 块 {opens:2d} 个，围栏总数 {fences}（偶数=配对正常）")
print(f"  → Mermaid 图总数：{total_blocks}")

# 逐块做基础语法检查
print("\n【检查 2】Mermaid 图类型与首行声明")
type_counter = {}
for name, txt in contents.items():
    for m in re.finditer(r"^```mermaid\s*\n(.*?)^```", txt, re.M | re.S):
        body = m.group(1).strip().split("\n")
        first = body[0].strip() if body else ""
        kind = first.split()[0] if first else "(空)"
        type_counter[kind] = type_counter.get(kind, 0) + 1
        if kind not in ("flowchart", "graph", "erDiagram", "sequenceDiagram", "classDiagram", "stateDiagram-v2"):
            print(f"  ⚠ {name}: 未知图类型 {kind!r}")
for k, v in sorted(type_counter.items(), key=lambda x: -x[1]):
    print(f"  {k:20s} {v} 个")

# ---------- 3. 禁用技术检查 ----------
print("\n【检查 3】禁用技术组件扫描（需人工确认是否仅为'不使用'声明）")
FORBIDDEN = ["Kafka", "Flume", "Spark Streaming", "Flink", "Hadoop", "Hive", "HBase",
             "Redis", "Docker", "Kubernetes", "K8s", "Elasticsearch", "RAG", "Agent",
             "向量数据库", "微服务", "消息队列"]
NEG_MARK = ["不引入", "不使用", "不采用", "禁止", "不得", "未使用", "无实际", "范围外",
            "排除", "红线", "无需", "不做", "不建", "不设计", "技术栈红线", "不需要"]
hits = {}
for name, txt in contents.items():
    lines = txt.split("\n")
    for li, line in enumerate(lines):
        for term in FORBIDDEN:
            if term not in line:
                continue
            # 语境 = 本行 + 上一行 + 下一行（标题常在上/下一行）
            ctx = "\n".join(lines[max(0, li - 1):li + 2])
            negated = any(k in ctx for k in NEG_MARK)
            hits.setdefault(term, []).append((name, negated, line.strip()[:100]))

ok_all = True
for term in FORBIDDEN:
    if term not in hits:
        continue
    total = len(hits[term])
    bad = [h for h in hits[term] if not h[1]]
    if bad:
        ok_all = False
        print(f"  ⚠ {term}: 共 {total} 处，其中 {len(bad)} 处**非否定语境**：")
        for name, _, line in bad[:4]:
            print(f"      - {name}: {line}")
    else:
        print(f"  OK {term}: 共 {total} 处，全部为'不使用/禁止'语境")
if ok_all:
    print("  → 结论：未发现实际引入禁用组件的设计")

# ---------- 4. 模块编号一致性 ----------
print("\n【检查 4】C4 组件编号在详细设计中的覆盖情况")
c4 = contents.get(os.path.join("docs", "architecture", "C4组件图.md").replace("\\", os.sep), "")
c4 = contents.get("docs\\architecture\\C4组件图.md") or contents.get("docs/architecture/C4组件图.md") or ""
design = contents.get("docs\\design\\详细设计说明书.md") or contents.get("docs/design/详细设计说明书.md") or ""
if not c4:
    c4 = [v for k, v in contents.items() if "C4组件图" in k][0]
if not design:
    design = [v for k, v in contents.items() if "详细设计" in k][0]

api_ids = sorted(set(re.findall(r"C-API-\d+", c4)))
bat_ids = sorted(set(re.findall(r"C-BAT-\d+", c4)))
spk_ids = sorted(set(re.findall(r"C-SPK-\d+", c4)))
all_ids = api_ids + bat_ids + spk_ids
print(f"  C4 组件图定义：C-API {len(api_ids)} 个 {api_ids[:3]}…、"
      f"C-BAT {len(bat_ids)} 个、C-SPK {len(spk_ids)} 个")
print(f"  组件编号合计：{len(all_ids)} 个")
missing = []
for cid in all_ids:
    if cid not in design:
        missing.append(cid)
if missing:
    print(f"  ⚠ 详细设计中未出现的组件编号：{missing}")
else:
    print(f"  OK 全部 {len(all_ids)} 个组件编号均在详细设计中被引用")

# ---------- 5. 关键数据一致性 ----------
print("\n【检查 5】关键数据口径一致性")
KEY_FACTS = {
    "评论总数 59033": ["59,033", "59033"],
    "景点总数 837": ["837"],
    "重点景点 57": ["57 个景点", "57个景点", "57 个"],
    "有效IP样本 35098": ["35,098", "35098"],
    "低信息量 10228/17.33%": ["10,228", "17.33%"],
    "重复 1285组/4239条": ["1,285", "4,239"],
    "5星占比 71.72%": ["71.72%"],
    "正文中位数 31字": ["31 字", "31字"],
}
for label, variants in KEY_FACTS.items():
    cnt = 0
    for txt in contents.values():
        for v in variants:
            cnt += txt.count(v)
    flag = "OK " if cnt > 0 else "⚠ "
    print(f"  {flag} {label:28s} 在文档中出现 {cnt} 次")

# 检查是否有与口径冲突的数字
print("\n  潜在冲突数字扫描（应仅出现在'范围外/已撤销'语境）：")
for bad in ["58,000", "60,000 条", "6万条评论", "1000 个景点"]:
    for name, txt in contents.items():
        for m in re.finditer(re.escape(bad), txt):
            ls = txt.rfind("\n", 0, m.start()) + 1
            le = txt.find("\n", m.end())
            print(f"    ⚠ {name}: {txt[ls:le if le>0 else len(txt)].strip()[:90]}")

# ---------- 6. 接口一致性 ----------
print("\n【检查 6】详细设计接口清单 ↔ 前后端交互页面对应")
apis = sorted(set(re.findall(r"`(/(?:api/)[a-zA-Z0-9_\-{}/]+)`", design)))
apis += sorted(set(re.findall(r"`(GET|POST)\s+(/api/[a-zA-Z0-9_\-{}/]+)`", design) and
                    [m[1] for m in re.findall(r"`(GET|POST)\s+(/api/[a-zA-Z0-9_\-{}/]+)`", design)]))
apis = sorted(set(apis))
print(f"  详细设计中出现的接口路径：{len(apis)} 个")
for a in apis:
    print(f"    {a}")

# 开发日报/BRD 中提到的模块与详细设计模块表对照
print("\n【检查 7】BRD 功能模块 ↔ 详细设计模块划分")
brd = [v for k, v in contents.items() if "业务需求文档" in k][0]
brd_mods = re.findall(r"\|\s*(M\d)\s*\|\s*([^|]+?)\s*\|", brd)
print(f"  BRD 模块：{[m[0] for m in brd_mods]}")
design_mods = re.findall(r"\|\s*(M\d)\s*\|\s*([^|]+?)\s*\|", design)
print(f"  详细设计模块：{[m[0] for m in design_mods]}")
bd = set(m[0] for m in brd_mods)
dd = set(m[0] for m in design_mods)
print(f"  BRD 有而详细设计无：{sorted(bd - dd) if bd - dd else '无'}")
print(f"  详细设计有而 BRD 无：{sorted(dd - bd) if dd - bd else '无'}")

print("\n" + "=" * 78)
print("检查完成")
print("=" * 78)
