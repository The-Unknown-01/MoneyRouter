"""月度复盘：把「计划 vs 实际」算成差异表，再把差异提炼成可复用的经验。

三件事，各有归属：

- :func:`build_diff`——**代码算**差异表。分类与目标的偏差**不重造**：直接复用
  ``MonthSnapshot.baselines`` 与 ``goal_alignment``（那些已经由 `month` 模块算好了）。
- :func:`build_candidates`——**代码产候选**：哪几类经验值得沉淀、强度多少，都由差异幅度决定。
  模型只负责给每条候选写一句结论（见 :class:`LessonDraft`），**不产候选、不写数字**。
- :func:`merge_pack`——把候选汇进经验包。**按 ``kind`` 归并**（每类只留最新一条），
  不是按 id 累积——因为 :func:`~moneyrouter_agent.domain.experience.apply_lessons` 对
  ``wants_down`` 是**连乘**的（``scale *= 1 - 0.6*strength``），跨月累积会叠成极端值。

口径诚实原则：能与计划真·同口径比较的只有 ``income / necessary / wants / savings``；
预备金与可投资余额的"计划"与"实际"含义不同，因此**不做成偏差**，只作为事实单列，
避免把两个不同口径的数字相减。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .experience import ExperiencePack, Lesson
from .money import format_yuan
from .month import MonthSnapshot, category_map
from .plan import NECESSARY_CATEGORIES, OPTIONAL_CATEGORIES
from .profile_delta import (
    EVENT_TYPES,
    EventEffect,
    ProfileDelta,
    ProfileEvent,
    ProfileFieldUpdate,
    close_event_id,
    make_event_id,
    validate_events,
)

LayerName = Literal["income", "necessary", "wants", "savings", "growth"]

LAYER_ORDER: tuple[str, ...] = ("income", "necessary", "wants", "savings", "growth")

LESSON_KIND_ORDER: tuple[str, ...] = (
    "wants_down",
    "risk_conservative",
    "reserve_priority",
    "debt_priority",
    "goal_pace",
    "custom",
)

# 强度下限：差异再小，也留一点微调余地；上限即满强度
STRENGTH_FLOOR = 0.4
# 投资亏损达到该金额（分）才值得沉淀成"更保守"的经验
INVEST_LOSS_MIN_CENTS = 100_000


class LayerDiff(BaseModel):
    """某一层「计划 vs 实际」的一行。**全部由代码算。**"""

    model_config = ConfigDict(extra="ignore")

    layer: LayerName = Field(description="资金分层名。")
    planned_cents: int | None = Field(default=None, description="计划值（分）；没有计划就留空，不补 0。")
    actual_cents: int | None = Field(default=None, description="实际值（分）；未了解到就留空。")
    delta_cents: int | None = Field(default=None, description="实际 − 计划（分）；任一项缺就留空。")
    delta_pct: float | None = Field(default=None, description="偏差百分比；计划缺或为 0 就留空。")


class CategoryDiff(BaseModel):
    """某一类支出的「预算 vs 实际」。"""

    model_config = ConfigDict(extra="ignore")

    category: str = Field(description="支出分类（固定枚举之一）。")
    planned_cents: int | None = Field(default=None, description="该类预算（分）；没有预算就留空。")
    actual_cents: int | None = Field(default=None, description="该类本月实际支出（分）；0 是合法值。")
    delta_cents: int | None = Field(default=None, description="实际 − 预算（分）；缺就留空。")
    delta_pct: float | None = Field(default=None, description="偏差百分比；预算缺或为 0 就留空。")


class PlanActualDiff(BaseModel):
    """本月的「计划 vs 实际」差异表。没有注入方案时如实标 ``plan_available=False``。"""

    model_config = ConfigDict(extra="ignore")

    period: str = Field(default="", description="对账月份 YYYY-MM。")
    plan_available: bool = Field(default=False, description="是否拿到了当月方案；缺则经验只限不依赖计划的类型。")
    plan_period: str = Field(default="", description="注入方案所属的期间，用于核对是不是同一个月。")
    layers: list[LayerDiff] = Field(default_factory=list, description="同口径可比的各层偏差，代码算。")
    categories: list[CategoryDiff] = Field(default_factory=list, description="分类级对比；没有分类预算时为空。")
    budget_available: bool = Field(default=False, description="是否有分类预算可比。")
    # —— 以下为"口径不同、不做偏差"的事实，供经验候选判断使用 ——
    reserve_target_cents: int | None = Field(default=None, description="预备金目标（分）。")
    reserve_existing_cents: int | None = Field(default=None, description="现有预备金（分）。")
    reserve_gap_cents: int | None = Field(default=None, description="预备金缺口（分）；0 表示已补足。")
    high_interest_debt: bool | None = Field(default=None, description="是否存在高息债务；未了解到留空。")
    investable_planned_cents: int | None = Field(default=None, description="计划的可投资余额（分）。")
    invested_actual_cents: int | None = Field(default=None, description="本月实际新投入（分）。")
    non_invested_actual_cents: int | None = Field(default=None, description="本月净留存、未投资的部分（分）。")
    reserve_progress_pct: float | None = Field(default=None, description="预备金进度（%），代码算。")
    goal_progress_pct: float | None = Field(default=None, description="目标进度（%），代码算。")
    goal_on_track: bool | None = Field(default=None, description="攒钱是否在计划轨道上，代码算。")
    investment_return_cents: int | None = Field(default=None, description="本月投资盈亏（分），亏损为负。")
    notes: list[str] = Field(default_factory=list, description="缺项与口径说明，代码填。")


class LessonDraft(BaseModel):
    """模型对一条**已有候选**的文本填充。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="候选的标识，原样回填，不要改写。")
    statement: str = Field(
        description="第三人称的结论，一句话：只写结论，不写过程、不引原话、不出现金额。"
    )


