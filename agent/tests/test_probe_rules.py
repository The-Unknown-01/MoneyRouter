"""关注点探测机制：规则正/负例、去重、排序、合并（已解答不重复问）、可插拔注册。"""

from __future__ import annotations

from moneyrouter_agent.domain.month import (
    Baseline,
    CategorySpend,
    FundAllocation,
    GoalAlignment,
    HoldingReturn,
    IncomeFact,
    InvestmentSnapshot,
    OneOffItem,
)
from moneyrouter_agent.domain.probe import (
    PROBE_RULES,
    Probe,
    ProbeContext,
    ProbeThresholds,
    generate_probes,
    merge_probes,
    open_probes,
    probe_rule,
)


def _ctx(**over) -> ProbeContext:
    base = dict(
        period="2026-09",
        categories=[],
        income=None,
        baselines=[],
        one_offs=[],
        goal_alignment=GoalAlignment(),
        allocation=FundAllocation(),
        investments=InvestmentSnapshot(),
        prior={},
        thresholds=ProbeThresholds(),
    )
    base.update(over)
    return ProbeContext(**base)


def _ids(probes) -> list[str]:
    return [p.id for p in probes]


# --------------------------------------------------------------------------- #
# 各规则正 / 负例
# --------------------------------------------------------------------------- #
def test_over_budget_positive_and_negative():
    cats = [CategorySpend(category="餐饮", amount_cents=300_000)]
    base = [Baseline(metric="budget", category="餐饮", amount_cents=200_000)]
    assert "over_budget:餐饮" in _ids(generate_probes(_ctx(categories=cats, baselines=base)))

    ok = [CategorySpend(category="餐饮", amount_cents=100_000)]
    assert "over_budget:餐饮" not in _ids(generate_probes(_ctx(categories=ok, baselines=base)))


def test_over_last_month_positive_and_negative():
    cats = [CategorySpend(category="交通", amount_cents=300_000)]
    base = [Baseline(metric="last_month", category="交通", amount_cents=100_000)]
    assert "over_last_month:交通" in _ids(generate_probes(_ctx(categories=cats, baselines=base)))


def test_one_off_large_positive_and_negative():
    ctx = _ctx(one_offs=[OneOffItem(label="换手机", amount_cents=200_000)])
    assert "one_off_large:换手机" in _ids(generate_probes(ctx))

    small = _ctx(one_offs=[OneOffItem(label="买书", amount_cents=5_000)], income=IncomeFact(amount_cents=1_000_000))
    assert "one_off_large:买书" not in _ids(generate_probes(small))


def test_spend_over_income_positive_and_negative():
    pos = _ctx(income=IncomeFact(amount_cents=100_000), categories=[CategorySpend(category="餐饮", amount_cents=200_000)])
    assert "spend_over_income:total" in _ids(generate_probes(pos))

    neg = _ctx(income=IncomeFact(amount_cents=900_000), categories=[CategorySpend(category="餐饮", amount_cents=200_000)])
    assert "spend_over_income:total" not in _ids(generate_probes(neg))


def test_goal_off_track_positive_and_negative():
    lag = _ctx(
        goal_alignment=GoalAlignment(
            target_cents=1_000_000, planned_this_month_cents=400_000, saved_this_month_cents=100_000
        )
    )
    assert "goal_off_track:goal" in _ids(generate_probes(lag))

    fine = _ctx(
        goal_alignment=GoalAlignment(
            target_cents=1_000_000, planned_this_month_cents=400_000, saved_this_month_cents=400_000
        )
    )
    assert "goal_off_track:goal" not in _ids(generate_probes(fine))


def test_missing_field_for_income():
    assert "missing_field:income" in _ids(generate_probes(_ctx()))
    assert "missing_field:income" not in _ids(generate_probes(_ctx(income=IncomeFact(amount_cents=1))))


def test_missing_allocation_when_surplus_unexplained():
    # 收入有、支出有、结余为正，但资金去向未说明 -> 追问
    ctx = _ctx(income=IncomeFact(amount_cents=500_000), categories=[CategorySpend(category="餐饮", amount_cents=100_000)])
    assert "missing_field:allocation" in _ids(generate_probes(ctx))

    explained = _ctx(
        income=IncomeFact(amount_cents=500_000),
        categories=[CategorySpend(category="餐饮", amount_cents=100_000)],
        allocation=FundAllocation(non_invested_cents=300_000, invested_cents=100_000),
    )
    assert "missing_field:allocation" not in _ids(generate_probes(explained))


def test_inconsistent_allocation_positive_and_negative():
    bad = _ctx(
        income=IncomeFact(amount_cents=500_000),
        categories=[CategorySpend(category="餐饮", amount_cents=100_000)],
        allocation=FundAllocation(non_invested_cents=100_000, invested_cents=100_000, unallocated_cents=200_000),
    )
    assert "inconsistent:allocation" in _ids(generate_probes(bad))

    good = _ctx(
        income=IncomeFact(amount_cents=500_000),
        categories=[CategorySpend(category="餐饮", amount_cents=100_000)],
        allocation=FundAllocation(non_invested_cents=300_000, invested_cents=100_000, unallocated_cents=0),
    )
    assert "inconsistent:allocation" not in _ids(generate_probes(good))


