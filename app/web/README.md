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

> **这条承诺有实测证据（不只是静态检索）**：`scripts/test_readonly_api.py` 会
> ① 快照全部结果表与运维表的行数 → ② 把 **33 个只读 GET 接口**（含页面、自检、错误路径）
> 逐个请求一遍 → ③ 再次快照并断言**逐表完全一致**，同时断言
> **未新增 `analysis_task` / `task_log`**（即"看页面不会触发任何批处理或模型调用"）。
> 实测：30 个 200 + 3 个 404，**17 张表逐表一致**。
>
> 两个**有意例外**（设计要求的写操作，不属于只读承诺范围）：
> `POST /api/qa/ask` 落 `qa_record`（§15.E.4）、`POST /api/spots/{id}/report/regenerate`
> 登记 `pending` 任务（接口 19）。二者都不调用模型。

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
| `routes/admin.py` | M6 系统管理接口（4 个，**均需管理员**） |
| `routes/auth.py` | 认证接口（注册/登录/注销/当前用户） |
| `routes/qa.py` | M5 智能问答接口（ask / history） |
| `qa.py` | 问答链路：分类 → 景点识别 → 事实检索 → 上下文组装 → 回答生成（仅最后一步可能调用模型） |
| `routes/_auth_helpers.py` | `login_required` / `admin_required` 装饰器与 `current_user()` |
| `auth.py` | 口令加盐哈希（PBKDF2-HMAC-SHA256）、用户读写、登录校验 |
| `routes/pages.py` | 页面路由（6 个页面 + 3 个旧地址重定向） |
| `templates/base.html` | 公共布局：**fixed 顶部导航**（首页／数据总览／景点分析／智能分析／景点对比／管理员）、页脚（含折叠的口径与局限说明）、返回顶部按钮 |
| `templates/home.html`、`overview.html`、`spots.html`、`smart.html`、`compare.html`、`admin.html`、`login.html`、`error.html` | 页面模板（`error.html` 为 404/405/500 的中文友好页） |
| `static/js/common.js` | 公共前端逻辑：`apiGet`、格式化（`fmtInt`/`fmtPct`）、`renderTable`、`caliberNote`、`initChart`、**返回顶部**、**内部文案用户化**（`humanize`/`noticeBox`/`techDetails`） |
| `static/js/{home,overview,spots,smart,evaluation,qa,compare,tasks,admin,login}.js` | 各页面逻辑；`evaluation.js`+`qa.js` 由 `smart.js` 统一调度（同一整合页的两个子标签） |
| `static/vendor/{echarts,axios}.min.js` | **本地内置**的 ECharts 5.5.1 与 axios 1.7.7（约 1.06 MB），运行时不依赖外网
| `static/img/*.svg` | **原创矢量插画**（5 个）：`tibet-hero.svg` 首屏雪域天际线（雪山／冰川湖／山脊上的布达拉宫／经幡），`scene-{potala,namtso,canyon,yamdrok}.svg` 景点风景卡。全部手写、合计 < 25 KB，**无第三方图片授权问题** |

> **视觉识别体系（2026-10 视觉改版）**：主题色取"高原天空与冰川湖"的藏蓝／深青
> （`--c-primary` 系），点缀色取经幡五色中最低调的一支（`--c-accent` 经幡黄）；
> 视觉签名是一条 3px 的**经幡色带**（导航底部、首屏底部）。
> 页面用**深色分区 `.band` 与浅色卡片交替**，避免"满屏白卡片、重点不突出"。
> 插画全部为自绘 SVG——不引入任何下载的照片与视频，因此**无需外部素材授权**，
> 也不存在"视频加载失败"的风险；同时仍实现了插画失败的降级（见 `common.js` 的 `_initArtFallback`）。/CDN |

> **2026-10 前台改版要点**（面向普通用户的可用性改造，未改动接口契约）：
> · 一级导航由 7 项收敛为 **6 项**；「智能评价」「智能问答」合并进 **`/smart`**（子标签切换），
>   「任务与口径」升级为 **`/admin`**（并接收原先放在首页的系统自检）；
> · 旧地址 `/evaluation`、`/qa`、`/tasks` 保留为 **301/302 重定向**，旧链接与书签不失效；
> · 首页改为"系统能做什么 + 真实数据概况 + 四个核心入口"，技术说明与口径降级到折叠区；
> · 全站前台不再出现 `Spark`／`MLlib`／`deepseek`／`stat_*`／`REPORT_NOT_GENERATED` 等内部用语
>   （内部细节改为写入浏览器控制台，管理员页仍保留完整术语）；
> · 覆盖 1440／1280／1024／~500px 四档宽度，均无横向溢出。