class EventDraft(BaseModel):
    """模型从本月实况里抽出的**一条画像变化**。

    ``id`` 与 ``from_period`` 一律由代码生成——模型只说"发生了什么"，不排号、不改日期，
    否则重跑同一个月会造出新条目、破坏幂等。
    """

    model_config = ConfigDict(extra="ignore")

    event_type: str = Field(description="事件类型，代码白名单：" + " / ".join(EVENT_TYPES) + "；不定就填 other。")
    statement: str = Field(
        description="第三人称的事实描述：本月发生了什么变化、影响是什么。只写事实，不写过程、不引原话。"
    )
    closes: str = Field(
        default="",
        description="若这件事是此前记录过、如今已结束（如康复、还清、搬回），填那条事件的标识；否则留空。",
    )
    field_updates: ProfileFieldUpdate = Field(
        default_factory=ProfileFieldUpdate, description="画像字段的新值；没有变化就全留空。"
    )
    effects: list[EventEffect] = Field(
        default_factory=list, description="对资金安排的影响（如某类支出需要放宽）；没有就留空。"
    )


class SummaryDraft(BaseModel):
    """模型在复盘环节的唯一可写面：叙述、给候选填结论、抽画像变化。"""

    model_config = ConfigDict(extra="ignore")

    headline: str = Field(default="", description="一句话总结这个月的计划执行情况。")
    sections: list[str] = Field(
        default_factory=list, description="复盘要点 3-5 条；复述系统给出的数字，不新增任何数值。"
    )
    lessons: list[LessonDraft] = Field(
        default_factory=list, description="给系统给出的每条候选各写一句结论；不要新增候选。"
    )
    events: list[EventDraft] = Field(
        default_factory=list,
        description="本月确实出现过的画像变化；没有就留空，不要为了凑数推测。",
    )
    notes: str = Field(default="", description="补充说明（要点，非过程叙述）。")


class MonthlySummary(BaseModel):
    """给用户看的月度复盘。数字全部由代码写，模型只写叙述。"""

    model_config = ConfigDict(extra="ignore")

    period: str = Field(default="", description="复盘月份 YYYY-MM。")
    generated_at: str = Field(default="", description="生成日期 YYYY-MM-DD，代码填。")
    headline: str = Field(default="", description="一句话结论。")
    sections: list[str] = Field(default_factory=list, description="复盘要点。")
    diff: PlanActualDiff = Field(default_factory=PlanActualDiff, description="差异表，代码算。")
    lessons: list[str] = Field(default_factory=list, description="本期沉淀的经验结论，代码汇总。")
    notes: str = Field(default="", description="补充说明。")
    sources: list[str] = Field(default_factory=list, description="来源标签，代码填。")
    degraded: bool = Field(default=False, description="是否走了规则降级。")


# --------------------------------------------------------------------------- #
# 差异表（代码）
# --------------------------------------------------------------------------- #
def _pct(delta: int, base: int | None) -> float | None:
    if base is None or base == 0:
        return None
    return round(delta / base * 100.0, 2)


