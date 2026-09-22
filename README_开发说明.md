# 开发环境与代码骨架说明（阶段一）

> 本文件面向**开发操作**，说明怎么把项目跑起来。
> 项目全局规范（AI 工作守则、数据冻结、开发日报规则）见根目录 `ReadMe.md`；
> 设计依据见 `docs/`；数据来源见 `数据采集/`。
>
> 阶段一完成时间：2026-09-22

---

## 一、一分钟跑起来

在 VS Code 终端中，切到项目根目录 `E:\tibet-tourism-ai`，依次执行：

```powershell
# 1) 创建虚拟环境并安装依赖（首次执行一次即可）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) 环境自检（Python / .env / MySQL / 17 张表 / 数据导入现状）
.\.venv\Scripts\python.exe scripts\check_env.py

# 3) 启动 Flask 开发服务器
.\.venv\Scripts\python.exe run.py
```

浏览器打开 <http://127.0.0.1:5000/>，点页面上的「运行自检」按钮；
或在终端直接访问两个自检接口：

```powershell
curl.exe http://127.0.0.1:5000/healthz
curl.exe http://127.0.0.1:5000/api/db-ping
```

也可以一步到位（自检 + 启动）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_dev.ps1
```

---

## 二、环境基线（本机实测）

| 项 | 实测值 |
|---|---|
| 操作系统 | Windows |
| Python | 3.11.7（虚拟环境 `.venv`） |
| MySQL | 8.0.32 @ `127.0.0.1:3306`，库 `tibet_review`，`utf8mb4` |
| JDK | 1.8.0_371 |
| Spark | `spark-submit` 已在 PATH（阶段四使用） |
| HADOOP_HOME | `D:\bigdata\hadoop-3.3.0`（已设置，winutils 就绪） |
| Git | 2.53.0（仓库已在项目根初始化） |
| 编辑器 | VS Code 1.137.0 |

> 依据：`项目现状分析.md` §8.1–§8.4、`docs/architecture/C4容器图.md` §2。

---

## 三、目录结构（阶段一建立的部分）

```
E:\tibet-tourism-ai\
├─ app\                        后端应用（C4 容器「Flask Web 应用」+「Python 批处理服务」）
│  ├─ __init__.py              包说明与版本号
│  ├─ config.py                集中配置（唯一读 .env 的入口，NR-M-01）
│  ├─ db.py                    数据访问组件 C-API-13（参数化 SQL、连接管理）
│  ├─ batch\                   Python 批处理（C-BAT-01 ~ C-BAT-07）
│  │  └─ import_dataset.py     ★ 阶段一核心：CSV → spot / review 导入
│  └─ web\                     Flask Web 应用
│     ├─ __init__.py           create_app() 应用工厂
│     ├─ response.py           统一响应体 {code,message,data}（详细设计 §6.3／§6.4）
│     ├─ routes\               接口层（health.py 自检、pages.py 页面）
│     ├─ templates\            Jinja2 模板（index.html 骨架页）
│     └─ static\               原生 CSS / JS（不引入前端框架与构建链）
├─ scripts\
│  ├─ check_env.py             环境自检（NR-M-04）
│  └─ run_dev.ps1              一键：激活环境 → 自检 → 启动
├─ spark\                      阶段四（Scala + Maven）预留，当前仅 README
├─ data\                       清洗/导入产物目录（已 gitignore）
├─ logs\                       运行日志目录（已 gitignore）
├─ .venv\                      虚拟环境（已 gitignore）
├─ .env                        本机真实配置（已 gitignore，**不提交**）
├─ .env.example                配置样例（入库）
├─ .gitignore                  Git 忽略规则（入库）
├─ requirements.txt            Python 依赖（入库）
├─ run.py                      Flask 启动入口
└─ （既有）docs\ 数据采集\ 开题\ 开发日报\ 爬虫工具\ 分析脚本\ tmp\ ReadMe.md 项目现状分析.md
```

**分层依据**：`docs/design/详细设计说明书.md` §2 与 `docs/architecture/C4组件图.md`。
代码只承载 C4 中**已定义**的组件；阶段一未实现的组件不建空文件，避免"有文件无实现"。

---

## 四、配置说明

配置集中由 `app/config.py` 读取，**其他模块不得直接读环境变量**（NR-M-01）。

| 配置组 | 关键项 | 说明 |
|---|---|---|
| `settings.db` | `DB_HOST/PORT/USER/PASSWORD/NAME/CHARSET` | 口令只写在 `.env`，代码与日志中一律不打印 |
| `settings.paths` | `DATA_DIR`、`FINAL_DATASET`、`OUTPUT_DIR`、`LOG_DIR` | `数据采集/` 为**只读冻结区**（BR-11） |
| `settings.deepseek` | `DEEPSEEK_API_KEY`、`BASE_URL`、`MODEL` | 阶段一不调用；Key 未填写时生成类功能应降级而非报错（NR-R-05） |
| `settings.web` | `FLASK_HOST/PORT/DEBUG` | 默认 `127.0.0.1:5000` |

> ✅ 口令边界：本地真实数据库口令只存在于 `.env` 中（`.env` 已被 `.gitignore` 排除，不提交 Git、从未入库）；
> 项目文档一律不记录明文口令——`项目现状分析.md` §8.3 等处的表述均为"口令见本地 `.env`（不写入文档、不提交仓库）"。
> 已实测核对：`.env` 中的口令值在 `项目现状分析.md` 全文中命中 0 次。

---

## 五、数据导入脚本用法

```powershell
# ① 先试运行：只解析、只统计，不写库（推荐第一步）
.\.venv\Scripts\python.exe -m app.batch.import_dataset --mode sample --limit 200 --dry-run

