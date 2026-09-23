# app/web/ —— Flask Web 应用

> 本目录对应 C4 容器「**Flask Web 应用**」，承载组件 `C-API-01`～`C-API-14`。
> 上级说明见 [`../README.md`](../README.md)。

---

## 1. 模块职责

提供**展示层与接口层**：页面渲染、REST 接口、统一响应封装。
详细设计说明书 §2 明确两条分层约束：

1. 展示层（HTML + ECharts + axios）**不直连数据库**，所有数据经接口层获取；
2. **不引入前端构建链与框架**（纯原生 HTML/CSS/JS + ECharts，见 §10）。

## 2. 主要文件

| 文件 | 行数 | 职责 |
|---|---|---|
| `__init__.py` | 46 | **应用工厂** `create_app()`：创建 Flask 实例、中文 JSON、注册蓝图 |
| `response.py` | 78 | 统一响应体 `{code,message,data}` 与业务码/HTTP 映射（详细设计 §6.3／§6.4） |
| `routes/__init__.py` | 11 | 路由层说明（接口层只做参数校验与响应封装，**不写业务规则**） |
| `routes/health.py` | 46 | 自检接口 `/healthz`（不查库）、`/api/db-ping`（查库） |
| `routes/pages.py` | 19 | 页面路由 `/`（骨架首页） |
| `templates/index.html` | — | 骨架页（证明模板可渲染、静态资源可加载、接口可调用） |
| `static/css/app.css`、`static/js/app.js` | — | 原生样式与脚本（含调用自检接口的示例） |

## 3. 输入 / 输出

| 接口 | 方法 | 输入 | 输出（HTTP 200，`code=0`） |
|---|---|---|---|
| `/` | GET | — | 渲染 `index.html` |
| `/healthz` | GET | — | `{app, version, stage, db_target}`，**不查库**；用于区分"服务没起来"与"数据库连不上" |
| `/api/db-ping` | GET | — | MySQL 的 `version` / `charset` / `database_name` / `table_count` / `foreign_key_count` |

其余接口（**未实现**）按详细设计 §6.2 的 26 个业务接口设计。
预期自检结果：`version=8.0.32`、`charset=utf8mb4`、`database_name=tibet_review`、`table_count=17`、`foreign_key_count=14`。

> 路由清单（实测 `app.url_map`）：业务路由 **3 个**（`/`、`/healthz`、`/api/db-ping`），
> 另有 Flask 自动注册的静态资源路由 `/static/<path:filename>` 1 个。

## 4. 调用关系

```
run.py ─> create_app()
            ├─> routes.pages  ─> templates/index.html
            └─> routes.health ─> app.db.ping() ─> app.config.settings
                              └─> app.web.response.ok() / .fail()
            ↑ app.__version__
```

- `create_app()` 采用**应用工厂模式**：便于后续按模块拆分蓝图，也便于自检脚本在不启动服务器的情况下获取 `app` 对象。
- 蓝图在工厂函数内部**惰性导入**，避免模块级循环导入。

## 5. 如何使用

```powershell
# 启动开发服务器（读取 .env 的 FLASK_HOST / FLASK_PORT / FLASK_DEBUG）
.\.venv\Scripts\python.exe run.py          # → http://127.0.0.1:5000/

# 接口自检
curl.exe http://127.0.0.1:5000/healthz
curl.exe http://127.0.0.1:5000/api/db-ping
```

## 6. 当前实际开发阶段（重要）

**系统整体处于「阶段二（Python 数据清洗与预处理）已完成、阶段四（Spark）未开始」的状态。**

但请注意一个容易误解的地方：

> `/healthz` 的返回中 `stage` 字段写的是 `"阶段一：开发环境与代码骨架初始化"`。
> 这是**阶段一建立该接口时为固定值写死的**，后续阶段未同步更新。
> 按「不修改接口返回值」的原则，本次整理**未改动该字段**——它不代表系统当前进度，
> 只表示"这个自检接口诞生于阶段一"。

也就是说：**判断项目进度请看 `开发上下文索引.md` 与 `开发日报/`，不要看 `/healthz` 的 `stage` 字段。**

## 7. 对应项目设计中的组件

| 组件 | 说明 | 状态 |
|---|---|---|
| `C-API-01`～`C-API-11` | 各业务接口（数据总览、景点分析、智能评价、对比、问答等） | **未实现**（阶段六） |
| `C-API-12` | DeepSeek 调用封装 | **未实现**，预留目录 `app/llm/` |
| `C-API-13` | 数据访问组件 | ✅ 已实现（`app/db.py`），`/api/db-ping` 是其联调入口 |
| `C-API-14` | 统一响应与错误处理 | ✅ 已实现（`response.py`） |

## 8. 与其他模块的关系

- **→ `app/db.py`**：接口层唯一的数据库出口，所有 SQL 参数化。
- **→ `app/batch/`**：Web 只**读**批处理写入的结果，不触发批处理（批处理是离线任务，不在请求周期内运行）。
- **→ `app/web/templates` + `static`**：展示层不直连数据库，数据一律通过 `/api/*` 获取。
- **← `run.py`**：唯一的启动入口。

## 9. 当前已实现 / 未实现

**已实现**
- 应用工厂、蓝图注册、中文 JSON（`ensure_ascii=False`）
- 统一响应体与 13 个业务码、业务码→HTTP 状态码映射表
- 3 个路由：`/`、`/healthz`、`/api/db-ping`

**未实现**
- 详细设计 §6.2 的 **26 个业务接口**
- 5 个业务页面（数据总览、景点分析、智能评价、景点对比、智能问答）
- 登录/权限（`sys_user` 表已建，但无认证逻辑）
- 用户管理、任务管理页面（M6 系统管理）

## 10. 答辩时重点理解

1. **为什么只有 2 个接口**：`/healthz` 与 `/api/db-ping` **不在**详细设计 §6.2 的 26 个业务接口清单内，它们是阶段一的环境自检口，用于确认"Flask 能起、MySQL 能连、17 张表在位"。阶段六正式接口落地后，`health.py` **可整体删除而不影响业务**。
2. **`code = 0` 不等于"有数据"**：`response.py` 中 `CODE_OK = 0` 同时用于「数据不足（available=false）」与「超范围拒答（OUT_OF_SCOPE）」两种**正常业务响应**（详细设计 §6.4）——前端不得把 `code=0` 一律当作"查询成功且有结果"。
3. **应用工厂模式的意义**：`create_app()` 让测试与自检脚本可以在不启动端口的情况下拿到 `app` 对象，也便于按模块拆分蓝图。
4. **前端为什么不用 Vue/React**：详细设计 §10 明确"不引入前端构建链"，用原生 HTML + ECharts + axios 即可满足图表展示需求，也更贴合本科毕业设计的可控范围。
5. **展示层不直连数据库**：这是分层设计的硬约束，避免业务规则被复制到前端（口径必须集中实现）。