## 4. 已实现的接口（前 3 个为自检，其余按详细设计 §6.2 编号）

| # | 方法 | 路径 | 说明 | 组件 |
|---|---|---|---|---|
| — | GET | `/healthz` | 进程存活（不查库） | — |
| — | GET | `/api/db-ping` | MySQL 连通性与表数量 | `C-API-13` |
| — | GET | `/` | 骨架首页 | — |
| 1 | POST | `/api/auth/register` | 用户注册（**一律普通用户，不可自助建管理员**） | `C-API-01` |
| 2 | POST | `/api/auth/login` | 登录（建立会话） | `C-API-01` |
| 3 | POST | `/api/auth/logout` | 注销（需登录） | `C-API-01` |
| 4 | GET | `/api/auth/me` | 当前用户与角色（需登录） | `C-API-01` |
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
| 19 | POST | `/api/spots/{spot_id}/report/regenerate` | 重新生成评价（**需管理员**；只登记 pending 请求，**不调用模型**） | `C-API-09` |
| 20 | GET | `/api/compare?spot_a=&spot_b=` | **景点对比**（指标由后端算；解读默认关闭，零 API 消费） | `C-API-10` |
| 21 | POST | `/api/qa/ask` | **智能问答**（先分类+检索；生成回答默认关闭，零 API 消费） | `C-API-11` |
| 22 | GET | `/api/qa/history` | 问答历史（**需登录**，只返回本人记录） | `C-API-11` |
| 23 | GET | `/api/admin/tasks` | 批处理任务列表与进度（**需管理员**） | `C-API-14` |
| 24 | GET | `/api/admin/tasks/{task_id}/logs` | 任务日志明细（**需管理员**） | `C-API-14` |
| 25 | GET | `/api/admin/users` | 用户列表（**需管理员**，不含口令哈希） | `C-API-14` |
| 26 | GET | `/api/admin/caliber` | 系统数据口径配置（**需管理员**） | `C-API-14` |

**尚未实现的接口（0 个）**：详细设计 §6.2 的 26 个接口**已全部实现**。

> **接口 19（重新生成评价）的实现取向（重要）**：设计标注为"异步提交"，因此它
> **只做三件事**——校验景点与门槛、登记一条 `status='pending'` 的任务、返回提交结果；
> **真正生成由离线批处理执行**：
> `python -m app.llm --stage report --spot-ids <id> --force`。
> 这样 **Web 端永远不会自己发起模型调用**——成本闸门（`--yes` / `--offline`）全部留在离线侧，
> "点一下按钮就烧钱"在设计上被排除。评论量 <100 的景点按 BR-02/BR-03 直接返回
> `available=false`（正常业务响应，不登记任务）。

> **接口 20（景点对比）已实现**，但它的"解读"部分默认**关闭**：
> 对比指标、差值、样本量比、方面对比全部由后端实时计算（不落库、零成本），
> 只有"用自然语言解释差异"需要模型，由 `APP_COMPARE_LIVE` 开关控制，**默认 false**。
> 关闭时接口照常返回 200 与全部数据，仅 `interpretation.available=false` 并带原因——
> 这既是"不误花钱"的保险，也正好复用设计 §15.D.3 的失败降级路径。

> **接口 21（智能问答）同理**：问题分类、景点识别、事实检索全部由本系统完成（零成本），
> 只有"把结构化事实组织成回答"需要模型，由 `APP_QA_LIVE` 控制（**默认 false**）。
> 关闭时返回 200 + 检索到的数据依据 + 明确原因，并给出 `reached_model` 字段说明本次是否真的调用了模型。
> 设计还内置**三条根本不进入模型的分支**（超范围拒答／未识别到景点／检索无数据），
> 它们**不落库**（与 §15.E.1 流程图一致）。
> **宁可不实现，也不留"看起来有权限校验其实没有"的接口。**

