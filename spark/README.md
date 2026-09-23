# Spark 离线分析模块（C-SPK-01~05 · 已实现）

本目录是 C4 容器「**Spark 离线分析**」（JVM 进程 / Scala）的实现。
对应组件 `C-SPK-01` 数据加载、`C-SPK-02` 统计聚合、`C-SPK-03` 特征工程、
`C-SPK-04` MLlib 情感基线、`C-SPK-05` LDA 主题发现。

> 状态：**五个组件均已实现并完成 59,033 条全量运行**（2026-09-23）。
> 本文件以**当前实际代码**为准；与设计文档的差异见第 11 节。

---

## 1. 模块作用

把 Python 阶段清洗后的评论数据，用 Spark 做**全量离线分析与建模**，结果写入 MySQL 结果表，
供后续 Flask 接口与前端图表只读查询。

两条硬约束（答辩要点，来自 `C4容器图.md` §3）：

1. **本容器不发起任何 HTTP 调用**，即不接触 DeepSeek；
2. **与 Python 侧通过 MySQL 解耦**，任一侧可单独重跑，职责不重叠。

```
最终数据集 CSV（只读）
   │ ① Python 清洗（C-BAT-01~04，阶段一/二，已完成）
   ▼
MySQL：spot(837) / review(59,033)
   │ ② 本模块（C-SPK-01~05）
   ▼
MySQL 结果表：stat_spot / stat_time / stat_ip / sentiment / topic / topic_word
   │ ③ 后续：Python 语义抽取 → 事实包 → 景点评价 → Flask 接口与前端
   ▼
```

## 2. C-SPK-01~05 对应关系（组件 → 源码）

| 组件 | 组件名 | 源文件 | 产出 |
|---|---|---|---|
| `C-SPK-01` | 数据加载 | `Cspk01DataLoad.scala` | 统一 schema 的评论 DataFrame + 景点维表 |
| `C-SPK-02` | 统计聚合 | `Cspk02Stats.scala` | `stat_spot` / `stat_time` / `stat_ip` |
| `C-SPK-03` | 特征工程 | `Cspk03Features.scala` | 分词与 TF-IDF 特征（**不单独产出表**） |
| `C-SPK-04` | MLlib 情感基线 | `Cspk04Sentiment.scala` | `sentiment(method='mllib')` + `analysis_task` 指标 |
| `C-SPK-05` | LDA 主题 | `Cspk05Topics.scala` | `topic` / `topic_word` |

辅助文件（不属于 C-SPK 编号，是工程支撑）：

| 文件 | 职责 |
|---|---|
| `Config.scala` | 唯一配置入口：读 `spark/config.env` 或项目根 `.env`，**代码内无口令、无绝对路径** |
| `Db.scala` | 唯一的 JDBC 出口：连接管理、批量写入、幂等清理、任务登记 |
| `SparkEnv.scala` | SparkSession 工厂（`local[*]`、时区 Asia/Shanghai、日志降噪） |
| `MainArgs.scala` | 命令行参数解析（`--source` / `--stage` / `--limit` / `--skip-db-write` 等） |
| `Main.scala` | 统一入口，按 `--stage` 串联组件 |
| `SelfCheck.scala` | 写库后的结果自检（核心表未被破坏 + 结果一致性） |
| `src/main/resources/stopwords_zh.txt` | 中文停用词表（282 词，项目自建，随 jar 打包） |
| `pom.xml` | Maven 构建（Spark 依赖 provided；jieba 打进作业 jar） |
| `tools/setup_spark_env.ps1` | 一键准备环境（下载 Maven 与 MySQL 驱动，幂等可重跑） |
| `scripts/run_spark.ps1` | 一键编译 + 提交作业 |

## 3. 数据输入

| 来源 | 位置 | 说明 |
|---|---|---|
| **MySQL（默认）** | 库 `tibet_review` 的 `review` + `spot` | 依据详细设计 §8.1/§8.2 与 C4「通过 MySQL 解耦」 |
| 清洗版 CSV（可选） | `data/旅游评论数据集_清洗版_v1.csv` | 阶段二产物（59,033 行 × 23 列），用 `--source csv` 切换 |

两条路产出**列名、列数、类型完全一致**的 DataFrame，下游组件无需分支。
CSV 路径会先做**表头逐列校验**——因为"列数相同但顺序不同"时 Spark 不会报错，只会静默错位
（本项目实际踩过：schema 少 4 列导致 `is_low_info` 读到 `publish_month`）。

## 4. 数据输出（结果表，全部为实测行数）

