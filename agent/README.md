# 薪安理得 Agent（画像访谈 + 金融情况 + 本月实况 + 方案生成）

用 Python + LangGraph 实现的后端 agent 逻辑，是薪安理得（工行杯赛题 08「财富管理服务」）的一块。
目前包含四个**独立可跑**的子能力，暂不依赖 Go 服务：

| 子能力 | 入口 | 做什么 |
|---|---|---|
| **画像访谈 Agent** | `ProfileAgent` / `scripts/repl.py` | 多轮对话理解用户 → 结构化财务画像 + 用户确认 |
| **金融情况 Agent** | `FinanceAgent` / `scripts/finance_ctx.py` | 联网取回当前金融环境 → 带来源的结构化摘要 |
| **本月实况 Agent** | `MonthAgent` / `scripts/month_repl.py` | 多轮对话核对本月收支与已有投资收益 → 结构化快照 + 异动归因 + 用户核对 |
| **方案生成 Agent** | `PlanAgent` / `scripts/plan_repl.py` | 整合画像 + 金融情况 + 收支 + 经验包 → 可复算方案（AI 自主调工具）+ 用户确认 / 反馈重算 |
| **总结 Agent** | `SummaryAgent` / `scripts/summary_repl.py` | 复盘已落定月份 → 经验包（喂方案）+ 画像增量（只增不改）+ 月度复盘 + 用户确认 |

## 画像访谈 Agent

目标驱动的**财务画像访谈 Agent**：通过一段自然的中文对话真正了解用户（从职业切入 → 按「方面」推进 →
主动引导不会表述的用户），在模型自己判断"已足够了解"时收尾，产出**结构化财务画像 + 一段自由表述**，
并经用户核对确认。

设计的核心分工（两个子能力共用）：

| 谁 | 管什么 |
|---|---|
| **Graph**（LangGraph） | 流程：多轮循环、状态累积、暂停/恢复（`messages` + checkpointer） |
| **Schema**（Pydantic） | 格式：结构化输出字段及其语义（`Field(description=...)`） |
| **Prompt** | 判断：目标、策略、纪律——这个 agent 的"大脑" |

## 快速开始

```bash
cd agent  # 在仓库根目录执行

# 1) 依赖（用受管 Python 3.13 建虚拟环境）
"C:/Users/xbf/.workbuddy/binaries/python/versions/3.13.12/python.exe" -m venv .venv
./.venv/Scripts/python.exe -m pip install --index-url https://pypi.org/simple -r requirements.txt

# 2) 跑测试
./.venv/Scripts/python.exe -m pytest tests -q

# 3) 多轮演练（不联网，走假模型）
./.venv/Scripts/python.exe scripts/repl.py --mock

# 4) 真连 DeepSeek（需密钥）
cp .env.example .env   # 然后填 DEEPSEEK_API_KEY，或复用 ../.env/deepseek_api.key
./.venv/Scripts/python.exe scripts/repl.py --real
./.venv/Scripts/python.exe scripts/repl.py --real --show-reasoning   # 每轮附模型思维链

# 5) 金融情况 Agent
#    指标表走 AKShare，免密钥；联网检索需要博查 API Key（见 .env.example）
./.venv/Scripts/python.exe scripts/finance_ctx.py --metrics-only     # 只取指标表（约 30 秒）
./.venv/Scripts/python.exe scripts/finance_ctx.py --mock             # 全部假数据演练
./.venv/Scripts/python.exe scripts/finance_ctx.py --real --show-trace
./.venv/Scripts/python.exe scripts/finance_ctx.py --real --json      # 输出原始 JSON

# 6) 账单清洗器（把支付宝 / 微信的原始导出洗成规范文档，类目交给 DeepSeek 判定）
./.venv/Scripts/python.exe scripts/clean_bill.py --alipay 支付宝交易明细.csv --wechat 微信账单.xlsx --out cleaned
./.venv/Scripts/python.exe scripts/clean_bill.py --wechat 微信账单.xlsx --period 2026-09   # 只要一个月
./.venv/Scripts/python.exe scripts/clean_bill.py --wechat 微信账单.xlsx --classifier keyword  # 不调模型（离线）

# 7) 本月实况 Agent（后馈的采集环节：核对这个月的情况）
./.venv/Scripts/python.exe scripts/month_repl.py --mock                        # 假模型走全流程（自带规范文档样例）
./.venv/Scripts/python.exe scripts/month_repl.py --mock --period 2026-10       # 换个期间：自动载入已有历史做环比
./.venv/Scripts/python.exe scripts/month_repl.py --real --bill cleaned/2026-09.json --period 2026-09
./.venv/Scripts/python.exe scripts/month_repl.py --real --bill 手工整理.csv   --period 2026-09  # CSV 兼容入口
./.venv/Scripts/python.exe scripts/month_repl.py --real --show-reasoning

# 8) 方案生成 Agent（整合全部信息 → 可复算方案 + 用户确认 / 反馈重算）
./.venv/Scripts/python.exe scripts/plan_repl.py --mock                         # 假模型走全流程（自带样例输入）
./.venv/Scripts/python.exe scripts/plan_repl.py --real --period 2026-10
./.venv/Scripts/python.exe scripts/plan_repl.py --real --show-reasoning
```

会话内命令：`/confirm` 通过核对（并落档）· `/edit <意见>` · `/more <补充>` · `/why` 看思维链 · `/state` 看快照 · `/probes` 看关注点 · `/history` 看历史留档 · `/quit`。
（方案生成 CLI 另有 `/trace` 看各步轨迹。）

> 注意：本机 pip 默认源（清华镜像）在当前网络下取不到包，安装时请加 `--index-url https://pypi.org/simple`。

## 结构

