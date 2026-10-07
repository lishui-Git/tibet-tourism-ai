# app/llm/ —— DeepSeek 调用封装与阶段四入口（C-BAT-05 / 06 / 07）

> 本目录对应 C4 组件 `C-API-12`「DeepSeek 调用封装」的 Python 实现，
> 同时承载阶段四三个批处理组件的**统一入口**。
> 上游说明见 [`../README.md`](../README.md)、[`../batch/README.md`](../batch/README.md)。
>
> 设计依据：`docs/design/详细设计说明书.md` §7（调用设计）、§15.A/B/C（三个 AI 功能）、§4.3/§4.9；
> 流程图：`docs/diagrams/DeepSeek语义分析流程图.md`、`景点智能评价流程图.md`。

---

## 1. 三个组件的职责（不要混为一谈）

| 组件 | 名称 | 输入 | DeepSeek 负责什么 | 输出表 |
|---|---|---|---|---|
| `C-BAT-05` | 评论语义分析 | 单条评论正文 + 景点名 | **只回答"这条评论在说什么"**：极性/强度/方面/关键词/摘要 | `sentiment`(deepseek)、`aspect`、`comment_semantic` |
| `C-BAT-06` | 景点事实包 | `spot` + `stat_spot` + `review` + `sentiment` + `aspect` | **完全不调用模型**（事实只能由 SQL 聚合产生） | `spot_fact_package` |
| `C-BAT-07` | 景点智能评价 | C-BAT-06 的事实包 | **只做语言组织**：综合评价/优势/问题/关注点四段 | `spot_report` |

一句话边界：**评论级 ≠ 景点级**。C-BAT-05 不产出任何景点结论；C-BAT-07 不允许计算任何统计数字。

## 2. 目录与文件

| 文件 | 职责 |
|---|---|
| `client.py` | DeepSeek HTTP 客户端：重试、限流退避、超时、错误分类、用量统计（**不含业务判断**） |
| `mock.py` | 假客户端：小样本链路联调（零 API 消耗；可注入失败用于验证失败处理） |
| `prompts.py` | Prompt 模板集中管理 + 版本号（`SEMANTIC_PROMPT_VERSION` / `REPORT_PROMPT_VERSION`） |
| `validators.py` | 模型输出校验：枚举、钳制、evidence 原文子串、长度截断、数字一致性 |
| `preflight.py` | **全量运行前预检**（只读、零调用）：工作量、费用区间、完整性、核心表校验和、READY/BLOCKED |
| `__main__.py` | 统一 CLI（`python -m app.llm --stage ...`）、规模闸门、离线闸门与结果自检 |
| `../batch/semantic_analysis.py` | C-BAT-05 实现 |
| `../batch/fact_package.py` | C-BAT-06 实现 |
| `../batch/spot_report.py` | C-BAT-07 实现 |
| `../batch/task_registry.py` | `analysis_task` / `task_log` 登记（三个组件共用） |

## 3. DeepSeek 配置（`app/config.py` → `settings.deepseek`）

配置只从 `.env` 读取（`NR-M-01`），**代码与文档中没有任何 Key**：

| 键（`.env`） | 默认 | 说明 |
|---|---|---|
| `APP_DEEPSEEK_API_KEY` | 空 | **未填写时：真实调用直接给出可操作报错，程序不崩溃**（`NR-R-05`） |
| `APP_DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API 地址 |
| `APP_DEEPSEEK_MODEL` | `deepseek-chat` | 冻结模型 |
| `APP_DEEPSEEK_TIMEOUT` | `60` | 单次请求超时（秒） |
| `APP_DEEPSEEK_MAX_CONCURRENCY` | `3` | 并发（设计 §7.2：3–5，避免限流） |
| `APP_DEEPSEEK_MAX_RETRY` | `2` | 重试上限（间隔 2s / 5s；限流更久） |
| `APP_DEEPSEEK_PRICE_INPUT` | `2.0` | 成本估算单价（元/百万 token），仅用于估算 |
| `APP_DEEPSEEK_PRICE_OUTPUT` | `8.0` | 同上；**真实费用以 API 账单为准** |

> `APP_` 前缀是**必须**的：项目根目录 `.env` 会被开发工具一并读取，无前缀的 `DEEPSEEK_BASE_URL`
> 属于工具保留变量，会导致工具拒绝启动（阶段一已踩过，见 `README_开发说明.md` §四）。

**安全边界（三条硬约束）**：
1. Key 只从 `.env` 进 `settings`，客户端不读环境变量；
2. 异常与日志只带状态码、URL 路径与响应片段，**绝不打印请求头/Key**；
3. README、日报、日志、代码中一律不出现真实 Key。

## 4. 调用链

```
python -m app.llm --stage semantic|facts|report|all|check
        │
        ├── C-BAT-05 semantic_analysis.run_semantic_analysis()
        │     ├─ SQL 分层：待调用（非低信息量、重复组代表）/ 规则层（≤10 字）/ 复用层（组内成员）
        │     ├─ client.chat(system+user, temperature=0.1, max_tokens=512)
        │     ├─ prompts.extract_json_text → validators.validate_semantic（7 条校验）
        │     └─ 写 sentiment / aspect / comment_semantic（幂等 upsert）
        │
        ├── C-BAT-06 fact_package.run_fact_package()      ← 无模型调用，纯 SQL 聚合
        │     └─ 写 spot_fact_package（UK(spot_id, version)）
        │
        └── C-BAT-07 spot_report.run_spot_report()
              ├─ 读 spot_fact_package（唯一输入）
              ├─ client.chat(事实包 JSON, temperature=0.3, max_tokens=1200)
              ├─ validators.validate_report + check_number_consistency
              └─ 写 spot_report（PK spot_id）
