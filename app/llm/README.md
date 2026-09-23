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
| `__main__.py` | 统一 CLI（`python -m app.llm --stage ...`）与阶段四自检 |
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

`--only-ids` 的补跑方式（示例）：

```powershell
# 1) 查未解决的失败项
mysql -e "SELECT ref_key, message FROM task_log WHERE level='ERROR' AND stage='semantic' AND resolved=0 LIMIT 20"
# 2) 定向补跑
.\.venv\Scripts\python.exe -m app.llm --stage semantic --only-ids 73603914,73611141
```

## 8. 成本控制（设计 §7.4 / NR-P-07）

| 措施 | 实测条数 |
|---|---|
| 正文 ≤10 字走规则判定，不调模型（`BR-05`） | 10,228 条 |
| 重复正文组内只调一次并复用（`BR-06`） | 4,239 条 / 1,285 组 → 实际只调 **585** 次（另有 700 组全为短评，0 次调用） |
| 正文为空不分析 | 1 条 |
| 结果落库后不重复调用 | 断点游标 |
| **全量调用次数** | **47,733 次**（设计上限 ≤45,000 的口径需按实际分层修正，见 §10） |

成本统计由客户端 `CallStats` 给出：请求数、实际请求次数（含重试）、输入/输出 token、
平均 token、耗时、失败分类；`estimate_cost()` 用配置单价换算金额。

## 9. 小样本验证与全量执行条件

```powershell
# ① 阶段四自检（只读）
.\.venv\Scripts\python.exe -m app.llm --stage check

# ② 只看工作集规模（不调用、不写库）
.\.venv\Scripts\python.exe -m app.llm --stage semantic --dry-run

# ③ 链路联调（mock，零消耗；--limit ≤ 50，否则 CLI 拒绝执行）
.\.venv\Scripts\python.exe -m app.llm --stage all --limit 8 --mock

# ④ 自动验证 29 项（写库→校验→幂等→断点→失败→清理）
#    运行前会检查结果表是否为空；有残留（上次未清理的 mock 数据）会直接拒绝执行
.\.venv\Scripts\python.exe scripts\verify_phase4.py
#    额外做一次全量 mock 压测（5.9 万条，写入后自动清理）
.\.venv\Scripts\python.exe scripts\verify_phase4.py --full-mock-check

# ⑤ 真实小样本（先填 .env 的 APP_DEEPSEEK_API_KEY）
.\.venv\Scripts\python.exe -m app.llm --stage semantic --limit 8

# ⑥ 全量（**必须显式 --yes 确认**，见下；不加 --limit 即全库）
.\.venv\Scripts\python.exe -m app.llm --stage semantic --yes
```

**全量执行的前置条件（四条全部满足才允许）**：
1. `scripts/verify_phase4.py` 29/29 通过；
2. 真实小样本 5–10 条通过，且记录了真实 token/费用；
3. 成本估算已按实测单价复核并汇报；
4. 明确指定"全量"并带 `--yes`（默认不跑全量，避免误触发 4.7 万次调用）。

> `--mock` 使用假客户端，结果会在 `analysis_task.task_name` 中标注 `[mock]`，
> **论文与答辩中不得引用 mock 结果**。
>
> **`--limit` 的作用范围**：它限制的是"需要调用模型"的条数。真实运行时规则层（≤10 字）与复用层
> 不受限制——它们不产生 API 费用，全量跑完反而更省事；`--mock` 联调时三层都会按 `--limit` 收窄，
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

## 11. 答辩时重点理解

1. **为什么"事实"与"解释"必须分开**：统计数字由 SQL 产生、模型只负责语言组织，
   这是 `BR-07`「生成内容必须基于给定事实」在工程上的落地方式，也是防止"模型编数"的唯一可靠办法。
2. **为什么 evidence 必须是原文子串**：这是"数据负责事实"在**单条评论粒度**的强制校验——
   模型若给出原文里找不到的"证据"，说明它在编造，该方面直接弃用（§15.A.4 第 5 条）。
3. **为什么幂等键选在结果表自身**：断点游标等于"结果表里已有什么"，
   不需要额外的状态文件，也不会出现"状态说成功了但数据没写进去"的不一致。
4. **为什么短评优先走规则**：短评（≤10 字）本身信息量不足以支撑方面抽取，
   调模型既浪费又不可靠；规则判定可解释、可复现，且直接省掉 1 万次调用。
5. **成本是可核对的**：每次调用的 token 由 API 返回并累计，金额由单价换算，
   全量费用是**用实测样本外推**得到的，不是估计。
