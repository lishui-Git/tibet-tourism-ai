# app/web/ —— Flask Web 应用

> 本目录对应 C4 容器「**Flask Web 应用**」，承载组件 `C-API-01`～`C-API-14`。
> 上级说明见 [`../README.md`](../README.md)。

---

## 1. 模块职责

提供**展示层与接口层**：页面渲染、REST 接口、统一响应封装、只读业务组合。
详细设计说明书 §2 明确两条分层约束：

1. 展示层（HTML + ECharts + axios）**不直连数据库**，所有数据经接口层获取；
2. **不引入前端构建链与框架**（纯原生 HTML/CSS/JS + ECharts，见 §10）。

## 2. 【架构原则】Web 层只读数据库，绝不在线调用模型

这是本项目最关键的一条架构约束（BR-12 / CC-1 / CC-2），答辩必答：

```
离线（一次性，产生费用）                      在线（每次请求，零模型费用）
  review ──DeepSeek──> sentiment/aspect/comment_semantic
  stat_* ──Spark────> 统计结果                 ┌─ /api/overview/*   ← stat_time / stat_ip / review
  spot_fact_package ──DeepSeek──> spot_report  ├─ /api/spots/*      ← spot / stat_spot / sentiment
                                               │                       / aspect / topic / review
                                               └─ /api/admin/*      ← analysis_task / task_log
                                                        ↑
                                             Web 只 SELECT，不触发任何批处理
```

- Web 层的**每一次查询都只读数据库里已经生成好的结果**；
- **没有任何一个接口会调用 DeepSeek**（可全局检索 `app/web/` 确认：不 import `app.llm`）；
- 因此"用户反复打开景点页面"不会产生模型费用，结果也可复现、响应稳定；
- 数据不足（`available=false`）是**正常业务响应**（HTTP 200、`code=0`），不是错误（BR-03、NR-U-03）。

## 3. 主要文件

| 文件 | 职责 |
|---|---|
| `__init__.py` | **应用工厂** `create_app()`：创建 Flask 实例、中文 JSON、注册蓝图 |
| `response.py` | 统一响应体 `{code,message,data}` 与业务码/HTTP 映射（详细设计 §6.3／§6.4） |
| `data_access.py` | 查询辅助：类型清理（Decimal/date → JSON 可序列化）、分页校验、**口径常量**（BR-10） |
| `services.py` | 业务层：只读组合分析结果（M1 总览 / M2 景点 / M3 评价 / M6 任务），**不含写操作** |
| `routes/__init__.py` | 路由层说明（接口层只做参数校验与响应封装，**不写业务规则**） |
| `routes/_helpers.py` | `respond` / `respond_one`：把 `ParamError`/`DatabaseError`/未预期异常统一翻译成业务码 |
| `routes/health.py` | 自检接口 `/healthz`（不查库）、`/api/db-ping`（查库） |
| `routes/overview.py` | M1 数据总览接口（5 个） |
| `routes/spots.py` | M2 景点分析 + M3 智能评价接口（9 个） |
| `routes/admin.py` | M6 系统管理只读接口（3 个） |
| `routes/pages.py` | 页面路由 `/`（骨架首页） |
| `templates/`、`static/` | 原生 HTML/CSS/JS（骨架页，业务页面待做） |

## 4. 已实现的接口（前 3 个为自检，其余按详细设计 §6.2 编号）