# --------------------------------------------------------------------------- #
# 已有投资的收益
# --------------------------------------------------------------------------- #
def test_missing_investments_when_unreported():
    assert "missing_field:investments" in _ids(generate_probes(_ctx()))
    assert "missing_field:investments" not in _ids(
        generate_probes(_ctx(investments=InvestmentSnapshot(has_investments=False)))
    )


def test_holding_returns_probe_only_when_has_investments_without_details():
    no_detail = _ctx(investments=InvestmentSnapshot(has_investments=True))
    assert "missing_field:holding_returns" in _ids(generate_probes(no_detail))

    with_detail = _ctx(
        investments=InvestmentSnapshot(
            has_investments=True,
            holdings=[HoldingReturn(name="沪深300", month_return_cents=1_000)],
        )
    )
    assert "missing_field:holding_returns" not in _ids(generate_probes(with_detail))

    # 明确没有投资时不该追问收益
    assert "missing_field:holding_returns" not in _ids(
        generate_probes(_ctx(investments=InvestmentSnapshot(has_investments=False)))
    )


def test_investment_loss_positive_and_negative():
    loss = _ctx(
        investments=InvestmentSnapshot(
            has_investments=True,
            holdings=[HoldingReturn(name="某股票基金", month_return_cents=-200_000, month_return_pct=-8.0)],
        )
    )
    assert "investment_loss:某股票基金" in _ids(generate_probes(loss))

    small = _ctx(
        investments=InvestmentSnapshot(
            has_investments=True,
            holdings=[HoldingReturn(name="某股票基金", month_return_cents=-10_000)],
        )
    )
    assert "investment_loss:某股票基金" not in _ids(generate_probes(small))

    gain = _ctx(
        investments=InvestmentSnapshot(
            has_investments=True,
            holdings=[HoldingReturn(name="某股票基金", month_return_cents=200_000)],
        )
    )
    assert "investment_loss:某股票基金" not in _ids(generate_probes(gain))


# --------------------------------------------------------------------------- #
# 去重与排序
# --------------------------------------------------------------------------- #
def test_generate_probes_is_deduped_and_sorted():
    ctx = _ctx(
        income=IncomeFact(amount_cents=100_000),
        categories=[CategorySpend(category="餐饮", amount_cents=300_000)],
        baselines=[
            Baseline(metric="budget", category="餐饮", amount_cents=100_000),
            Baseline(metric="last_month", category="餐饮", amount_cents=100_000),
        ],
        one_offs=[OneOffItem(label="换手机", amount_cents=200_000)],
    )
    probes = generate_probes(ctx)
    ids = _ids(probes)
    assert len(ids) == len(set(ids)), "同 id 应只保留一条"
    assert probes == sorted(probes, key=lambda p: (p.severity, p.id))


# --------------------------------------------------------------------------- #
# 合并：已解答不重复问
# --------------------------------------------------------------------------- #
def test_merge_keeps_answered_and_does_not_reask():
    answered = Probe(
        id="over_budget:餐饮", kind="over_budget", topic="t", detail="d", status="answered", answer="聚餐多"
    )
    candidates = [
        Probe(id="over_budget:餐饮", kind="over_budget", topic="t", detail="d"),
        Probe(id="over_last_month:交通", kind="over_last_month", topic="t2", detail="d2"),
    ]
    merged = merge_probes(candidates, {"over_budget:餐饮": answered})
    by_id = {p.id: p for p in merged}

    assert by_id["over_budget:餐饮"].status == "answered"
    assert by_id["over_budget:餐饮"].answer == "聚餐多"
    assert "over_budget:餐饮" not in [p.id for p in open_probes(merged)]


def test_merge_dismisses_probe_that_no_longer_triggers():
    answered = Probe(id="over_budget:餐饮", kind="over_budget", topic="t", detail="d", status="answered", answer="x")
    merged = merge_probes([], {"over_budget:餐饮": answered})
    assert merged[0].status == "dismissed"
    assert merged[0].answer == "x"
    assert open_probes(merged) == []


# --------------------------------------------------------------------------- #
# 可插拔注册
# --------------------------------------------------------------------------- #
def test_new_rule_is_picked_up_automatically():
    @probe_rule("always_flagging", priority=1)
    def _always(ctx: ProbeContext) -> list[Probe]:
        return [Probe(id="always_flagging:x", kind="always_flagging", topic="t", detail="d")]

    try:
        assert any(rule.kind == "always_flagging" for rule in PROBE_RULES)
        assert "always_flagging:x" in _ids(generate_probes(_ctx()))
    finally:
        PROBE_RULES[:] = [rule for rule in PROBE_RULES if rule.kind != "always_flagging"]