def _layer(layer: str, planned: int | None, actual: int | None) -> LayerDiff:
    delta = (actual - planned) if (planned is not None and actual is not None) else None
    return LayerDiff(
        layer=layer,  # type: ignore[arg-type]
        planned_cents=planned,
        actual_cents=actual,
        delta_cents=delta,
        delta_pct=_pct(delta, planned) if delta is not None else None,
    )


def _category_totals(snapshot: MonthSnapshot) -> dict[str, int]:
    return category_map(snapshot.categories)


def build_diff(snapshot: MonthSnapshot, *, plan: Any | None = None, period: str = "") -> PlanActualDiff:
    """算「计划 vs 实际」。``plan`` 可以是方案对象，也可以是 ``None``（未注入）。"""
    target = period or snapshot.period or ""
    diff = PlanActualDiff(period=target, plan_period=getattr(plan, "period", "") or "")
    if plan is not None and diff.plan_period and target and diff.plan_period != target:
        diff.notes.append(f"注入的方案属于 {diff.plan_period}，与本期的 {target} 不是同一个月，已排除该方案。")
        plan = None

    actual_by_category = _category_totals(snapshot)
    necessary_actual = sum(
        amount for category, amount in actual_by_category.items() if category in NECESSARY_CATEGORIES
    )
    optional_actual = sum(
        amount for category, amount in actual_by_category.items() if category in OPTIONAL_CATEGORIES
    )
    income_actual = snapshot.income.amount_cents if snapshot.income is not None else None

    # —— 口径不同、不做偏差的事实 ——
    allocation = snapshot.allocation
    diff.invested_actual_cents = allocation.invested_cents
    diff.non_invested_actual_cents = allocation.non_invested_cents
    investments = snapshot.investments
    diff.investment_return_cents = investments.month_return_cents

    goal = snapshot.goal_alignment
    diff.goal_progress_pct = goal.progress_pct
    diff.goal_on_track = goal.on_track

    if plan is None:
        diff.plan_available = False
        diff.layers = [
            _layer("income", None, income_actual),
            _layer("necessary", None, necessary_actual),
            _layer("wants", None, optional_actual),
            _layer("savings", None, snapshot.balance_cents),
        ]
        diff.notes.append("本月没有可对照的方案，只给出实际值，不做偏差判断。")
    else:
        diff.plan_available = True
        budget = plan.budget
        reserve = plan.reserve
        diff.layers = [
            _layer("income", budget.income_cents, income_actual),
            _layer("necessary", budget.necessary_cents, necessary_actual),
            _layer("wants", budget.wants_cents, optional_actual),
            _layer("savings", budget.savings_cents, snapshot.balance_cents),
        ]
        growth_planned = next(
            (slice_.amount_cents for slice_ in plan.allocation.recommended if slice_.category == "增长配置"),
            None,
        )
        if growth_planned is not None:
            diff.layers.append(_layer("growth", growth_planned, None))

        diff.reserve_target_cents = reserve.target_cents or None
        diff.reserve_existing_cents = reserve.existing_cents
        diff.reserve_gap_cents = reserve.gap_cents
        diff.high_interest_debt = bool(reserve.high_interest_debt)
        diff.investable_planned_cents = reserve.investable_cents
        if diff.reserve_target_cents:
            existing = reserve.existing_cents or 0
            diff.reserve_progress_pct = round(
                min(1.0, existing / diff.reserve_target_cents) * 100.0, 2
            )
        diff.notes.append(
            "预备金与可投资余额的「计划」与「实际」口径不同，只作为事实列出，不算偏差。"
        )
        if diff.plan_period and target and diff.plan_period != target:
            diff.notes.append(
                f"注入的方案属于 {diff.plan_period}，与本期的 {target} 不是同一个月。"
            )

    # —— 分类级：优先用方案里的分类上限，其次用快照里已经算好的预算基线 ——
    planned_by_category: dict[str, int] = {}
    if plan is not None:
        planned_by_category = {
            str(k): int(v)
            for k, v in (plan.strategy.category_cap_cents or {}).items()
            if v is not None
        }
    if not planned_by_category:
        planned_by_category = {
            baseline.category: baseline.amount_cents
            for baseline in snapshot.baselines
            if baseline.metric == "budget" and baseline.category
        }
    diff.budget_available = bool(planned_by_category)
    categories = sorted(set(planned_by_category) | set(actual_by_category))
    for category in categories:
        planned = planned_by_category.get(category)
        actual = actual_by_category.get(category)
        delta = (actual - planned) if (planned is not None and actual is not None) else None
        diff.categories.append(
            CategoryDiff(
                category=category,
                planned_cents=planned,
                actual_cents=actual,
                delta_cents=delta,
                delta_pct=_pct(delta, planned) if delta is not None else None,
            )
        )
    if not diff.budget_available:
        diff.notes.append("没有分类级预算，无法逐类比较。")

    return diff