| # | 方法 | 路径 | 说明 | 组件 |
|---|---|---|---|---|
| — | GET | `/healthz` | 进程存活（不查库） | — |
| — | GET | `/api/db-ping` | MySQL 连通性与表数量 | `C-API-13` |
| — | GET | `/` | 骨架首页 | — |
| 5 | GET | `/api/overview/summary` | 数据集规模、评分分布、结果覆盖情况 | `C-API-02` |
| 6 | GET | `/api/overview/trend?granularity=year\|month` | 评论量时间趋势（读 `stat_time`） | `C-API-02` |
| 7 | GET | `/api/overview/distribution` | 评论量分档 + 来源口径构成 | `C-API-02` |
| 8 | GET | `/api/overview/provinces?limit=20` | 客源地分布（BR-01 口径） | `C-API-02` |
| 9 | GET | `/api/overview/data-note` | 数据来源、质量与已知局限 | `C-API-02` |
| 10 | GET | `/api/spots?keyword=&page=1&page_size=20` | 景点列表 | `C-API-03` |
| 11 | GET | `/api/spots/ranking?by=reviews\|rating\|positive_rate` | 景点排行 | `C-API-03` |
| 12 | GET | `/api/spots/{spot_id}` | 景点详情与统计指标 | `C-API-03/04` |
| 13 | GET | `/api/spots/{spot_id}/trend` | 景点时间趋势 | `C-API-04` |
| 14 | GET | `/api/spots/{spot_id}/sentiment?method=deepseek\|mllib\|dict` | 情感占比（多方法并列） | `C-API-05` |
| 15 | GET | `/api/spots/{spot_id}/aspects?method=deepseek` | 方面分析（含 BR-04 门槛判断） | `C-API-07` |
| 16 | GET | `/api/spots/{spot_id}/topics` | LDA 主题（无专属主题时回退 global） | `C-API-06` |
| 17 | GET | `/api/spots/{spot_id}/reviews?limit=5` | 代表性正负面评论 | `C-API-08` |
| 18 | GET | `/api/spots/{spot_id}/report` | **景点智能评价（含事实依据回显）** | `C-API-09` |
| 23 | GET | `/api/admin/tasks?limit=20&task_type=` | 批处理任务列表与进度 | `C-API-14` |
| 24 | GET | `/api/admin/tasks/{task_id}/logs` | 任务日志明细 | `C-API-14` |
| 26 | GET | `/api/admin/caliber` | 系统数据口径配置 | `C-API-14` |

**尚未实现的接口（8 个，未注册路由 → 调用返回 404，属预期）**：
1–4 认证类（`/api/auth/*`，依赖用户体系）、19 重新生成评价（需写操作 + 管理员角色）、
20 景点对比解读（需**在线**调用模型）、21–22 问答（需在线调用模型）、25 用户列表（依赖用户体系）。

> **为什么不先做**：19/20/21/22 需要"在线调用 DeepSeek"，与 §1 的只读原则冲突，
> 必须先把调用封装、缓存与权限设计清楚；1–4/25 依赖 `sys_user` 与会话。
> **宁可不实现，也不留"看起来有权限校验其实没有"的接口。**

## 5. 口径纪律（BR-10）

受口径影响的返回都带 `caliber_note` 与 `sample_size`，统一在 `data_access.py` 定义，避免各处自写一套：

| 常量 | 内容 |
|---|---|
| `CALIBER_IP` | 客源地仅用 2022-08 之后样本（BR-01） |
| `CALIBER_TREND` | 时间趋势是"评论发布时间"，**不代表客流量**（FR-OV-03） |
| `CALIBER_SENTIMENT` | deepseek 与 mllib 两种方法结论不可混用（FR-SA-04） |
| `CALIBER_ASPECT` | 方面样本 <10 条不出结论（BR-04） |
| `CALIBER_THRESHOLD` | 评论量 ≥100 条才生成完整评价（BR-02/BR-03） |

## 6. 如何使用

```powershell
# 启动开发服务器（读取 .env 的 FLASK_HOST / FLASK_PORT / FLASK_DEBUG）
.\.venv\Scripts\python.exe run.py          # → http://127.0.0.1:5000/

# 接口冒烟测试（本地 test_client，不启端口、零 API 调用、零写库）
.\.venv\Scripts\python.exe scripts\smoke_api.py

# 手工抽查
curl.exe "http://127.0.0.1:5000/api/overview/summary"
curl.exe "http://127.0.0.1:5000/api/spots/564"
curl.exe "http://127.0.0.1:5000/api/spots/564/sentiment?method=mllib"
curl.exe "http://127.0.0.1:5000/api/spots/564/report"
```

