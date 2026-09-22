# -*- coding: utf-8 -*-
import sys, zipfile, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

z = zipfile.ZipFile(r"E:\tibet-tourism-ai\开题\业务需求文档.docx")
x = z.read("word/document.xml").decode("utf-8")
t = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", x, re.S))

print("=== 残留 markdown 标记检查 ===")
for pat in ["**", "##", "|---", "| ---", "```", "->|"]:
    print(f"  {pat!r}: {t.count(pat)} 次")

print("\n=== 关键标题是否生成 ===")
for k in ["业务需求文档", "1. 引言", "2. 项目概述", "3. 数据需求", "4. 用户角色与用例",
          "5. 功能需求", "6. 非功能需求", "7. 业务规则汇总", "8. 需求追溯矩阵",
          "9. 范围外事项", "10. 附录"]:
    print(f"  {'OK ' if k in t else '缺 '} {k}")

print("\n=== 需求编号统计 ===")
for pre in ["FR-OV-", "FR-SA-", "FR-IE-", "FR-CP-", "FR-QA-", "FR-SY-", "NR-P-", "NR-C-",
            "NR-R-", "NR-S-", "NR-U-", "NR-M-", "BR-", "UC-", "S1", "S2"]:
    print(f"  {pre:8s} {t.count(pre)} 次")

print("\n=== 正文样例 ===")
i = t.find("1. 引言")
print(repr(t[i:i + 200]))
print("\n=== 总字数 ===", len(t))