> **`qa_record` 的落库口径（本轮与设计对齐修正）**：**只有真正生成了回答才落库**。
> 以下情形一律**不保存残缺记录**（设计 §六："问答调用失败 → 返回 4001 与友好提示，
> 提供重试按钮；**不保存残缺记录**"；§15.E.1 流程图也是"校验通过 → 写入 qa_record"）：
>
> | 情形 | reason | 落库 |
> |---|---|---|
> | 正常生成回答 | — | ✅ 落库 |
> | 模型调用失败 | `LLM_FAILED` | ❌ 不落库 |
> | 模型返回格式非法 | `LLM_BAD_FORMAT` | ❌ 不落库 |
> | 在线能力关闭 | `LIVE_DISABLED` | ❌ 不落库 |
> | 未配置 API Key | `API_KEY_MISSING` | ❌ 不落库 |
> | 超范围拒答 / 未识别景点 / 无数据 / 空问题 | `OUT_OF_SCOPE` 等 | ❌ 不落库 |
>
> 此前实现把"失败"也落库（理由写的是"审计上应当留痕"），**与设计冲突**，已改正。
> 影响：模型故障时 `qa_record` 不会堆满"没有回答"的记录，同时前端仍拿到数据依据与重试提示。
> 由 `scripts/test_qa.py` 的 `[6]` 组固定（用假客户端分别验证"失败不落库"与"成功才落库"）。

## 4b. 已实现的页面（6 个公开页 + 管理后台 + 登录页）

| 页面 | 路径 | 内容 | 依赖接口 |
|---|---|---|---|
| 首页 | `/` | **首屏 Hero**（自绘雪域插画 + 系统定位 + 四个关键数据 + 进入按钮）、**数据规模**（重点数字块）、**系统能做什么**（深色分区四入口）、**高原上的热门景点**（插画卡 + 真实统计）、**评论量排行**（榜单卡）、结果生成情况（如实标注未生成项）、技术说明与口径收在折叠区 | `/api/overview/summary`、`/api/spots/ranking`、`/api/spots` |
| 数据总览 | `/overview` | 规模与评分分布（饼图＋范围构成柱图）、时间趋势（年/月切换）、评论量分档、游客来源地区 Top-N、数据来源与局限（折叠） | `/api/overview/*` |
| 景点分析 | `/spots` | 景点排行（三种排序）、关键字检索+分页、景点详情（资料缺失时整块隐藏而非显示"字段缺失"）、情感分布（两种口径用中性键切换）、年度趋势、关注方面、评论主题、代表评论；具备条件的景点可一键跳转智能分析 | `/api/spots/*` |
| **智能分析** | `/smart?tab=evaluation\|qa` | **整合页**：`景点评价` 子标签（选景点 → 综合评价／优势／问题／关注点 + 数据依据；未生成时如实说明并给出基础统计入口）与 `智能问答` 子标签（提问 → 系统回答，或"为什么没生成 + 检索到的数据依据"）；支持 `?spot=<id>` 直达某景点评价 | `/api/spots/{id}/report`、`/api/qa/ask`、`/api/spots/ranking` |
| 景点对比 | `/compare` | 选择两个**不同**景点（默认已预选两个不同景点，相同景点在**提交前**拦截）→ 指标对比表（含差值）、情感对比、方面对比、可靠性提示；解读默认关闭并如实说明"数据部分不受影响" | `/api/compare` |
| 管理后台 | `/admin` | **需登录**（管理员）：当前登录信息与退出、**系统运行自检**、数据口径配置、批处理任务列表与日志明细（内部术语在此保留，并配中文说明） | `/api/auth/*`、`/api/admin/*`、`/healthz`、`/api/db-ping` |
| 管理员登录 | `/login` | 用户名/口令登录；已登录则自动进入 `/admin`；账号与安全说明收在折叠区 | `/api/auth/login`、`/api/auth/me` |

**旧地址兼容（改版不破坏既有链接）**：

| 旧地址 | 行为 |
|---|---|
| `/evaluation` | **301** → `/smart?tab=evaluation` |
| `/qa` | **301** → `/smart?tab=qa` |
| `/tasks` | **302** → `/admin`（未登录先跳 `/login`） |

