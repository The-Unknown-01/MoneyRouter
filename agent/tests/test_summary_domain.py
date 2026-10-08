"""月度复盘领域：差异表、候选经验、按 kind 归并（防连乘）。"""

from __future__ import annotations

from moneyrouter_agent.domain.experience import ExperiencePack, Lesson, apply_lessons
from moneyrouter_agent.domain.month import (
    Baseline,
    CategorySpend,
    FundAllocation,
    GoalAlignment,
    HoldingReturn,
    IncomeFact,
    InvestmentSnapshot,
    MonthSnapshot,
    recompute,
)
from moneyrouter_agent.domain.plan import (
    AllocationProposal,
    AllocationSlice,
    BudgetBaseline,
    Plan,
    PlanStrategy,
    ReservePlan,
)
from moneyrouter_agent.domain.summary import (
    PlanActualDiff,
    build_candidates,
    build_diff,
    collect_lesson_statements,
    compute_strength,
    layer_of,
    merge_lessons,
    merge_pack,
    over_ratio,
)

PERIOD = "2026-09"


def _snapshot(**over) -> MonthSnapshot:
    base: dict = dict(
        period=PERIOD,
        coverage_complete=True,
        income=IncomeFact(amount_cents=1_000_000),
        categories=[
            CategorySpend(category="餐饮", amount_cents=300_000),
            CategorySpend(category="娱乐社交", amount_cents=400_000),
        ],
        allocation=FundAllocation(non_invested_cents=100_000, invested_cents=200_000),
    )
    base.update(over)
    # 派生字段（支出合计、收益合计）必须由代码算过，否则差异表拿不到值
    return recompute(MonthSnapshot(**base))


def _kinds(snapshot: MonthSnapshot, plan: Plan | None) -> list[str]:
    return [item.kind for item in build_candidates(build_diff(snapshot, plan=plan))]


def _plan(**over) -> Plan:
    base: dict = dict(
        period=PERIOD,
        budget=BudgetBaseline(
            income_cents=1_000_000,
            necessary_cents=300_000,
            debt_cents=0,
            wants_cents=400_000,
            savings_cents=300_000,
        ),
        reserve=ReservePlan(
            target_cents=1_800_000, existing_cents=900_000, gap_cents=900_000, investable_cents=100_000
        ),
        allocation=AllocationProposal(
            investable_cents=100_000,
            recommended=[AllocationSlice(category="增长配置", amount_cents=40_000)],
        ),
    )
    base.update(over)
    return Plan(**base)


# --------------------------------------------------------------------------- #
# 差异表
# --------------------------------------------------------------------------- #
def test_plan_unavailable_leaves_planned_none_and_says_so():
    diff = build_diff(_snapshot(), plan=None)

    assert diff.plan_available is False
    assert diff.reserve_gap_cents is None
    assert all(item.planned_cents is None for item in diff.layers)
    assert all(item.delta_cents is None for item in diff.layers)
    assert layer_of(diff, "wants").actual_cents == 400_000
    assert any("没有可对照的方案" in note for note in diff.notes)


def test_other_month_plan_cannot_generate_spurious_budget_lessons():
    diff = build_diff(_snapshot(), plan=_plan(period="2026-10"))
    assert not diff.plan_available
    assert all(item.planned_cents is None and item.delta_cents is None for item in diff.layers)
    assert not any(item.kind == "wants_down" for item in build_candidates(diff))
    assert any("已排除" in note for note in diff.notes)


def test_zero_budget_is_known_and_compared():
    plan = _plan(budget=BudgetBaseline(income_cents=1_000_000, necessary_cents=300_000,
                                     wants_cents=0, savings_cents=700_000))
    diff = build_diff(_snapshot(), plan=plan)
    assert layer_of(diff, "wants").planned_cents == 0
    assert layer_of(diff, "wants").delta_cents == 400_000


def test_layers_compare_same_caliber_only():
    diff = build_diff(_snapshot(), plan=_plan())

    assert diff.plan_available is True
    assert layer_of(diff, "income").delta_cents == 0
    wants = layer_of(diff, "wants")
    assert wants.planned_cents == 400_000 and wants.actual_cents == 400_000 and wants.delta_cents == 0
    necessary = layer_of(diff, "necessary")
    assert necessary.planned_cents == 300_000 and necessary.actual_cents == 300_000
    savings = layer_of(diff, "savings")
    assert savings.actual_cents == 300_000  # 1000000 − 700000
    assert layer_of(diff, "growth").planned_cents == 40_000


def test_reserve_and_investable_are_facts_not_deltas():
    diff = build_diff(_snapshot(), plan=_plan())

    assert diff.reserve_target_cents == 1_800_000
    assert diff.reserve_gap_cents == 900_000
    assert diff.reserve_progress_pct == 50.0
    assert diff.investable_planned_cents == 100_000
    assert diff.invested_actual_cents == 200_000
    assert diff.non_invested_actual_cents == 100_000
    assert all(item.layer != "reserve" for item in diff.layers)
    assert any("口径不同" in note for note in diff.notes)