```
agent/
├── scripts/repl.py                画像访谈的多轮 CLI（--mock / --real / --show-reasoning）
├── scripts/finance_ctx.py         金融情况的一次性 CLI（--mock / --real / --show-trace / --json）
├── scripts/month_repl.py          本月实况的多轮 CLI（--mock / --real / --bill / --period）
├── scripts/plan_repl.py           方案生成的多轮 CLI（--mock / --real / --period）
├── scripts/summary_repl.py        月度复盘的 CLI（--mock / --real / --period / --plan / --no-write）
├── src/moneyrouter_agent/
│   ├── config.py                  Settings（DeepSeek）+ SearchSettings（博查）+ MarketDataSettings（AKShare）
│   ├── domain/money.py            分/元换算与取值范围常量
│   ├── domain/profile.py          ProfileDraft / Profile / ProfileResult
│   ├── domain/turn.py             TurnDecision（converse 的结构化输出）
│   ├── domain/finance.py          指标注册表 / MetricPoint / MonthlyAnalysis / FinanceBriefing + 引用校验
│   ├── domain/month.py            分类体系 / 月度实况快照（收支·去向·投资收益·目标）/ 基线与派生计算
│   ├── domain/probe.py            关注点探测：Probe + 规则注册表 + generate/merge（可插拔）
│   ├── domain/month_turn.py       MonthTurnDecision（本月 converse 的结构化输出）
│   ├── domain/plan.py             方案领域模型 + 七步纯函数（预算/预备金/风险/配置/目标/独立复核）
│   ├── domain/plan_turn.py        PlanTurnDecision / PlanAdjustment（方案生成的模型可写面）
│   ├── domain/experience.py       经验契约：Lesson / ExperiencePack / apply_lessons（软调整）
│   ├── domain/summary.py          复盘领域：PlanActualDiff 差异表 / 候选经验 / 事件草稿 / MonthlySummary
│   ├── domain/profile_delta.py    画像增量：ProfileEvent 事件流（append-only）+ 生效画像叠加
│   ├── summary_store.py           复盘落档：经验包 / 画像事件 / 月度复盘（协议 + JSON + 内存）
│   ├── summary_agent.py           SummaryAgent（summarize / snapshot / 只读口）
│   ├── model/deepseek.py          ChatDeepSeek + 官方 JSON Output + 有界重试 + 思维链捕获
│   ├── tools/bocha.py             博查 Web Search 客户端（官方 API + 并发检索）
│   ├── tools/market_data.py       AKShare 取数层（逐指标降级 + 多窗口分位 + TTL 缓存）
│   ├── tools/bills.py             账单解析：BillParser 协议 + 共享聚合 + CSV 宽表（兼容入口）
│   ├── tools/dossier.py           **规范输入文档**（JSON：流水 / 结余去向 / 投资收益）+ 严格校验
│   ├── tools/bill_cleaner.py      **账单清洗器**：支付宝 CSV / 微信 XLSX → 规范文档（按月切分 + 对账报告）
│   ├── tools/bill_classify.py     类目分类后端：DeepSeek 结构化输出（模型只写类目，不碰金额）
│   ├── tools/plan_tools.py        方案七步工具：纯函数委托 + LangChain @tool 包装（make_plan_tools）
│   ├── prompts/interview.py       INTERVIEW_SYSTEM / FINALIZE_SYSTEM
│   ├── prompts/finance.py         PLAN / REFLECT / ANALYZE 三处提示词 + 渲染函数
│   ├── prompts/rules.py           降级引导文案
│   ├── prompts/month.py           MONTH_SYSTEM / MONTH_FINALIZE_SYSTEM + 渲染函数
│   ├── prompts/month_rules.py     本月实况的降级引导文案
│   ├── prompts/plan.py            方案生成三处提示词（编排 / 判断 / 反馈解析）+ 渲染函数
│   ├── prompts/plan_rules.py      方案生成的降级叙述模板
│   ├── prompts/summary.py         SUMMARY_SYSTEM + 渲染函数
│   ├── prompts/summary_rules.py   复盘的降级文案（要点模板 + 结论兜底）
│   ├── graph/{state,nodes,build}.py           画像访谈图
│   ├── graph/finance_{state,nodes,build}.py   金融情况子图
│   ├── graph/month_{state,nodes,build}.py     本月实况图
│   ├── graph/plan_{state,nodes,build}.py      方案生成图（agent ⇄ tools + 确认/反馈）
│   ├── graph/summary_{state,nodes,build}.py   总结图（collect → diff → reflect → compose → 确认）
│   ├── history.py                 历史留档：MonthRecord + JSON 文件库（落定自动落档 / 自动回喂基线）
│   ├── agent.py                   ProfileAgent（turn / snapshot）
│   ├── finance_agent.py           FinanceAgent（briefing）
│   ├── month_agent.py             MonthAgent（turn / snapshot / parse_bill）
│   ├── plan_agent.py              PlanAgent（plan / snapshot）
│   └── api/{contract,server}.py   集成契约 + FastAPI 骨架（预留）
└── tests/
```

## 画像访谈流程

```
START → converse ──(未收尾)──→ END（等下一句）
             │
             ├──(已足够了解)──→ finalize → confirm(interrupt) ──confirm──→ END(confirmed)
             │                                      └──edit / more──→ converse
             └──(模型不可用/解析失败)──→ fallback(规则引导) → END
```

## 模型接入要点（对齐 DeepSeek 官方文档）

**思考模式默认开启**（官方默认）。开关 `{"thinking": {"type": "enabled"/"disabled"}}` 经 `extra_body` 传入，
力度用官方参数 `reasoning_effort`（`low` / `high` / `max`，本项目默认 `low` 兼顾多轮交互的延迟）。

| 官方规定 | 本项目的做法 |
|---|---|
| 思考模式下 `temperature` 无效 | 该模式**不发送** `temperature`（关闭思考时才发） |
| `top_p` 仅思考模式生效，区间 0.95–1.0 | 可选发送，发送前**自动钳制**到 0.95–1.0 |
| 推理 token 计入 completion token | `max_tokens` 默认 4096，避免 JSON 被截断 |
| JSON Output 要求 prompt 含 "json" 字样 **且给出 JSON 示例** | 由 `format_instruction()` 按 schema **自动派生**「字段清单 + 具体 JSON 示例」，在模型层注入 |
| 思考模式不支持强制 `tool_choice` | 结构化输出**统一走 `json_mode`**，不用 `function_calling` |
| JSON Output 偶发返回空内容 | `include_raw=True` + **有界重试**（默认总尝试 3 次，重试时追加纠正指令） |
| `reasoning_content`：不带 `tools` 时无需回传 | 只做**观测**（`TurnResult.reasoning` / `--show-reasoning` / `/why`），绝不写回 `messages` |

> 字段清单只存在于**模型层**由 schema 生成，`prompts/` 里的提示词保持干净（不掺格式与管道内容）。

## 金融情况 Agent

为方案 Agent 提供**「世界金融情况支撑依据」**。产出两块：一张带全部高价值数据的**指标表**，
和一份**当月情况分析**。

```
START → fetch_metrics → plan → search ──(有材料)──→ reflect ──够用/达上限──→ analyze → compose → END
                                             └──(零材料)──────────────────────→ analyze
                                                      reflect ──不够且有补搜意图──→ search（回环）
```

### 两条输入线互不阻塞

这是这一版最重要的结构——数值与叙述各走各的路：

| 输入线 | 来源 | 负责 | 失败时 |
|---|---|---|---|
| **指标线** | AKShare（结构化，**免密钥**） | "是多少"：带单位/时点/口径的可复算数值 | 单个指标进 `missing`，其余照常 |
| **检索线** | 博查（联网搜索） | "为什么"：政策意图、事件、归因 | 搜不到就少掉"政策与事件"一段，指标表仍完整 |

六个节点里只有 `plan` / `reflect` / `analyze` 调模型；`fetch_metrics` / `search` / `compose` 都是纯代码。

### 指标表

注册表里 **20 个指标**（实测 **20 项全部取到、0 缺口**）：
无风险基准 6 项（中美国债收益率、期限利差、LPR）、通胀 3 项（中/美 CPI、PPI）、
现金类收益 2 项（Shibor）、权益估值 4 项（沪深300 / 上证50 / 中证500 / 中证1000 的市盈率）、
波动与风险 2 项（沪深300 股权风险溢价、中国波指）、避险资产 3 项（上海金、人民币中间价、国际原油）。

- **数值由代码写入，模型没有写入通路** → 指标不可能被编造；
- 每个值都带 **单位 / 时点 / 口径 / 来源**，以及较上期的变化；
- 长历史序列（波指、金价、汇率、原油）附**四个窗口的历史分位**（近 3 年 / 近 5 年 / 近 10 年 / 全历史），
  每个都带**窗口起点与样本数**——同一序列在不同窗口下能算出完全不同的分位，不写清窗口等于不可复现；
