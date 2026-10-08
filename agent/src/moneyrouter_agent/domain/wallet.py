"""Agent-authored monthly allocations. Code validates facts; it never selects a mix."""
from __future__ import annotations

from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from ..periods import history_window, period_context, valid_period


class InvestmentStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str = Field(description="投入日期 YYYY-MM-DD，属于方案月份")
    amount_cents: int = Field(ge=0, strict=True)


class Wallet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80, description="跨版本稳定的用途标识")
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["expense", "buffer", "goal", "extra_debt", "investment"]
    category: str = Field(default="", description="消费钱包必须使用系统分类；最低还款使用 债务还款")
    amount_cents: int = Field(ge=0, strict=True, description="本月总安排，包含已经花掉的金额；整数分")
    reason: str = Field(min_length=1, max_length=1200, description="用户可见的事实依据与取舍，不是内部思维过程")
    execution: str = Field(min_length=1, max_length=1200, description="具体执行方式；不要重复书写金额，界面引用字段")
    change_reason: str = Field(default="", max_length=1200)
    assumptions: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list, description="只能引用金融简报实际存在的 URL")
    target_cents: int | None = Field(default=None, ge=0, strict=True,
        description="缓冲钱包必填：自主选择的储备总目标，包含已有 reserve_cents；本月投入不能超过总目标减已有储备的缺口。不是本月新投入金额。其他钱包可空。")
    asset_class: Literal["liquid", "steady", "growth"] | None = None
    asset_scope: str = Field(default="", description="资产类别、市场范围或策略；不指定未经核实的产品")
    horizon_months: int | None = Field(default=None, ge=1, le=600)
    liquidity: str = ""
    risks: list[str] = Field(default_factory=list)
    review_conditions: list[str] = Field(default_factory=list)
    steps: list[InvestmentStep] = Field(default_factory=list)


class WalletProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headline: str = Field(min_length=1, max_length=200)
    wallets: list[Wallet] = Field(min_length=1, max_length=40)
    caveats: list[str] = Field(default_factory=list)


def planning_facts(inputs):
    """Current month facts, never historical averages or automatically spendable reserves."""
    snapshot = inputs.snapshot
    window = history_window(inputs.period) if valid_period(inputs.period) and inputs.period > "0001-03" else []
    return {
        "period": inputs.period, "as_of": inputs.as_of,
        "planning_mode": inputs.planning_mode, "source_period": inputs.source_period,
        "period_context": period_context(inputs.period, date.fromisoformat(inputs.as_of) if inputs.as_of else None) if valid_period(inputs.period) else {},
        "profile": inputs.profile.model_dump(mode="json") if inputs.profile else None,
        "month": snapshot.model_dump(mode="json") if snapshot else None,
        "history_window": window,
        "history": [s.model_dump(mode="json") for s in inputs.history if s.period in window],
        "missing_history_periods": [p for p in window if not any(s.period == p for s in inputs.history)],
        "experience": inputs.experience.model_dump(mode="json") if inputs.experience else None,
        "briefing": inputs.briefing.model_dump(mode="json") if inputs.briefing else None,
        "notice": "period 是方案月份；as_of 是信息基准日。历史开销不是预算。income 是已到账实际；expected_income 是包含已到账的预计整月总收入，不能相加。categories 是已花事实，obligations 是未支付义务。未来月是预案，历史月只回看。储备不自动计入可用资金。",
    }