def test_mismatched_plan_period_is_flagged():
    diff = build_diff(_snapshot(), plan=_plan(period="2026-08"))
    assert any("不是同一个月" in note for note in diff.notes)


def test_zero_is_legal_in_category_diff():
    snapshot = _snapshot(
        categories=[
            CategorySpend(category="餐饮", amount_cents=300_000),
            CategorySpend(category="娱乐社交", amount_cents=0),
        ]
    )
    plan = _plan(strategy=PlanStrategy(category_cap_cents={"娱乐社交": 100_000}))
    diff = build_diff(snapshot, plan=plan)

    row = next(item for item in diff.categories if item.category == "娱乐社交")
    assert row.actual_cents == 0  # 0 是合法值，不是"没了解到"
    assert row.delta_cents == -100_000
    assert diff.budget_available is True


def _with(snapshot: MonthSnapshot, **over) -> MonthSnapshot:
    """在**重算之后**再覆盖字段——用于 baselines / goal_alignment 这类会被 recompute 重写的派生字段。"""
    return snapshot.model_copy(update=over)


def test_category_budget_falls_back_to_snapshot_baselines():
    snapshot = _with(
        _snapshot(),
        baselines=[Baseline(metric="budget", category="娱乐社交", amount_cents=200_000)],
    )
    diff = build_diff(snapshot, plan=None)

    row = next(item for item in diff.categories if item.category == "娱乐社交")
    assert row.planned_cents == 200_000
    assert diff.budget_available is True


def test_pct_is_none_when_plan_is_zero_or_missing():
    snapshot = _snapshot(categories=[CategorySpend(category="娱乐社交", amount_cents=1_000)])
    plan = _plan(budget=BudgetBaseline(income_cents=1_000_000, wants_cents=0, savings_cents=0))
    diff = build_diff(snapshot, plan=plan)

    assert layer_of(diff, "wants").delta_pct is None


# --------------------------------------------------------------------------- #
# 强度公式（代码算）
# --------------------------------------------------------------------------- #
def test_strength_is_code_computed_and_clamped():
    assert compute_strength(None) == 0.4
    assert compute_strength(0.0) == 0.4
    assert compute_strength(0.25) == 0.55
    assert compute_strength(1.0) == 1.0
    assert compute_strength(5.0) == 1.0  # 钳住


def test_over_ratio_needs_a_usable_plan_value():
    diff = build_diff(_snapshot(), plan=None)
    assert over_ratio(diff, "wants") is None


# --------------------------------------------------------------------------- #
# 候选生成
# --------------------------------------------------------------------------- #
def test_candidates_ids_are_kind_period_stable():
    snapshot = _snapshot(categories=[CategorySpend(category="娱乐社交", amount_cents=500_000)])
    plan = _plan(budget=BudgetBaseline(income_cents=1_000_000, wants_cents=400_000, savings_cents=300_000))
    found = build_candidates(build_diff(snapshot, plan=plan))

    wants = [item for item in found if item.kind == "wants_down"]
    assert len(wants) == 1
    assert wants[0].id == f"wants_down:{PERIOD}"
    assert wants[0].from_period == PERIOD
    assert wants[0].strength == 0.55
    assert wants[0].statement == ""  # 结论留给模型/降级模板
    assert wants[0].evidence  # 依据由代码填（可含金额）


def test_no_candidate_when_nothing_is_over_plan():
    diff = build_diff(_snapshot(), plan=_plan())
    kinds = [item.kind for item in build_candidates(diff)]
    assert "wants_down" not in kinds


def test_custom_lesson_is_never_emitted():
    """`custom` 在 apply_lessons 里不改算法，却会被记进 lessons_applied —— 一律不产。"""
    snapshot = _snapshot()
    plan = _plan(reserve=ReservePlan(target_cents=1_000, existing_cents=1_000, gap_cents=0))
    kinds = [item.kind for item in build_candidates(build_diff(snapshot, plan=plan))]
    assert "custom" not in kinds


def test_reserve_priority_requires_both_gap_and_new_investment():
    no_new_money = _snapshot(
        allocation=FundAllocation(non_invested_cents=300_000, invested_cents=0)
    )
    with_gap = _plan(reserve=ReservePlan(gap_cents=500, investable_cents=100_000))
    without_gap = _plan(reserve=ReservePlan(gap_cents=0, investable_cents=100_000))

    assert "reserve_priority" in _kinds(_snapshot(), with_gap)
    # 预备金已补足：不该再提醒
    assert "reserve_priority" not in _kinds(_snapshot(), without_gap)
    # 没有新增投资：与"预备金优先"无关
    assert "reserve_priority" not in _kinds(no_new_money, with_gap)