- 取不到的指标进 `missing` 并写明原因，**绝不补 0 或估一个值**。

> **权益估值为什么没有分位**：唯一能给长历史的来源是乐咕乐股（legulegu），实测**连打必挂**
> （3 轮：4/4 → 2/4 → 0/4，是站点限流）。整组改用**中证指数公司官方接口**后稳定了，
> 但该接口只返回最近约 20 个交易日——用 20 天样本算出的"近 5 年分位"是误导，**宁可没有**。

### 分析的可追溯性

分析里可以引用**指标标识**与**材料编号**，`compose` 用代码校验两者是否真实存在，
引用不上的直接剔除并计入 `caveats`；`sources` 只列出被实际引用到的材料（均含标题 + 链接）。
提示里只列缺失指标的**名称**，技术性原因留在 `missing[].reason` 供排查。

### 材料的来源分级

检索线专门为**地缘政治、突发事件**做过实测——这类题材最容易灌水：搜"全球军事冲突
哪些地区"会直接召回**虎扑论坛的小说帖**和自媒体标题党。所以材料入库前先判一个级别，
规则集中在 `domain/finance.py::classify_source`，按 URL 与站点名判定：

| 级别 | 谁 | 处理 |
|---|---|---|
| `official` | 政府（`.gov.cn` 按后缀信任）、央行、交易所与指数公司、央媒 | 排最前，可作事实依据 |
| `mainstream` | 主流财经媒体：财新、一财、证券时报、财联社、经观、21 世纪、新浪财经、腾讯/网易新闻频道… | 靠前，可作事实依据 |
| `general` | 其余辨认得出的站点 | 靠后，只作参考线索 |
| `low` | 论坛、问答、自媒体平台，以及正规站点的**自媒体路径**：搜狐号 `/a/`、大风号 `/c/`、网易号 `/dy/article/`、头条号… | **默认剔除**（`SEARCH_DROP_LOW_TIER=false` 可只排序不剔除） |

两条纪律：**只认明确特征**，认不出来一律 `general`（误判成 low 会丢掉真实材料，
误判成 general 只是少一次排序优先，代价不对称）；**全被判成低级别时原样保留**，
不会把整轮打空。模型侧只知道哪几条是官方发布 / 主流媒体，正文里**不照抄级别字样**
（会挤占 400 字额度），引用非官方来源时改写成"据媒体报道"这类转述口径——实测中
模型会主动把未标级别的材料降格为"仅作参考线索，不作为既定事实使用"。

另外，**兜底意图里固定留了一条地缘/事件类**（`DEFAULT_QUERIES`）：规划节点降级退回兜底时，
事件类信息是最不该消失的一项。

### 实测踩到的坑（都已固化为回归测试）

| 坑 | 真相 |
|---|---|
| 数据源排序方向不一致 | `macro_china_cpi` 是**降序**（首行最新），直接取最后一行会把 **2008 年的 CPI 当当期值**报出去 |
| 债券收益率尾部 | `bond_zh_us_rate` 的中国收益率**尾部是 NaN**，必须取最后一个非空值并记它自己的日期 |
| 汇率单位 | `currency_boc_safe` 的美元是**每 100 美元**，必须 ÷100 |
| 中国 CPI 接口 | `macro_china_cpi_yearly` 陈旧到 2025-09，必须用 `macro_china_cpi()` |
| 指数估值源 | `stock_index_pe_lg`（乐咕乐股）**连打必挂**（3 轮 4/4 → 2/4 → 0/4，站点限流）→ 整组改用中证指数公司官方接口 |
| 官方估值接口 | `stock_zh_index_value_csindex` **只给最近约 20 个交易日** → 估值类刻意不给历史分位 |
| 跨源日期对齐 | 各源日期列类型不一致（`date` / `Timestamp` / `str`），直接 merge 会**静默对不上** → 归一化成 `YYYY-MM-DD` 再内连接 |

### 配置与耗时

- **结构化指标**：免密钥，装好依赖即可；参数见 `.env.example` 的 `MARKET_DATA_*`。
- **联网检索**：`BOCHA_API_KEY` → `BOCHA_API_KEY_FILE` → `../.env/bocha_api.key`；
  <https://open.bochaai.com/> 注册可得 1000 次免费额度。
- **取数耗时**：全量约 20 秒（顺序打 14 个数据源），因此内置 **6 小时进程内缓存**
  （`MARKET_DATA_CACHE_TTL_S`）。多用户场景务必保留缓存。
- **源站故障的兜底**：另有一层**落盘缓存**（`MARKET_DATA_CACHE_DIR`，默认 `.cache/market_data`），
  **只在实时取数失败时启用**——只要成功取到过一次，之后源站挂掉仍能出简报。
  此时数据时点由各指标的 `as_of` 如实反映，并在 `caveats` 里注明"使用的是缓存数据"。
  另外，源站级故障（504 / 连接被断）**不会按满额重试**：这类错误几秒内不会自愈，
  满额重试会把一组指标从 5 秒拖到几分钟。

**输出**：`FinanceBriefing{as_of, period, metrics[], missing[], analysis{headline, sections[], implications[], caveats[]}, sources[], degraded}`。

## 本月实况 Agent

后馈流程的**采集环节**：通过一段中文对话把这个月的真实收支问清楚，产出结构化「月度实况快照」
+ 一段第三人称实况结论，并经用户核对落定。本轮只做 **采集 + 核实 + 归因**。

一份快照装下这个月的**全景**并用同一个结构交付：收入、分类支出、结余、**结余去向（投资 / 非投资）**、
**已有投资的收益**、目标达成与近月趋势、净财富变动——下游既能直接出图，也能直接接着做分析。

```
START → ingest（代码：解析账单）→ survey（代码：重算关注点）→ converse（模型）
                                   ├─(未收尾)──→ END（等下一句）
                                   ├─(已够)──→ finalize → confirm(interrupt) ─confirm→ END
                                   └─(降级)──→ fallback（规则）→ END
```

融合了两套既有模式：**画像访谈图**的多轮循环与 `finalize → confirm`，
**金融情况子图**的"代码确定性产数据、模型只读"。

### 输入：规范格式（「最干净的格式」）

**主入口是一份 JSON 文档**（`tools/dossier.py`），各家的账单导出由**转换器**统一转成它之后再进来，
解析侧就只需面对一种形状。`--bill` 指向的文件会**自动识别**：`{` 开头按规范文档，否则按 CSV 宽表（兼容入口）。

```json
{
  "period": "2026-09",
  "cashflow": [
    {"date": "2026-09-01", "direction": "expense", "amount": 88.00, "category": "餐饮", "note": "朋友聚餐"},
    {"date": "2026-09-05", "direction": "income",  "amount": 8000.00, "category": "工资"},
    {"date": "2026-09-25", "direction": "transfer", "amount": 1000.00, "note": "还信用卡"}
  ],
  "savings": {"non_invested": 3249.50, "invested": 0, "note": "留着当应急金"},
  "investments": {
    "has_investments": true,
    "holdings": [
      {"name": "沪深300指数基金", "kind": "基金", "cost": 20000.00,
       "market_value": 20800.00, "month_return": 80.00, "total_return": 800.00}
    ]
  }
}
```