def layer_of(diff: PlanActualDiff, name: str) -> LayerDiff | None:
    """按名字取一层。"""
    return next((item for item in diff.layers if item.layer == name), None)


def over_ratio(diff: PlanActualDiff, layer: str) -> float | None:
    """某一层超出计划的比例（0 表示没超、None 表示算不出）。"""
    item = layer_of(diff, layer)
    if item is None or item.planned_cents in (None, 0) or item.actual_cents is None:
        return None
    exceed = item.actual_cents - int(item.planned_cents)
    return max(0.0, exceed / int(item.planned_cents))


# --------------------------------------------------------------------------- #
# 候选生成（代码）
# --------------------------------------------------------------------------- #
def compute_strength(ratio: float | None) -> float:
    """把"超出计划的比例"折成软调整强度——**由代码算**，模型不写这个数。"""
    if ratio is None or ratio <= 0:
        return STRENGTH_FLOOR
    scaled = STRENGTH_FLOOR + (1.0 - STRENGTH_FLOOR) * min(1.0, float(ratio))
    return round(max(0.0, min(1.0, scaled)), 2)


def _candidate(kind: str, period: str, strength: float, evidence: list[str]) -> Lesson:
    return Lesson(
        id=f"{kind}:{period}",
        kind=kind,  # type: ignore[arg-type]
        statement="",  # 留给模型（或降级模板）填
        strength=strength,
        from_period=period,
        evidence=evidence,
    )


def build_candidates(diff: PlanActualDiff, *, period: str = "") -> list[Lesson]:
    """按差异表产出**候选经验**。没有依据的一律不产，不硬凑。

    注意 `custom` **永不产出**：它在 `apply_lessons` 里不改任何算法，却会被记进
    `Plan.lessons_applied` 与来源标签，属于误导性泄漏。分类级影响只留在画像事件的 effects 里。
    """
    target = period or diff.period
    if not target:
        return []

    found: list[Lesson] = []

    # 可选支出超出计划 → 收紧可选上限
    ratio = over_ratio(diff, "wants")
    if ratio is not None and ratio > 0:
        item = layer_of(diff, "wants")
        found.append(
            _candidate(
                "wants_down",
                target,
                compute_strength(ratio),
                [
                    f"计划可选支出 {format_yuan(int(item.planned_cents))} 元，"
                    f"实际 {format_yuan(int(item.actual_cents))} 元",
                    f"超出 {format_yuan(int(item.actual_cents) - int(item.planned_cents))} 元"
                    f"（{item.delta_pct}%）",
                ],
            )
        )

    # 本月投资明显亏损 → 风险档更保守
    month_return = diff.investment_return_cents
    if month_return is not None and month_return <= -INVEST_LOSS_MIN_CENTS:
        found.append(
            _candidate(
                "risk_conservative",
                target,
                1.0,
                [f"本月投资亏损 {format_yuan(abs(month_return))} 元"],
            )
        )

    # 预备金还有缺口，却把结余投了出去 → 预备金优先
    if (
        diff.reserve_gap_cents
        and diff.reserve_gap_cents > 0
        and diff.invested_actual_cents
        and diff.invested_actual_cents > 0
    ):
        found.append(
            _candidate(
                "reserve_priority",
                target,
                1.0,
                [
                    f"预备金尚缺 {format_yuan(diff.reserve_gap_cents)} 元",
                    f"本月仍新投入 {format_yuan(diff.invested_actual_cents)} 元",
                ],
            )
        )

    # 有高息债务 → 债务优先
    if diff.high_interest_debt is True:
        found.append(
            _candidate(
                "debt_priority",
                target,
                1.0,
                ["本月存在高息债务"],
            )
        )

    # 攒钱进度落后 → 目标口径更保守
    if diff.goal_on_track is False:
        evidence = ["本月攒钱进度落后于计划"]
        if diff.goal_progress_pct is not None:
            evidence.append(f"目标进度 {diff.goal_progress_pct}%")
        found.append(_candidate("goal_pace", target, 1.0, evidence))

    return found


