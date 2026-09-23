# scripts/ —— 辅助脚本

> 本目录**不属于** C4 架构中的任何容器，是开发与验收用的辅助工具。
> 它们**反向依赖** `app/`（读取配置与数据），而 `app/` 不依赖它们。

---

## 1. 模块职责

回答三个问题：

| 脚本 | 回答的问题 |
|---|---|
| `check_env.py` | 「这台机器现在能不能跑这个项目？」——环境、数据库、表结构、数据是否就位 |
| `verify_cleaned.py` | 「阶段二的清洗产物对不对？」——按设计口径重算并逐项比对 |
| `run_dev.ps1` | 「怎么一键把项目跑起来？」——自检 → 启动 Flask |

## 2. 主要文件

| 文件 | 行数 | 类型 | 说明 |
|---|---|---|---|
| `check_env.py` | 289 | Python | 环境自检（对应非功能需求 **NR-M-04**），6 组检查 |
| `verify_cleaned.py` | 260 | Python | 清洗产物复核（对应 `C-BAT-02` 数据校验思想），6 类复核项 |
| `run_dev.ps1` | 49 | PowerShell | 一键：虚拟环境检查 → 环境自检 → 启动开发服务器 |

## 3. 输入 / 输出

**`check_env.py`**（只读，不写任何数据）

| 组 | 检查内容 | 输入来源 |
|---|---|---|
| 1 | Python 版本（3.11.7）与项目依赖 | 当前解释器 + `requirements.txt` |
| 2 | 配置与数据源（`.env`、最终数据集、来源标注、重复清单是否存在） | `app.config.settings` |
| 3 | MySQL 服务与库 `tibet_review` | `app.db` |
| 4 | 表结构与约束基线（17 表 / 17 主键 / 7 唯一 / 14 外键） | 脚本内 `EXPECTED_*` 常量 + 库 |
| 5 | 数据导入现状（`spot` 837 / `review` 59,033） | 库 |
| 6 | 大数据环境（JDK / Spark / Scala）——**未就绪只提示，不判失败** | 系统 PATH 与环境变量 |

退出码：`0` = 就绪；非 `0` = 有未通过项（`run_dev.ps1` 据此只警告、不阻断）。

**`verify_cleaned.py`**（只读）

| 项 | 内容 |
|---|---|
| 输入 | `数据采集/旅游评论数据集_最终版.csv` + `data/旅游评论数据集_清洗版_v1.csv` |
| 输出 | 终端比对结果与 `[OK]` / `[FAIL]` 结论；退出码 `0` = 全部一致、`1` = 有不符、`2` = 产物不存在 |
| 复核方式 | **不信任脚本自报的统计**，重新读源文件与产物、按设计口径独立重算 |

复核的 7 类：① 规模（59,033 行 / 23 列 / 编号无重复）② 评论级 11 字段逐行重算
③ 景点级 **3 个**字段（地址 / 开放时间 / 景点介绍）按代表值规则跨全量重算
④ 派生字段自洽（`content_length` 与正文长度、`is_low_info` 与 ≤10 字口径、日期格式）
⑤ 关键口径 9 项 ⑥ 分布与范围（**仅打印，不参与成败判定**）⑦ 结论。

> **官方电话（第 4 个景点级字段）不在复核范围内**：它的口径含 `spot.phone VARCHAR(64)` 的截断守卫，
> 本脚本不重复实现该规则，只核对非空。这一点已在脚本 docstring 中注明。

**`run_dev.ps1`**（不做破坏性操作，不改数据库）

## 4. 调用关系

```
run_dev.ps1 ──> check_env.py ──> app.config（惰性导入）
                             └─> app.db（惰性导入：query_all）

verify_cleaned.py ──> app.batch.import_dataset（只取设计基线常量 + norm_text）
                  └─> app.config.settings（路径）

app/* ──×──> scripts/*        （app 不依赖 scripts，方向单一）
```

`check_env.py` 与 `verify_cleaned.py` 都在**函数内部惰性导入** `app.*`，
这样即使把脚本单独拷到别处、或只想看 `--help`，也不会因为在错误的目录下运行而导入失败。