三条关键约定：

- **金额一律用「元」**（可带小数、可带 `¥`/`,`），内部自动换算成「分」；
- **方向由 `direction` 给定，不靠金额正负**——正负号正是各家导出最容易出错的地方。
  `income` / `expense` / `transfer`（别名 `收入` / `支出` / `不计收支`）；
  `transfer` 不计入收支，只计数（`ParsedBill.skipped_rows`），**既不静默丢弃也不当告警**；
- `savings` / `investments` 两段可选，给了就按**文件来源**对待，优先于对话口述。

`tools/dossier.py` 里的 `sample_document()` / `sample_dossier()` 就是一份可直接跑的样例
（`--mock` 默认用它）。

**校验**：解析**永不抛异常**——读不懂的文档、认不出的方向、非正的金额，都如实进 `warnings`
并**带上"第几条"**（便于转换器作者定位），能用的部分照常产出。

### 转换器（账单清洗器）：支付宝 / 微信 → 规范文档

`tools/bill_cleaner.py` + `scripts/clean_bill.py` 把**两家平台的原始导出**直接洗成规范文档：

```bash
./.venv/Scripts/python.exe scripts/clean_bill.py \
    --alipay "支付宝交易明细.csv" \
    --wechat "微信支付账单流水文件.xlsx" \
    --out cleaned
```

- **来源与形状**：支付宝「交易明细」CSV（GBK，带前言与回单尾注）、微信「账单流水文件」XLSX
  （前 17 行是元信息，第 18 行表头）；**就这两个**，银行 / 券商导出暂不支持。
- **按月切分**：规范文档是单期间形状，所以按自然月各出一份 `cleaned/<YYYY-MM>.json`
  （混月喂进去只会被统计其中一个月）；`--period 2026-09` 可以只要一个月。
- **方向只认平台的「收/支」列**（`支出/收入/不计收支`；微信的中性交易是 `/`），金额一律取正数；
  两家的**退款口径不一致**（微信记成收入、支付宝记为不计收支）。CLI 与导入库默认 `net`：
  退款不算收入，可靠关联的退款冲减原消费月份；全额退款状态可直接归零原消费。无法唯一关联或超额退款需复核，
  不猜原单。`raw_cashflow` 保留原始流水；`keep` 保留平台口径，`transfer` 为兼容排除选项。
  底层 `clean()` 为兼容既有调用仍默认 `keep`，调用方可显式传 `refund_policy="net"`。
- **类目由 DeepSeek 判定**（`tools/bill_classify.py` + `prompts/classify.py`）：只有商家 / 商品 /
  平台类目这类**文字依据**进模型，出参只有「编号 → 类目」，金额、方向、日期一概不经过模型；
  返回值一律过 `domain/month.normalize_category` 收口，判定不了就落「其他」。
  关键词表（`TEXT_CATEGORY_KEYWORDS`）退居**兜底与参考**：模型不可用时按它分类，可用时把它的结论
  当参考交给模型复核。`--classifier keyword` 可以完全离线跑。
- **分类默认关闭思考模式**，温度为 0；相同来源、商家、商品、平台类目与交易类型的文字依据在一次清洗中复用判定，
  日期和金额仍逐行保留。`--thinking` 仅供显式开启。不明用途的转账、红包、群收款、二维码付款落「其他」，
  不直接当成人情往来；人情往来需明确礼金、随礼等用途。规则不使用单字关键词。
- **重复导入与隔离**：CLI 默认使用 `.data/bill-imports.sqlite`；`--user` 指定用户，`--ledger` 指定库。
  相同交易幂等导入，重叠文件去重，后到退款可修正前月净额，冲突上传整体回滚；重新导入保留人工分类。
  多用户服务必须传真实用户标识。`--report-only` 不写导入库。
- **月份覆盖**：从导出起止日期判断完整月份，保留覆盖区间、复核数量与核算口径；明确不完整的月份不进入历史月均基线。
- **可复核与纠正**：各流水保留来源、源行号、交易编号和分类来源；`review.json` 收集未知用途及退款相关记录。
  `--category-overrides corrections.json` 接收人工分类，例如 `{"wechat:交易编号": "居住"}`，
  人工结果优先于模型。无交易编号时使用 `wechat:row:源行号`（支付宝同理）。
- **上传字节流**：`clean(alipay=csv_bytes, wechat=xlsx_bytes)` 可直接接收文件内容，微信字节流同样读取平台汇总。
  `--period` 在模型分类前筛选，避免为未选月份付费；未知类目显式写「其他」，避免下游重新猜测。
- **自检**：报告会把明细累加与**回单自带汇总**逐档对账，并列出「模型补空 / 改判」的每一笔供人复核；
  `--no-verify` 之外默认再用 `tools/dossier.parse_bill` **回读**一遍产出，确认下游真的接得住。

> 已知差异：支付宝回单声称支出 123 笔 4207.31 元，而明细里支出行累加是 4465.91 元
> （差 258.60 元）——回单自己在特别提示第 6 条声明了「明细直接累加可能与统计金额不一致」，
> 清洗器如实报出这处不一致，不替它对账。此外明细里有一笔 0.00 元的支出（全额优惠券），
> 这笔零金额流水现在保留用于笔数对账，金额合计不变；负数、无效日期、列数错位记录跳过并告警。

真实文件验收入口（输出目录须为新目录；只打印汇总，不打印个人交易明细）：

```powershell
./.venv/Scripts/python.exe scripts/validate_bill_cleaner.py --real --alipay "支付宝交易明细.csv" --wechat "微信账单.xlsx" --output .data/bill-validation/new-run
```

验收独立读取原始列，检查方向、金额、交易编号不被模型修改，并核对每个月的下游统计；
平台汇总与明细差异保留在报告中，分类的语义准确性仍需结合用途人工复核。

完整后端连续验收可使用 `scripts/full_flow_smoke.py --real --output 新目录`。
加 `--alipay CSV路径 --wechat XLSX路径` 时，会重新分类真实账单并另测八月、九月的月度确认留档；
真实账单与构造用户使用隔离历史。该扩展会向 DeepSeek 发送清洗后的月度金额、分类和备注，
不同于分类器只发送文字用途；运行前应确保用户已授权该范围。阶段耗时和结果写入 `report.json`。

### 数据来源 = 混合

规范文档（或 CSV）解析出的事实标 `source="file"`；缺的靠对话补 `source="stated"`。
合并时**文件来源不被口述覆盖**：分类、收入、结余去向、投资收益都是这样，
计算字段（合计、结余、基线、目标达成、收益合计）一律由代码重算——模型没有写入通路。
于是**账单已经给了的，agent 不再重复问**，只问账单解释不了的地方（异动的原因、目标进度等）。

### 关注点：通用、可扩展的"值得追问"机制

这是本模块的核心。`domain/probe.py` 用**装饰器规则注册表**生成关注点：每条规则是一个纯函数
`(ProbeContext) -> list[Probe]`，**新增一类追问 = 加一个函数**，不改主流程。内置示例规则：
超预算 / 超上月 / 超近三月均值 / 一次性大额 / 缺报项 / 支出超收入 / 目标进度落后 /
投资收益未了解 / 投资明细未了解 / 某笔投资本月明显亏损。