def validate_wallets(proposal, inputs):
    from .month import SPEND_CATEGORIES, category_map, planning_income_cents, actual_income_cents
    errors = []
    snapshot = inputs.snapshot
    income = planning_income_cents(snapshot) if snapshot else None
    if not valid_period(inputs.period):
        errors.append("缺少有效的方案月份")
    if inputs.planning_mode == "next_month":
        from ..periods import shift_period
        from .month import complete_month
        if not valid_period(inputs.source_period) or shift_period(inputs.source_period, 1) != inputs.period:
            errors.append("账单月份 M 与方案月份 M+1 必须相邻")
        if not any(s.period == inputs.source_period and complete_month(s) for s in inputs.history):
            errors.append("缺少上一月完整账单依据")
        if snapshot and (snapshot.income or snapshot.categories or snapshot.wallet_execution):
            errors.append("下一月预期中不能混入已发生收支，请使用当月调整入口")
    if inputs.as_of and inputs.period < inputs.as_of[:7]:
        errors.append("历史月份仅供查看与复盘，请选择当前或未来月份生成方案")
    if snapshot and inputs.as_of and inputs.period > inputs.as_of[:7] and (actual_income_cents(snapshot) is not None or snapshot.categories or snapshot.wallet_execution):
        errors.append("未来月份不能有已到账收入、已花或已执行记录，请核对预计资料")
    if snapshot and snapshot.expected_income and actual_income_cents(snapshot) is not None and income < actual_income_cents(snapshot):
        errors.append("预计整月总收入不能低于已到账实际收入")
    if not snapshot or snapshot.period != inputs.period:
        errors.append("本月实况与方案期间不一致，请先核对正确月份")
    if income is None or income < 0:
        errors.append("缺少方案月份的有效收入，请核对实际收入或预计整月收入")
    if not snapshot or not snapshot.obligations_reviewed:
        errors.append("请先由 MonthAgent 核对尚未支付义务（包括明确没有）")
    spent = category_map(snapshot.categories) if snapshot else {}
    obligations = {}
    obligation_ids = [item.id for item in getattr(snapshot, "obligations", []) or []]
    if len(set(obligation_ids)) != len(obligation_ids):
        errors.append("待支付义务重复，请核对")
    for item in getattr(snapshot, "obligations", []) or []:
        obligations[item.category] = obligations.get(item.category, 0) + item.amount_cents
    if inputs.debt_payment_cents is not None:
        obligations["债务还款"] = max(obligations.get("债务还款", 0), inputs.debt_payment_cents)
    ids = [w.id for w in proposal.wallets]
    if len(set(ids)) != len(ids):
        errors.append("钱包 ID 重复，同一用途不得重复计入")
    expenses = {}
    known_urls = {s.url for s in inputs.briefing.sources} if inputs.briefing else set()
    if proposal.wallets and len({w.id for w in proposal.wallets if w.kind == "buffer"}) > 1:
        errors.append("当前只支持一个总缓冲钱包，避免共享现有储备重复抵扣")
    investment_total = sum(w.amount_cents for w in proposal.wallets if w.kind == "investment")
    executions = {x.wallet_id: x.amount_cents for x in snapshot.wallet_execution} if snapshot else {}
    if snapshot and len(executions) != len(snapshot.wallet_execution):
        errors.append("钱包执行记录重复")
    already_invested = snapshot.allocation.invested_cents if snapshot else None
    executed_investment = sum(executions.get(w.id, 0) for w in proposal.wallets if w.kind == "investment")
    if already_invested is not None and executed_investment != already_invested:
        errors.append("投资钱包已执行记录与本月新增投资合计不一致，请由 MonthAgent 核对")
    if any(key not in ids for key in executions):
        errors.append("已执行钱包在候选方案中遗漏，请保留原钱包 id")
    loss = 0
    for w in proposal.wallets:
        if any(url not in known_urls or not url.startswith(("https://", "http://")) for url in w.source_urls):
            errors.append(f"{w.name} 引用未提供的来源")
        if w.kind != "expense" and w.amount_cents < executions.get(w.id, 0):
            errors.append(f"{w.name} 预算低于本月已经执行金额")
        if w.kind == "expense":
            if w.category not in (*SPEND_CATEGORIES, "债务还款"):
                errors.append(f"{w.name} 消费分类无效")
            if w.category in expenses:
                errors.append(f"{w.name} 消费分类重复，请合并")
            expenses[w.category] = expenses.get(w.category, 0) + w.amount_cents
        elif w.category:
            errors.append(f"{w.name} 非消费钱包不得占用消费分类")
        if w.kind != "investment" and w.steps:
            errors.append(f"{w.name} 非投资钱包不能包含投资投入")
        if w.kind == "buffer" and (w.target_cents is None or w.target_cents <= 0):
            errors.append(f"{w.name} 必须给出自主选择的缓冲目标")
        if w.kind == "extra_debt" and not (inputs.profile and inputs.profile.debt_cents):
            errors.append("没有确认债务，不能安排额外还款")
        if w.kind == "investment":
            if not w.asset_class or not w.asset_scope or not w.liquidity or not w.risks or not w.review_conditions or not w.horizon_months:
                errors.append(f"{w.name} 缺少投资范围、期限、流动性、风险或调整条件")
            if sum(s.amount_cents for s in w.steps) != w.amount_cents-executions.get(w.id, 0):
                errors.append(f"{w.name} 投入节奏与钱包金额不一致")
            for step in w.steps:
                try:
                    date.fromisoformat(step.date)
                    if step.date[:7] != inputs.period or (inputs.as_of and step.date < inputs.as_of):
                        raise ValueError()
                except ValueError:
                    errors.append(f"{w.name} 投入日期必须属于方案月份 {inputs.period} 且不早于信息基准日")
            horizon = inputs.profile.horizon_months if inputs.profile else None
            if w.horizon_months and horizon and w.horizon_months > horizon:
                errors.append(f"{w.name} 持有期限超过用户确认的资金使用期限")
            if w.asset_class in ("steady", "growth") and w.amount_cents:
                minimum = 12 if w.asset_class == "steady" else 24
                if horizon is None or not w.horizon_months or min(horizon, w.horizon_months) < minimum:
                    errors.append(f"{w.name} 缺少足够的已确认资金期限")
                loss += w.amount_cents * (8 if w.asset_class == "steady" else 35)
    for category in spent.keys() | obligations.keys():
        required = spent.get(category, 0) + obligations.get(category, 0)
        if expenses.get(category, 0) < required:
            errors.append(f"{category} 预算不足：须覆盖已花及待支付的 {required} 分")
    buffer_total = sum(w.amount_cents for w in proposal.wallets if w.kind == "buffer")
    buffer_target = sum(w.target_cents or 0 for w in proposal.wallets if w.kind == "buffer")
    existing = inputs.profile.reserve_cents if inputs.profile and inputs.profile.reserve_cents is not None else None
    if buffer_total and existing is None:
        errors.append("安排缓冲前请核对现有可动用储备")
    if buffer_total > max(0, buffer_target - (existing or 0)):
        errors.append("本月缓冲投入超过目标缺口")
    additional = snapshot.additional_funds_cents or 0 if snapshot else 0
    if additional and not snapshot.additional_funds_evidence.strip():
        errors.append("收入外余额缺少用户明确允许动用的依据")
    funding = income + additional if income is not None else None
    total = sum(w.amount_cents for w in proposal.wallets)
    if funding is not None and total != funding:
        errors.append(f"钱包总额 {total} 分不等于本月确认可用资金 {funding} 分；请调整或核对缺口")
    tolerance = inputs.profile.max_loss_pct if inputs.profile else None
    stress = loss / investment_total if investment_total else 0
    if loss and (tolerance is None or stress > tolerance):
        errors.append("投资压力情景超出已确认亏损承受范围，或缺少承受范围")
    return {"ok": not errors, "errors": errors, "income_cents": income,
            "funding_cents": funding, "additional_funds_cents": additional, "allocated_cents": total, "stress_loss_pct": round(stress, 2),
            "stress_assumptions": {"liquid": 0, "steady": 8, "growth": 35}}