## 5. 如何运行

```powershell
# 环境自检（6 组，只读）
.\.venv\Scripts\python.exe scripts\check_env.py

# 清洗产物复核（只读）
.\.venv\Scripts\python.exe scripts\verify_cleaned.py
.\.venv\Scripts\python.exe scripts\verify_cleaned.py --cleaned data\旅游评论数据集_清洗版_v1.csv

# 一键：自检 → 启动开发服务器
powershell -ExecutionPolicy Bypass -File scripts\run_dev.ps1
```

> Windows 控制台默认代码页为 GBK，脚本会输出 `✔` / `✘` / `⚠` 等符号，
> 因此三个脚本**都显式把 stdout 切到 UTF-8**（`sys.stdout.reconfigure` / `[Console]::OutputEncoding`），
> 否则会抛 `UnicodeEncodeError` 并中断在第 1 项。

## 6. 对应项目设计中的组件

| 脚本 | 对应设计 |
|---|---|
| `check_env.py` | 非功能需求 **NR-M-04「环境自检脚本」**；其检查基线取自 `docs/database/建表脚本.sql`（17 表 / 17 主键 / 7 唯一 / 14 外键） |
| `verify_cleaned.py` | 体现 `C-BAT-02` 数据校验思想；期望值取自 `docs/diagrams/数据处理流程图.md` §2 的实测结果 |
| `run_dev.ps1` | 无对应组件（纯开发便捷脚本） |

## 7. 与其他模块的关系

- **← `app/`**：读取 `app.config.settings`（路径、数据库）与 `app.db`（只读查询）、复用 `app.batch.import_dataset` 的规范化函数与设计基线常量——**不复制口径**。
- **→ 数据库**：只做查询（`SELECT` / `information_schema`），**不写库**。
- **与 `tmp/` 的区别**：`tmp/` 是文档生成与设计校验脚本（非运行代码，且已被 `.gitignore` 排除部分大文件）；`scripts/` 是**随项目交付、面向运行与验收**的正式脚本。

## 8. 当前已实现 / 未实现

**已实现**：上述 3 个脚本，均已实测跑通
- `check_env.py` → 6 组检查全绿，输出「环境就绪，可以继续开发。」
- `verify_cleaned.py` → `[OK] 清洗产物与设计口径完全一致`（约 2.4 秒）

**未实现**（后续阶段可能需要）：
- 阶段四～六的验收脚本（Spark 统计口径复核、DeepSeek 语义结果抽检、26 个接口的回归测试）
- 单元测试目录 `tests/`（当前项目未建立测试框架；验证以「可重复运行的自检脚本 + 实测复核」方式进行）

## 9. 答辩时重点理解

1. **"自检脚本"是设计要求的交付物**，不是随手写的工具：NR-M-04 明确要求环境自检，`check_env.py` 就是它的实现，答辩时可以直接演示。
2. **为什么复核脚本要"不信自己算的数"**：`verify_cleaned.py` 重新读源文件、按设计口径**独立重算**再比对，而不是读取清洗脚本生成的统计 JSON。这样脚本本身出错时也能被发现——这是"数据可信度"的证据。
3. **退出码是有意义的**：`check_env.py` 的退出码供 `run_dev.ps1` 判断是否警告；`verify_cleaned.py` 的退出码可供后续 CI 或流水线使用。
4. **基线常量不是随手写的数字**：`check_env.py` 顶部的 `EXPECTED_TABLE_COUNT=17`、`EXPECTED_PK_COUNT=17`、`EXPECTED_UNIQUE_COUNT=7`、`EXPECTED_FK_COUNT=14`、`EXPECTED_SPOT_ROWS=837`、`EXPECTED_REVIEW_ROWS=59033`，以及 `verify_cleaned.py` 的 `EXPECTED_COUNTS`（部分从 `app.batch.import_dataset` 导入，保证与清洗脚本同源），全部来自设计文档的实测值——一旦库或产物与设计不符就会报错，起到"看门狗"作用。
5. **只读原则**：两个 Python 脚本都只读不写，任何"写"的操作都留在 `app/batch/` 与数据库导入脚本里，便于审计。