- 关注点由**代码**算好，交给对话去问用户为什么；**模型不得编造**，但"先问哪个、怎么问"由模型定。
- `id = f"{kind}:{scope}"` 稳定去重；`merge_probes` 保证**已解答的不再重复问**、失效的置为 `dismissed`。

### 可视化就绪

结构刻意做成"可视化就绪"：分类占比、预算执行率、环比/近月趋势、**结余去向（投资 / 非投资）**、
**已有投资收益与走势**、**目标达成进度**、净财富变动、异常标注与归因都能从 `MonthSnapshot`
直接取到，供下游（前端 ECharts）出图。本模块**只保证数据，不画图**。

| 想画的图 | 直接取 |
|---|---|
| 本月收支与结余 | `income` / `spend_total_cents` / `balance_cents` |
| 分类占比、预算执行率 | `categories[]` / `baselines[metric=budget]` |
| 环比、近三月趋势 | `baselines[metric=last_month\|trailing_3m_avg]` / `trailing[]` |
| 结余去向（投资 / 非投资） | `allocation` |
| 已有投资收益（本月 / 累计） | `investments.holdings[]` / `investments.month_return_cents` / `total_return_cents` |
| 收益走势 | `trailing[].investment_return_cents` |
| 净财富变动 | `net_worth_change_cents`（结余 + 本月投资盈亏） |
| 目标达成进度 | `goal_alignment` |
| 异常标注与归因 | `probes[]`（`status` / `answer` / `related`） |

### 结余去向：非投资的资金也要采集

不是所有结余都会拿去投资——应急金、活期、保守储蓄这类**不承担市场风险**的资金同样要如实掌握。
`MonthSnapshot.allocation`（`FundAllocation`）记录本月结余里「不投资的」与「投出去的」各占多少，
并算出差额 `unallocated_cents`：对不上就生成 `inconsistent:allocation` 关注点、去向未说明就生成
`missing_field:allocation`，交给对话问清楚。

### 已有投资的收益：和收支一起采集

只看"这个月新投了多少"是不够的——**已经投出去的那部分赚赔**，同样是这个月的实况。
`MonthSnapshot.investments`（`InvestmentSnapshot`）逐笔记录：本金、当前市值、本月盈亏、累计盈亏。
单笔满足 `市值 = 成本 + 累计收益`，**给到任意两项，第三项由代码推出来**；收益率算不出就留空。
合计只在**每笔都有值**时才求和（不部分求和、不补 0）。

- `has_investments` 是**用户才能回答的事实**：`false`（明确没有投资）是一个有效结论，不会被当成"未了解到"，
  也不会反复追问；`None` 才生成 `missing_field:investments`。
- 某笔本月亏损超过 `MONTH_INVEST_LOSS_MIN_CENTS`（默认 1000 元）→ 生成 `investment_loss:<产品>` 关注点，
  由对话去问原因，用户的解释写回 `answer`。
- 净财富变动 `net_worth_change_cents = 结余 + 本月投资盈亏`（两项缺一即留空），把「攒下多少」与
  「投资赚赔多少」合成一个可直接呈现的总额。

### 历史留档：落定之后，这个月就变成历史

采集环节产出的只是"这一个月"，可后馈真正要看的是**多月对比**——环比上月、近三月均值、趋势、
攒钱速度，都得靠历史。而 LangGraph 的检查点重启即丢，所以落定的那一版必须落到更持久的地方。

`moneyrouter_agent/history.py` 负责这件事，默认**一个期间一个 JSON 文件**：`.data/months/<YYYY-MM>.json`。

- **落定才留档**：只有用户核对通过（`confirmed`）的那一版才写；过程中间态不算数。
- **按期间幂等**：同一个月重复落定 = 覆盖（先写 `.tmp` 再 `replace`，避免读到半个文件）。
- **留档内容**：`MonthRecord{ period, recorded_at, snapshot, articulation, probes }`——
  除了快照和结论，**关注点与归因也一并留下**（"为什么偏高、为什么亏"正是复盘的料）。
- **自动回喂**：下次跑更晚的月份时，门面会**自动把更早的月份装成基线**（`MonthAgent` 构造时读、
  每轮按当前期间重装），不再需要外部手动注入 `history`。同期间会被排除，不会拿本月跟自己比。
- **累计已攒**：`goal_alignment.saved_to_date_cents` 由历史各月结余累加而来——所以落档之后，
  目标进度才真正开始"往前走"。
- **读要皮实、写要出声**：批量读遇到坏文件**跳过并如实报告**（`history_warnings`）；
  点名读（`load_record`）失败则抛 `HistoryError`，**不把"读不出来"伪装成"没有记录"**；
  写失败只降级成 `error` + `recorded=False`，不打断会话。

```bash
./.venv/Scripts/python.exe scripts/month_repl.py --mock --period 2026-09   # 落定 → 写入 .data/months/2026-09.json
./.venv/Scripts/python.exe scripts/month_repl.py --mock --period 2026-10   # 自动载入 09 做环比基线
```

会话内 `/history` 可以看已有月份与结论；`--no-history` 可整场关闭读与写；
`MONTH_HISTORY_DIR`（留空＝关闭）/ `MONTH_HISTORY_LIMIT`（回看几个月，默认 3）可配置。

> 测试隔离：真实留档会被下个月当基线用，所以测试造的假月份绝不能写进真实目录——
> `tests/conftest.py` 把 `history.AGENT_ROOT` 一并重定向到 `tmp_path`，并有守卫测试锁住。

### 输出

`MonthResult{ snapshot: MonthSnapshot（含 allocation / investments / goal_alignment / net_worth_change_cents）, articulation }`
+ 过程中的 `probes`（含归因）。落定后另存一份 `MonthRecord` 到历史留档。
`MonthAgent` 支持可选注入 `budget`（来自方案模块）/ `goal`（来自画像模块）与显式 `history`；
未注入的规则不触发，缺的数据留空、图表显示"暂无"，**不补 0、不估算**。

## 方案生成 Agent

把前三个子能力的产物**整合成一份可复算的月度资金安排**：先保障生活、再补足应急预备金、
最后在可承受风险内做适当增值；给出后**先由用户确认**，不同意就接受反馈重算。

```
START → ingest（代码：现金流）→ agent（模型：自主调工具）⇄ tools（ToolNode）
      → decide（模型：追问 or 出方案）
          ├─(追问)──→ END（等下一句）
          └─(出方案)→ compose（代码：跑完七步）→ validate（代码：独立复核）──ok──→ confirm
                                                                   └─fail──→ fallback → confirm
      confirm ──confirm──→ END
              ├─edit──────→ adjust（模型：反馈→结构化调整）→ compose（重算 + 复核 → 回 confirm）
              └─more──────→ agent（带补充信息重回工具循环）
```

### 输入：整合全部信息（外部注入，不进图状态）

