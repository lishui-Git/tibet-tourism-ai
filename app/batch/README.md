# app/batch/ —— Python 批处理服务

> 本目录对应 C4 容器「**Python 批处理服务**」，承载组件 `C-BAT-01`～`C-BAT-07`。
> 上级说明见 [`../README.md`](../README.md)。

---

## 1. 模块职责

把**离线**数据任务串成可重复执行的流程：CSV → 校验 → 规范化 → 拆分 → 分层标记 → 入库 / 产出文件。
服务对象是后续的 Spark 分析、DeepSeek 语义分析与 Web 展示，三者都通过 MySQL 取数。

## 2. 主要文件

| 文件 | 行数 | 阶段 | 职责 |
|---|---|---|---|
| `__init__.py` | 26 | — | 包说明与 C-BAT-01~07 的实现进度表 |
| `import_dataset.py` | 926 | 阶段一 | 最终数据集 CSV → `spot` / `review` 两张核心表（**唯一写核心表的入口**） |
| `clean_dataset.py` | 773 | 阶段二 | 清洗与预处理 SOP：产出清洗版文件 + 统计报告（**默认不写核心表**） |

## 3. 输入 / 输出

**`import_dataset.py`**

| 项 | 内容 |
|---|---|
| 输入 | `数据采集/旅游评论数据集_最终版.csv`（59,033 行 × 15 列，UTF-8 with BOM，**只读**）<br>`数据采集/景点来源标注.csv`（景点 → 西藏/进藏沿线）<br>`数据采集/内容重复清单.csv`（1,285 组 / 4,239 条） |
| 输出 | MySQL `spot`(837 行)、`review`(59,033 行)；运维表 `analysis_task`(1 行)、`task_log`(每步 1 行，实测 7 行/次) |

**`clean_dataset.py`**

| 项 | 内容 |
|---|---|
| 输入 | 同上三份 CSV（**只读**，不写库） |
| 输出 | `data/旅游评论数据集_清洗版_v1.csv`（59,033 行 × 23 列）<br>`data/…_v1_清洗统计.json`（机器可读）<br>`data/…_v1_清洗报告.md`（人工可读，可作论文「数据清洗」章节素材）<br>`--log-task` 时应本人要求登记 `analysis_task` + `task_log` |

> `data/` 已被 `.gitignore` 排除：产物约 114 MB，超过 Git 托管平台单文件 100 MB 上限，
> 以本地文件为唯一权威副本（与原始数据集同样的处置方式）。

## 4. 调用关系与执行顺序

```
import_dataset.py
   ├─ 读 app.config.settings（路径与数据库配置）
   ├─ 读 app.db.connection（写库）
   └─ 流程：读取 → 行数校验(59,033) → 构建 spot → 写 spot → 回读 spot_id
            → 构建 review → 写 review → 写 task_log → 18 项校验

clean_dataset.py
   ├─ 复用 import_dataset 的规范化函数（norm_text / parse_date / normalize_ip /
   │   pick_representative / pick_phone / build_spots / build_reviews）—— 不复制第二套口径
   └─ 流程：7 步 SOP（读取源文件 → 行数校验 → 字段规范化 → 评论级景点级拆分
            → 去重与分层 → 写出规范化文件 → 输出统计报告）
```

**注意**：`import_dataset.py` 的规范化函数是**唯一实现**，`clean_dataset.py` 只做编排与产物输出。
若需修改清洗口径，只改一处，不要在两处各写一份（否则必然漂移）。

## 5. 关键数据处理规则（答辩要点）

| 规则 | 口径 | 依据 |
|---|---|---|
| 行数校验前置且强制 | 全量必须 == 59,033，否则**终止且不写任何数据** | NR-R-03 |
| 景点级 4 字段取代表值 | 非空 → 出现次数最多 → 并列取最长 → 再并列取首次出现（同一景点内完全重复，故取代表值消除冗余） | BR-09 |
| `content_length` | `len(content.strip())`，空正文记 0 | 数据处理流程图 §2 |
| `publish_year/month` | 由 `publish_date` 拆分；日期异常行**跳过并计数**，不静默丢弃 | 流程图 §4 |
| `ip_province` | **仅当** `publish_date >= 2022-08-01` 且归属地非"未知"时填写（携程 2022-08 起才展示 IP） | BR-01 |
| `ip_is_unknown` | `ip_location == '未知'` → 1（24,025 条 / 40.70%） | — |
| `is_low_info` | 正文非空且长度 **≤ 10** → 1（10,228 条 / 17.33%） | BR-05 |
| `is_dup_content` / `dup_group_id` | 取自 `内容重复清单.csv`，**不在脚本内重新判定**，避免两套结果 | BR-06 |
| `has_full_evaluation` | `review_count >= 100` → 1（57 个景点） | BR-02 |
| 缺失值 | 空字符串一律 NULL，**缺失即 NULL、不填补、不造数** | 流程图 §3 |
| 标记而非删除 | 低信息量、重复正文**保留在库**只打标记（双口径：统计用全量，文本建模按标记过滤） | 流程图 §3 |
| 官方电话长度守卫 | `spot.phone` 为 VARCHAR(64)，超长时按片段合并 → 仍超限则退化保留首片段，并写 `level=WARN` 的 `task_log` | 实测（羊卓雍错 67 字） |