| 表 | 行数 | 口径要点 | 幂等键 |
|---|---|---|---|
| `stat_spot` | 837 | 一景一行；好评率=4–5 星占比、差评率=1–2 星占比（分母只算有评分评论） | `PK(spot_id)` |
| `stat_time` | 3,088 | year/month × global/spot；合计均等于 59,033 | `UK(period_type, period, scope_type, scope_id)` |
| `stat_ip` | 3,235 | **仅 2022-08 后**（BR-01）；分母 34,578 = 35,098 − 520 条"未知" | `UK(scope_type, scope_id, ip_province)` |
| `sentiment` | 47,110 | `method='mllib'`，三分类极性；**不写** `stat_spot.sentiment_*`（那是 DeepSeek 口径） | `PK(comment_id, method)` |
| `topic` | 265 | global 5 个 + 52 个景点各 5 个 | `UK(scope_type, scope_id, topic_index)` |
| `topic_word` | 2,650 | 每个主题 Top10 主题词 | 随 `topic` 重建 |

另：`analysis_task` 记录任务与**模型实验指标**（`model_type` / `model_version` / `model_path` /
`random_seed` / `model_metrics_json`），`task_log` 记录每步计数。

> **模型持久化**：`CountVectorizerModel`、`IDFModel`、朴素贝叶斯模型写入
> `data/spark/models/`（已 gitignore，约数 MB），用于说明"同一套词表可复现"。

## 5. 如何编译

```powershell
# ① 首次准备环境（下载 Maven 3.9.9 与 mysql-connector-j 8.0.33，幂等可重跑）
powershell -ExecutionPolicy Bypass -File spark\tools\setup_spark_env.ps1

# ② 编译打包（脚本会自动调用，也可单独执行）
cd spark
D:\bigdata\apache-maven-3.9.9\bin\mvn.cmd clean package
# 产物：spark\target\spark-offline-analysis-1.0.0.jar（自含 jieba）
```

## 6. 如何运行

```powershell
# 方式一：一键脚本（推荐；内部会先编译再 spark-submit）
powershell -ExecutionPolicy Bypass -File spark\scripts\run_spark.ps1 -Source mysql -Stage all
```

```powershell
# 方式二：直接用 spark-submit（在项目根目录执行）
spark-submit --class com.tibet.tourism.spark.Main --master "local[*]" `
  --driver-memory 2g --driver-java-options "-Dfile.encoding=UTF-8" `
  --jars "D:\bigdata\jars\mysql-connector-j-8.0.33.jar" `
  spark\target\spark-offline-analysis-1.0.0.jar --source mysql --stage all