`PlanInputs` 一次装下：用户画像 `Profile`、金融情况 `FinanceBriefing`（当月新闻/指标/来源）、
本月实况 `MonthSnapshot`（+ 历史月份），以及**经验包 `ExperiencePack`（可选，来自月底后馈）**。
这些对象由门面（或服务端）在**进程内**注入，闭包捕获，不进检查点。

### AI 自主编排 + 代码独立复核

- **工具是唯一数值来源**：七步各对应一个 LangChain 工具（`summarize_cashflow` / `allocate_budget` /
  `calculate_reserve` / `assess_risk` / `propose_allocation` / `find_sources` / `check_goal`），
  入参只有少量 enum/数值旋钮，**模型无法注入金额**；工具内核是 `domain/plan.py` 的纯函数。
- **compose 永远由代码重算**：无论模型调不调工具，规范方案的金额都由代码按输入 + 策略旋钮算出；
  模型负责 `headline`、策略旋钮与「追问还是出方案」的判断；最终 `narrative` 与回复由代码按最终金额生成。
- **validate 独立复核**（`validate_plan`）：按方案自身策略重算逐项对账，检查金额非负、预算不超收入、
  三类配置平衡、增长 ≤ 风险上限、预备金补足 + 可投资 = 可储蓄、风险档不高于三档最保守值；
  数据不足（无完整月份）直接判错 → 走 `fallback`（**代码可否决模型的收尾**）。

### 七步算法（对齐已批准的 WORKPLAN §4.1/§4.2，金额一律「分」）

1. **数据核验**：月均收入/必要/可选/债务 + `DataQuality`（月数、是否与前冲突、可信度）。
2. **预算基线**：`可用 = 收入 − 必要 − 债务`；`可选 = min(实际, 可用, 收入×30%)`；`可储蓄 = max(0, 可用 − 可选)`；
   负结余 → 不安排新增投资。
3. **预备金**：收入稳定且无家庭负担取 **3** 个月，否则 **6** 个月；`月度补足 = min(可储蓄, 缺口)`；
   `可投资 = max(0, 可储蓄 − 月度补足)`；高息债务（月供/收入 > 30%）单独提醒。
4. **风险约束**：能力 / 意愿 / 期限各评一档，**取最保守**；增长类上限 低/中/高 = **0/20/40%**。
5. **候选配置**：对可投资余额在 保守储蓄 / 稳健配置 / 增长配置 三类分配。
6. **目标检验**：假设情景加权年化（保守 2.0% / 稳健 3.5% / 增长 6.0%）− 成本 0.5% 对比「>3%」——
   明确**是情景演算，不是收益预测**。
7. **独立复核**：见上。

### 用户确认与反馈

`confirm` 用 `interrupt` 挂起，交 `{plan, validation}` 供核对：
`confirm` 通过 · `edit` 把自由文本意见交模型解析成结构化调整（某类支出上限比例 / 风险档 / 预备金月数 /
目标·期限·可承受亏损变化）后**重算并复核**再确认 · `more` 补充信息回到工具循环。

### 经验积累（契约 + 注入点 + **产生器见「总结 Agent」**）

`ExperiencePack` 是一组 `Lesson`（`wants_down` / `risk_conservative` / `reserve_priority` / `debt_priority` /
`goal_pace`），`apply_lessons` 折算成**软调整**（下调可选上限、风险档保守化、结余全留预备金…），
在 `build_plan` 内、`validate` 之前应用，**绝不突破硬约束**。
产生路径（月底后馈 → 经验包）由 **总结 Agent** 负责，见下一节。

### 与 DeepSeek 思考模式的关系（重要）

工具循环**默认关闭思考**（`PLAN_TOOLS_THINKING=false`）：思考模式 + tools 时 `reasoning_content`
必须回传，而 `langchain_deepseek` 只把它存进 `additional_kwargs`、不回传 → 第二次调用会 400。
判段/反馈解析不带 tools，仍可用思考模式。且**从不强制 `tool_choice`**（思考模式不支持）。

## 总结 Agent

后馈的第二块：**把一个已落定的月份变成"下个月该怎么调"**。对应 PRD 的
「调整分配的模型算法：agent 总结经验，更新用户画像」与「总结当月消费情况：提供结果反馈」。

**产出三样，各有明确消费方**——这是本模块的设计骨架：

| 产物 | 消费方 | 契约 |
|---|---|---|
| `ExperiencePack`（经验） | **方案生成** `PlanInputs.experience` | ✅ 既有，本模块补上**产生器** |
| `ProfileDelta`（画像增量） | 画像 / 下游读 `EffectiveProfile` | 本模块新建 |
| `MonthlySummary`（月度复盘） | 用户（PRD 的"结果反馈"） | 本模块新建 |

```
START → collect(代码) → diff(代码) → reflect(模型) ─(降级)→ fallback(规则) ┐
                                         └─(正常)──────────────────────┴→ compose(代码) → confirm(interrupt)
                                                                                       ├confirm→ END（**门面**写盘）
                                                                                       └edit───→ reflect
```

沿用「代码算、模型判」：`diff` 算差异表、`compose` 合成产物且**模型没有写数字的通路**；
模型只在 `reflect` 里写叙述、给候选填结论、抽画像变化。**`save` 不设节点**——门面在用户确认后写三份，
图因此保持无副作用、恢复重跑也幂等。

### 差异表：只比同口径的东西

`PlanActualDiff` 的 `layers` 只放**真·同口径**的对照：`income / necessary / wants / savings`
（+ 仅有计划的 `growth`）。预备金与可投资余额的"计划"（月度补足、可投资余额）与"实际"
（本月净留存、新投入）含义不同，所以**不做偏差**，只作为事实单列并在 `notes` 里说明——
不把两个口径不同的数字相减。分类偏差优先用方案的分类上限，其次复用快照里已算好的预算基线；
目标进度直接复用 `goal_alignment`，**不重造**。

### 经验产生器：三处刻意的取舍

1. **候选项由代码产，模型只填结论**。哪几类值得沉淀、强度多少，都由差异幅度决定：
   `strength = clamp(0.4 + 0.6 × 超支比例)`，模型不碰这个数。
2. **经验包按 `kind` 归并，不是按 id 累积**。`apply_lessons` 对 `wants_down` 是
   `scale *= 1 − 0.6×strength`——按 id 累积跨两个月就会叠成 `0.4 × 0.4 = 0.16` 的极端值。
   归并后每类只留 `from_period` 最新的一条（回归测试锁住）。
3. **`custom` 类永不产出**。它在 `apply_lessons` 里不改任何算法，却会被记进 `Plan.lessons_applied`
   并计进来源标签「经验积累（N 条）」——属于误导性泄漏。分类级影响只留在画像事件的 `effects` 里
   （`category_cap` 等），**首版只描述、不接算法**。

### 画像增量：只增不改

PRD 的「更新用户画像」在这里等于**事件流**：用户确认过的那版 `Profile` 永远不动，
新情况以**事件**追加。于是「伤好了」不是去改「受伤」那条，而是**再追加一条关闭事件**：

```
追加 { id: "life_event:2026-09",  statement: "受伤，出行不便", effects: [category_cap(交通, up)] }
追加 { id: "close:life_event:2026-09", event_type: "event_ended", closes: "life_event:2026-09" }
        ↓ 读时派生（不写回磁盘）
life_event:2026-09 → status = ended，ended_period = "2026-11"
```

