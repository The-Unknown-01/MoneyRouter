"""经验契约测试：`ExperiencePack` 结构与它如何软调整方案。"""

from __future__ import annotations

from moneyrouter_agent.domain.experience import ExperiencePack, Lesson, apply_lessons
from moneyrouter_agent.domain.month import CategorySpend, IncomeFact, MonthSnapshot
from moneyrouter_agent.domain.plan import PlanInputs, build_plan
from moneyrouter_agent.domain.profile import Profile


def _snapshot(
    *, income_cents: int = 1_000_000, necessary_cents: int = 200_000, optional_cents: int = 500_000
) -> MonthSnapshot:
    return MonthSnapshot(
        period="2026-09",
        income=IncomeFact(amount_cents=income_cents, source="file"),
        categories=[
            CategorySpend(category="居住", amount_cents=necessary_cents, source="file"),
            CategorySpend(category="购物", amount_cents=optional_cents, source="file"),
        ],
    )


def _profile(**overrides) -> Profile:
    base = dict(
        income_cents=1_000_000,
        income_stable=True,
        family_load=False,
        reserve_cents=10_000_000,
        horizon_months=120,
        max_loss_pct=20,
        experience="experienced",
    )
    base.update(overrides)
    return Profile(**base)


def _pack(*lessons: Lesson) -> ExperiencePack:
    return ExperiencePack(version=1, from_period="2026-09", lessons=list(lessons))


# --------------------------------------------------------------------------- #
# 契约本身
# --------------------------------------------------------------------------- #
def test_empty_pack_means_no_adjustment() -> None:
    effects = apply_lessons(None)
    assert effects.wants_ratio_scale == 1.0
    assert effects.risk_downgrade_notches == 0
    assert effects.force_reserve_first is False
    assert effects.applied_ids == []

    effects_empty = apply_lessons(ExperiencePack())
    assert effects_empty.wants_ratio_scale == 1.0


def test_pack_round_trips() -> None:
    pack = _pack(
        Lesson(id="wants_down:2026-09", kind="wants_down", statement="可选支出偏高", strength=0.5)
    )
    restored = ExperiencePack.model_validate(pack.model_dump())
    assert restored.lessons[0].id == "wants_down:2026-09"
    assert restored.lessons[0].strength == 0.5


def test_duplicate_ids_keep_last() -> None:
    pack = _pack(
        Lesson(id="dup", kind="wants_down", statement="a", strength=0.1),
        Lesson(id="dup", kind="risk_conservative", statement="b", strength=1.0),
    )
    effects = apply_lessons(pack)
    assert effects.applied_ids == ["dup"]
    assert effects.risk_downgrade_notches == 1


# --------------------------------------------------------------------------- #
# 软调整对方案的影响
# --------------------------------------------------------------------------- #
def test_wants_down_lowers_optional_budget() -> None:
    inputs = PlanInputs(profile=_profile(), snapshot=_snapshot())
    baseline = build_plan(inputs)
    pack = _pack(Lesson(id="w", kind="wants_down", statement="下调可选", strength=1.0))
    adjusted = build_plan(PlanInputs(profile=_profile(), snapshot=_snapshot(), experience=pack))
    assert adjusted.budget.wants_cents < baseline.budget.wants_cents
    assert adjusted.lessons_applied == ["w"]


def test_reserve_priority_blocks_new_investment() -> None:
    # 现有预备金充足 → 默认有可投资；经验要求优先留作预备金 → 可投资归零
    inputs = PlanInputs(profile=_profile(reserve_cents=10_000_000), snapshot=_snapshot())
    assert build_plan(inputs).reserve.investable_cents > 0

    pack = _pack(Lesson(id="r", kind="reserve_priority", statement="先补预备金"))
    adjusted = build_plan(PlanInputs(profile=_profile(), snapshot=_snapshot(), experience=pack))
    assert adjusted.reserve.investable_cents == 0
    assert adjusted.reserve.monthly_cents == adjusted.budget.savings_cents


def test_risk_conservative_downgrades_level() -> None:
    inputs = PlanInputs(profile=_profile(), snapshot=_snapshot())
    assert build_plan(inputs).risk.level == "高"

    pack = _pack(Lesson(id="rc", kind="risk_conservative", statement="保守一点"))
    adjusted = build_plan(PlanInputs(profile=_profile(), snapshot=_snapshot(), experience=pack))
    assert adjusted.risk.level == "中"
    assert adjusted.risk.growth_cap_pct == 20