> 路由实测：**API 27 个** + 页面/自检 10 个（另有 3 个旧地址重定向与 `/static/<path>`）。
> `/this-page-does-not-exist` 等未知页面返回**中文 404 页**（`error.html`），
> 而不是框架默认的英文 "Not Found"；`/api/**` 的 404/405 仍返回统一 JSON 信封。

**M5 智能问答的链路（答辩要点）**：问题 → ① 规则分类 → ② 景点实体识别（含前缀模糊匹配）
→ ③ 按类型检索结构化事实 → ④ 组装上下文（≤30 条、≤3,000 字、**只含结构化事实**）→ ⑤ 生成回答。
前三步全是本系统自己的规则与 SQL（零成本），且设计内置**三条根本不进入模型的分支**：
超范围直接拒答、未识别到景点则提示补充、检索无数据则如实说明"没有相关信息"。
**模型在本系统中没有任何"自己知道答案"的场合。**

**前端两条纪律**：
1. **不引入构建链**（§10）：原生 HTML/CSS/JS + ECharts + axios，无 npm、无打包；
2. **图表库本地内置**：ECharts 与 axios 已下载到 `static/vendor/`，
   答辩现场断网也能正常渲染（不依赖 CDN）。

**页面的三类自动检查**（覆盖不同盲区，互为补充）：

| 脚本 | 查什么 | 查不到的 |
|---|---|---|
| `smoke_api.py` | 路由与静态资源是否 200、错误码是否正确 | 页面里的控件是否真的能用 |
| `check_api_contract.py` | 接口返回的字段是否都在（前端取空白的根源） | 模板与脚本的绑定是否正确 |
| `check_page_bindings.py` | **JS 引用的元素 id 是否都存在**（含脚本动态生成的 id） | 事件是否触发（需真机浏览器） |
| `test_auth.py` / `test_qa.py` | 会话与业务分支的行为 | 视觉与交互手感 |

> 第三类是本轮补的：`getElementById('打错字的id')` 返回 null，JS 在该行抛错、
> 后续逻辑全不执行——页面"能打开但某些功能没反应"，而前三类检查**都发现不了**。

未实现的页面：无（六个模块的页面均已落地，并在 2026-10 前台改版中收敛为 6 个一级入口）。

**前台改版新增的验收脚本**：`scripts/test_frontend_redesign.py`（75 项）——
逐页渲染检查页面可达性、**用户可见文案里不出现内部用语**、导航结构、
整合页子标签、对比页相同景点**提交前拦截**、旧地址重定向、中文 404 页、
以及"前端传参不得超过接口上限"（曾因 `limit: 200` 超出上限 100 导致页面报错）。

## 5. 认证与鉴权（§13.1 / §13.2）

| 项 | 实现 |
|---|---|
| 口令存储 | **PBKDF2-HMAC-SHA256** 加盐（20 万次迭代），存储串含算法名与迭代次数，便于日后升级参数而不失效旧口令。**不引入第三方依赖**（`requirements.txt` 冻结：不加 bcrypt/passlib） |
| 会话 | Flask 签名 Cookie（密钥 `APP_SECRET_KEY`，**本机 `.env` 已配置**，因此**重启服务后登录态仍有效**；未配置时会退化为一次性随机密钥、重启即失效）；`HttpOnly` + `SameSite=Lax`；**只存 `user_id`** |
| 每次请求回查 | `current_user()` 每次请求都回查 `sys_user` 确认"用户仍存在且未停用"，否则清会话——**帐号停用后旧 Cookie 立即失效**（已实测） |
| 角色 | 注册一律 `user`；**管理员只能由 `scripts/create_admin.py` 创建**，注册接口传入 `role=admin` 会被忽略（防自助提权，已实测） |
| 接口鉴权 | `@login_required` → 未登录 2001(401)；`@admin_required` → 未登录 2001、非管理员 2002(403) |
| 登录失败 | 统一 2004「用户名或口令错误」，**不区分**用户是否存在（防用户名枚举，已实测） |
| 越权防护 | 用户列表不返回 `password_hash`（已实测响应全文不含 `pbkdf2`）；管理接口全部走角色校验 |