- **`status` / `ended_period` 永不落盘**，全部由 `event_status` / `event_ended_period` 读时算；
- **重复追加同一 id 是 no-op**（返回原条目）——这就是"重跑同一个月幂等"的实现；
- 事件的 **id 与发生月份由代码生成**（`{event_type}:{月份}`），模型只说"发生了什么"，
  不排号、不改日期；类型不在白名单、关闭目标不存在，都会被丢弃并**说明原因**；
- 字段变化（换工作 → 收入变了）以 `field_updates` 承载，派生 `EffectiveProfile{base, updates, applied_event_ids}`。
  **`base` 一字未改**，下游显式 `merged()` 取投影。
  ⚠️ `EffectiveProfile` **严禁走画像访谈图的 `_fill_missing`**——那边是"缺就补旧"，这边是"显式设值"，
  方向相反，串起来会让"已结束"的事件静默复活；
- `Profile.debt_cents`（负债总额）≠ 方案需要的 `debt_payment_cents`（月供），这段缺口**尚未打通**。

### 落盘：三份东西的写语义不一样

| 内容 | 位置 | 语义 |
|---|---|---|
| 经验包 | `.data/experience/<用户>.json` | **版本化覆盖**（每次汇入 version+1、按 kind 归并） |
| 画像事件 | `.data/profile_events/<用户>.json` | **append-only**（已有 id no-op，原文永不改写） |
| 月度复盘 | `.data/summaries/<用户>/<YYYY-MM>.json` | **按期间覆盖**（可重算的派生件） |

统一纪律照 `history.py`：原子写（`.tmp` → `replace`）、**点名读失败抛 `SummaryStoreError`
/ 批量读跳过坏文件并如实报告**、用户标识做路径消毒。目录由 `SUMMARY_*` 配置，任一留空 = 关闭该留档。

### 怎么跑

```bash
# 先有"本月实况"留档（上一节），再复盘
./.venv/Scripts/python.exe scripts/month_repl.py --mock                    # 落定一个月
./.venv/Scripts/python.exe scripts/summary_repl.py --mock                  # 复盘它（默认取最近月份）
./.venv/Scripts/python.exe scripts/summary_repl.py --real --plan 方案.json  # 真连模型 + 注入上一版方案
./.venv/Scripts/python.exe scripts/summary_repl.py --mock --no-write       # 只演流程
```

会话内命令：`/confirm`（确认并落盘）· `/edit <意见>` · `/show`（差异表）· `/pack`（经验包）·
`/events`（画像事件与读时状态）· `/summary` · `/why` · `/quit`。

> 测试隔离：三份落档都会被下个月读到，测试造的假数据绝不能落进真实目录——
> `tests/conftest.py` 把 `summary_store.AGENT_ROOT` 一并重定向到 `tmp_path`，并有守卫测试锁住。

## 与 Go 侧对接（预留）

四个稳定入口，契约都在 `api/contract.py`：

- `ProfileAgent.turn(thread_id, user_message, *, resume=None) -> TurnResult`；
  将来经 `POST /profile/turn`（或子进程 JSONL）暴露。
- `FinanceAgent.briefing(request, *, as_of=None, period=None, focus=None) -> FinanceBriefingResult`；
  将来经 `POST /finance/briefing` 暴露。它是**一次性任务**，无多轮、无确认环节。
- `MonthAgent.turn(thread_id, user_message, *, resume=None, bill=None, period=None) -> MonthTurnResult`；
  将来经 `POST /month/turn` 暴露。多轮 + 核对环节，首轮可带账单与期间；
  返回里的 `recorded` / `history_periods` / `history_location` / `history_warnings` 反映历史留档情况。
- `PlanAgent.plan(thread_id, *, inputs=None, user_message="", resume=None) -> PlanResult`；
  将来经 `POST /plan/plan` 暴露。多轮 + 确认/反馈；**输入（画像/简报/账单/经验包）由服务端在进程内注入**，
  不走 HTTP；`resume` 承载 confirm/edit/more。
- `SummaryAgent.summarize(thread_id, *, period, plan=None, resume=None) -> SummaryResult`；
  将来经 `POST /summary/run` 暴露。一次性任务 + 核对；`plan` 由方案模块注入；
  返回里的 `pack` / `profile_delta` / `summary` 就是三份产物的形状。

各入口的 `reasoning` / `trace` 字段仅供本地观测与调优，对接时可不下发。

## 已知边界

- 完成与否**完全由模型判断**，代码不设必需字段门槛；`rationale` 仅用于观测调优。
- `max_loss_pct=0`（完全不接受亏损）是合法值，不能用 0 表示"未回答"。
- 无密钥 / 调用失败 / 解析失败时走规则降级，保证"没有 AI 也能用"。
- 思考模式可用 `DEEPSEEK_THINKING=disabled` 应急关闭（会牺牲推理质量）。
- 四类对话默认用 SQLite checkpointer，分别存于 `.data/checkpoints/`，可用 `MONEYROUTER_CHECKPOINT_DIR` 指定目录；设为 `:memory:` 可临时关闭落盘。重启后沿用相同 `thread_id` 即可恢复，预算、方案输入及复盘上下文同步持久化。
- 检索侧目前**没有跨请求缓存**：每次调用都会真发检索请求。上多用户前建议按"主题 + 日期"加短 TTL 缓存。
- 指标侧的缓存是**进程内**的，多进程部署时会各存一份；届时需要换成 Redis 或落库。
  落盘兜底缓存（`.cache/`）是**单机共享**的，多实例部署时要注意各实例的目录不要互相覆盖。
- AKShare 接口来自公开页面抓取，**偶发失败是常态**。已按"宁缺毋滥"取舍过一轮：
  乐咕乐股（连打会被限流）、货币基金 7 日年化（数据源给无效值）、东财全球指数（VIX / 美元指数）
  都已**从指标表移除**，权益 PE 改用中证指数公司官方接口；保留的 20 个指标实测 **0 缺口**。
- 检索侧偶发 **429 限流**（实测 6 条并发里有 1 条被打回）：单条失败不影响其余，
  但若频繁出现，应降低 `BOCHA_MAX_PARALLEL` 或加退避重试。
- 取数目前是**顺序**执行的，全量约 20 秒。若将来对首屏延迟敏感，
  可改为并发取数并预热缓存——注意 1 vCPU 上并发收益有限，且需要处理共享缓存的线程安全。
- 来源分级是**基于域名与站点名**的启发式判断，判的是"这个站是谁"，不是"这段内容对不对"。
  因此 `news.qq.com/rain` 这类正规频道与企鹅号混排的站点会算主流媒体，地方党报转载的
  耸动标题（如"未来七年可能爆发大战"）会算 `general` 而非 `low`。**宁可漏判、不误杀**是
  刻意的取舍：漏掉一条劣质材料只是少一点噪声，误杀一条真实材料则是信息损失。

本月实况 Agent 的边界：

- **关注点不写死**：规则可增删（装饰器注册），阈值集中在 `config.MonthSettings`（`MONTH_*` 可覆盖）。
- `over_budget` / `goal_off_track` 依赖"方案预算 / 画像目标"，Python 侧尚未落地 → 首版按**可选注入**处理，
  未注入则不触发；`history`（近月趋势）与 `goal` 同理，缺就留空、图表显示"暂无"，**不补 0**。
