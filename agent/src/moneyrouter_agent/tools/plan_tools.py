"""方案计算的**工具层**——把七步纯函数暴露成模型可自主调用的 LangChain 工具。

设计要点（对齐 WORKPLAN §4.2「Agent 调用专业计算工具、工具返回可复算数字」）：

- **真实输入来自闭包**:class:`PlanToolContext`**，模型的入参只有少量 enum/数值旋钮**
  （如可选上限比例、预备金月数、风险档）——模型**无法注入金额**，杜绝编造数字；
- 每个工具都是**纯函数委托**，返回值是 JSON 可序列化的「元 + 分」；金额口径与
  :mod:`moneyrouter_agent.domain.plan` 完全一致；
- 工具名沿用 WORKPLAN 的七个名字，便于与后续 Go 侧对齐。

即便模型一个工具都不调，方案生成也会由代码跑完整管线——工具的作用是让模型**看见**中间
结果、据此决定是否追问与如何解释。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from ..domain import plan as plan_domain
from ..domain.money import format_yuan
from ..domain.plan import PlanContext

TOOL_NAMES: tuple[str, ...] = (
    "summarize_cashflow",
    "allocate_budget",
    "calculate_reserve",
    "assess_risk",
    "propose_allocation",
    "find_sources",
    "check_goal",
)


def _money(cents: int | None) -> str:
    return format_yuan(int(cents)) if cents is not None else "—"


def make_plan_tools(context: PlanContext) -> list[BaseTool]:
    """构造七步工具（每次调用返回新的一批，绑定到给定上下文）。"""

    @tool
    def summarize_cashflow() -> dict:
        """核验账单与画像，返回月均收入、必要支出、可选支出、债务与数据可信度。"""
        cf = context.build().cashflow
        return {
            "months_covered": cf.months_covered,
            "income_cents": cf.income_cents,
            "necessary_cents": cf.necessary_cents,
            "optional_cents": cf.optional_cents,
            "debt_cents": cf.debt_cents,
            "available_cents": cf.available_cents,
            "confidence": cf.data_quality.confidence,
            "sufficient": cf.data_quality.sufficient,
            "missing": cf.data_quality.missing,
            "note": f"月均收入 {_money(cf.income_cents)}、必要 {_money(cf.necessary_cents)}、"
            f"可选 {_money(cf.optional_cents)}、债务 {_money(cf.debt_cents)}",
        }

    @tool
    def allocate_budget(wants_ratio_pct: float | None = None) -> dict:
        """按 50/30/20 对照与真实必要支出，算出月度可选支出上限与可储蓄额。

        Args:
            wants_ratio_pct: 可选支出上限占收入的比例（%）；留空用默认 30，会被钳到 0-100。
        """
        plan = context.build(context.with_strategy(wants_ratio_pct=wants_ratio_pct))
        b = plan.budget
        return {
            "income_cents": b.income_cents,
            "necessary_cents": b.necessary_cents,
            "debt_cents": b.debt_cents,
            "available_cents": b.available_cents,
            "wants_cents": b.wants_cents,
            "savings_cents": b.savings_cents,
            "wants_ratio_pct": b.wants_ratio_pct,
            "negative_balance": b.negative_balance,
            "reference": "50/30/20 仅作对照，真实必要支出与债务优先",
        }

    @tool
    def calculate_reserve(reserve_months: int | None = None) -> dict:
        """算出应急预备金的目标、缺口、建议月度补足与可投资余额。

        Args:
            reserve_months: 预备金目标月数，只接受 3 或 6；留空按收入稳定性与家庭负担自动判定。
        """
        plan = context.build(context.with_strategy(reserve_months_override=reserve_months))
        r = plan.reserve
        return {
            "months": r.months,
            "target_cents": r.target_cents,
            "existing_cents": r.existing_cents,
            "gap_cents": r.gap_cents,
            "monthly_cents": r.monthly_cents,
            "investable_cents": r.investable_cents,
            "high_interest_debt": r.high_interest_debt,
            "basis": r.basis,
        }

    @tool
    def assess_risk(risk_level_override: str | None = None) -> dict:
        """分别评估风险能力、意愿、期限，取最保守档并给出增长类上限。

        Args:
            risk_level_override: 用户明确要求的风险档（低/中/高）；只会更保守，不会高于评估档。
        """
        override = risk_level_override if risk_level_override in ("低", "中", "高") else None
        plan = context.build(context.with_strategy(risk_level_override=override))
        r = plan.risk
        return {
            "ability": r.ability,
            "willingness": r.willingness,
            "horizon": r.horizon,
            "level": r.level,
            "growth_cap_pct": r.growth_cap_pct,
            "missing": r.missing,
            "rationale": r.rationale,
        }

    @tool
    def propose_allocation() -> dict:
        """把可投资余额分到保守储蓄 / 稳健配置 / 增长配置三类，增长类不超过风险上限。"""
        a = context.build().allocation
        return {
            "investable_cents": a.investable_cents,
            "growth_cap_pct": a.growth_cap_pct,
            "slices": [
                {"category": s.category, "amount_cents": s.amount_cents, "pct": s.pct} for s in a.recommended
            ],
        }

    @tool
    def find_sources(max_items: int = 5) -> dict:
        """取回当月金融简报里的宏观指标与来源（作方案解释的背景依据，不改变风控计算）。

        Args:
            max_items: 最多返回多少条来源，1-10。
        """
        briefing = context.inputs.briefing
        if briefing is None:
            return {"available": False, "metrics": [], "sources": [], "note": "本次未接入金融简报"}
        count = max(1, min(10, int(max_items)))
        return {
            "available": True,
            "as_of": briefing.as_of,
            "headline": briefing.analysis.headline if briefing.analysis else "",
            "metrics": [
                {"key": m.key, "label": m.label, "value": m.value, "unit": m.unit, "as_of": m.as_of}
                for m in briefing.metrics[:count]
            ],
            "sources": [
                {"title": s.title, "url": s.url} for s in briefing.sources[:count]
            ],
        }

    @tool
    def check_goal() -> dict:
        """对「预期年化 >3%」做情景演算（假设情景，不是收益预测），给出是否可达与提醒。"""
        scenario = context.build().scenario
        if scenario is None:
            return {
                "available": False,
                "goal_reachable": False,
                "note": "当前没有可配置资金，暂不做目标情景演算",
            }
        return {
            "available": True,
            "weighted_annual_pct": scenario.weighted_annual_pct,
            "annual_cost_pct": scenario.annual_cost_pct,
            "net_annual_pct": scenario.net_annual_pct,
            "goal_reachable": scenario.goal_reachable,
            "is_scenario": scenario.is_scenario,
            "caveats": scenario.caveats,
        }

    return [
        summarize_cashflow,
        allocate_budget,
        calculate_reserve,
        assess_risk,
        propose_allocation,
        find_sources,
        check_goal,
    ]
