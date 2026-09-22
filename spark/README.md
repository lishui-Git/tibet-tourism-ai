# Spark 离线分析（阶段四，尚未开始）

本目录是 C4 容器「**Spark 离线分析**」（JVM 进程 / Scala）的预留位置。
**阶段一不在此目录写入任何代码**，仅保留本说明，避免出现「有目录无实现」的假进度。

---

## 1. 为什么这里暂时是空的

按 `docs/design/详细设计说明书.md` §16「设计到开发的对应」给出的既定实现顺序：

1. 建表 → 2. **Python 清洗与数据导入（阶段一～三）** → 3. Spark 离线统计与模型（阶段四）
→ 4. DeepSeek 语义抽取 → 5. 事实包与景点评价 → 6. Flask 接口与前端 → 7. 景点对比
→ 8. 智能问答 → 9. 测试验收

阶段一只做第 1 步的验证与第 2 步的环境骨架，因此 Spark 部分尚未开始。

---

## 2. 已确定的技术方案（不得自行更改）

依据 `docs/architecture/C4容器图.md` §2 与 `项目现状分析.md` §7.3、§8.1、§8.2：

| 项 | 取值 |
|---|---|
| Spark | **3.3.1**（3.3.x 是最后一个正式支持 Java 8 的版本线，**不要升级**） |
| Scala | **2.13.10**，所有 Spark 构件必须用 `_2.13` 后缀 |
| JDK | **1.8** |
| 运行模式 | `local[*]`（单机） |
| JVM 参数 | `-Xms512m -Xmx2g` |
| 语言选择 | **Scala 写 Spark 作业（主）**；PySpark 3.3.1 官方仅验证到 Python 3.10，本机为 3.11.7，故不用 PySpark |
| 构建 | Maven |

必须锁定的依赖坐标：

```
spark-core_2.13     3.3.1
spark-sql_2.13      3.3.1
spark-mllib_2.13    3.3.1
mysql-connector-j   8.0.33
```

---

## 3. Spark 在本项目中的职责（对应 C4 组件）

| 组件编号 | 组件名 | 职责 |
|---|---|---|
| `C-SPK-01` | 数据加载组件 | 从 MySQL 读取清洗后的 `review` 明细 |
| `C-SPK-02` | 统计聚合组件 | Spark SQL 多维聚合 → `stat_overview` / `stat_spot` / `stat_time` / `stat_ip` |
| `C-SPK-03` | 特征工程组件 | 中文分词、停用词、TF-IDF（**共同前置，不单独产出表**） |
| `C-SPK-04` | MLlib 情感基线组件 | 以星级为弱标签训练三分类模型并批量推理 → `sentiment(method='mllib')`，指标写入 `analysis_task.model_metrics_json` |
| `C-SPK-05` | LDA 主题组件 | 主题发现 → `topic` / `topic_word` |

---

## 4. 两条硬约束（答辩要点）

1. **Spark 容器不发起任何 HTTP 调用**，即**不接触 DeepSeek**（`C4容器图.md` §3）。
2. **职责不重叠**：Spark 做全量聚合与模型训练；Python 做行级清洗与外部 HTTP 集成。
   二者通过 MySQL 解耦，任一侧可单独重跑。

> 关于「MLlib 能做情感分析，为什么还要 DeepSeek」：MLlib 输出**整体极性**（回答"整体情感如何"），
> DeepSeek 输出**细粒度语义**（情感极性＋强度＋所针对的评价方面＋关键词＋摘要，回答"评论在说什么、
> 游客在乎什么"）。二者是**基线与增强**关系，通过对比实验说明差异，不是替代关系。

---

## 5. 环境坑位（动手前必读，来源：`项目现状分析.md` §8.4）

| # | 坑 | 处理 |
|---|---|---|
| K1 | 缺 `winutils.exe` / `HADOOP_HOME` | 放置到 `D:\hadoop\bin`，设 `HADOOP_HOME` 并加入 PATH |
| K2 | 照抄 JDK17 风格 JVM 参数 | JDK1.8 下用 `-Xms512m -Xmx2g` |
| K3 | `Task not serializable` | 进入闭包的对象需 `extends Serializable`；**JDBC 连接不要跨闭包传递**，在 `foreachPartition` 内创建 |
| K4 | `No suitable driver found` | MySQL 驱动随 `--jars` 传入或打进 fat jar |
| K5 | MySQL8 认证 | 连接串加 `allowPublicKeyRetrieval=true&useSSL=false` |
| K6 | 中文乱码 | 库/表 `utf8mb4`，连接串 `characterEncoding=utf8` |
| K7 | 时区偏移 | 连接串 `serverTimezone=Asia/Shanghai` |

连接串基线（`项目现状分析.md` §8.3）：

```
jdbc:mysql://localhost:3306/tibet_review?useSSL=false&serverTimezone=Asia/Shanghai&characterEncoding=utf8&allowPublicKeyRetrieval=true
```
