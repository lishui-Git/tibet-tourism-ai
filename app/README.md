# app/ —— 后端应用包

> 本文件说明 `app/` 的整体职责与内部分层。更细的说明见各子目录 README：
> [`batch/README.md`](batch/README.md)、[`web/README.md`](web/README.md)。

---

## 1. 模块职责

`app/` 是系统的**后端全部代码**，对应 C4 的两个容器：

| 子目录 / 文件 | 对应 C4 容器或组件 |
|---|---|
| `web/` | 容器「**Flask Web 应用**」，组件 `C-API-01`～`C-API-14` |
| `batch/` | 容器「**Python 批处理服务**」，组件 `C-BAT-01`～`C-BAT-07` |
| `config.py` | 集中配置（非功能需求 NR-M-01：连接/Key/限流集中配置） |
| `db.py` | 数据访问组件 `C-API-13` |

> 预留但**尚未创建**的目录：`app/llm/`（DeepSeek 调用封装，组件 `C-API-12`）。
> 按项目纪律「不实现 C4 中不存在的模块」，尚未实现的组件**不建空文件**，避免假进度。

## 2. 主要文件

| 文件 | 行数 | 职责 |
|---|---|---|
| `__init__.py` | 19 | 包声明与内部版本号 `__version__`（供 `/healthz` 返回） |
| `config.py` | 220 | **唯一**读取 `.env` / 环境变量的入口；四组冻结配置 |
| `db.py` | 124 | 连接管理、参数化 SQL、结果映射；**不含业务判断** |
| `web/` | — | Flask 应用（应用工厂 + 路由 + 统一响应体 + 模板/静态资源） |
| `batch/` | — | 两个可独立运行的批处理脚本（导入、清洗） |

## 3. 输入 / 输出

- **输入**：`.env`（配置与口令）、`数据采集/` 下的冻结数据文件（只读）
- **输出**：MySQL 库 `tibet_review`；`data/`（清洗产物，已 gitignore）；`logs/`（预留）
- 约束：`数据采集/` 是**只读冻结区**（BR-11），任何转换结果另存新文件

## 4. 调用关系（实测 import 图）

```
run.py
  └─> app.web.create_app()
        ├─> web.routes.health ──> app.db ──> app.config
        └─> web.routes.pages                    ↑
app.batch.import_dataset ───────────────────────┤
app.batch.clean_dataset ─> app.batch.import_dataset（复用规范化函数）
                        └─> app.db（仅 --log-task 时惰性导入）
```

**两条铁律**（答辩常被追问）：

1. 配置只有 `app/config.py` 一个出口——其他模块**不得**自行调用 `os.environ`；
2. 数据库只有 `app/db.py` 一个出口——所有 SQL 参数化（`%s`），禁止字符串拼接（NR-S-03 防注入）。

无循环依赖，无跨层越界调用。

## 5. 如何运行

```powershell
# 启动 Web（开发服务器）
.\.venv\Scripts\python.exe run.py                 # → http://127.0.0.1:5000/

# 批处理：数据导入（阶段一，会写库）
.\.venv\Scripts\python.exe -m app.batch.import_dataset --mode sample --limit 200 --dry-run

# 批处理：数据清洗与预处理（阶段二，默认不写库）
.\.venv\Scripts\python.exe -m app.batch.clean_dataset --limit 200
```

## 6. 当前已实现 / 未实现

**已实现**
- 集中配置、数据访问、统一响应体
- 阶段一：CSV → `spot`(837) / `review`(59,033) 全量导入 + 18 项校验
- 阶段二：清洗与预处理（C-BAT-01～04），产出清洗版文件与统计报告
- 自检接口 2 个：`/healthz`（不查库）、`/api/db-ping`（查库）

**未实现**
- `app/llm/`（DeepSeek 调用封装）——阶段五
- 详细设计 §6.2 的 **26 个业务 REST 接口**——阶段六
- 5 个业务前端页面（现仅有 1 张骨架页）
- `C-BAT-05/06/07`（语义抽取、事实包、评价生成）——阶段五

## 7. 与其他模块的关系

- 与 `spark/`：**通过 MySQL 解耦**。Python 只负责行级清洗与外部 HTTP 集成，Spark 负责全量聚合与模型训练，任一侧可单独重跑；Spark 容器**不发起任何 HTTP 调用**（不接触 DeepSeek）。
- 与 `scripts/`：`scripts/` 是辅助工具（环境自检、产物复核），反向导入 `app`，不被 `app` 依赖。
- 与 `tmp/`：`tmp/` 是文档生成与校验脚本，**不属于运行代码**，不参与系统运行。

## 8. 答辩时重点理解

1. **为什么只有一个配置文件和一个数据库出口**——集中配置（NR-M-01）与参数化 SQL（NR-S-03）是可审计、防注入的前提。
2. **两层容器的职责边界**：`web/` 只在请求周期内工作；`batch/` 是离线批处理，通过 MySQL 衔接，不阻塞 Web。
3. **`code = 0` 不等于"有数据"**：统一响应体中「数据不足（available=false）」与「超范围拒答（OUT_OF_SCOPE）」都是**正常业务响应**，不是错误（详细设计 §6.4）。
4. **为什么现在只有 2 个接口**：它们是环境自检口（`C-API-13` 的联调入口），**不在** §6.2 的 26 个业务接口清单内，阶段六落地后可整体删除而不影响业务。
5. **清洗版 CSV 的作用**：阶段二产物既可作为 Spark 的数据源（与库内 `review` 口径一致），也是数据可追溯的证据链。
