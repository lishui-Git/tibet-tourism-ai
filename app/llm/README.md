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
| **C-BAT-05 评论语义** | 每次调用的 `usage` 写入 `sentiment.raw_json.usage` | `CallStats` 累计输入/输出 token × 实际单价 |
| **C-BAT-07 景点评价** | 每次调用的 `usage` 拆分写入 `CallStats`（修复轮的两次调用**累加**） | `_estimate_cost()` 按**实际拆分** × 实际单价计算 |

> **本轮修正的一处口径问题**：`spot_report` 原先只把 token **总量**塞进 `CallStats`
> （`usage={"total_tokens": n}`），导致 `prompt_tokens` 恒为 0，
> 费用只能按 **7:3 经验比例**估算。而输入 ¥2/M、输出 ¥8/M 相差 4 倍，
> 比例猜错就会算错钱。现已改为返回**真实用量**（含 prompt/completion 拆分，修复轮累加），
> 费用按 `prompt/1e6×单价_in + completion/1e6×单价_out` 精确计算，
> 结果字段也从 `cost_total_cny_estimated` 改名为 `cost_total_cny`（口径变了，名字要跟着变）。
> 由 `scripts/test_report_usage.py`（11 项）固定：断言拆分保留、费用与手算一致、修复轮累加。

> **同一套兜底也覆盖景点评价（C-BAT-07）**：`run_spot_report` 的写库失败同样会
> "只重试写库 → 落盘待补 → 记 `task_log`(ERROR) 并继续下一个景点"，
> 且**每个景点写成功即提交**。共享实现见 `app/batch/write_recovery.py`，
> 两类组件使用**同一种 JSONL 结构**（`{"text":…, "payload":…}`），因此可以用
> `load_recovery(prefix)` 统一读出后零成本补写。

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

### 8.3 全量成本推算（基于实测）

计算口径：单条 **480 token**（实测均值，区间 431–562）、输入:输出 = **73% : 27%**、
单价 输入 ¥2 / 输出 ¥8 每百万 token → **单条约 ¥0.00174**。

| 项 | 推算 |
|---|---|
| 评论级（C-BAT-05）47,701 次 | 均值口径 **¥74.42**；最坏（562 token/条）**¥97.04** |
| 景点级（C-BAT-06 零调用；C-BAT-07 57 次） | 输入 ≈2,700 + 输出 ≈700 token/次 → **¥0.63** |
| **阶段四全量合计** | **约 ¥75–98** |

> 该区间与 `--stage preflight` 的实时输出完全一致（同一个计算函数），不要与本文件的历史版本混淆。
>
> **数据来源与局限（如实说明）**：
> · 32 次实测中，**最后 8 次**的 token 用量已随结果写入 `sentiment.raw_json.usage`，可仅凭数据库复核；
>   更早的 24 次当时尚未落库 usage，只能引用进程统计（preflight 会把这 24 条作为**提示项**列出，不阻断）。
> · 样本取 `comment_id` 最小的 32 条（`--limit` 的确定性口径），短评比例与全库不同，
>   全量费用应以实际运行后的统计为准；本表用于判断"是否可负担"，不作为结题定稿数字。
> · 真实费用最终以 DeepSeek 账单为准；单价可用 `.env` 的 `APP_DEEPSEEK_PRICE_INPUT/OUTPUT` 覆盖。

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

### 9.1 离线模式（开发/演示期的零消费保证）
```powershell
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
> 2. **preflight 会检出并阻断**：`mock_rows_in_results > 0` 时 `STATUS: BLOCKED`，
>    提示"必须清除后再全量运行"。
> 建议：**不要在正式库上跑会写库的 mock**；要联调就用 `--dry-run`（零写入），
> 或用测试脚本里的 `MockClient`（它们自带快照与清理）。
>
> **`--limit` 的作用范围**：它限制的是"需要调用模型"的条数。真实运行时规则层（≤10 字）与复用层
> 不受限制——它们不产生 API 费用；`--mock` 联调时三层都会按 `--limit` 收窄，
> 避免"只跑 5 条却往库里写了 1 万条规则结果"。

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
   这是 `BR-07`「生成内容必须基于给定事实」在工程上的落地方式，也是防止"模型编数"的唯一可靠办法。
2. **为什么 evidence 必须是原文子串**：这是"数据负责事实"在**单条评论粒度**的强制校验——
   模型若给出原文里找不到的"证据"，说明它在编造，该方面直接弃用（§15.A.4 第 5 条）。
   **实测 32 条评论中就有 20 个方面被这条规则拦下**，说明该规则不是摆设。
3. **为什么幂等键选在结果表自身**：断点游标等于"结果表里已有什么"，
   不需要额外的状态文件，也不会出现"状态说成功了但数据没写进去"的不一致。
4. **为什么短评优先走规则**：短评（≤10 字）本身信息量不足以支撑方面抽取，
   调模型既浪费又不可靠；规则判定可解释、可复现，且直接省掉 1 万次调用。
5. **成本是可核对的**：每次调用的 token 由 API 返回、累计后写入 `raw_json.usage`，
   金额由单价换算；32 次实测合计约 ¥0.059，全量费用是**按实测外推**得到的。
