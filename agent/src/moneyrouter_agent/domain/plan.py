"""历史方案兼容模型 + 七步可复算算法（**全部由代码执行，模型没有写入通路**）。

流程与规则取自已批准的 WORKPLAN §4.1/§4.2：

1. 数据核验 :func:`summarize_cashflow` —— 月均收入/必要/可选/债务 + 数据可信度；
2. 预算基线 :func:`allocate_budget` —— 50/30/20 仅作对照，真实必要支出与债务优先；
3. 预备金   :func:`calculate_reserve` —— 3 或 6 个月必要支出，先补缺口；
4. 风险约束 :func:`assess_risk` —— 能力/意愿/期限各评一档，取最保守；
5. 候选配置 :func:`propose_allocation` —— 保守储蓄 / 稳健配置 / 增长配置，增长上限 0/20/40%；
6. 目标检验 :func:`check_goal` —— 「>3%」做**情景演算**（不是预测）；
7. 独立复核 :func:`validate_plan` —— 重算所有金额、检查风险上限与总额平衡。

:func:`build_plan` 串起 1-6 并产出规范 :class:`Plan`；:func:`validate_plan` 是第 7 步的独立复核。

设计纪律（与全仓一致）：金额一律「分」(int)；未了解到留 ``None``、**不补 0、不估算**（``0`` 是合法值）；
模型交回来的任何数字都会被本模块重算覆盖。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .experience import ExperienceEffects, ExperiencePack, apply_lessons
from .finance import FinanceBriefing
from .money import format_yuan
from .month import MonthSnapshot, SPEND_CATEGORIES
from .profile import Profile

# --------------------------------------------------------------------------- #
# 常量（口径与已批准规则一致）
# --------------------------------------------------------------------------- #
# 支出分类的「必要 / 可选」归属（对齐 WORKPLAN 的必要支出口径：住房·餐饮·交通·医疗·教育）
NECESSARY_CATEGORIES: tuple[str, ...] = ("居住", "餐饮", "交通", "医疗健康", "学习成长")
OPTIONAL_CATEGORIES: tuple[str, ...] = tuple(c for c in SPEND_CATEGORIES if c not in NECESSARY_CATEGORIES)

WANTS_RATIO_CAP_PCT = 30.0  # 可选支出上限 = 税后月收入 × 30%
RESERVE_MONTHS_STABLE = 3
RESERVE_MONTHS_UNSTABLE = 6
HIGH_DEBT_INCOME_RATIO_PCT = 30  # 债务月供 / 收入 超过该比例 → 视为高息债务提醒线

RISK_LEVELS: tuple[str, ...] = ("低", "中", "高")
GROWTH_CAP_PCT: dict[str, int] = {"低": 0, "中": 20, "高": 40}
# 稳健配置占比（按风险档）；剩余归入保守储蓄
STEADY_RATIO: dict[str, float] = {"低": 0.0, "中": 0.50, "高": 0.40}
# 情景假设年化（**不是预测**）：保守 2.0% / 稳健 3.5% / 增长 6.0%
SCENARIO_ANNUAL_PCT: dict[str, float] = {"保守储蓄": 2.0, "稳健配置": 3.5, "增长配置": 6.0}
SCENARIO_COST_PCT = 0.5
GOAL_SCENARIO_THRESHOLD_PCT = 3.0

MAX_MONTHS_FOR_AVERAGE = 3

RiskLevel = Literal["低", "中", "高"]


def _level_index(level: str) -> int:
    return RISK_LEVELS.index(level) if level in RISK_LEVELS else 0


def _more_conservative(a: str, b: str) -> RiskLevel:
    """取更保守（档位更低）的一个。"""
    return RISK_LEVELS[min(_level_index(a), _level_index(b))]  # type: ignore[return-value]


def _downgrade(level: str, notches: int) -> RiskLevel:
    return RISK_LEVELS[max(0, _level_index(level) - max(0, int(notches)))]  # type: ignore[return-value]


def _clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


# --------------------------------------------------------------------------- #
# 输入整合
# --------------------------------------------------------------------------- #
class PlanInputs(BaseModel):
    """方案生成要整合的**全部信息**（由外部注入，不自己去调其他 agent）。

    这些对象放在门面/节点的闭包里传入，**不进图状态**，以免把外部依赖塞进检查点。
    """

    model_config = ConfigDict(extra="ignore")

    profile: Profile | None = Field(default=None, description="用户画像（来自画像访谈 agent）。")
    briefing: FinanceBriefing | None = Field(
        default=None, description="金融情况简报（当月新闻/指标/来源，来自金融情况 agent）。"
    )
    snapshot: MonthSnapshot | None = Field(
        default=None, description="本月实况快照（来自本月实况 agent），用于现金流与结余。"
    )
    history: list[MonthSnapshot] = Field(
        default_factory=list, description="更早月份的快照，用于月均与基线；最近的在末尾。"
    )
    experience: ExperiencePack | None = Field(
        default=None, description="经验包（可选，来自月底后馈）；不注入则不产生任何软调整。"
    )
    period: str = Field(default="", description="方案所属期间 YYYY-MM。")
    as_of: str = Field(default="", description="信息基准日期 YYYY-MM-DD。")
    planning_mode: Literal["next_month", "adjustment"] = "adjustment"
    source_period: str = Field(default="", description="主流程依据的上一月完整账单月份，与方案月份相差一个自然月")
    debt_payment_cents: int | None = Field(
        default=None, description="每月债务最低还款（分）；未了解到留空（不补 0）。"
    )


# --------------------------------------------------------------------------- #
# 七步产物
# --------------------------------------------------------------------------- #
class DataQuality(BaseModel):
    """数据可信度——**由代码判**，用于决定能不能出「精确方案」。"""

    model_config = ConfigDict(extra="ignore")

    months_covered: int = Field(default=0, description="参与月均的完整月份数，代码算。")
    sufficient: bool = Field(default=False, description="是否已够一个完整月份且无金额冲突，代码判。")
    has_conflict: bool = Field(default=False, description="画像收入与账单收入是否明显冲突，代码判。")
    confidence: Literal["high", "medium", "low"] = Field(default="low", description="可信度，代码判。")
    missing: list[str] = Field(default_factory=list, description="仍缺的关键项（income/debt_payment/profile）。")
    notes: str = Field(default="", description="一句话说明可信度依据。")


class CashflowSummary(BaseModel):
    """第 1 步产物：月均现金流概览。金额一律「分」。"""

    model_config = ConfigDict(extra="ignore")

    period: str = ""
    months_covered: int = 0
    income_cents: int = Field(default=0, description="月均税后收入（分）。未知为 0。")
    necessary_cents: int = Field(default=0, description="月均必要支出（分）。")
    optional_cents: int = Field(default=0, description="月均可选支出（分，实际值）。")
    debt_cents: int = Field(default=0, description="每月债务最低还款（分）；未知暂按 0 计并标注。")
    available_cents: int = Field(default=0, description="可用余额 = 收入 − 必要 − 债务（分），代码算。")
    net_balance_cents: int = Field(default=0, description="月净结余 = 收入 − 必要 − 债务 − 可选（分），代码算。")
    ref_50_cents: int = Field(default=0, description="50/30/20 参考：收入×50%（分，仅对照）。")
    ref_30_cents: int = Field(default=0, description="50/30/20 参考：收入×30%（分，仅对照）。")
    ref_20_cents: int = Field(default=0, description="50/30/20 参考：收入×20%（分，仅对照）。")
    data_quality: DataQuality = Field(default_factory=DataQuality)
    warnings: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list, description="数据出处标签（账单/本月实况/画像）。")


class PlanStrategy(BaseModel):
    """策略旋钮——**模型可通过它参与取舍**，但一律会被代码钳到合法区间。"""

    model_config = ConfigDict(extra="ignore")

    wants_ratio_pct: float = Field(
        default=WANTS_RATIO_CAP_PCT, description="可选支出上限占收入的比例（%）；默认 30，钳到 0-100。"
    )
    risk_level_override: RiskLevel | None = Field(
        default=None, description="用户明确要求的风险档；最终只会**更保守**，不会高于评估档。"
    )
    risk_downgrade_notches: int = Field(default=0, description="风险档再保守化的档位数（0-2）。")
    reserve_months_override: int | None = Field(
        default=None, description="预备金目标月数；只接受 3 或 6，其余忽略。"
    )
    force_reserve_first: bool = Field(default=False, description="结余是否**全部**优先留作预备金/现金。")
    debt_priority: bool = Field(default=False, description="是否提示高息债务优先、暂缓投资。")
    goal_conservative: bool = Field(default=False, description="目标措辞是否更保守。")
    category_cap_cents: dict[str, int] = Field(
        default_factory=dict, description="某类支出的上限（分）；只对**可选类**生效。"
    )


class BudgetBaseline(BaseModel):
    """第 2 步产物：预算基线。"""

    model_config = ConfigDict(extra="ignore")

    income_cents: int = 0
    necessary_cents: int = 0
    debt_cents: int = 0
    optional_actual_cents: int = Field(default=0, description="实际可选支出（分）。")
    available_cents: int = Field(default=0, description="收入 − 必要 − 债务（分，非负），代码算。")
    wants_cents: int = Field(default=0, description="计划的可行支出 = min(实际, 可用, 收入×比例)（分），代码算。")
    savings_cents: int = Field(default=0, description="可储蓄 = max(0, 可用 − wants)（分），代码算。")
    net_balance_cents: int = Field(default=0, description="月净结余（实际口径）（分），代码算。")
    negative_balance: bool = Field(default=False, description="可用余额是否为负，代码判；为真不安排新增投资。")
    wants_ratio_pct: float = Field(default=WANTS_RATIO_CAP_PCT, description="本次采用的可选上限比例（%）。")


class ReservePlan(BaseModel):
    """第 3 步产物：预备金。"""

    model_config = ConfigDict(extra="ignore")

    months: int = Field(default=RESERVE_MONTHS_STABLE, description="目标月数：3 或 6，代码判。")
    basis: str = Field(default="", description="为什么取这个月数（收入稳定性 / 家庭负担）。")
    target_cents: int = Field(default=0, description="目标 = 必要支出 × 月数（分），代码算。")
    existing_cents: int = Field(default=0, description="现有可动用预备金（分）。")
    gap_cents: int = Field(default=0, description="缺口 = max(0, 目标 − 现有)（分），代码算。")
    monthly_cents: int = Field(default=0, description="建议月度补足 = min(结余, 缺口)（分），代码算。")
    investable_cents: int = Field(default=0, description="可投资余额 = max(0, 结余 − 月度补足)（分），代码算。")
    high_interest_debt: bool = Field(default=False, description="是否存在高息债务（月供/收入超线），代码判。")
    notes: str = ""


class RiskAssessment(BaseModel):
    """第 4 步产物：风险约束（能力/意愿/期限取最保守）。"""

    model_config = ConfigDict(extra="ignore")

    ability: RiskLevel = Field(default="低", description="风险承受能力档，代码评。")
    willingness: RiskLevel = Field(default="低", description="风险意愿档（主观可承受亏损），代码评。")
    horizon: RiskLevel = Field(default="低", description="期限约束档，代码评。")
    level: RiskLevel = Field(default="低", description="最终档 = 三者最保守（含覆盖与保守化），代码算。")
    growth_cap_pct: int = Field(default=0, description="增长类上限：低/中/高 = 0%/20%/40%，代码算。")
    missing: list[str] = Field(default_factory=list, description="缺失的评判输入（缺失按保守档并标注）。")
    rationale: str = Field(default="", description="一句话说明为何取此档。")


class AllocationSlice(BaseModel):
    """一类资金的建议金额。"""

    model_config = ConfigDict(extra="ignore")

    category: Literal["保守储蓄", "稳健配置", "增长配置"]
    amount_cents: int = Field(description="该类别建议金额（分），代码算。")
    pct: float = Field(default=0.0, description="占可投资余额的百分比，代码算。")


class AllocationProposal(BaseModel):
    """第 5 步产物：三类资金配置。"""

    model_config = ConfigDict(extra="ignore")

    investable_cents: int = 0
    growth_cap_pct: int = 0
    recommended: list[AllocationSlice] = Field(default_factory=list, description="推荐配置；无可投资金时为空。")
    notes: str = ""


class ScenarioCheck(BaseModel):
    """第 6 步产物：目标情景演算（**情景不是预测**）。"""

    model_config = ConfigDict(extra="ignore")

    goal: str = ""
    horizon_months: int | None = None
    weighted_annual_pct: float = Field(default=0.0, description="按配置加权的年化（%），代码算。")
    annual_cost_pct: float = Field(default=SCENARIO_COST_PCT, description="成本假设（%），代码算。")
    net_annual_pct: float = Field(default=0.0, description="加权年化 − 成本（%），代码算。")
    is_scenario: bool = Field(default=True, description="恒为真：这是情景演算，不是收益预测。")
    goal_reachable: bool = Field(default=False, description="该情景下「>3%」是否可达，代码判。")
    goal_conservative: bool = Field(default=False, description="是否采用了更保守的口径（经验提示）。")
    caveats: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list, description="引用的指标 key / 来源标签。")


class ValidationIssue(BaseModel):
    """一条复核问题。"""

    model_config = ConfigDict(extra="ignore")

    code: str = Field(description="问题代码，如 recompute_mismatch / growth_over_cap。")
    message: str
    severity: Literal["error", "warning"] = "error"


class ValidationReport(BaseModel):
    """第 7 步产物：独立复核结论。"""

    model_config = ConfigDict(extra="ignore")

    ok: bool = Field(default=False, description="是否通过全部复核，代码判。")
    issues: list[ValidationIssue] = Field(default_factory=list)
    recomputed: dict[str, int] = Field(default_factory=dict, description="代码重算的关键金额（分），供对账。")


class Plan(BaseModel):
    """版本化方案：v1 是历史规则结果；v2 的钱包金额由 Agent 提出，代码计算汇总并校验。"""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    wallets: list[dict] = Field(default_factory=list)
    input_facts: dict = Field(default_factory=dict)
    funding: dict = Field(default_factory=dict)
    stress_loss_pct: float | None = None
    period: str = ""
    version: int = Field(default=1, description="方案版本号，代码递增。")
    headline: str = Field(default="", description="一句话结论（模型写）。")
    strategy: PlanStrategy = Field(default_factory=PlanStrategy, description="本次采用的策略旋钮。")
    cashflow: CashflowSummary = Field(default_factory=CashflowSummary)
    budget: BudgetBaseline = Field(default_factory=BudgetBaseline)
    reserve: ReservePlan = Field(default_factory=ReservePlan)
    risk: RiskAssessment = Field(default_factory=RiskAssessment)
    allocation: AllocationProposal = Field(default_factory=AllocationProposal)
    scenario: ScenarioCheck | None = Field(default=None, description="仅当有可投资金时给出。")
    data_quality: DataQuality = Field(default_factory=DataQuality)
    warnings: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list, description="来源标签（画像/账单/金融简报/经验）。")
    narrative: str = Field(default="", description="按最终方案金额由代码生成的解释。")
    lessons_applied: list[str] = Field(default_factory=list, description="生效的经验条目 id。")
    degraded: bool = False
    error: str | None = None
    trace: list[str] = Field(default_factory=list, description="各步算式说明，便于展示「方案如何产生」。")


class PlanContext:
    """方案生成的外部依赖容器——**由门面闭包注入，不进图状态**。

    既给工具层提供输入（:mod:`moneyrouter_agent.tools.plan_tools`），也给图节点提供
    :meth:`build` 入口。多线程/多用户时，门面在每轮开跑前用 :meth:`set_inputs` 装载当次输入。
    """

    def __init__(self, inputs: PlanInputs | None = None, *, strategy: PlanStrategy | None = None) -> None:
        self.inputs = inputs or PlanInputs()
        self.strategy = strategy or PlanStrategy()

    def set_inputs(self, inputs: PlanInputs) -> None:
        self.inputs = inputs

    def with_strategy(self, **changes: object) -> PlanStrategy:
        return self.strategy.model_copy(update={k: v for k, v in changes.items() if v is not None})

    def build(self, strategy: PlanStrategy | None = None, inputs: PlanInputs | None = None) -> Plan:
        """按给定策略（或默认策略）与输入跑完第 1-6 步，产出规范方案。"""
        return build_plan(
            inputs if inputs is not None else self.inputs,
            strategy if strategy is not None else self.strategy,
        )


# --------------------------------------------------------------------------- #
# 第 1 步：数据核验
# --------------------------------------------------------------------------- #
def _months_for_average(inputs: PlanInputs) -> list[MonthSnapshot]:
    """取参与月均的快照：历史优先，其次本月；按期间去重，最多 :data:`MAX_MONTHS_FOR_AVERAGE` 个月。"""
    merged: dict[str, MonthSnapshot] = {}
    for snap in list(inputs.history or []) + ([inputs.snapshot] if inputs.snapshot else []):
        key = snap.period or f"#{len(merged)}"
        merged[key] = snap
    months = [s for s in merged.values() if s.coverage_complete is not False and (s.income is not None or s.categories)]
    if len(months) <= MAX_MONTHS_FOR_AVERAGE:
        return months
    return months[-MAX_MONTHS_FOR_AVERAGE:]


def summarize_cashflow(inputs: PlanInputs) -> CashflowSummary:
    """第 1 步：把账单/本月实况汇总成月均现金流，并给出数据可信度。"""
    months = _months_for_average(inputs)
    covered = len(months)

    incomes = [s.income.amount_cents for s in months if s.income is not None]
    income = round(sum(incomes) / len(incomes)) if incomes else 0

    necessary_total = 0
    optional_total = 0
    for snap in months:
        for item in snap.categories:
            amount = int(item.amount_cents)
            if item.category in NECESSARY_CATEGORIES:
                necessary_total += amount
            else:
                optional_total += amount
    necessary = round(necessary_total / covered) if covered else 0
    optional = round(optional_total / covered) if covered else 0

    debt = inputs.debt_payment_cents
    debt_known = debt is not None
    debt_cents = int(debt) if debt_known else 0

    available = income - necessary - debt_cents
    net_balance = income - necessary - debt_cents - optional

    profile_income = inputs.profile.income_cents if inputs.profile else None
    has_conflict = bool(
        profile_income is not None and income > 0 and abs(profile_income - income) / income > 0.30
    )

    missing: list[str] = []
    if income <= 0:
        missing.append("income")
    if not debt_known:
        missing.append("debt_payment")
    if inputs.profile is None:
        missing.append("profile")

    if covered >= 3 and not has_conflict:
        confidence: Literal["high", "medium", "low"] = "high"
    elif covered >= 1:
        confidence = "medium"
    else:
        confidence = "low"
    sufficient = covered >= 1 and income > 0 and not has_conflict

    notes_bits: list[str] = []
    if covered:
        notes_bits.append(f"基于最近 {covered} 个月的账单汇总")
    else:
        notes_bits.append("暂无可用账单，方案仅作初步参考")
    if not debt_known:
        notes_bits.append("债务月供未知，暂按 0 计，可能低估必要支出")
    if has_conflict:
        notes_bits.append("画像收入与账单收入差距较大，请核对")

    warnings: list[str] = []
    if income <= 0:
        warnings.append("缺少有效收入，请在画像中补充税后月收入")
    if has_conflict:
        warnings.append("画像收入与账单汇总不一致，请确认以哪个为准")
    if not debt_known:
        warnings.append("缺少债务月供信息，暂按 0 计")

    sources: list[str] = []
    if inputs.profile is not None:
        sources.append("用户画像")
    if months:
        sources.append(f"账单/本月实况（{covered} 个月）")

    return CashflowSummary(
        period=inputs.period,
        months_covered=covered,
        income_cents=income,
        necessary_cents=necessary,
        optional_cents=optional,
        debt_cents=debt_cents,
        available_cents=available,
        net_balance_cents=net_balance,
        ref_50_cents=int(income * 0.5),
        ref_30_cents=int(income * WANTS_RATIO_CAP_PCT / 100),
        ref_20_cents=int(income * 0.2),
        data_quality=DataQuality(
            months_covered=covered,
            sufficient=sufficient,
            has_conflict=has_conflict,
            confidence=confidence,
            missing=missing,
            notes="；".join(notes_bits),
        ),
        warnings=warnings,
        sources=sources,
    )


# --------------------------------------------------------------------------- #
# 策略合并（把经验软调整折进旋钮）
# --------------------------------------------------------------------------- #
def merge_strategy(base: PlanStrategy, effects: ExperienceEffects) -> PlanStrategy:
    """把经验软调整合进策略旋钮（有界、只往保守方向）。"""
    merged = base.model_copy(deep=True)
    merged.wants_ratio_pct = round(
        max(0.0, min(100.0, float(base.wants_ratio_pct) * float(effects.wants_ratio_scale))), 2
    )
    merged.risk_downgrade_notches = min(2, int(base.risk_downgrade_notches) + int(effects.risk_downgrade_notches))
    merged.force_reserve_first = bool(base.force_reserve_first or effects.force_reserve_first)
    merged.debt_priority = bool(base.debt_priority or effects.debt_priority)
    merged.goal_conservative = bool(base.goal_conservative or effects.goal_conservative)
    return merged


# --------------------------------------------------------------------------- #
# 第 2 步：预算基线
# --------------------------------------------------------------------------- #
def allocate_budget(cashflow: CashflowSummary, strategy: PlanStrategy) -> BudgetBaseline:
    """第 2 步：以真实必要支出与债务为硬约束，在剩余额度内安排可选与储蓄。"""
    income = cashflow.income_cents
    necessary = cashflow.necessary_cents
    debt = cashflow.debt_cents
    optional_actual = cashflow.optional_cents

    raw_available = income - necessary - debt
    negative_balance = raw_available < 0
    available = max(0, raw_available)

    ratio = _clamp_int(round(strategy.wants_ratio_pct), 0, 100) / 100.0
    wants_cap = int(income * ratio)
    wants = min(optional_actual, available, wants_cap)

    if strategy.category_cap_cents:
        capped_total = sum(
            int(v) for k, v in strategy.category_cap_cents.items() if k in OPTIONAL_CATEGORIES and v is not None
        )
        if capped_total > 0:
            wants = min(wants, capped_total)

    wants = max(0, wants)
    savings = max(0, available - wants)

    return BudgetBaseline(
        income_cents=income,
        necessary_cents=necessary,
        debt_cents=debt,
        optional_actual_cents=optional_actual,
        available_cents=available,
        wants_cents=wants,
        savings_cents=savings,
        net_balance_cents=income - necessary - debt - optional_actual,
        negative_balance=negative_balance,
        wants_ratio_pct=round(ratio * 100, 2),
    )


# --------------------------------------------------------------------------- #
# 第 3 步：预备金
# --------------------------------------------------------------------------- #
def _reserve_months(profile: Profile | None, strategy: PlanStrategy) -> tuple[int, str]:
    override = strategy.reserve_months_override
    if override in (RESERVE_MONTHS_STABLE, RESERVE_MONTHS_UNSTABLE):
        return int(override), f"按方案策略取 {int(override)} 个月"

    income_stable = bool(profile.income_stable) if profile and profile.income_stable is not None else False
    family_load = bool(profile.family_load) if profile and profile.family_load is not None else False
    if income_stable and not family_load:
        return RESERVE_MONTHS_STABLE, "收入稳定且无家庭负担，取 3 个月"
    if not income_stable and family_load:
        return RESERVE_MONTHS_UNSTABLE, "收入不稳定且有家庭负担，取 6 个月"
    if profile is not None and profile.income_stable is False:
        return RESERVE_MONTHS_UNSTABLE, "收入不稳定，取 6 个月"
    if family_load:
        return RESERVE_MONTHS_UNSTABLE, "有家庭负担，取 6 个月"
    return RESERVE_MONTHS_UNSTABLE, "收入或家庭负担不明确，按保守取 6 个月"


def calculate_reserve(
    cashflow: CashflowSummary, budget: BudgetBaseline, inputs: PlanInputs, strategy: PlanStrategy
) -> ReservePlan:
    """第 3 步：算出目标、缺口、月度补足与可投资余额。"""
    months, basis = _reserve_months(inputs.profile, strategy)
    target = budget.necessary_cents * months

    existing_raw = inputs.profile.reserve_cents if inputs.profile else None
    existing = int(existing_raw) if existing_raw is not None else 0
    gap = max(0, target - existing)

    savings = budget.savings_cents
    monthly = min(savings, gap)
    investable = max(0, savings - monthly)

    if strategy.force_reserve_first:
        monthly = savings
        investable = 0

    income = cashflow.income_cents
    high_debt = bool(
        budget.debt_cents > 0 and income > 0 and budget.debt_cents * 100 > income * HIGH_DEBT_INCOME_RATIO_PCT
    )
    notes = ""
    if existing_raw is None:
        notes = "未了解到现有预备金，按 0 计缺口可能偏大"

    return ReservePlan(
        months=months,
        basis=basis,
        target_cents=target,
        existing_cents=existing,
        gap_cents=gap,
        monthly_cents=monthly,
        investable_cents=investable,
        high_interest_debt=high_debt,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# 第 4 步：风险约束
# --------------------------------------------------------------------------- #
def assess_risk(profile: Profile | None, cashflow: CashflowSummary, strategy: PlanStrategy) -> RiskAssessment:
    """第 4 步：能力 / 意愿 / 期限各评一档，取最保守；缺失按保守档并标注。"""
    missing: list[str] = []

    loss = profile.max_loss_pct if profile else None
    if loss is None:
        willingness: RiskLevel = "低"
        missing.append("max_loss_pct（可承受亏损）")
    elif loss >= 15:
        willingness = "高"
    elif loss >= 5:
        willingness = "中"
    else:
        willingness = "低"

    horizon_months = profile.horizon_months if profile else None
    if horizon_months is None:
        horizon: RiskLevel = "低"
        missing.append("horizon_months（资金期限）")
    elif horizon_months >= 60:
        horizon = "高"
    elif horizon_months >= 24:
        horizon = "中"
    else:
        horizon = "低"

    income_stable = bool(profile.income_stable) if profile and profile.income_stable is not None else False
    experience = (profile.experience if profile else None) or "none"
    ability: RiskLevel = "高"
    if not income_stable or experience == "none":
        ability = "中"
    if cashflow.income_cents <= 0 or (
        cashflow.debt_cents > 0
        and cashflow.income_cents > 0
        and cashflow.debt_cents * 100 > cashflow.income_cents * HIGH_DEBT_INCOME_RATIO_PCT
    ):
        ability = "低"

    level = _more_conservative(willingness, horizon)
    level = _more_conservative(level, ability)
    if strategy.risk_level_override is not None:
        level = _more_conservative(level, strategy.risk_level_override)
    level = _downgrade(level, strategy.risk_downgrade_notches)

    cap = GROWTH_CAP_PCT[level]
    rationale = (
        f"意愿档 {willingness}（可承受亏损 {loss if loss is not None else '—'}%）、"
        f"期限档 {horizon}、能力档 {ability}，取最保守得 {level}；增长类上限 {cap}%"
    )
    return RiskAssessment(
        ability=ability,
        willingness=willingness,
        horizon=horizon,
        level=level,
        growth_cap_pct=cap,
        missing=missing,
        rationale=rationale,
    )


# --------------------------------------------------------------------------- #
# 第 5 步：候选配置
# --------------------------------------------------------------------------- #
def propose_allocation(reserve: ReservePlan, risk: RiskAssessment) -> AllocationProposal:
    """第 5 步：把可投资余额分到 保守储蓄 / 稳健配置 / 增长配置 三类。"""
    investable = max(0, reserve.investable_cents)
    if investable <= 0:
        return AllocationProposal(investable_cents=0, growth_cap_pct=risk.growth_cap_pct, recommended=[])

    cap = max(0, min(100, risk.growth_cap_pct))
    growth = investable * cap // 100
    steady = int(investable * STEADY_RATIO.get(risk.level, 0.0))
    conservative = investable - growth - steady
    if conservative < 0:  # 理论上不会发生，兜底
        conservative = 0
        steady = investable - growth

    def _slice(category: str, amount: int) -> AllocationSlice:
        pct = round(amount / investable * 100, 2) if investable else 0.0
        return AllocationSlice(category=category, amount_cents=amount, pct=pct)  # type: ignore[arg-type]

    return AllocationProposal(
        investable_cents=investable,
        growth_cap_pct=cap,
        recommended=[
            _slice("保守储蓄", conservative),
            _slice("稳健配置", steady),
            _slice("增长配置", growth),
        ],
    )


# --------------------------------------------------------------------------- #
# 第 6 步：目标检验
# --------------------------------------------------------------------------- #
def check_goal(
    allocation: AllocationProposal, profile: Profile | None, strategy: PlanStrategy
) -> ScenarioCheck | None:
    """第 6 步：对「>3%」做情景演算。无可投资金时返回 ``None``。"""
    investable = allocation.investable_cents
    if investable <= 0:
        return None

    weighted = sum(
        float(slice.amount_cents) * SCENARIO_ANNUAL_PCT.get(slice.category, 0.0)
        for slice in allocation.recommended
    ) / investable
    net = round(weighted - SCENARIO_COST_PCT, 2)

    caveats = [
        "保守 2.0% / 稳健 3.5% / 增长 6.0% 是假设情景并扣 0.5% 成本，不是收益预测或承诺。",
        "实际收益会波动，可能出现亏损；本情景不代表未来表现。",
    ]
    if strategy.goal_conservative:
        caveats.append("已按历史经验采用更保守的口径评估。")

    return ScenarioCheck(
        goal=(profile.goal if profile and profile.goal else "") or "",
        horizon_months=profile.horizon_months if profile else None,
        weighted_annual_pct=round(weighted, 2),
        annual_cost_pct=SCENARIO_COST_PCT,
        net_annual_pct=net,
        is_scenario=True,
        goal_reachable=net > GOAL_SCENARIO_THRESHOLD_PCT,
        goal_conservative=bool(strategy.goal_conservative),
        caveats=caveats,
        sources=[f"情景假设：{k} {v}%" for k, v in SCENARIO_ANNUAL_PCT.items()],
    )


# --------------------------------------------------------------------------- #
# 组装：build_plan
# --------------------------------------------------------------------------- #
def _collect_warnings(
    inputs: PlanInputs,
    cashflow: CashflowSummary,
    budget: BudgetBaseline,
    reserve: ReservePlan,
    risk: RiskAssessment,
    allocation: AllocationProposal,
    scenario: ScenarioCheck | None,
    strategy: PlanStrategy,
) -> list[str]:
    out: list[str] = list(cashflow.warnings)
    if budget.negative_balance:
        out.append("必要支出与债务还款已超过收入；当前不安排增长配置")
    if cashflow.income_cents <= 0:
        pass  # 已有「缺少有效收入」提示
    elif cashflow.data_quality.months_covered == 0:
        out.append("账单样本不足，方案仅作初步参考")
    if reserve.high_interest_debt:
        out.append("债务月供占收入较高，请优先核对并偿还高成本债务")
    if strategy.debt_priority:
        out.append("经验提示：优先处理高息债务，暂缓新增投资")
    if allocation.investable_cents <= 0 and reserve.gap_cents > 0:
        out.append("当前月度结余优先用于补足预备金，暂不新增投资")
    elif allocation.investable_cents <= 0 and not budget.negative_balance:
        out.append("当前没有可配置资金，先以保障与储蓄为主")
    if scenario is not None and not scenario.goal_reachable:
        out.append("在当前示例情景下未达到 >3% 目标；不会为凑目标增加风险")
    if risk.missing:
        out.append("部分风险信息缺失，已按保守档处理并标注")
    return list(dict.fromkeys(out))


def _collect_sources(inputs: PlanInputs, lessons_applied: list[str]) -> list[str]:
    out: list[str] = []
    if inputs.profile is not None:
        out.append("用户画像")
    if inputs.snapshot is not None or inputs.history:
        out.append("账单/本月实况")
    if inputs.briefing is not None:
        label = f"金融简报 {inputs.briefing.as_of}" if inputs.briefing.as_of else "金融简报"
        headline = inputs.briefing.analysis.headline if inputs.briefing.analysis else ""
        out.append(f"{label}：{headline}" if headline else label)
        for ref in inputs.briefing.sources[:5]:
            out.append(f"{ref.title} — {ref.url}")
    if lessons_applied:
        out.append(f"经验积累（{len(lessons_applied)} 条）")
    return out


def build_plan(inputs: PlanInputs, strategy: PlanStrategy | None = None) -> Plan:
    """跑完第 1-6 步，产出规范 :class:`Plan`（金额全部由代码写）。"""
    base_strategy = strategy or PlanStrategy()
    effects = apply_lessons(inputs.experience)
    effective = merge_strategy(base_strategy, effects)
    return _build_with_effective_strategy(inputs, effective, effects)


def _build_with_effective_strategy(
    inputs: PlanInputs, effective: PlanStrategy, effects: ExperienceEffects
) -> Plan:
    """按已生效策略计算；复核复用此入口，不再叠加经验。"""

    cashflow = summarize_cashflow(inputs)
    budget = allocate_budget(cashflow, effective)
    reserve = calculate_reserve(cashflow, budget, inputs, effective)
    risk = assess_risk(inputs.profile, cashflow, effective)
    allocation = propose_allocation(reserve, risk)
    scenario = check_goal(allocation, inputs.profile, effective)

    warnings = _collect_warnings(inputs, cashflow, budget, reserve, risk, allocation, scenario, effective)
    sources = _collect_sources(inputs, effects.applied_ids)

    trace = [
        f"数据核验：月均收入 {format_yuan(cashflow.income_cents)}、必要 {format_yuan(cashflow.necessary_cents)}、"
        f"可选 {format_yuan(cashflow.optional_cents)}、债务 {format_yuan(cashflow.debt_cents)}"
        f"（{cashflow.data_quality.notes}）",
        f"预算基线：可用 {format_yuan(budget.available_cents)}，可选上限取 min(实际, 可用, 收入×"
        f"{budget.wants_ratio_pct:.0f}%) = {format_yuan(budget.wants_cents)}，可储蓄 {format_yuan(budget.savings_cents)}",
        f"预备金：目标 {reserve.months} 个月 = {format_yuan(reserve.target_cents)}，缺口 "
        f"{format_yuan(reserve.gap_cents)}，本月补足 {format_yuan(reserve.monthly_cents)}，"
        f"可投资 {format_yuan(reserve.investable_cents)}",
        f"风险约束：{risk.rationale}",
    ]
    if allocation.recommended:
        parts = "、".join(f"{s.category} {format_yuan(s.amount_cents)}" for s in allocation.recommended)
        trace.append(f"候选配置：{parts}")
    if scenario is not None:
        trace.append(
            f"目标检验：情景加权年化 {scenario.weighted_annual_pct:.2f}% − 成本 {scenario.annual_cost_pct:.2f}% "
            f"= {scenario.net_annual_pct:.2f}%（{'可达' if scenario.goal_reachable else '未达'} >3%）"
        )

    return Plan(
        period=inputs.period,
        strategy=effective,
        cashflow=cashflow,
        budget=budget,
        reserve=reserve,
        risk=risk,
        allocation=allocation,
        scenario=scenario,
        data_quality=cashflow.data_quality,
        warnings=warnings,
        sources=sources,
        lessons_applied=effects.applied_ids,
        trace=trace,
    )


# --------------------------------------------------------------------------- #
# 第 7 步：独立复核
# --------------------------------------------------------------------------- #
def _mismatch(issues: list[ValidationIssue], name: str, got: int, want: int) -> None:
    if got != want:
        issues.append(
            ValidationIssue(
                code="recompute_mismatch",
                message=f"{name} 与重算不一致：方案 {got} vs 重算 {want}",
            )
        )


def validate_plan(plan: Plan, inputs: PlanInputs) -> ValidationReport:
    if plan.schema_version == 2:
        from .wallet import Wallet, WalletProposal, validate_wallets, compose_wallet_plan
        try:
            candidate = WalletProposal(headline=plan.headline, wallets=[Wallet.model_validate({
                k: v for k, v in row.items() if k in Wallet.model_fields}) for row in plan.wallets], caveats=plan.warnings)
            report = validate_wallets(candidate, inputs)
            if report["ok"]:
                rebuilt = compose_wallet_plan(candidate, inputs)
                if (plan.budget.model_dump() != rebuilt.budget.model_dump() or plan.wallets != rebuilt.wallets
                        or plan.allocation.model_dump() != rebuilt.allocation.model_dump() or plan.funding != rebuilt.funding):
                    report["errors"].append("展示汇总与钱包金额不一致")
            return ValidationReport(ok=not report["errors"], issues=[ValidationIssue(code="wallet_validation", message=e) for e in report["errors"]])
        except (ValueError, TypeError, KeyError) as exc:
            return ValidationReport(ok=False, issues=[ValidationIssue(code="wallet_schema", message=str(exc))])

    """第 7 步：独立复核——按方案自身的策略重算一遍，逐项对账并检查硬约束。"""
    ref = _build_with_effective_strategy(inputs, plan.strategy, apply_lessons(inputs.experience))
    issues: list[ValidationIssue] = []

    _mismatch(issues, "月均收入", plan.cashflow.income_cents, ref.cashflow.income_cents)
    _mismatch(issues, "必要支出", plan.cashflow.necessary_cents, ref.cashflow.necessary_cents)
    _mismatch(issues, "可选支出", plan.cashflow.optional_cents, ref.cashflow.optional_cents)
    _mismatch(issues, "债务还款", plan.cashflow.debt_cents, ref.cashflow.debt_cents)
    _mismatch(issues, "可用余额", plan.budget.available_cents, ref.budget.available_cents)
    _mismatch(issues, "可选预算", plan.budget.wants_cents, ref.budget.wants_cents)
    _mismatch(issues, "可储蓄", plan.budget.savings_cents, ref.budget.savings_cents)
    _mismatch(issues, "预备金目标", plan.reserve.target_cents, ref.reserve.target_cents)
    _mismatch(issues, "预备金缺口", plan.reserve.gap_cents, ref.reserve.gap_cents)
    _mismatch(issues, "预备金月补足", plan.reserve.monthly_cents, ref.reserve.monthly_cents)
    _mismatch(issues, "可投资余额", plan.reserve.investable_cents, ref.reserve.investable_cents)

    allocated = {s.category: s.amount_cents for s in plan.allocation.recommended}
    ref_allocated = {s.category: s.amount_cents for s in ref.allocation.recommended}
    for category in ("保守储蓄", "稳健配置", "增长配置"):
        _mismatch(issues, f"配置-{category}", allocated.get(category, 0), ref_allocated.get(category, 0))

    # 金额非负
    for name, value in (
        ("收入", plan.cashflow.income_cents),
        ("必要支出", plan.cashflow.necessary_cents),
        ("债务还款", plan.cashflow.debt_cents),
        ("可选预算", plan.budget.wants_cents),
        ("可储蓄", plan.budget.savings_cents),
        ("可投资余额", plan.reserve.investable_cents),
    ):
        if value < 0:
            issues.append(ValidationIssue(code="negative_amount", message=f"{name}不得为负：{value}"))

    income = plan.cashflow.income_cents
    if income > 0 and (plan.budget.necessary_cents + plan.budget.debt_cents + plan.budget.wants_cents) > income:
        issues.append(ValidationIssue(code="budget_over_income", message="必要 + 债务 + 可选 超过收入"))

    total_alloc = sum(s.amount_cents for s in plan.allocation.recommended)
    investable = plan.reserve.investable_cents
    if total_alloc != investable:
        issues.append(
            ValidationIssue(code="allocation_unbalanced", message=f"三类配置合计 {total_alloc} ≠ 可投资 {investable}")
        )

    growth = allocated.get("增长配置", 0)
    cap = plan.risk.growth_cap_pct
    if investable > 0 and growth * 100 > investable * cap:
        issues.append(ValidationIssue(code="growth_over_cap", message="增长配置超过风险上限"))

    if plan.risk.level != _more_conservative(
        _more_conservative(plan.risk.willingness, plan.risk.horizon), plan.risk.ability
    ) and _level_index(plan.risk.level) > _level_index(
        _more_conservative(
            _more_conservative(plan.risk.willingness, plan.risk.horizon), plan.risk.ability
        )
    ):
        issues.append(ValidationIssue(code="risk_level_inconsistent", message="风险档高于能力/意愿/期限所允许的档位"))

    if cap != GROWTH_CAP_PCT.get(plan.risk.level, 0):
        issues.append(ValidationIssue(code="growth_cap_inconsistent", message="增长上限与风险档不匹配"))

    if plan.reserve.monthly_cents + investable != plan.budget.savings_cents:
        issues.append(ValidationIssue(code="savings_unbalanced", message="预备金补足 + 可投资 ≠ 可储蓄"))

    if plan.data_quality.months_covered == 0:
        issues.append(
            ValidationIssue(
                code="insufficient_data",
                message="没有可用的完整月份账单，不能出具精确方案",
                severity="error",
            )
        )

    recomputed = {
        "income_cents": ref.cashflow.income_cents,
        "necessary_cents": ref.cashflow.necessary_cents,
        "optional_cents": ref.cashflow.optional_cents,
        "savings_cents": ref.budget.savings_cents,
        "reserve_target_cents": ref.reserve.target_cents,
        "investable_cents": ref.reserve.investable_cents,
        "growth_cents": ref_allocated.get("增长配置", 0),
    }
    ok = not any(issue.severity == "error" for issue in issues)
    return ValidationReport(ok=ok, issues=issues, recomputed=recomputed)