# --------------------------------------------------------------------------- #
# 汇入经验包（代码）
# --------------------------------------------------------------------------- #
def merge_lessons(
    existing: list[Lesson] | None, incoming: list[Lesson] | None
) -> list[Lesson]:
    """**按 ``kind`` 归并**：每类只留 ``from_period`` 最新的一条（同期则以靠后的为准）。

    这是为了避免 ``apply_lessons`` 里 ``wants_down`` 的连乘效应——
    两条 0.4 强度的教训叠起来会变成 0.16，比任何单月结论都激进得多。
    """
    latest: dict[str, Lesson] = {}
    for lesson in [*(existing or []), *(incoming or [])]:
        current = latest.get(lesson.kind)
        if current is None or (lesson.from_period, lesson.id) >= (current.from_period, current.id):
            latest[lesson.kind] = lesson
    return sorted(
        latest.values(),
        key=lambda item: (
            LESSON_KIND_ORDER.index(item.kind) if item.kind in LESSON_KIND_ORDER else len(LESSON_KIND_ORDER),
            item.id,
        ),
    )


def merge_pack(
    existing: ExperiencePack | None,
    incoming: list[Lesson] | None,
    *,
    period: str = "",
    notes: str = "",
) -> ExperiencePack:
    """把新教训汇进经验包：``version`` 递增、按 kind 归并、``from_period`` 取最新。"""
    merged = merge_lessons(
        existing.lessons if existing is not None else [], incoming
    )
    periods = [item.from_period for item in merged if item.from_period]
    # 第一次生成就是第 1 版；后续每次汇入 +1
    version = 1 if existing is None else int(existing.version or 0) + 1
    return ExperiencePack(
        version=version,
        generated_at=date.today().isoformat(),
        from_period=max(periods) if periods else period,
        lessons=merged,
        notes=notes or (existing.notes if existing is not None else ""),
    )


def collect_lesson_statements(lessons: list[Lesson] | None) -> list[str]:
    """汇总本期经验的结论句（供月度总结展示）。"""
    return [item.statement.strip() for item in (lessons or []) if item.statement.strip()]


# --------------------------------------------------------------------------- #
# 从模型草稿生成画像事件（代码排号、校验、定日期）
# --------------------------------------------------------------------------- #
def build_profile_delta(
    drafts: list[EventDraft] | None,
    *,
    period: str,
    existing_events: list[ProfileEvent] | None = None,
    evidence: list[str] | None = None,
    notes: str = "",
) -> tuple[ProfileDelta, list[str]]:
    """把模型的草稿消息变成规范的画像事件。

    代码负责：**校验类型白名单**、**生成 id**、**定发生月份（一律本期）**、**填依据**、
    校验关闭目标确实存在。模型不排号、不改日期，所以重跑同一期间只会得到同样的 id（幂等）。

    返回 ``(本期增量, 告警清单)``；不合法的草稿被丢弃并说明原因，不静默吞掉。
    """
    known = {item.id for item in (existing_events or [])}
    warnings: list[str] = []
    events: list[ProfileEvent] = []
    taken = set(known)

    for index, draft in enumerate(drafts or [], start=1):
        label = f"第 {index} 条变化"
        event_type = (draft.event_type or "").strip()
        if event_type not in EVENT_TYPES:
            warnings.append(f"{label}：事件类型「{draft.event_type}」不在允许范围内，已忽略。")
            continue
        statement = (draft.statement or "").strip()
        if not statement:
            warnings.append(f"{label}：没有写事实描述，已忽略。")
            continue

        target = (draft.closes or "").strip()
        if target:
            if target not in known:
                warnings.append(f"{label}：要关闭的 {target} 不存在，已忽略。")
                continue
            event_id = close_event_id(target)
            if event_id in taken:
                continue  # 已经关过了，幂等
        else:
            event_id = make_event_id(event_type, period, taken)

        taken.add(event_id)
        events.append(
            ProfileEvent(
                id=event_id,
                event_type=event_type,  # type: ignore[arg-type]
                statement=statement,
                from_period=period,
                field_updates=draft.field_updates,
                effects=list(draft.effects),
                closes=target,
                evidence=list(evidence or []),
            )
        )
        if target:
            # 同一批里后面的草稿不能再关它
            known = known - {target}

    problems = validate_events([*(existing_events or []), *events])
    if problems:
        warnings.extend(problems)
    return ProfileDelta(period=period, events=events, notes=notes), warnings