```

## 5. Prompt 管理

- 全部模板集中在 `prompts.py`，业务代码里**不散落 Prompt 文本**；
- 每个场景 = `system`（硬性约束段，`BR-07` 的实现手段）+ `user`（数据 + 输出 Schema）；
- **版本号可追溯**：`SEMANTIC_PROMPT_VERSION` 写入 `sentiment.raw_json.prompt_version`；
  `REPORT_PROMPT_VERSION` 写入 `spot_report.prompt_version`（建表脚本该列默认 `p1`）。
  模板一改就必须递增版本号，否则历史结果无法解释；
- 校验不通过时会走一次**修复轮**（把"上次错在哪"回传），修复轮复用同一模板与版本号。

## 6. 数据库写入与幂等

| 表 | 幂等键 | 写入方式 |
|---|---|---|
| `sentiment` | `PK(comment_id, method)` | `INSERT ... ON DUPLICATE KEY UPDATE` |
| `aspect` | `UK(comment_id, aspect_name)` | **先按 comment_id 删旧行再插入**（方面数量会随重跑变化，避免残留） |
| `comment_semantic` | `PK(comment_id)` | upsert |
| `spot_fact_package` | `UK(spot_id, version)` | upsert；每次生成新 `version` 不覆盖历史（`FR-IE-07`） |
| `spot_report` | `PK(spot_id)` | upsert；已有同事实包版本则**跳过**，`--force` 才重生成 |
| `analysis_task` / `task_log` | — | 复用冻结的运维表，`task_type` = `semantic`/`fact_package`/`spot_report` |

**断点续跑**：断点游标就是结果表自身——每次启动先用 SQL 排除"已有 `sentiment.method='deepseek'`"的评论，
因此：已成功的自动跳过、失败的可补跑、**重跑不重复计费**、中断后直接重跑同一命令即可继续。

**断点粒度**：每 `WRITE_BATCH=200` 条提交一次，宕机最多重做 200 条。

### 6.1 幂等的边界：它挡不住"两个实例同时跑"

`WHERE NOT EXISTS(...)` 是**计划阶段**的判断，而写入在后面。所以：

```
实例 A 计划 → 查到 0 条已完成 → 开始调用（付费）
实例 B 计划 → 同样查到 0 条已完成 → 也开始调用（重复付费）
```

`ON DUPLICATE KEY UPDATE` 只保证**写库不重复**，**拦不住重复的 API 调用**——
代价是**双倍费用**（约 ¥75 → ¥150）。

因此新增了**并发运行闸门**（`app/llm/__main__.py` 的 `_guard_concurrent_run`）：

| 情形 | 行为 |
|---|---|
| 同 `task_type` 有 `status='running'` 且启动时间较新 | **拒绝执行**，并说明处理办法 |
| 该任务已超过 `STALE_RUNNING_HOURS = 6` 小时 | 视为上次崩溃遗留，不阻断（避免一次崩溃永久锁死） |
| `--dry-run` / 只读阶段（`check`/`preflight`/`replay`） | 不受影响 |
| 确认无并行、或确需并行 | 显式 `--allow-concurrent` 放行（并打印警告） |

由 `scripts/test_cli_guards.py` 的 `[H]` 组（8 项）固定，含"6 小时陈旧不阻断"与清理断言。

## 7. 失败处理

| 情况 | 处理 |
|---|---|
| 网络错误 / 5xx / 429 | 客户端自动重试 ≤2 次（2s、5s；429 用 5s、15s），仍失败则判该条失败 |
| 4xx（参数/鉴权） | **不重试**（无意义且会产生等待） |
| 返回非法 JSON / 校验不过 | 走一次修复轮；仍失败记失败 |
| 单条失败 | 写 `task_log`（`level=ERROR`、`stage=semantic`、`ref_key=comment_id`、`resolved=0`），**批次继续** |
| 重新跑失败项 | `--only-ids comment_id,...` 定向补跑 |
| **写库失败（模型已成功）** | **只重试写库（≤3 次，带退避），绝不重新调用模型**；仍失败则把已付费结果落盘到 `logs/recovery/*.jsonl` 待补写。见 §7.1 |

`--only-ids` 的补跑方式（示例）：

```powershell
# 1) 查未解决的失败项
mysql -e "SELECT ref_key, message FROM task_log WHERE level='ERROR' AND stage='semantic' AND resolved=0 LIMIT 20"
# 2) 定向补跑
.\.venv\Scripts\python.exe -m app.llm --stage semantic --only-ids 73611141,74138768
```

### 7.1 写库失败的兜底（"不因为写不进去而重复扣费"）

调用层的顺序是"**先调用模型（钱已经花了）→ 再写库**"。
如果写库抛错，`app/db.py` 的 `connection()` 会**整批回滚**，
那么这一批已付费的结果就会丢掉；而下次运行时游标发现这些评论"还没有结果"，
就会**再调用一次模型**——同一批数据被重复扣费。

因此调用层统一走 `write_records_safely()`：

| 步骤 | 行为 |
|---|---|
| ① 首次写库失败 | 打印失败原因，进入重试 |
| ② 重试（≤3 次，退避 2s/3s） | **只重写数据库，绝不重新调用模型**；`write_records` 整批在同一事务内，失败会整批回滚，因此重试**不会产生重复行或残留脏数据** |
| ③ 重试仍失败 | 把已付费结果写入 `logs/recovery/semantic_<n>.jsonl`（按 `comment_id % 100` 分片），控制台明确提示"已落盘待补" |
| ④ 补写 | 数据未入库的评论**仍然是待处理**，下次正式运行时会被游标重新选中并按正常流程写库；落盘文件只作为"结果没丢"的凭证与人工补写依据 |

### 7.2 运行结束后的"实际 usage / 实际费用"

保险机制第 11 条要求"运行结束后输出**实际** usage 与实际费用"，两个生成组件都做到了：

| 组件 | 实际用量 | 实际费用 |
|---|---|---|
| **C-BAT-05 评论语义** | 每次调用的 `usage` 写入 `sentiment.raw_json.usage`；**修复轮与首次调用相加**，逐次明细写入 `raw_json.usage_calls` | `CallStats` 累计输入/输出 token × 实际单价（**含修复轮**） |
| **C-BAT-07 景点评价** | 每次调用的 `usage` 拆分写入 `CallStats`（修复轮的两次调用**累加**） | `_estimate_cost()` 按**实际拆分** × 实际单价计算 |

> **2026-10-06 修掉的一处真实缺陷（C-BAT-05 的修复轮 usage 曾丢失）**：
> `spot_report`（C-BAT-07）早就做到了"修复轮累加"，但 `semantic_analysis`（C-BAT-05）
> **没有**：修复轮返回的 `retry.usage` 被直接丢弃——
> `stats.record_success(response)` 与 `_build_api_record(..., response.usage)`
> 都只用了**首次**调用的用量。而修复轮是**第二次真实且计费**的请求，
> 于是"事后按库核对费用"会**少算一次调用**，与本节标题的要求相矛盾。
>
> 现在由 `merge_call_results(first, second)` 统一合并后上报：
> · `usage` 仍是**同样三个键**（prompt / completion / total），值为两次之和 ⇒ **旧读取方无需改动**；
> · `attempts` 取两次之和 ⇒ `CallStats.retry_count = attempts − requests` 能把修复轮这次额外请求算进去；
> · `content` 取**最终成功**的那次（修复轮）⇒ 业务语义不变；
> · 逐次明细写入 `raw_json.usage_calls`（`[{call:"first",...}, {call:"repair",...}]`），
>   **只在真的发生修复轮时才写**，避免给只调了一次的结果添噪音。
> 由 `scripts/test_repair_usage.py`（27 项）固定：合并值等于两次之和、
> `CallStats` 含修复轮、`retry_count=1`、`raw_json.usage_calls` 明细完整、
> **不发生修复轮时行为完全不变**。

> **本轮修正的一处口径问题**：`spot_report` 原先只把 token **总量**塞进 `CallStats`
> （`usage={"total_tokens": n}`），导致 `prompt_tokens` 恒为 0，
> 费用只能按 **7:3 经验比例**估算。而输入 ¥2/M、输出 ¥8/M 相差 4 倍，
> 比例猜错就会算错钱。现已改为返回**真实用量**（含 prompt/completion 拆分，修复轮累加），
> 费用按 `prompt/1e6×单价_in + completion/1e6×单价_out` 精确计算，
> 结果字段也从 `cost_total_cny_estimated` 改名为 `cost_total_cny`（口径变了，名字要跟着变）。
> 由 `scripts/test_report_usage.py`（14 项）固定：断言拆分保留、费用与手算一致、
> 修复轮累加、**且两个组件的费用字段形状必须一致**（见下）。

#### 两个组件的费用字段（刻意保持同形）

| 字段 | 含义 |
|---|---|
| `prompt_tokens` / `completion_tokens` / `total_tokens` | 本次运行的**实际**用量（修复轮已累加） |
| `price_input_per_million` / `price_output_per_million` | 计算所用的单价（来自 `settings.deepseek`，可配置） |
| `cost_input_cny` / `cost_output_cny` / `cost_total_cny` | 按实际拆分算出的**实际**费用（元） |
| `avg_tokens_per_call` | 单次平均 token（便于与 §8.2 实测口径交叉核对） |

> **为什么必须同形**：C-BAT-05 与 C-BAT-07 各报一套字段的话，费用汇总就要写特例分支，
> 也容易漏算其中一项。现在 `semantic_analysis.estimate_cost()` 与
> `spot_report._estimate_cost()` 返回**完全相同的 9 个键**，
> 并由测试断言"两个组件字段集合完全相同"——**形状漂移会被立刻发现**。

#### 运行前也能看到调用量与费用（保险机制第 1、2 条）

三个地方都会在**运行前**给出预估，且数字**口径一致**（同一实测 token 区间 × 同一配置单价）：

| 入口 | 给出的内容 |
|---|---|
| `--stage preflight` | 全局体检：待调用/待复用/待生成份数 + 费用区间 + `STATUS` |
| `--stage semantic --dry-run` | `api_calls_planned`、`estimated_tokens_per_call`（实测均值与区间）、`estimated_cost_cny`（min/max） |
| `--stage facts --dry-run` | `eligible_spots`（可评价景点数）、`already_has_version`、`pending_api_calls: 0`（事实包零模型调用） |
| `--stage report --dry-run` | `eligible_spots`（业务口径：评论量 ≥100 的景点数）、`packages_available`（当前就绪事实包）、`pending_api_calls`（本次真正会调用的份数）、必要时给出 `hint` 提示先跑 facts |

> **为什么要把 `--dry-run` 也测起来**：CLI 会打印
> `[dry-run] 只统计不写库；真实调用与写库均不会发生`——这是**代码的承诺**。
> 本轮实测抓到它被违反过：`--stage all --dry-run` 里 C-BAT-06 **忽略**了 `dry_run` 参数，
> 照样写入 57 行事实包并登记 1 条任务（零成本，但**改变了库状态**）。
> 这会让"先试跑看看"变得不可信，并让后续 `report --dry-run` 报出
> `packages_available=57`、误导人以为"评价依据已就绪"。
> 现已补齐，并由 `scripts/test_dry_run_contract.py`（11 项）断言
> **四个 dry-run 组合跑完后 17 张表逐表未变**、且事实包输出自证"零调用"。

> **为什么要分 `eligible_spots` 与 `packages_available`**：这两个数天然不同——
> 前者是"设计上应该生成评价的景点数"（57），后者是"当前库里已有事实包、因而**马上**能生成的份数"。
> 如果只报一个数，就会出现"dry-run 说 0 份、preflight 说 57 份"的矛盾观感，
> 让人怀疑哪个算错了。现在两者并列显示，且 `pending_api_calls` 只认后者（没有依据就不生成）。
> `scripts/test_cost_projection.py`（11 项）断言 dry-run 与 preflight 的数字**逐项相等**。

除了 JSON 明细，命令行在两个生成阶段结束后会额外打印一行**给人看**的结论
（`app/llm/__main__.py` 的 `_print_cost_line()`）：

```
[本次实际用量与费用] 输入 10,691 + 输出 4,681 = 合计 15,372 tokens；单次均 480.4 tokens；实际费用 ¥0.0588
  （金额按实际输入/输出 token × 配置单价计算；最终以 DeepSeek 账单为准）
```

- 数字直接来自 `CallStats`（**实际**用量），不是估算；
- 事实包（`--stage facts`）零调用、没有 `cost` 字段，因此**不会打印这一行**——
  避免让人误以为它花了钱；
- 两种极限情况（真实有量、复用全 0）都有测试覆盖。

> **同一套兜底也覆盖景点评价（C-BAT-07）**：`run_spot_report` 的写库失败同样会
> "只重试写库 → 落盘待补 → 记 `task_log`(ERROR) 并继续下一个景点"，
> 且**每个景点写成功即提交**。共享实现见 `app/batch/write_recovery.py`，
> 两类组件使用**同一种 JSONL 结构**（`{"text":…, "payload":…}`），因此可以用
> `load_recovery(prefix)` 统一读出后零成本补写。
>
> **补写入口（本轮补上的一环）**：`python -m app.llm --stage replay [--dry-run]`
> ——把 `logs/recovery/*.jsonl` 里"**已付费但没写进库**"的结果写回数据库。
> 为什么必须补：`load_recovery()` 与 `payload_to_record()` 早就写好了、docstring 也写着
> "补写用"，但**没有任何生产入口调用它们**（只有测试在用）——
> 也就是说恢复文件曾经是"**只写不读**"的，真出事后得手工处理。
> 现在这条链路是通的：
>
> | 行为 | 说明 |
> |---|---|
> | **零模型调用** | 只做 `payload → 数据库`；`app/batch/replay.py` 不持有客户端、不导入 HTTP |
> | `--dry-run` | 只报告"几个文件、多少条待补"，**不写库、不删文件** |
> | 补写成功才删凭证 | 以"数据库里到底有没有"为准重写恢复文件；全部成功才删除 |
> | 失败仍保留 | 补写自身若再失败，仍走原重试/落盘机制，绝不丢已付费结果 |
>
> 由 `scripts/test_replay.py`（16 项）固定：造文件 → dry-run 不写库 →
> 实际补写入表 → 文件删除 → **行数回到基线**。

> **事务粒度的修正（实测缺口）**：`connection()` 原本**只在正常退出时提交一次**，
> 意味着 4.7 万次调用的中途任何异常都会把当轮已付费结果**全部回滚**——
> 这与 `app/db.py` 文档里写的"按批 commit，便于断点续跑"并不一致。
> 现已改为：规则层每批、复用层每次、调用层**每批写库后立刻 `conn.commit()`**，
> 断点粒度 = 一个写批次（200 条）。

> 该行为已由两个测试固定（均**不调用任何模型**）：
> `scripts/test_write_recovery.py`（语义：注入写库失败 → 重试/落盘/补写闭环，
> 并断言测试前后结果表行数逐项一致）与
> `scripts/test_spot_report_write_failure.py`（景点评价：写失败 → 记 failed + task_log ERROR +
> 落盘 + 任务标 partial + **批次继续**，而非整轮回滚）。

## 8. 成本控制与**实测**成本（设计 §7.4 / NR-P-07）

### 8.1 分层过滤省下的调用

| 措施 | 实测条数 |
|---|---|
| 正文 ≤10 字走规则判定，不调模型（`BR-05`） | 10,228 条（已全部按规则入库，`source='rule'`） |
| 重复正文组内只调一次并复用（`BR-06`） | 4,239 条 / 1,285 组 → 实际只调 **585** 次（另 700 组全为短评，0 次调用） |
| 正文为空不分析 | 1 条 |
| 结果落库后不重复调用 | 断点游标（已实测：显式 `--only-ids` 重跑时调用数为 0） |
| **全量调用次数** | **47,733 次**（设计估算 44,565，差异见 §10 第 5 条） |

#### 分层覆盖自洽性（preflight `[2b]`，证明"没有评论被静默跳过"）

光看各层计数**证明不了**"每条评论都会被处理到"——可能出现某类评论既不属于低信息量、
也不属于重复组、又没进入待调用集合，于是被静默跳过，而各层数字看起来都正常。
因此 preflight 增加了一组只读校验，验证三层是**互斥且穷尽**的划分（实测）：

| 层 | 覆盖口径 | 条数 | 说明 |
|---|---|---|---|
| 规则层 | **全部**正文 ≤10 字（无论是否重复组成员） | 10,228 | BR-05；已完成 10,228 |
| 复用层 | 重复组非代表成员，且自身不是低信息量 | 1,071 | BR-06；复制代表结果，零调用 |
| 调用层 | 其余（真正进入模型） | 47,733 | 已完成 31，待处理 47,702 |
| — | 三层之和 | **59,032** | = 可分析评论数；另有 1 条正文为空不参与 |

同时校验：各层"待处理 = 总量 − 已完成"必须成立（否则会出现"显示待处理 0 条、实际还有没做的"）。
**这两处口径错误都是在这组校验写出来后当场抓出来的**（详见 §10 第 11 条），
并由 `scripts/test_layering.py`（14 项）固定。

#### ⚠️ 别把阶段二的 `47,149` 与本节的 `47,733` 当成矛盾

阶段二 `data/…清洗统计.json` 里有一个 **“文本可用条数 = 47,149”**，与本节的调用层
`47,733` 相差 585。**两个数都对，只是回答的问题不同**，直接相减会得出"文档自相矛盾"的错误结论：

| 口径 | 数值 | 回答的问题 | 边界 |
|---|---|---|---|
| 阶段二 `文本可用条数` | **47,149** | "有多少条正文是**独立且非低信息量**的"（供文本建模用） | 从 59,033 起算，**包含**那 1 条空正文 |
| 阶段四 `调用层总量` | **47,733** | "有多少条**需要模型处理**"（用于算钱） | 从**可分析评论** 59,032 起算，**排除**空正文 |

两者关系（可逐项复核）：

```
调用层 47,733 − (文本可用 47,149 − 空正文 1) = 585
            = 非低信息量的重复组代表数（全量时各调用一次）
```

- 重复组共 **1,285** 组，其中 **700** 组整组都是低信息量（代表走规则层、不调用），
  仅 **585** 组非低信息量 → 这 585 条代表进入调用层，其余成员复用；
- 低信息量与重复正文**存在 2,583 条重叠**（短评本身容易重复），
  所以 `总数 − 低信息量 − 重复 = 44,566` 这种"直接相减"是**错的**，
  正确算式要么带上重叠（`+2,583` 得 47,149），要么按"组内非代表"算。

> 由 `scripts/test_cross_phase_numbers.py`（15 项）固定：
> 断言重叠存在、断言 47,149 与库内标记位一致、断言 47,733 的三层划分穷尽、
> 断言两口径之差**能被那 585 与 1 条空正文完整解释**。

### 8.2 真实调用实测（2026-09-24，共 32 次）

| 项 | 实测值 |
|---|---|
| 真实调用 | **32 次**（4 批 × 8 条，`--limit 8`、`concurrency=2`） |
| 成功 / 失败 | **32 / 0** |
| 重试次数 | **0**（无网络错误、无限流、无 JSON 解析失败） |
| 输入 token | 10,691 |
| 输出 token | 4,681 |
| 总 token | **15,372** |
| 平均每条约 | **480 token**（单批区间 431–562） |
| 单批耗时 | 8.7–9.9 秒（8 条、并发 2），约 1.1–1.2 秒/条 |
| 实付费用 | **约 ¥0.059**（32 次合计） |
| 单条平均费用 | 约 ¥0.0018 |

**质量侧同时实测到的现象（重要）**：模型确实会**编造 evidence**——
`dropped_aspects` 累计 20 个被校验拦下，例如「青藏公路美翻了」在原文中并不存在。
这正是 §15.A.4 第 5 条存在的意义；被拦下的方面与原因已留档在 `raw_json.dropped_aspects`。

### 8.3 全量成本：**已执行完毕的实际值**

**实际花费（2026-10-07 两阶段跑完，不是估算）**：

| 项 | 实测 |
|---|---|
| 评论级（C-BAT-05）**47,702** 次 | 输入 16,199,065 + 输出 6,881,503 = **23,080,568** tokens → **¥87.4502** |
| 景点级（C-BAT-07）**57** 次 | 输入 122,293 + 输出 25,141 = **147,434** tokens → **¥0.4457** |
| 事实包（C-BAT-06） | **0 次调用、¥0**（纯 SQL 聚合，不含任何 LLM 客户端） |
| **合计** | **约 ¥87.90**；评论级与成本模型中位预测相差 **0.18%**（口径经实测检验） |

**当前推算口径**（已按全量实测重校）：单条均值 **484** token（实测 483.8）、
最小 **335**、观测最大 **2695**；输入:输出 = **69.55% : 30.45%**；单价 输入 ¥2 / 输出 ¥8 每百万 token。

> **⚠️ 概念区分（重要）**：**观测最大值 2695 token/条**是分布**尾部的一个观测点**，
> 既不是"逐条理论上界"，也不是预算口径——拿它乘全部调用数会把预算夸大到失去指导意义。
> 预算看按正文长度分布加权的**保守上界**。二者由 `scripts/test_cost_model.py` **分别断言**：
> 观测最大值只需"覆盖库内实测最大"（防漂移），预算上界只需"≥ 实测均值"且**允许低于**该尾部值。

> **为什么分成"区间"与"保守估算上界"两档（2026-10-06 重新校准）**
>
> 原先只有一个区间 `¥79.39–103.31`，其中**上限 562 token/条**是"那 32 条样本内的最大值"。
> 但那个样本取 `comment_id` 最小者，**正文极短**（8 条落库样本里 7 条只有 15–22 字，
> 最长那条 126 字便已用掉 **600 token**），而全量待处理里有 **5,637 条**正文超过 126 字
> （>300 字 1,094 条，最长 1,829 字）。**拿短样本的最大值当全量上限，方向上是低报风险。**
> 因此现在按"口径可复核"原则重定三个锚点与一个上界：
>
> | 口径 | 值 | 依据（可用只读 SQL 复核） |
> |---|---|---|
> | 最低 | **392** token/条 | `sentiment.raw_json.usage` 现存 8 条的最小值 |
> | 均值 | **480** token/条 | 32 次真实调用的进程统计汇总（10,691+4,681=15,372 ÷ 32） |
> | 已观察最大 | **600** token/条 | 同一批 8 条落库样本的最大值（原 562 已被它突破） |
> | **保守估算上界** | 按正文长度加权 → **647.3** token/条 | `300`（固定 prompt，实测最短正文 prompt_tokens 304–312）+ `0.7×正文长度`（中文保守取值）+ `300`（输出上限，实测 88–232、`max_tokens=512`） |
>
> · 8 条落库样本为：392 / 402 / 403 / 407 / 408 / 409 / 429 / 600。
> · 加权用的是**待处理集合的真实正文长度分布**（`preflight.LENGTH_BUCKET_SQL`，只读）。
> · **保守上界是"上界"而不是"真实最坏情况"**——真最坏情况无法在不做全量真实调用的前提下确定。
> · 三档必须递增（最低 ≤ 已观察最大 ≤ 保守上界），由 `scripts/test_cost_model.py` 固定；
>   其中一条断言专门盯"**库内实测最大不得超过配置上界**"，一旦将来跑出更大值，测试会失败并强制重新校准。
> · 校准证据会随 `--stage preflight` 一起打印（`cost.calibration`），不必翻文档。
>
> **这两个数是"一次成功、无重试"的估算——重试同样计费。**
> 客户端对网络错误 / 5xx / 限流会**自动重试**（`--max-retry` 默认 2），
> 每次重试约使该次调用成本**翻倍**；实测 32 次为 **0 重试**，因此没有可用重试率可加权。
> 与其用猜的比例去"修正"，不如**把风险讲清楚**：
> **若运行中出现较多重试，实际费用会高于以上数字**。
> 预检会打印这条说明（`cost.retry_note`），由 `scripts/test_cost_model.py` 固定。
> 更稳妥的做法是**按保守估算上界以上留余量充值**。
>
> **修复轮也会额外计费**（校验不通过时会有第二次真实调用）。该花费现已如实计入
> `CallStats` 与 `raw_json`（见 §7.2），但**没有单独预留预算**——它包含在"重试同样计费"
> 这条风险之内，因此判断预算时以**保守估算上界**为准更安全。

> 该区间与 `--stage preflight` 的实时输出完全一致（同一个计算函数）。
> **合计由各分项四舍五入到分后相加得出**，因此打印出来的"评论级 + 景点级 = 合计"
> 可以当场按计算器核对（此前用未舍入中间值算合计，出现过 74.43 + 0.63 显示为 75.05 的观感问题（该组数字已随比例修正而更新））。
>
> **数据来源与局限（如实说明）**：
> · 32 次实测中，**最后 8 次**的 token 用量已随结果写入 `sentiment.raw_json.usage`，可仅凭数据库复核；
>   更早的 23 次当时尚未落库 usage，只能引用进程统计（preflight 会把这 23 条作为**提示项**列出，不阻断）。
> · 样本取 `comment_id` 最小的 32 条（`--limit` 的确定性口径），短评比例与全库不同，
>   全量费用应以实际运行后的统计为准；本表用于判断"是否可负担"，不作为结题定稿数字。
> · 真实费用最终以 DeepSeek 账单为准；单价可用 `.env` 的 `APP_DEEPSEEK_PRICE_INPUT/OUTPUT` 覆盖。
>
> **口径订正（2026-10-06）**：上面"更早的 23 次"原写作 24（并在本句重复为"这 24 条"）。
> 32 次是**历史调用次数**，而 `comment_id=73603914` 已在开发期事故中删除（见 §6 待补跑 1 条），
> 其 usage 状态无从复原；因此**当前库内实测为 23 条**
> （`raw_json.source='deepseek'` 共 31 条 = 8 条有 usage + 23 条无），
> 与 `--stage preflight` 的 `usage_missing` 输出逐位一致。**调用次数 32 与现存行数 31、缺 usage 行数 23 并不矛盾。**

成本统计由客户端 `CallStats` 给出：请求数、实际请求次数（含重试）、输入/输出 token、
平均 token、耗时、失败分类；`estimate_cost()` 用配置单价换算金额。

## 9. 小样本验证与全量执行条件

```powershell
# ① 阶段四结果健康自检（只读）
.\.venv\Scripts\python.exe -m app.llm --stage check

# ①b 全量运行前预检（只读、零调用；给出工作量、费用区间与 READY/BLOCKED）
#     这是"要不要开跑、要花多少钱"的唯一权威入口
.\.venv\Scripts\python.exe -m app.llm --stage preflight

# ② 只看工作集规模（不调用、不写库）
.\.venv\Scripts\python.exe -m app.llm --stage semantic --dry-run

# ③ 链路联调（mock，零消耗；--limit ≤ 50，否则 CLI 拒绝执行）
.\.venv\Scripts\python.exe -m app.llm --stage all --limit 8 --mock

# ④ 自动验证 34 项（写库→校验→幂等→断点→失败→清理）
#    运行前只清理历史 mock 残留；**库内真实结果会被保留**（按快照差异删除新增行）
.\.venv\Scripts\python.exe scripts\verify_phase4.py
#    额外做一次全量 mock 压测（5.9 万条，写入后自动清理）
.\.venv\Scripts\python.exe scripts\verify_phase4.py --full-mock-check

# ⑤ 真实小样本（先填 .env 的 APP_DEEPSEEK_API_KEY）
.\.venv\Scripts\python.exe -m app.llm --stage semantic --limit 8

# ⑥ 全量（**必须显式 --yes 确认**，见下；不加 --limit 即全库）
.\.venv\Scripts\python.exe -m app.llm --stage semantic --yes
```

> **`--limit` 会"往后推进"，不会反复处理同一批**：它处理的是"接下来的 N 条待处理数据"，
> 因此带 `--limit` 重跑会继续处理后面的评论（这是断点续跑应有的行为，避免漏数据），
> 每跑一次都会产生真实费用。**要验证幂等跳过**（确认已处理的不会重复调用），请用显式 ID：
>
> ```powershell
> .\.venv\Scripts\python.exe -m app.llm --stage semantic --only-ids 73603914,73611141
> # 实测输出：api_calls_planned = 0、requests = 0、written = 0（未产生任何费用）
> ```

**全量执行的前置条件（四条全部满足才允许）**：
1. `python -m app.llm --stage preflight` 输出 **STATUS: READY**（核心表校验和一致、无重复结果、无非法值）；
2. `scripts/verify_phase4.py` **34/34** 通过（含全量 mock 压测）；
3. 真实小样本已通过，且记录了真实 token/费用（见 §8.2）；
4. 明确指定"全量"并带 `--yes`（默认不跑全量，避免误触发 4.7 万次调用）。

### 9.1 离线模式（开发/演示期的零消费保证）```powershell
# 任何真实调用都被硬阻断；只允许 preflight / check / --dry-run / --mock
.\.venv\Scripts\python.exe -m app.llm --stage semantic --yes --offline
#   → [已阻断] 离线模式下不允许执行 --stage semantic 的真实调用。

$env:APP_LLM_OFFLINE = "1"   # 也可以用环境变量全局生效
```

`--offline` 是**代码级**保证（两道闸门：`_guard_offline` + `_build_client`），
不依赖"记得别跑"这种自觉；适合在写接口、调前端、准备答辩演示时全程开启。

> `--mock` 使用假客户端，结果会在 `analysis_task.task_name` 中标注 `[mock]`，
> **论文与答辩中不得引用 mock 结果**。
>
> ⚠️ **`--mock` 会真实写库，而且假结果在结果表里和真结果几乎一样**（实测教训）：
> `MockClient` 产出的结果同样以 `source='deepseek'`、同样的字段写进
> `sentiment` / `comment_semantic` / `aspect`，甚至带一份**假 token 数**；
> 因此一次 `--mock --limit 1` 的试跑就会把一条待处理评论变成"看似真实的第 32 条结果"，
> 直接污染"真实 API 完成数"与费用核算。
> 为此做了两件事：
> 1. **`raw_json.mode` 标记**：mock 写入时记为 `"mock"`，真实调用记为 `"real"`
>    （31 条历史真实结果没有该字段，读作真实，符合事实）；
>    **复用行也标记为 `"reuse"`**（本轮补上，原因见下）；
> 2. **preflight 会检出并阻断**：`mock_rows_in_results > 0` 时 `STATUS: BLOCKED`，
>    提示"必须清除后再全量运行"；另有 `test_trace_rows_in_results`（mock + 复用）作为可见指标。
> 建议：**不要在正式库上跑会写库的 mock**；要联调就用 `--dry-run`（零写入），
> 或用测试脚本里的 `MockClient`（它们自带快照与清理）。
>
> **复用行为什么也必须标记（实测教训）**：复用行原先只有 `comment_semantic.source='reuse'`，
> 但该列会被**后续重跑覆盖**（同一 `comment_id` upsert 时可能写回 `'deepseek'`），
> 一旦被覆盖就再也认不出来。实测踩到过：11 条 mock 行 + **4 条复用行**一起残留在结果表里，
> 其中复用行因无标记而**与真实结果无法区分**。现在两者都写进 `raw_json.mode`，
> 可被一条 SQL 精确定位与清理。

### 7.4 `spot_report` 的 mock 行也要查（它没有 raw_json）

`sentiment` 的 mock 检出靠 `raw_json.mode='mock'`，但**景点评价表没有 `raw_json`**，
因此那条检查**覆盖不到它**。而 mock 模式写入的评价会把 `spot_report.model` 记为 `'mock'`
（见 `app/batch/spot_report.py`）：

```sql
SELECT COUNT(*) FROM spot_report WHERE model = 'mock'   -- 正常应为 0
```

> 为什么必须单独查：一条 mock 评价在库里与真实评价**完全一样**（同样的四段文案与字段）。
> 若全量运行前残留一条，那个景点会因"**已有评价**"被游标跳过而**永不重新生成**，
> 最终库里就混着一条假评价。preflight 现在对此 `STATUS: BLOCKED`。
>
> **`--limit` 的作用范围**：它限制的是"需要调用模型"的条数。真实运行时规则层（≤10 字）与复用层
> 不受限制——它们不产生 API 费用；`--mock` 联调时三层都会按 `--limit` 收窄，
> 避免"只跑 5 条却往库里写了 1 万条规则结果"。

#### 阶段不相关的选项会被明确提示（不静默忽略）

有些选项只对特定阶段有意义（例如 `--mock` 对事实包、`--concurrency` 对景点评价）。
如果传了它们却什么也没发生，操作者会以为"**参数坏了**"。
因此 CLI 会在执行前打印一行提示，说明"哪些选项在当前阶段不会生效、为什么"：

```
[提示] 以下选项在当前阶段不会生效（已按阶段实际行为执行）：
  · --mock 对 --stage facts 不起作用：事实包是纯 SQL 聚合、零模型调用，--mock 对它没有意义
```

对照表（`app/llm/__main__.py` 的 `STAGE_IRRELEVANT_FLAGS`）：

| 阶段 | 会被忽略的选项 | 原因 |
|---|---|---|
| `semantic` | `--force` | 评论语义按游标幂等跳过，没有 `--force` 语义 |
| `semantic` | `--version` | `--version` 只对事实包 / 景点评价有意义 |
| `facts` | `--mock`、`--concurrency` | 事实包零模型调用，两者都没有意义 |
| `facts` | `--force` | 事实包默认覆盖重算；要跳过已存在的请用 `--only-missing` |
| `report` | `--concurrency` | 景点评价按景点顺序生成（一次调用即提交），不并发 |
| `report` | `--only-missing` | 评价幂等由 `fact_package_version` 决定，不适用该选项 |

> 由 `scripts/test_cli_guards.py` 的 `[G]` 组断言：不相关选项**必须**有提示、相关选项**不得**打扰。

## 10. 与设计文档的差异（如实记录）

| # | 事项 | 设计文档 | 本项目实现 | 原因 |
|---|---|---|---|---|
| 1 | `comment_semantic.is_low_info` | §15.A.6 写入清单含该字段 | **不写**（该列在建表脚本中不存在） | 以冻结的表结构为准，不新增字段；低信息量以 `source='rule'` 体现 |
| 2 | `sentiment.is_valid` | §15.A.6 未列 | 正常 1；`polarity` 非法被判 neutral 时置 0 | 建表脚本该列 `NOT NULL`，语义见注释 |
| 3 | `spot_report.prompt_version` / `token_usage` | §15.C.3 入库清单未列 | 均写入 | 建表脚本有此两列，且"结果可追溯"要求记录 |
| 4 | 数字一致性阈值 | §15.C.3「0.5 个百分点」 | 统一换算为百分点比较 | 事实包占比是 0–1 小数，需统一单位 |
| 5 | 全量调用次数 | §7.4 估算 ≈44,565 次 | **实测 47,733 次** | 设计把 4,239 条重复全部计入"可省"，但组内仍需各调 1 次代表；且短评与重复存在 2,583 条重叠。结论仍在上限口径内，属如实修正 |
| 6 | §15.B 事实包示例数字 | 布达拉宫 review_count 3965 / avg_score 4.62 | 库内实测 avg_score **4.49** | 示例为占位值，一律以库内实测为准 |
| 7 | 事实包是否含 LDA 主题 | §15.B 未列 | **不含** | 六部分之外不擅自扩大范围（M2 景点分析另行读取 `topic`） |
| 8 | `stat_spot.sentiment_*` | 口径为 DeepSeek 占比 | **本阶段不回填** | 事实包直接按 `sentiment(method='deepseek')` 现算，避免同一口径两处维护；回填属接口阶段决策 |
| 9 | `sentiment.raw_json` 的内容 | 只写"模型原始返回" | 同时写 `model_raw`（原始文本）、`usage`（token 用量）、`dropped_aspects`（被拦下的方面及原因）、`prompt_version` | 只留校验后的结果将无法事后判断"模型当时编了什么、花了多少 token"，与"便于复核与复现"的字段注释相矛盾 |
| 10 | 事务与断点粒度 | `db.py` 注释为"按批 commit，便于断点续跑" | **实现原先只在正常退出时提交一次**（本轮修正为逐批/逐景点提交） | 注释描述的是设计意图，但代码没做到；不改会导致中途异常把当轮已付费结果整批回滚 |
| 11 | 分层计数口径 | 设计未定义"分层覆盖"检查 | 新增 preflight `[2b]`：三层必须互斥穷尽、且"待处理 = 总量 − 已完成" | 写这组校验时**当场抓出两处口径错误**：① 首版把"既低信息量又是重复组成员"的 2,583 条重复计入两层；② 调用层"已完成"误数了结果表全表（含复制来的行），导致 pending 对不上。两者都会让 preflight **少算工作量**、跑完仍留没结果的评论 |
| 12 | mock 结果的可辨识性 | 设计未涉及 | `raw_json.mode` 标注 `real`/`mock`，且 preflight 检出 mock 行即阻断 | mock 假结果与真实结果在库里几乎一致（含假 token 数），一次 `--mock` 试跑就会污染真实完成数；不标记就无法事后区分 |

## 11. 答辩时重点理解

1. **为什么"事实"与"解释"必须分开**：统计数字由 SQL 产生、模型只负责语言组织，
   这是 `BR-07`「生成内容必须基于给定事实」在工程上的落地方式，也是防止"模型编数"的唯一可靠办法。2. **为什么 evidence 必须是原文子串**：这是"数据负责事实"在**单条评论粒度**的强制校验——
   模型若给出原文里找不到的"证据"，说明它在编造，该方面直接弃用（§15.A.4 第 5 条）。
   **实测 32 条评论中就有 20 个方面被这条规则拦下**，说明该规则不是摆设。
3. **为什么幂等键选在结果表自身**：断点游标等于"结果表里已有什么"，
   不需要额外的状态文件，也不会出现"状态说成功了但数据没写进去"的不一致。
4. **为什么短评优先走规则**：短评（≤10 字）本身信息量不足以支撑方面抽取，
   调模型既浪费又不可靠；规则判定可解释、可复现，且直接省掉 1 万次调用。
5. **成本是可核对的**：每次调用的 token 由 API 返回、累计后写入 `raw_json.usage`，
   金额由单价换算；32 次实测合计约 ¥0.059，全量费用是**按实测外推**得到的。
