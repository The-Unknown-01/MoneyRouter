# agent_update 融合实现约定

当前生产路径为 Go Web → Python 内部服务 → LangGraph。Go Eino 分支只提供产品与交互参考，不进入运行链路。

## Agent 分工
- ProfileAgent 仅首次建立基础画像；Income / Outcome / Feature 是展示摘要。通常开销不是本月已花或预算，未知保持 null。
- MonthAgent 每月核对收入、已花、待支付义务与用户确认的生活环境；保留原有异常探测、去重、原因追问与确认。可口述或导入账单。
- FinanceAgent 提供有日期、来源与缺口标记的金融资料。环境是证据，不是收益预测。
- PlanAgent 自主安排消费钱包、缓冲、目标储蓄、额外还款、投资范围和投入节奏；代码只计算与独立校验。
- SummaryAgent 保留月度对比、计划执行、画像事件与经验；新增 insights 数据为未来高级可视化准备，本次不新增复盘可视化。

## 正式方案
Plan.schema_version=2，wallets 是唯一分配来源，budget/reserve/allocation 是由钱包计算的兼容汇总。整数分，消费预算包含已花与尚未支付义务；同一资金不可重复分配。已有储备不自动计入本月收入。用户明确允许动用的收入外余额由 additional_funds_cents/evidence 记录，funding 分开展示收入、额外余额与总可用资金；复盘不将动用旧余额算作新增财富。

Wallet.id 跨版本稳定；kind 为 expense/buffer/goal/extra_debt/investment。消费分类沿用现有分类，最低还款为债务还款。每项有 reason/execution/change_reason/assumptions/source_urls。投资另外有 asset_class/asset_scope/horizon_months/liquidity/risks/review_conditions/steps，比例以新增投资总额为分母。

StateGraph：prepare → inspect ↔ tools → propose → validate。校验不通过最多三次模型提案，具体错误返回 Agent；通过后 interrupt 等待用户确认，edit/more 重新提案。关键资料缺失 clarification_target=month，页面引导回本月核对。

不使用 30% 消费上限、3/6 月缓冲、风险档固定配比或经验自动缩放。domain.plan 中旧纯函数与旧图仅用于历史方案和旧单元验证，不在 PlanAgent 正式链路运行。无模型不生成默认方案，已有正式计划和版本保留。

风险校验沿用明确披露的演示压力假设（流动类 0%、稳健 8%、增长 35%），不是实际风险测量或收益预测。非流动类需确认期限与亏损承受范围；期限约束是校验条件，不决定配置。金融来源 URL 必须来自本次简报，具体产品推荐不在本次范围。

## 月度事实与输入
MonthSnapshot.obligations 只记录尚未支付义务，不能重复已花 categories；obligations_reviewed 必须由用户核对后成立。environment 只记录明确确认的假期、实习、旅行等，不凭身份或日历猜测。纯口述支持，无账单也能进入月度核对。手填确认可补收入与义务核对状态，不覆盖冲突账单。

## 历史与恢复
新方案检查点使用 wallet-v2 后缀，旧待确认方案不在新图恢复。旧已确认版本可继续查看。同账号任务隔离、显式确认、幂等版本保存与 SQLite 持久化继续沿用。

## 复盘数据
MonthlySummary.insights 包含 metrics、category_changes、wallet_execution、goal、milestones、confirmed_environment、accounting。只比较紧邻上月；上月缺失不补零。非消费钱包缺少实际执行数据时保持 unknown。结余、储蓄去向、投资投入与市场盈亏分开，不相加重复称作攒下的钱。成果依用户目标和证据，不以消费越少、市场上涨为成绩。

## 页面
方案页面显示本月概览、资金图和每项可折叠钱包。解释引用结构化金额，投资显示分批日期与金额。图表、表格与历史版本使用相同钱包数据。画像默认三概念摘要，详细字段可展开编辑；账本提供直接聊本月入口。