页面侧：`/admin`（以及旧地址 `/tasks`）在服务端检查会话，未登录**重定向到 `/login`**
（避免"页面能开但接口全 401"的破壳体验）；真正的权限边界始终在 API 层。
登录页与「退出登录」按钮均已实测可用（登录 → 管理后台 → 退出 → 回登录页；
未登录访问 `/admin` 返回 302 而非空壳页）。

**首次使用请先创建管理员**：

```powershell
.\.venv\Scripts\python.exe scripts\create_admin.py --username admin
# 口令交互式输入（不回显）；或 $env:APP_ADMIN_PASSWORD="……" 走非交互
```

## 5b. 响应信封的覆盖范围（`/api/**` 一律 JSON）

设计 §6.3 约定响应体固定为 `{code, message, data}`。但有一个**容易被忽略的漏洞**：
若请求路径连路由都匹配不上（例如 `/api/spots/abc` 被 `<int:spot_id>` 转换器拒绝），
Flask 会在**进入视图之前**就返回它自己的 HTML 404 页面——项目自己的 `respond()` 根本没机会执行，
客户端按 `body.code` 取错误码会拿到 `undefined`。

因此在应用工厂里注册了**只作用于 `/api/**`** 的错误处理器（`app/web/__init__.py`）：

| 情况 | 返回 | 依据 |
|---|---|---|
| `/api/**` 路径不存在（含转换器拒绝的路径） | `3001` 资源不存在 / HTTP 404 | §6.4 |
| `/api/**` 路径存在但方法不对（如 GET 一个 POST 接口） | `1001` 参数/请求格式错误 / HTTP 400 | §6.4（方法不匹配归入"请求格式错误"，**未新增码**） |
| 页面路径（非 `/api/`）不存在 | Flask 默认 HTML 404 | 浏览器访问，保持 HTML 更合适 |

> 实测：`/api/spots/abc`、`/api/spots/-1`、`/api/nope` → JSON `code=3001`；
> `GET /api/qa/ask`、`POST /api/overview/summary` → JSON `code=1001`；
> `/nope`、`/spots/nope` → 仍是 HTML 404。已纳入 `scripts/smoke_api.py`。

## 6. 口径纪律（BR-10）
受口径影响的返回都带 `caliber_note` 与 `sample_size`，统一在 `data_access.py` 定义，避免各处自写一套：

| 常量 | 内容 |
|---|---|
| `CALIBER_IP` | 客源地仅用 2022-08 之后样本（BR-01） |
| `CALIBER_TREND` | 时间趋势是"评论发布时间"，**不代表客流量**（FR-OV-03） |
| `CALIBER_SENTIMENT` | deepseek 与 mllib 两种方法结论不可混用（FR-SA-04） |
| `CALIBER_ASPECT` | 方面样本 <10 条不出结论（BR-04） |
| `CALIBER_THRESHOLD` | 评论量 ≥100 条才生成完整评价（BR-02/BR-03） |

## 7. 如何使用

```powershell
# ★ 一键全量验证（答辩前/提交前推荐）：环境自检 + 全部测试 + 契约 + 就绪状态汇总
.\.venv\Scripts\python.exe scripts\verify_all.py          # 约 12 秒
.\.venv\Scripts\python.exe scripts\verify_all.py --fast   # 跳过最慢的 verify_phase4

# 启动开发服务器（读取 .env 的 FLASK_HOST / FLASK_PORT / FLASK_DEBUG）
.\.venv\Scripts\python.exe run.py          # → http://127.0.0.1:5000/

# 首次使用：创建管理员账号（口令交互式输入，不回显；不提供自助注册管理员）
.\.venv\Scripts\python.exe scripts\create_admin.py --username admin

# 接口冒烟测试（本地 test_client，不启端口、零 API 调用、零写库）
.\.venv\Scripts\python.exe scripts\smoke_api.py

# 认证与鉴权测试（口令哈希 / 注册 / 登录 / 会话 / 越权 / 停用失效；测试用户自动清理）
.\.venv\Scripts\python.exe scripts\test_auth.py

# 事实包验证：证明 C-BAT-06 零模型调用 + 幂等跳过 + 清理回基线
.\.venv\Scripts\python.exe scripts\test_fact_package.py

# 接口字段契约校验：逐个断言"前端依赖的字段"确实存在（仅公开接口）
.\.venv\Scripts\python.exe scripts\check_api_contract.py

# 页面绑定检查：JS 引用的元素 id 是否都存在于对应模板/脚本（静态、零成本）
#   —— 冒烟测试与契约测试都发现不了"JS 取了一个打错字的 id"这类静默失效
.\.venv\Scripts\python.exe scripts\check_page_bindings.py

# 手工抽查
curl.exe "http://127.0.0.1:5000/api/overview/summary"
curl.exe "http://127.0.0.1:5000/api/spots/564"
curl.exe "http://127.0.0.1:5000/api/spots/564/report"
curl.exe "http://127.0.0.1:5000/api/compare?spot_a=564&spot_b=196"
```

