"""方案领域纯函数测试：七步边界、数据质量与独立复核。"""

from __future__ import annotations

import pytest

from moneyrouter_agent.domain.month import CategorySpend, IncomeFact, MonthSnapshot
from moneyrouter_agent.domain.plan import (
    GROWTH_CAP_PCT,
    PlanInputs,
    PlanStrategy,
    build_plan,
    validate_plan,
)
from moneyrouter_agent.domain.profile import Profile


def _snapshot(
    *,
    period: str = "2026-09",
    income_cents: int | None = 1_200_000,
    necessary_cents: int = 200_000,
    optional_cents: int = 100_000,
) -> MonthSnapshot:
    categories = [
        CategorySpend(category="居住", amount_cents=necessary_cents, source="file"),
        CategorySpend(category="购物", amount_cents=optional_cents, source="file"),
    ]
    income = None if income_cents is None else IncomeFact(amount_cents=income_cents, source="file")
    return MonthSnapshot(period=period, income=income, categories=categories)


def _profile(**overrides) -> Profile:
    base = dict(
        income_cents=1_200_000,
        income_stable=True,
        family_load=False,
        reserve_cents=0,
        horizon_months=36,
        max_loss_pct=10,
        experience="some",
        goal="攒钱",
    )
    base.update(overrides)
    return Profile(**base)


def _inputs(snapshot: MonthSnapshot | None = None, **overrides) -> PlanInputs:
    base = dict(profile=_profile(), snapshot=snapshot if snapshot is not None else _snapshot())
    base.update(overrides)
    return PlanInputs(**base)


# --------------------------------------------------------------------------- #
# 第 1 步：数据核验 / 数据质量
# --------------------------------------------------------------------------- #
def test_zero_income_does_not_crash() -> None:
    plan = build_plan(_inputs(_snapshot(income_cents=0)))
    assert plan.cashflow.income_cents == 0
    assert plan.budget.wants_cents == 0
    assert plan.budget.savings_cents == 0
    assert plan.reserve.investable_cents == 0
    assert plan.allocation.recommended == []
    assert plan.scenario is None
    assert plan.data_quality.sufficient is False


def test_no_data_marks_low_quality_and_validation_error() -> None:
    plan = build_plan(PlanInputs(profile=_profile(), period="2026-10"))
    assert plan.data_quality.months_covered == 0
    report = validate_plan(plan, PlanInputs(profile=_profile(), period="2026-10"))
    assert report.ok is False
    assert any(issue.code == "insufficient_data" for issue in report.issues)


def test_income_conflict_detected() -> None:
    inputs = _inputs(_snapshot(income_cents=1_200_000), profile=_profile(income_cents=500_000))
    plan = build_plan(inputs)
    assert plan.data_quality.has_conflict is True
    assert plan.data_quality.sufficient is False


# --------------------------------------------------------------------------- #
# 第 2 步：预算基线
# --------------------------------------------------------------------------- #
def test_negative_balance_blocks_new_investment() -> None:
    snap = _snapshot(income_cents=100_000, necessary_cents=200_000, optional_cents=0)
    plan = build_plan(_inputs(snap))
    assert plan.budget.negative_balance is True
    assert plan.budget.savings_cents == 0
    assert plan.reserve.investable_cents == 0
    assert any("超过收入" in w for w in plan.warnings)


def test_wants_is_min_of_actual_available_ratio_cap() -> None:
    # 收入 10000 元、必要 2000 元、可选实际 5000 元 → 上限取收入 30% = 3000 元
    snap = _snapshot(income_cents=1_000_000, necessary_cents=200_000, optional_cents=500_000)
    plan = build_plan(_inputs(snap))
    assert plan.budget.wants_cents == 300_000
    assert plan.budget.savings_cents == 500_000  # available 8000 - wants 3000


def test_wants_limited_by_available_when_necessary_is_high() -> None:
    snap = _snapshot(income_cents=1_000_000, necessary_cents=700_000, optional_cents=500_000)
    plan = build_plan(_inputs(snap))
    assert plan.budget.available_cents == 300_000
    assert plan.budget.wants_cents == 300_000  # min(5000, 3000, 3000)
    assert plan.budget.savings_cents == 0


# --------------------------------------------------------------------------- #
# 第 3 步：预备金
# --------------------------------------------------------------------------- #
def test_reserve_months_stable_is_three() -> None:
    plan = build_plan(_inputs(_snapshot(necessary_cents=200_000)))
    assert plan.reserve.months == 3
    assert plan.reserve.target_cents == 600_000


@pytest.mark.parametrize(
    "profile_kwargs",
    [dict(income_stable=False), dict(family_load=True)],
)
def test_reserve_months_six_when_unstable_or_family(profile_kwargs) -> None:
    plan = build_plan(_inputs(_snapshot(necessary_cents=200_000), profile=_profile(**profile_kwargs)))
    assert plan.reserve.months == 6
    assert plan.reserve.target_cents == 1_200_000