## 6. 如何运行

```powershell
# ---------- 阶段一：数据导入（会写库） ----------
# ① 只解析不写库（推荐第一步）
.\.venv\Scripts\python.exe -m app.batch.import_dataset --mode sample --limit 200 --dry-run
# ② 抽样导入 200 条评论
.\.venv\Scripts\python.exe -m app.batch.import_dataset --mode sample --limit 200
# ③ 全量导入（强制行数校验）
.\.venv\Scripts\python.exe -m app.batch.import_dataset --mode full
# ④ 清空两表后重导（破坏性，必须加 --yes）
.\.venv\Scripts\python.exe -m app.batch.import_dataset --mode full --reset --yes

# ---------- 阶段二：清洗与预处理（默认不写库） ----------
# ① 小样本验证逻辑
.\.venv\Scripts\python.exe -m app.batch.clean_dataset --limit 200
# ② 全量（行数校验不等 59,033 即终止且不产出文件）
.\.venv\Scripts\python.exe -m app.batch.clean_dataset
# ③ 覆盖已有产物（默认**不覆盖**，已存在时 exit=2）
.\.venv\Scripts\python.exe -m app.batch.clean_dataset --force
# ④ 额外把各步骤计数登记到运维表（只 INSERT，不做 UPDATE/DELETE）
.\.venv\Scripts\python.exe -m app.batch.clean_dataset --force --log-task

# 产出复核
.\.venv\Scripts\python.exe scripts\verify_cleaned.py
```

**幂等与断点续跑**：两个脚本都设计为可重复执行——
`import_dataset.py` 用 `INSERT ... ON DUPLICATE KEY UPDATE`（`spot` 以 `spot_name`、`review` 以 `comment_id` 去重），
重复执行不产生重复行；`clean_dataset.py` 覆盖产物需显式 `--force`，避免误覆盖。

## 7. 对应项目设计中的组件

| 组件 | 实现位置 |
|---|---|
| `C-BAT-01` 任务编排与断点续跑 | `clean_dataset.py` 的 7 步 SOP 编排（`SOP_STAGES` / `StageRecorder`） |
| `C-BAT-02` 数据校验 | 两个脚本的行数/表头校验；`clean_dataset.py` 的 `validate_row_count` / `validate_columns` / `validate_spot_level` |
| `C-BAT-03` 清洗与规范化 | `import_dataset.py` 的字段映射与派生；`clean_dataset.py` 的拆分与规范化文件输出 |
| `C-BAT-04` 去重与分层 | 低信息量（BR-05）与重复正文（BR-06）标记 |
| `C-BAT-05/06/07` | **未实现**（语义抽取、事实包构造、评价生成 → 阶段五） |

## 8. 与其他模块的关系

- **→ MySQL**：唯一持久化出口。`clean_dataset.py` 默认不写核心表，只读 CSV 产出文件。
- **→ `spark/`**：Spark 从 `review` 表或清洗版 CSV 取数；清洗规则（含派生字段与分层标记）**已在 Python 侧算好**，Spark 不重复实现。
- **→ `scripts/`**：`scripts/verify_cleaned.py` 复核本目录的清洗产物；`scripts/check_env.py` 校验库结构是否与设计一致。
- **← `app/config.py`**：所有路径与数据库配置均来自 `settings.paths` / `settings.db`，**代码内无绝对路径**。

## 9. 当前已实现 / 未实现

**已实现**：`C-BAT-01`～`C-BAT-04`（阶段一 + 阶段二，均已实测跑通全量）
- 阶段一：`spot` 837 行、`review` 59,033 行全量导入，18 项校验全部通过
- 阶段二：清洗产物 59,033 行 × 23 列，丢弃 0 / 异常 0，关键口径 9 项与设计实测值完全一致

**未实现**：`C-BAT-05`（DeepSeek 语义抽取）、`C-BAT-06`（事实包构造）、`C-BAT-07`（景点评价生成）——阶段五。

> **分词不在本目录实现**（已定论）：中文分词、停用词过滤、TF-IDF 归 Spark 的 `C-SPK-03`，
> Python 侧只做清洗、规范化、质量控制与分层标记。

## 10. 答辩时重点理解

1. **为什么"标记而不删除"**：低信息量（10,228 条）与重复正文（4,239 条）是**真实数据现象**，不是脏数据。删除会破坏统计口径；打标记后统计类功能用全量、文本建模类按标记过滤（双口径），两类需求都满足。
2. **为什么景点级字段要拆出来**：这 4 个字段在同一景点内完全重复，留在评论表既冗余又容易被误当作特征（BR-09）。
3. **为什么 `ip_province` 有 2022-08 这条时间线**：携程自 2022-08 起才展示 IP，此前 100% 为"未知"——不设这条线，客源地分析会被"未知"污染。
4. **断点续跑怎么实现**：`review` 以 `comment_id` 为幂等键 upsert，重跑不产生重复行；`analysis_task` / `task_log` 记录每次执行的任务与每一步的处理量。
5. **两个脚本为什么分开**：阶段一解决"数据进得来"，阶段二解决"数据可解释、可复用、可复核"，且阶段二**不动已入库的核心数据**，降低风险。