```

参数说明：

| 参数 | 取值 | 说明 |
|---|---|---|
| `--source` | `mysql`（默认）/ `csv` | 数据源 |
| `--stage` | `2` / `3` / `4` / `5` / `all`（默认） | 只跑某一环，便于单独重跑 |
| `--limit` | N | 小样本验证（**随机抽样**，固定种子 42；不要用于最终结果） |
| `--skip-db-write` | — | 只计算不写库（验证用，确保不动数据库） |
| `--dry-run` | — | 等价于 `--skip-db-write --limit 2000` |

## 7. 小样本测试方式

```powershell
# 推荐第一步：2000 条随机样本，不写库，验证整条链路
powershell -ExecutionPolicy Bypass -File spark\scripts\run_spark.ps1 -Source csv -Limit 2000 -SkipDbWrite
```

小样本下会自动**跳过 57 个景点级 LDA**（样本不足，避免堆出不可信的主题模型），
并把 `CountVectorizer` 的 `minDF` 从 2 降级为 1（否则窄样本下词表为空）。

## 8. 全量运行方式与实测结果

```powershell
powershell -ExecutionPolicy Bypass -File spark\scripts\run_spark.ps1 -Source mysql -Stage all
```

| 项 | 实测值 |
|---|---|
| 输入评论数 | 59,033（行数校验通过，NR-R-03） |
| 建模输入 | 47,148（剔除低信息量 10,228、重复正文 4,239） |
| 词表规模 / TF-IDF 维度 | 28,382 词（minDF=2） |
| 有效 token 总数 | 837,042（平均每行 17.8 个） |
| 朴素贝叶斯测试集准确率 | 78.13%（F1 0.819，加权精确率 0.8822） |
| 情感极性分布 | positive 34,719 / neutral 6,374 / negative 6,017 |
| LDA | global 5 主题 + 52 个景点 × 5 主题 |
| **全量耗时** | **352 秒（约 5.9 分钟）**，`local[*]` |
| 核心表校验和 | `spot` / `review` 的 CRC32 与运行前**完全一致**（未被破坏） |

## 9. 当前已实现 / 尚未实现

**已实现**：`C-SPK-01`～`C-SPK-05` 全部五个组件，以及结果自检、幂等重跑、双数据源。

**尚未实现**（属后续阶段，不在本模块范围内）：

- `stat_time.sentiment_avg` 未填 —— 该列语义是"情感均值（正=1/中=0.5/负=0）"，
  需由 MLlib 判定结果驱动；为保持组件单一职责，`C-SPK-02` 不依赖 `C-SPK-04` 的输出。
- `stat_overview` 未重建 —— 设计 §4.8/§8.2 的 `C-SPK-02` 输出清单未包含它（详见第 11 节）。
- `stat_spot.sentiment_positive/neutral/negative` 未填 —— 口径是 **DeepSeek** 占比，留待阶段五。
- MLlib 情感**未做**与 DeepSeek 的对比实验（DeepSeek 侧尚未实现）。

## 10. 答辩需要重点理解的内容

1. **为什么 Scala 而不用 PySpark**：PySpark 3.3.1 官方仅验证到 Python 3.10，本机为 3.11.7；且作业以 Maven 构件交付，Scala 更贴合 Spark 原生生态。
2. **为什么锁死 Spark 3.3.1 / JDK 1.8 / Scala 2.13**：3.3.x 是最后一个正式支持 Java 8 的版本线；Scala 2.13 决定所有构件必须用 `_2.13` 后缀。本地 scalac 为 2.13.10，与 Spark 自带的 2.13.8 二进制兼容。
3. **为什么 jieba 要打进作业 jar**：Spark 任务执行使用独立 classloader，只设 driver 类路径不够；而 Windows 上给 `spark-submit` 传多个 jar 不稳定（实测 `Invalid argument`）。shade 进 jar 一次解决。
4. **TF-IDF 与 LDA 用的是两套特征**：`C-SPK-04` 用 TF-IDF 向量；`C-SPK-05` 用 `CountVectorizer` 的**词频**——LDA 的生成过程建立在词计数上，喂 TF-IDF 会破坏其概率假设。两者共用同一套分词与词表。
5. **星级是弱标签**：1–2 星=negative、3 星=neutral、4–5 星=positive。准确率只代表"模型复现星级划分"的能力，**不等同于真实情感识别精度**，论文中须如实说明这一局限。
6. **BR-01 的两个口径不要混淆**：`stat_overview.valid_ip_sample = 35,098`（2022-08 后全部，含"未知"）与 `stat_ip.sample_size = 34,578`（再排除 520 条"未知"）。
7. **Spark 不接触 DeepSeek**：与 MLlib 是"基线 + 增强"关系——MLlib 回答"整体情感如何"，DeepSeek 回答"评论在说什么、游客在乎什么"，二者做对比实验而非替代。
8. **可复现**：固定随机种子 42（划分与 LDA），模型版本与实验指标写入 `analysis_task`，可用建表脚本 §9 的 SQL 直接复核。

## 11. 与设计文档的差异与实现选择（如实记录）

设计文档未细化或存在冲突之处，本模块采用的取值与理由：

| # | 事项 | 设计文档 | 本模块实现 | 理由 |
|---|---|---|---|---|
| 1 | 数据源 | §8.1/§8.2 写 MySQL | 默认 MySQL，**保留 `--source csv`** | 遵守设计；CSV 开关便于无库环境复现，两者列集合一致 |
| 2 | `stat_overview` | 表注释写"由离线任务重建"，但 §4.8/§8.2 的 C-SPK-02 输出清单未列它 | **未重建**（保留阶段一占位行的表结构） | 组件级设计未把它列为产出，本阶段不擅自扩大范围。该表的聚合字段目前仍为 0，建议在后续阶段确认归属 |
| 3 | 主题数 K | **未规定** | 默认 5，可由 `SPARK_LDA_TOPICS` 配置 | 属建模选择，非设计硬性规定 |
| 4 | 时间粒度 | `period_type` 注释为 year/month | year + month（global）；仅 year（spot） | 837 景点 × 上百月会产生大量样本量 1–2 条的行，无展示价值 |
| 5 | 景点级 LDA 范围 | 未细化 | 仅 `has_full_evaluation=1` 且可建模评论 ≥100 的 **52 个**景点 | 与 BR-02「≥100 条才做完整评价」一致；57 个景点中有 5 个剔除低信息量/重复后不足 100 条 |
| 6 | 情感算法族 | 只要求"三分类 + 固定种子 + 记录指标" | 多项式朴素贝叶斯（multinomial NB） | 文本 TF-IDF 高维场景训练快、基线稳；属冻结范围内的实现选择 |
| 7 | 停用词表 | 只说"停用词过滤"，未规定来源 | 项目自建精简词表（282 词，纳入版本管理） | 通用大词表会剔除仍有信息量的词；自建表可解释、可复现 |
| 8 | `stat_time.sentiment_avg` | 表结构有此列 | **不填**（NULL） | 其数据来源是 C-SPK-04，保持 C-SPK-02 不依赖 04 的单一职责 |
| 9 | 全局 LDA 主题可解释性 | — | 5 个主题词面较接近（均以"景色/西藏/风景/雪山/海拔"为主） | 语料以短评与高星集中为主，主题区分度有限。**如实记录**，未为了"好看"而调参或改口径 |

> 第 2 项（`stat_overview` 归属）属**设计文档与实际实现之间的待确认问题**，
> 已同步记录在开发日报中，未擅自修改设计文档或数据库结构。