def test_debt_priority_only_when_high_interest_debt():
    with_debt = _plan(reserve=ReservePlan(high_interest_debt=True))
    without = _plan()

    assert "debt_priority" in _kinds(_snapshot(), with_debt)
    assert "debt_priority" not in _kinds(_snapshot(), without)


def test_risk_conservative_only_on_meaningful_loss():
    # 用"预备金已补足"的方案把 reserve_priority 排除掉，只观察亏损这条
    neutral = _plan(reserve=ReservePlan(gap_cents=0, investable_cents=100_000))
    loss = _snapshot(
        investments=InvestmentSnapshot(
            has_investments=True,
            holdings=[HoldingReturn(name="某基金", month_return_cents=-200_000)],
        )
    )
    small = _snapshot(
        investments=InvestmentSnapshot(
            has_investments=True,
            holdings=[HoldingReturn(name="某基金", month_return_cents=-1_000)],
        )
    )

    assert "risk_conservative" in _kinds(loss, neutral)
    assert "risk_conservative" not in _kinds(small, neutral)


def test_goal_pace_follows_on_track_flag():
    lagging = _with(_snapshot(), goal_alignment=GoalAlignment(on_track=False, progress_pct=12.0))
    fine = _with(_snapshot(), goal_alignment=GoalAlignment(on_track=True))
    neutral = _plan(reserve=ReservePlan(gap_cents=0, investable_cents=100_000))

    assert "goal_pace" in _kinds(lagging, neutral)
    assert "goal_pace" not in _kinds(fine, neutral)


def test_plan_unavailable_restricts_candidate_kinds():
    kinds = _kinds(_snapshot(), None)

    # 需要计划值的一律不产
    assert "wants_down" not in kinds
    assert "reserve_priority" not in kinds
    assert "debt_priority" not in kinds


def test_candidates_are_empty_without_a_period():
    assert build_candidates(PlanActualDiff()) == []


# --------------------------------------------------------------------------- #
# 归并（核心回归：不连乘）
# --------------------------------------------------------------------------- #
def _wants_down(period: str, strength: float = 1.0) -> Lesson:
    return Lesson(
        id=f"wants_down:{period}",
        kind="wants_down",
        statement="可选支出高于计划，下月宜收紧。",
        strength=strength,
        from_period=period,
        evidence=["测试依据"],
    )


def test_merge_lessons_keeps_latest_per_kind():
    merged = merge_lessons([_wants_down("2026-08")], [_wants_down("2026-09", 0.5)])
    assert [item.id for item in merged] == ["wants_down:2026-09"]
    assert merged[0].strength == 0.5


def test_wants_down_does_not_multiply_across_periods():
    pack = merge_pack(None, [_wants_down("2026-08", 1.0)], period="2026-08")
    pack = merge_pack(pack, [_wants_down("2026-09", 1.0)], period="2026-09")

    assert len(pack.lessons) == 1
    effects = apply_lessons(pack)
    # 只缩一次：1 − 0.6×1.0 = 0.4；若按 id 累积会变成 0.16
    assert effects.wants_ratio_scale == 0.4


def test_merge_pack_bumps_version_and_tracks_latest_period():
    first = merge_pack(None, [_wants_down("2026-08")], period="2026-08")
    second = merge_pack(first, [_wants_down("2026-09")], period="2026-09")

    assert first.version == 1 and second.version == 2
    assert second.from_period == "2026-09"
    assert second.generated_at  # 代码填


def test_merge_pack_keeps_unrelated_kinds_together():
    pack = merge_pack(None, [_wants_down("2026-09")], period="2026-09")
    pack = merge_pack(
        pack,
        [Lesson(id="debt_priority:2026-09", kind="debt_priority", statement="债务优先。", from_period="2026-09")],
        period="2026-09",
    )

    assert [item.kind for item in pack.lessons] == ["wants_down", "debt_priority"]
    effects = apply_lessons(pack)
    assert effects.wants_ratio_scale == 0.4 and effects.debt_priority is True


def test_merge_pack_is_idempotent_for_the_same_input():
    once = merge_pack(None, [_wants_down("2026-09")], period="2026-09")
    twice = merge_pack(None, [_wants_down("2026-09")], period="2026-09")
    assert once.model_dump() == twice.model_dump()


def test_merge_pack_accepts_empty_input():
    pack = merge_pack(None, [], period="2026-09")
    assert isinstance(pack, ExperiencePack)
    assert pack.lessons == []


def test_collect_lesson_statements_skips_blank():
    lessons = [_wants_down("2026-09"), Lesson(id="x:1", kind="custom", statement="  ", from_period="2026-09")]
    assert collect_lesson_statements(lessons) == ["可选支出高于计划，下月宜收紧。"]