def compose_wallet_plan(proposal, inputs):
    """Derive display/legacy aggregates from model amounts, without choosing allocations."""
    from .plan import Plan, BudgetBaseline, AllocationProposal, AllocationSlice, CashflowSummary
    from .plan import ReservePlan, RiskAssessment, NECESSARY_CATEGORIES
    from .month import category_map
    report = validate_wallets(proposal, inputs)
    if not report["ok"]:
        raise ValueError("；".join(report["errors"]))
    spent = category_map(inputs.snapshot.categories)
    rows = []
    for wallet in proposal.wallets:
        row = wallet.model_dump(exclude={"spent_cents"})
        row["spent_cents"] = spent.get(wallet.category, 0 if inputs.snapshot.coverage_complete else None) if wallet.kind == "expense" else next((x.amount_cents for x in inputs.snapshot.wallet_execution if x.wallet_id == wallet.id), None)
        row["remaining_cents"] = (wallet.amount_cents - row["spent_cents"]) if row["spent_cents"] is not None else None
        total = sum(w.amount_cents for w in proposal.wallets if w.kind == "investment")
        row["investment_pct"] = round(wallet.amount_cents / total * 100, 2) if wallet.kind == "investment" and total else None
        rows.append(row)
    needs = sum(w.amount_cents for w in proposal.wallets if w.kind == "expense" and w.category in NECESSARY_CATEGORIES)
    debt = sum(w.amount_cents for w in proposal.wallets if w.kind == "expense" and w.category == "债务还款")
    wants = sum(w.amount_cents for w in proposal.wallets if w.kind == "expense" and w.category not in (*NECESSARY_CATEGORIES, "债务还款"))
    savings = report["funding_cents"] - needs - debt - wants
    investment = sum(w.amount_cents for w in proposal.wallets if w.kind == "investment")
    buffers = [w for w in proposal.wallets if w.kind == "buffer"]
    existing = inputs.profile.reserve_cents if inputs.profile and inputs.profile.reserve_cents is not None else 0
    target = sum(w.target_cents or 0 for w in buffers)
    return Plan(schema_version=2, period=inputs.period, headline=proposal.headline, wallets=rows,
        input_facts=planning_facts(inputs), funding={"income_cents": report["income_cents"], "additional_funds_cents": report["additional_funds_cents"], "total_cents": report["funding_cents"]}, stress_loss_pct=report["stress_loss_pct"],
        warnings=proposal.caveats, sources=sorted({url for w in proposal.wallets for url in w.source_urls}),
        cashflow=CashflowSummary(period=inputs.period, income_cents=report["income_cents"]),
        budget=BudgetBaseline(income_cents=report["income_cents"], necessary_cents=needs, debt_cents=debt,
            wants_cents=wants, savings_cents=savings),
        reserve=ReservePlan(months=0, target_cents=target, existing_cents=existing,
            gap_cents=max(0, target-existing), monthly_cents=sum(w.amount_cents for w in buffers), investable_cents=investment),
        risk=RiskAssessment(level="低" if not report["stress_loss_pct"] else "中", growth_cap_pct=0,
            rationale="压力情景仅为演示假设，不决定固定配置比例"),
        allocation=AllocationProposal(investable_cents=investment, recommended=[AllocationSlice(
            category=label, amount_cents=sum(w.amount_cents for w in proposal.wallets if w.kind == "investment" and w.asset_class == key),
            pct=round(sum(w.amount_cents for w in proposal.wallets if w.kind == "investment" and w.asset_class == key)/investment*100, 2) if investment else 0)
            for key, label in [("liquid", "保守储蓄"), ("steady", "稳健配置"), ("growth", "增长配置")]]),
        narrative=f"{inputs.period} 规划资金 {report['funding_cents']/100:.2f} 元，消费安排 {(needs+debt+wants)/100:.2f} 元，储蓄与投资安排 {savings/100:.2f} 元。预计收入包含已到账部分，不能重复相加。请展开各钱包查看安排与原因。")