> **演示还要看什么**：见 `docs/答辩演示手册.md`（演示动线、常见提问的标准回答、
> 全量生产命令、以及"不要做的事"清单）。

## 8. 当前进度（截至阶段四）

| 层 | 状态 |
|---|---|
| 数据层 | ✅ 阶段一导入、阶段二清洗、阶段三 Spark 全量统计（`stat_*`/`sentiment`(mllib)/`topic`） |
| 语义层 | 🟡 阶段四 C-BAT-05～07 代码完成、32 条真实调用验证通过；**全量 47,701 次待授权** |
| 接口层 | ✅ 自检 3 个 + 业务只读 16 个 + 认证 4 个 + 问答 2 个 + 管理端 4 个 + 重新生成 1 个（本文件 §4） |
| 页面层 | ✅ 已做 7 个业务页 + 登录页；M3 页面对管理员显示「重新生成评价」入口 |
| 认证/权限 | ✅ 已实现（注册/登录/注销/当前用户 + 管理员接口鉴权；`sys_user` 表已启用） |

> `/healthz` 的 `stage` 字段已与项目实际进度同步（阶段四）；
> 判断进度仍以 `开发上下文索引.md` 与 `开发日报/` 为准——`stage` 只是一个概览字段。

## 9. 对应项目设计中的组件

| 组件 | 说明 | 状态 |
|---|---|---|
| `C-API-01` | 用户注册/登录/角色 | ✅ 已实现（接口 1–4；口令加盐哈希 + 会话 + `login_required`/`admin_required`） |
| `C-API-02` | 数据总览统计 | ✅ 5 个接口 |
| `C-API-03`～`C-API-08` | 景点列表/详情/趋势/情感/主题/方面/代表评论 | ✅ 已实现（只读） |
| `C-API-09` | 景点智能评价读取 | ✅ 已实现（接口 18 读取 + 接口 19 `regenerate` **登记 pending 请求**，不在 Web 层调模型） |
| `C-API-10` | 景点对比 | ✅ 已实现（接口 20；指标由后端计算，解读由 `APP_COMPARE_LIVE` 控制、默认关闭） |
| `C-API-11` | 智能问答 | ✅ 已实现（接口 21–22；分类/检索零成本，回答生成由 `APP_QA_LIVE` 控制、默认关闭） |
| `C-API-12` | DeepSeek 调用封装 | ✅ 已实现（`app/llm/`，离线批处理用；**Web 层不引用**） |
| `C-API-13` | 数据访问组件 | ✅ 已实现（`app/db.py` + `data_access.py`） |
| `C-API-14` | 统一响应与错误处理、任务查询 | ✅ 已实现（响应封装 + 4 个管理端接口，**均需管理员**） |

> 本表此前把 `C-API-01/10/11` 标为"未实现"、把接口 19 标为"未实现"，
> 那是它们落地之前的记录；现已按实际状态更正（26 个接口全部实现，页面对应 8 个路由）。

## 10. 与其他模块的关系

- **→ `app/db.py` / `data_access.py`**：接口层唯一的数据库出口，所有 SQL 参数化且只读。
- **→ `app/batch/`、`app/llm/`**：Web 只**读**它们离线写入的结果，**不调用、不触发**（批处理依赖人工运行）。
- **→ `templates` + `static`**：展示层不直连数据库，数据一律通过 `/api/*` 获取。
- **← `run.py`**：唯一的启动入口。

## 11. 答辩时重点理解

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