def test_reserve_shortfall_prioritised_before_investing() -> None:
    # 现有预备金 0，缺口 600000；结余 500000 → 全部用于补缺口，无可投资
    snap = _snapshot(income_cents=1_000_000, necessary_cents=200_000, optional_cents=300_000)
    plan = build_plan(_inputs(snap, profile=_profile(reserve_cents=0)))
    assert plan.reserve.monthly_cents == 500_000
    assert plan.reserve.investable_cents == 0


def test_high_interest_debt_flag() -> None:
    snap = _snapshot(income_cents=1_000_000, necessary_cents=200_000, optional_cents=0)
    plan = build_plan(_inputs(snap, debt_payment_cents=400_000))
    assert plan.reserve.high_interest_debt is True
    assert any("债务" in w for w in plan.warnings)


# --------------------------------------------------------------------------- #
# 第 4 步：风险约束
# --------------------------------------------------------------------------- #
def test_low_risk_zero_growth_cap() -> None:
    profile = _profile(max_loss_pct=0, horizon_months=6, experience="none")
    plan = build_plan(_inputs(profile=profile))
    assert plan.risk.level == "低"
    assert plan.risk.growth_cap_pct == 0
    assert all(s.amount_cents == 0 or s.category == "保守储蓄" for s in plan.allocation.recommended)


def test_high_risk_growth_cap_forty() -> None:
    profile = _profile(max_loss_pct=20, horizon_months=120, experience="experienced")
    snap = _snapshot(income_cents=1_000_000, necessary_cents=200_000, optional_cents=100_000)
    plan = build_plan(_inputs(snap, profile=profile))
    assert plan.risk.level == "高"
    assert plan.risk.growth_cap_pct == 40
    growth = next(s for s in plan.allocation.recommended if s.category == "增长配置")
    assert growth.pct == 40.0


def test_risk_override_only_gets_more_conservative() -> None:
    profile = _profile(max_loss_pct=20, horizon_months=120, experience="experienced")
    plan = build_plan(_inputs(profile=profile), PlanStrategy(risk_level_override="低"))
    assert plan.risk.level == "低"
    assert plan.risk.growth_cap_pct == GROWTH_CAP_PCT["低"]


# --------------------------------------------------------------------------- #
# 第 5/6 步：配置与情景
# --------------------------------------------------------------------------- #
def test_allocation_sums_to_investable() -> None:
    snap = _snapshot(income_cents=1_000_000, necessary_cents=200_000, optional_cents=100_000)
    plan = build_plan(_inputs(snap, profile=_profile(reserve_cents=10_000_000)))
    total = sum(s.amount_cents for s in plan.allocation.recommended)
    assert total == plan.reserve.investable_cents


def test_scenario_weighted_and_reachable_for_high_risk() -> None:
    profile = _profile(max_loss_pct=20, horizon_months=120, experience="experienced", reserve_cents=10_000_000)
    snap = _snapshot(income_cents=1_000_000, necessary_cents=200_000, optional_cents=100_000)
    plan = build_plan(_inputs(snap, profile=profile))
    assert plan.scenario is not None
    # 20%×2.0 + 40%×3.5 + 40%×6.0 = 4.2 → −0.5 = 3.7
    assert plan.scenario.weighted_annual_pct == pytest.approx(4.2, abs=0.01)
    assert plan.scenario.net_annual_pct == pytest.approx(3.7, abs=0.01)
    assert plan.scenario.goal_reachable is True
    assert plan.scenario.is_scenario is True


# --------------------------------------------------------------------------- #
# 第 7 步：独立复核
# --------------------------------------------------------------------------- #
def test_validate_ok_for_fresh_plan() -> None:
    inputs = _inputs(_snapshot(income_cents=1_000_000, necessary_cents=200_000, optional_cents=100_000))
    plan = build_plan(inputs)
    report = validate_plan(plan, inputs)
    assert report.ok is True
    assert report.issues == []
    assert report.recomputed["income_cents"] == plan.cashflow.income_cents


def test_validate_detects_tampered_amount() -> None:
    inputs = _inputs(_snapshot(income_cents=1_000_000, necessary_cents=200_000, optional_cents=100_000))
    plan = build_plan(inputs)
    plan.budget.wants_cents += 12345
    report = validate_plan(plan, inputs)
    assert report.ok is False
    assert any(issue.code == "recompute_mismatch" for issue in report.issues)


def test_validate_detects_growth_over_cap() -> None:
    inputs = _inputs(_snapshot(income_cents=1_000_000, necessary_cents=200_000, optional_cents=100_000))
    plan = build_plan(inputs)
    # 人为把增长类抬到超过上限（并同步保守/稳健以保持合计平衡）
    slices = {s.category: s for s in plan.allocation.recommended}
    investable = plan.reserve.investable_cents
    if investable > 0:
        slices["增长配置"].amount_cents = investable  # 全部放增长
        slices["保守储蓄"].amount_cents = 0
        slices["稳健配置"].amount_cents = 0
        report = validate_plan(plan, inputs)
        assert report.ok is False
        assert any(issue.code in {"growth_over_cap", "recompute_mismatch"} for issue in report.issues)