## 7. 当前进度（截至阶段四）

| 层 | 状态 |
|---|---|
| 数据层 | ✅ 阶段一导入、阶段二清洗、阶段三 Spark 全量统计（`stat_*`/`sentiment`(mllib)/`topic`） |
| 语义层 | 🟡 阶段四 C-BAT-05～07 代码完成、32 条真实调用验证通过；**全量 47,701 次待授权** |
| 接口层 | ✅ 自检 3 个 + 业务只读 17 个（本文件 §4） |
| 页面层 | ❌ 仅有骨架页，5 个业务页面待做 |
| 认证/权限 | ❌ 未实现（`sys_user` 表已建） |

> `/healthz` 的 `stage` 字段已与项目实际进度同步（阶段四）；
> 判断进度仍以 `开发上下文索引.md` 与 `开发日报/` 为准——`stage` 只是一个概览字段。

## 8. 对应项目设计中的组件

| 组件 | 说明 | 状态 |
|---|---|---|
| `C-API-01` | 用户注册/登录/角色 | **未实现**（依赖用户体系） |
| `C-API-02` | 数据总览统计 | ✅ 5 个接口 |
| `C-API-03`～`C-API-08` | 景点列表/详情/趋势/情感/主题/方面/代表评论 | ✅ 已实现（只读） |
| `C-API-09` | 景点智能评价读取 | ✅ 已实现；`regenerate`（接口 19）未实现 |
| `C-API-10` | 景点对比（实时调模型） | **未实现** |
| `C-API-11` | 智能问答（实时调模型） | **未实现** |
| `C-API-12` | DeepSeek 调用封装 | ✅ 已实现（`app/llm/`，离线批处理用；**Web 层不引用**） |
| `C-API-13` | 数据访问组件 | ✅ 已实现（`app/db.py` + `data_access.py`） |
| `C-API-14` | 统一响应与错误处理、任务查询 | ✅ 响应封装 + 3 个只读接口 |

## 9. 与其他模块的关系

- **→ `app/db.py` / `data_access.py`**：接口层唯一的数据库出口，所有 SQL 参数化且只读。
- **→ `app/batch/`、`app/llm/`**：Web 只**读**它们离线写入的结果，**不调用、不触发**（批处理依赖人工运行）。
- **→ `templates` + `static`**：展示层不直连数据库，数据一律通过 `/api/*` 获取。
- **← `run.py`**：唯一的启动入口。

## 10. 答辩时重点理解

1. **为什么不是每次用户查询都重新调用大模型**：本系统面向旅游评论数据集的**离线**智能分析——
   DeepSeek 负责离线把评论语义结构化并落库，Web 层只做查询、展示与组合；
   这样结果**可复现**、响应**稳定**、模型成本**可控**（一次性生产、之后只读）。
2. **`code = 0` 不等于"有数据"**：`CODE_OK = 0` 同时用于"数据不足（available=false）"
   与"超范围拒答"两种**正常业务响应**（§6.4）——前端不得把 `code=0` 一律当作"查到结果"。
3. **BR-04 在接口层怎么落地**：方面接口对每个方面给出 `conclusive` 标志，
   样本 <10 时 `positive_rate/negative_rate` 返回 `null` 并带 `note`，
   而不是"照常给一个不可靠的百分比"。
4. **缺失字段不展示**：景点详情返回 `missing_fields`，前端据此隐藏字段（FR-SA-01），
   不用占位符或 0 填充（那会误导读者）。
5. **应用工厂模式**：`create_app()` 让测试脚本不启端口就能拿到 `app` 对象
   （`scripts/smoke_api.py` 就是用它做 40 项接口断言）。