- **输入的规范格式已定**（`tools/dossier.py`），**转换器已覆盖支付宝 / 微信两家**（`tools/bill_cleaner.py`）：
  原始导出有前言、金额恒为正、方向在单独的「收/支」列，直接喂 CSV 入口会读空——现在先过清洗器即可。
  银行 / 券商导出**暂不支持**，那类请按规范文档整理，或用结构相近的 CSV 宽表。
- 规范文档目前覆盖 **本月流水 + 结余去向 + 已有投资收益**；历史月 / 预算 / 目标仍走**可选注入**
  （清洗器也不产出这两类数据，它们是「方案」与「画像」模块的产物）。**历史月现在可以不注入**——
  落档之后由 `history.py` 自动回喂。
- 历史留档是 **JSON 文件库**（单机、按月一个文件，人可读、便于迁移）。多实例部署时要么共享同一
  目录、要么换成数据库实现——`MonthHistoryStore` 是协议，替换不动调用方。
- 落档里存的是**落定时的完整快照（含派生字段）**，读取后直接当历史用；跨版本口径变更时应重跑或
  手工修档，代码不会在读取时静默重算。
- 与画像 Agent 一致：对话与核对状态默认 SQLite 持久化；落定月份另行留档。无模型时走规则降级并**永不收尾**。

方案生成 Agent 的边界：

- **工具循环默认关思考**（`PLAN_TOOLS_THINKING=false`）：这是为避开"思考模式 + tools 时 reasoning_content
  必须回传、而客户端不回传会 400"的硬约束；判段/反馈解析仍走默认思考模式（不带 tools）。
- **模型不写任何金额**：`compose`/`validate` 由代码跑完七步，模型只写 `headline` 与
  「追问还是出方案」的判断；模型通过少量**策略旋钮**（可选上限比例 / 风险档 / 预备金月数）参与，
  一律被代码钳到合法区间（风险档只会更保守）。
- 最终方案的金额说明与回复由 `render_plan_narrative` 按重算结果生成，避免模型复述工具旧值。
  `wants_ratio_pct` 明确是应用经验前的基础上限，工具里的已生效比例不能再当基础值回填。
- **经验包的产生器见「总结 Agent」**：`ExperiencePack` / `Lesson` / `apply_lessons` 与产生器都已就绪；
  `PlanInputs.experience` 不注入则不产生任何软调整。
- 输入（画像 / 金融简报 / 账单快照 / 历史月份）**不进图状态**，由门面闭包注入；多用户部署时
  门面需按会话装载当次输入。
- 债务口径：画像的 `debt_cents` 是**负债总额**，方案需要的是**每月最低还款** →
  暂由 `PlanInputs.debt_payment_cents` 显式给出；未给则暂按 0 计并在 `warnings` 标注（**不静默**）。
- 必要/可选分类归属：必要 = 居住 / 餐饮 / 交通 / 医疗健康 / 学习成长；其余为可选。口径如需调整，
  改 `domain/plan.py` 的常量即可。
- 场景演算的假设年化（2.0% / 3.5% / 6.0%）与 0.5% 成本是**算法常量**，不是收益预测。
- 对话及方案输入默认 SQLite 持久化；无模型时走确定性降级，**仅复核通过的方案可确认**。数据不足或金额校验失败时提示补充资料。

总结 Agent 的边界：

- **不含方案生成**：复盘只产出经验、画像增量与叙述；"下个月具体怎么分"仍由方案生成负责。
- **`PlanActualDiff` 只比同口径**：预备金与可投资余额的"计划/实际"含义不同，只作为事实列出、
  不做偏差；分类级对照只在方案给了分类上限、或注入了预算基线时才成立。
- **分类级影响尚未接算法**：画像事件里的 `effects`（如 `category_cap`）已定契约，但
  `ExperienceEffects` 只有 5 个旋钮，且方案的 `strategy.category_cap_cents` **只对可选类生效**
  （`交通` / `餐饮` / `居住` 属必要类，不认）。要真正落地得改方案侧，本轮不动。
- **画像事件的抽取依赖模型**：代码只负责排号、校验类型白名单、定日期、填依据；
  "本月是否真的发生了这件事"由模型从该月实况与归因里判断——与本月实况 Agent 同一套信任边界。
- **经验复核已修复（2026-10-07）**：按方案内已经生效的策略独立重算，不再次叠加经验；
  回归测试要求复核完全通过，并验证修改金额仍会被拒绝。
- **月份与零值**：异月方案不参与本月偏差和经验计算；预算为 0 是已知值，仍计算偏差。
- **实况到复盘的事实传递**：复盘模型同时收到已确认实况结论、额外事实与关注点归因，
  避免职业或收入稳定性变化未落在消费关注点里时被遗漏。
- 事件类型的全部白名单由 schema 描述传给模型；被拒绝的画像事件通过 `event_warnings` 返回。
- 持久化：对话及复盘上下文默认 SQLite 持久化，**三份产物另行落盘**（`.data/` 下，可配置关闭）。

## 五个 Agent 完整联动测试

从画像开始，顺次运行金融简报、八月实况、九月方案（含反馈修改）、九月实况、复盘、十月方案。
所有模块通过实际门面与 LangGraph 图运行；复盘保存后新建读取门面，用落盘的经验和画像事件生成下月方案。

```powershell
# 可重复的离线模型/数据测试（仍运行真实图与文件库）
./.venv/Scripts/python.exe scripts/full_flow_smoke.py --output .data/smoke/offline-1
# 真实模型、博查检索和金融数据；需要已有密钥，每次指定新目录
./.venv/Scripts/python.exe scripts/full_flow_smoke.py --real --output .data/smoke/real-1
# 全量回归
New-Item -ItemType Directory -Path .pytest_tmp -Force | Out-Null
./.venv/Scripts/python.exe -m pytest -q --basetemp=.pytest_tmp/regression
```

脚本每步保存 JSON 结果和 `report.json`，失败立即非零退出；限定问答轮数。
测试模拟用户离职后收入不稳、可选支出超预算，要求下一期经验生效、预备金目标转为六个月、金额复核通过。
输出目录必须不存在，且行情缓存、月份、经验、画像事件与复盘均隔离于该目录；不读取真实用户留档。
这验证 Python Agent 链路，Go Web 对接仍需单独完成。

结构化模型调用遇到 `LengthFinishReasonError` 时，在原有尝试次数内将输出预算加倍，
默认从 4096 到 8192，再到最高 16384；普通空 JSON/网络错误不会提高预算。
重试使用复制的模型客户端，不修改共享配置；仍失败则按原有规则降级。

如果只修改后馈链路，可读取完整测试已经确认的上游产物继续跑真实模型：

```powershell
./.venv/Scripts/python.exe scripts/feedback_smoke.py --source .data/smoke/real-1 --output .data/smoke/feedback-1
```

此脚本校验上游确认状态，不模拟模型和账单；新生成的复盘、经验、画像事件、下月方案另存到新目录。
同时保存复盘模型的结构化草稿，便于区分「模型未提取」和「代码拒绝了事件」。