# ② 抽样导入 200 条评论（景点表按全量口径写入 837 行）
.\.venv\Scripts\python.exe -m app.batch.import_dataset --mode sample --limit 200

# ③ 全量导入（强制行数校验必须等于 59,033，否则终止且不写任何数据）
.\.venv\Scripts\python.exe -m app.batch.import_dataset --mode full

# ④ 清空两表后重导（破坏性，必须加 --yes 确认）
.\.venv\Scripts\python.exe -m app.batch.import_dataset --mode full --reset --yes
```

### 5.1 写入范围（阶段一）

只写 **`spot`（837 行）** 与 **`review`（抽样 N 行 / 全量 59,033 行）** 两张**核心数据表**，
以及运维表 `analysis_task`（每跑一次登记 1 行）与 `task_log`（每跑一次登记 4 行，含 1 条可能的 WARN）。
其余 13 张表由后续阶段（Spark 统计、DeepSeek 语义、评价生成等）写入。

### 5.2 字段映射与派生口径

CSV 的 15 列在 `spot` / `review` 中的落库位置，与 `docs/设计阶段一致性检查报告.md` §4.1 逐字段核对结果一致。

**景点级 4 字段的代表值规则**（本脚本新增，设计文档只给了结果值未给算法）：
> ① 只考虑规范化后非空的取值；② 出现次数最多者优先；③ 次数并列取最长；④ 仍并列取首次出现。

**派生字段**：

| 字段 | 口径 |
|---|---|
| `content_length` | `len(content.strip())`，空正文记 0 |
| `publish_year` / `publish_month` | 由 `publish_date` 拆分 |
| `ip_is_unknown` | `ip_location == '未知'` 置 1 |
| `ip_province` | **仅当 `publish_date >= 2022-08-01` 且归属地非"未知"**时填写（BR-01） |
| `is_low_info` | 正文非空且长度 **≤ 10** 时为 1（BR-05） |
| `is_dup_content` / `dup_group_id` | 取自 `数据采集/内容重复清单.csv`（BR-06） |
| `spot.review_count` | 该景点在**全量** CSV 中的评论条数 |
| `spot.has_full_evaluation` | `review_count >= 100`（BR-02，共 57 个） |

**空值策略**：空字符串一律写 NULL（可空列），**缺失即 NULL、不填补、不造数**。

### 5.3 官方电话的长度守卫（需要你知道的一个真实问题）

`spot.phone` 是 `VARCHAR(64)`，而 CSV 中「羊卓雍错」的电话原文（含换行与页面标签"全部"）有 **67 字**，
在 MySQL 严格模式（本机 `sql_mode` 含 `STRICT_TRANS_TABLES,TRADITIONAL`）下会直接报
`1406 Data too long for column 'phone'` 并**中断整个导入**。

处理方式（**未修改冻结的表结构**）：
1. 按换行/Tab 拆分片段，丢弃页面 UI 残留片段（"全部"等），以 `; ` 重连；
2. 合并后仍超 64 字时（实测仅此 1 个景点，合并为 65 字），退化保留**首个片段**（47 字，含两个票务电话）；
3. 该截断写入一条 `level=WARN` 的 `task_log`，可随时复核。

### 5.4 幂等与断点续跑

两张表都用 `INSERT ... ON DUPLICATE KEY UPDATE` 写入（`spot` 以 `spot_name`、`review` 以 `comment_id` 去重），
**重复执行不产生重复行**。已实测：连续执行两次 `--mode sample --limit 200` 后仍是 `spot=837 / review=200`。

---

## 六、阶段一验收结果（实测）

| 验收项 | 期望 | 实测 | 结果 |
|---|---|---|---|
| MySQL 连接 | 8.0.32 / utf8mb4 | 8.0.32 / utf8mb4 | ✔ |
| 表数量 | 17 | 17 | ✔ |
| 主键 / 唯一 / 外键 | 17 / 7 / 14 | 17 / 7 / 14 | ✔ |
| `spot` 行数 | 837 | 837 | ✔ |
| `spot.source_scope` | tibet 554 / route 283 | 554 / 283 | ✔ |
| `spot.has_full_evaluation` | 57 | 57 | ✔ |
| 景点级字段覆盖 | 地址 837 / 开放时间 546 / 电话 99 / 介绍 738 | 完全相同 | ✔ |
| `review` 行数（抽样） | 200 | 200 | ✔ |
| **`review` 行数（全量）** | **59,033** | **59,033** | ✔ |
| **全量导入后 18 项业务口径** | **全部一致** | 10,228／4,239／1,285／37／1／24,025／34,578／1,829／50,562 | ✔ |
| 评分↔评分描述 错配 | 0 | 0 | ✔ |
| 电话超长景点 | 0 | 0 | ✔ |
| 外键失配评论数 | 0 | 0 | ✔ |
| Flask `/healthz`、`/api/db-ping`、`/` | 200 | 200 | ✔ |
| 重复执行不产生重复行 | 是 | 是 | ✔ |

> **全量导入已完成并校验通过（2026-09-22 11:33，耗时 4 秒）**：`analysis_task` 中登记为
> `task_id=4`、`task_name=CSV 导入 spot/review（mode=full）`、`status=success`、`success_count=59033`。
> 脚本内置的 18 项校验**全部 OK**（含上述全部评论级口径），因此 `spot` / `review` 两张核心表的
> 业务口径已与设计文档完全对齐，可以进入阶段三。
>
> 复验方式（不写库，只跑校验）：
> ```powershell
> .\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'.'); from app.batch.import_dataset import verify_import, print_verification; print_verification(verify_import('full'))"
> ```

---

## 七、当前未做的事（避免误读进度）

- ✘ 完整前端（只有一张骨架页；5 个业务页面未做）
- ✘ 完整 REST 接口（详细设计 §6.2 的 26 个接口未实现，只做了 2 个自检口）
- ✘ DeepSeek 业务逻辑（无 `app/llm/`，无任何模型调用）
- ✘ Spark 离线分析（`spark/` 仅 README）
- ✘ 数据清洗的完整实现（低信息量/重复标记已在导入中完成；分词、分档统计、清洗日志、清洗后版本文件输出等未做）
- ✘ 14 张结果表的写入（统计、情感、方面、主题、事实包、评价报告、问答记录）—— 阶段三之后逐步写入
- ✘ `spot.poi_url` 填充（CSV 无此列；`携程西藏景点清单.csv` 仅覆盖 283/837，待后续阶段）
- ✔ **已完成**：`spot` 837 行、`review` 59,033 行全量导入，18 项业务口径全部校验通过

---

## 八、常见问题排查

| 现象 | 原因与处理 |
|---|---|
| `数据库口令未配置` | `.env` 未创建或 `DB_PASSWORD` 为空 → `copy .env.example .env` 后填写 |
| `Access denied for user` | `.env` 中口令不正确 |
| `Unknown database 'tibet_review'` | 库未创建 → 先执行 `docs/database/建表脚本.sql` 中的建库语句 |
| `Data too long for column` | 已由脚本内置电话守卫处理；若换字段报错请把完整报错发给 AI |
| 中文乱码 | 确认库/表为 `utf8mb4`、连接 `charset=utf8mb4` |
| `mysql.exe` 读不了中文路径 | 执行 SQL 脚本前先复制到纯 ASCII 路径（如 `C:\Windows\Temp\`） |
| 端口 5000 被占用 | 改 `.env` 的 `FLASK_PORT` |

---

## 九、相关文档索引

| 用途 | 文件 |
|---|---|
| 项目全局规范（必读） | `ReadMe.md` |
| 技术路线定稿依据 | `项目现状分析.md` |
| 功能范围与业务规则 | `开题/业务需求文档.md` |
| 架构与组件编号 | `docs/architecture/C4架构视图.md` 等 4 份 |
| 模块、接口、DeepSeek 调用设计 | `docs/design/详细设计说明书.md` |
| 表结构与字段口径 | `docs/database/数据库设计说明.md`、`ER图.md`、`建表脚本.sql` |
| 处理流程 | `docs/diagrams/` 7 份 |
| 设计与数据的一致性核对 | `docs/设计阶段一致性检查报告.md` |
| 数据来源与质量 | `数据采集/采集工作报告.md`、`最终数据质量报告.md` |
