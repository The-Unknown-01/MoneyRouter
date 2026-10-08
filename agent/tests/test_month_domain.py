"""本月实况领域语义：分类归一、合计/结余、代码层覆盖、目标达成计算。"""

from __future__ import annotations

from moneyrouter_agent.domain.month import (
    Baseline,
    CategorySpend,
    FundAllocation,
    GoalAlignment,
    HoldingReturn,
    IncomeFact,
    InvestmentSnapshot,
    MonthSnapshot,
    build_baselines,
    category_map,
    compute_allocation,
    compute_goal_alignment,
    compute_investments,
    merge_categories,
    merge_holdings,
    normalize_category,
    recompute,
)


# --------------------------------------------------------------------------- #
# 分类归一
# --------------------------------------------------------------------------- #
def test_normalize_category_is_idempotent_and_safe():
    assert normalize_category("外卖") == "餐饮"
    assert normalize_category(normalize_category("外卖")) == "餐饮"
    assert normalize_category("完全不认识的东西") == "其他"
    assert normalize_category(None) == "其他"
    assert normalize_category("餐饮") == "餐饮"


def test_longer_keyword_wins():
    # 「外卖」应命中餐饮，而不是被更短的关键词抢先
    assert normalize_category("美团外卖") == "餐饮"


# --------------------------------------------------------------------------- #
# 合计 / 结余
# --------------------------------------------------------------------------- #
def test_recompute_sums_and_balance():
    snap = MonthSnapshot(
        period="2026-09",
        income=IncomeFact(amount_cents=800_000),
        categories=[
            CategorySpend(category="餐饮", amount_cents=200_000),
            CategorySpend(category="交通", amount_cents=50_000),
        ],
    )
    out = recompute(snap)
    assert out.spend_total_cents == 250_000
    assert out.balance_cents == 550_000


def test_balance_is_none_without_income():
    snap = MonthSnapshot(period="2026-09", categories=[CategorySpend(category="餐饮", amount_cents=100)])
    out = recompute(snap)
    assert out.spend_total_cents == 100
    assert out.balance_cents is None
    assert "income" in out.unsettled


def test_zero_amount_is_legal_and_survives():
    snap = MonthSnapshot(
        period="2026-09",
        income=IncomeFact(amount_cents=100_000),
        categories=[CategorySpend(category="娱乐社交", amount_cents=0)],
    )
    out = recompute(snap)
    assert out.categories[0].amount_cents == 0
    assert out.spend_total_cents == 0


# --------------------------------------------------------------------------- #
# 结余去向：非投资 vs 投资
# --------------------------------------------------------------------------- #
def test_allocation_unallocated_is_computed():
    out = compute_allocation(500_000, FundAllocation(non_invested_cents=300_000, invested_cents=100_000))
    assert out.unallocated_cents == 100_000


def test_allocation_unallocated_none_without_inputs():
    assert compute_allocation(500_000, FundAllocation()).unallocated_cents is None


def test_recompute_marks_allocation_unsettled_until_explained():
    snap = MonthSnapshot(
        period="2026-09",
        income=IncomeFact(amount_cents=500_000),
        categories=[CategorySpend(category="餐饮", amount_cents=100_000)],
    )
    assert "allocation" in recompute(snap).unsettled

    explained = snap.model_copy(
        update={"allocation": FundAllocation(non_invested_cents=400_000, invested_cents=0)}
    )
    assert "allocation" not in recompute(explained).unsettled


# --------------------------------------------------------------------------- #
# 已有投资的收益
# --------------------------------------------------------------------------- #
def test_compute_investments_derives_missing_leg():
    # 给成本 + 市值 → 推出累计收益与收益率
    inv = compute_investments(
        InvestmentSnapshot(
            has_investments=True,
            holdings=[HoldingReturn(name="A", cost_cents=1_000_000, market_value_cents=1_080_000)],
        )
    )
    holding = inv.holdings[0]
    assert holding.total_return_cents == 80_000
    assert holding.total_return_pct == 8.0
    assert inv.market_value_total_cents == 1_080_000

    # 给市值 + 累计收益 → 推出成本
    inv2 = compute_investments(
        InvestmentSnapshot(
            holdings=[HoldingReturn(name="B", market_value_cents=1_000_000, total_return_cents=20_000)]
        )
    )
    assert inv2.holdings[0].cost_cents == 980_000
    assert inv2.holdings[0].total_return_pct == 2.04


def test_compute_investments_sums_only_when_every_holding_reports():
    inv = compute_investments(
        InvestmentSnapshot(
            has_investments=True,
            holdings=[
                HoldingReturn(name="A", cost_cents=100_000, market_value_cents=110_000, month_return_cents=5_000),
                HoldingReturn(name="B", cost_cents=200_000, market_value_cents=200_000),
            ],
        )
    )
    # 月收益只有一笔有值 → 合计留空（不部分求和、不补 0）
    assert inv.month_return_cents is None
    assert inv.total_return_cents == 10_000  # 两笔都能推出累计收益
    assert inv.total_return_pct == 3.33


def test_has_investments_false_is_a_conclusion_not_missing():
    out = recompute(MonthSnapshot(period="2026-09", investments=InvestmentSnapshot(has_investments=False)))
    assert "investments" not in out.unsettled
    assert out.investments.month_return_cents is None


def test_net_worth_change_combines_balance_and_investment_return():
    snap = MonthSnapshot(
        period="2026-09",
        income=IncomeFact(amount_cents=800_000),
        categories=[CategorySpend(category="餐饮", amount_cents=50_000)],
        investments=InvestmentSnapshot(
            has_investments=True,
            holdings=[HoldingReturn(name="A", cost_cents=100_000, market_value_cents=110_000, month_return_cents=8_000)],
        ),
    )
    out = recompute(snap)
    assert out.balance_cents == 750_000
    assert out.investments.month_return_cents == 8_000
    assert out.net_worth_change_cents == 758_000

    no_investment = recompute(
        MonthSnapshot(
            period="2026-09",
            income=IncomeFact(amount_cents=100_000),
            categories=[CategorySpend(category="餐饮", amount_cents=50_000)],
        )
    )
    assert no_investment.net_worth_change_cents is None


def test_merge_holdings_keeps_file_source_and_appends_stated():
    base = [HoldingReturn(name="A", cost_cents=100, market_value_cents=120, source="file")]
    incoming = [
        HoldingReturn(name="A", cost_cents=999),
        HoldingReturn(name="B", cost_cents=50),
    ]
    merged = {h.name: h for h in merge_holdings(base, incoming)}
    assert merged["A"].cost_cents == 100 and merged["A"].source == "file"
    assert merged["B"].cost_cents == 50


# --------------------------------------------------------------------------- #
# 代码层覆盖
# --------------------------------------------------------------------------- #
def test_file_categories_are_not_overridden_by_stated():
    base = [CategorySpend(category="餐饮", amount_cents=250_050, source="file", evidence="账单")]
    incoming = [CategorySpend(category="餐饮", amount_cents=1, source="stated")]
    merged = merge_categories(base, incoming)
    assert merged[0].amount_cents == 250_050
    assert merged[0].source == "file"


def test_stated_fills_missing_and_overrides_stated():
    base = [CategorySpend(category="餐饮", amount_cents=100, source="stated")]
    incoming = [CategorySpend(category="交通", amount_cents=200, source="stated")]
    merged = {c.category: c for c in merge_categories(base, incoming)}
    assert merged["餐饮"].amount_cents == 100  # 模型漏报时旧值保留
    assert merged["交通"].amount_cents == 200


def test_recompute_overwrites_model_supplied_derived_fields():
    """模型若在快照里塞了假的合计/结余，recompute 必须覆盖它。"""
    snap = MonthSnapshot(
        period="2026-09",
        income=IncomeFact(amount_cents=100_000),
        categories=[CategorySpend(category="餐饮", amount_cents=30_000)],
        spend_total_cents=999_999,
        balance_cents=-1,
    )
    out = recompute(snap)
    assert out.spend_total_cents == 30_000
    assert out.balance_cents == 70_000


# --------------------------------------------------------------------------- #
# 基线
# --------------------------------------------------------------------------- #
def test_build_baselines_from_budget_and_history():
    categories = [CategorySpend(category="餐饮", amount_cents=300_000)]
    history = [
        MonthSnapshot(period="2026-06", coverage_complete=True, categories=[]),
        MonthSnapshot(period="2026-07", coverage_complete=True, categories=[CategorySpend(category="餐饮", amount_cents=100_000)]),
        MonthSnapshot(period="2026-08", coverage_complete=True, categories=[CategorySpend(category="餐饮", amount_cents=200_000)]),
    ]
    baselines = build_baselines(categories, budget={"餐饮": 200_000}, history=history, period="2026-09")
    by_metric = {b.metric: b for b in baselines}

    assert by_metric["budget"].delta_cents == 100_000
    assert by_metric["budget"].delta_pct == 50.0
    assert by_metric["last_month"].amount_cents == 200_000
    assert by_metric["trailing_3m_avg"].amount_cents == 100_000  # (0+100+200)/3
    assert category_map(categories) == {"餐饮": 300_000}


# --------------------------------------------------------------------------- #
# 目标达成
# --------------------------------------------------------------------------- #
def test_goal_alignment_progress_and_projection():
    goal = GoalAlignment(goal="攒首付", target_cents=10_000_000, horizon_months=24)
    out = compute_goal_alignment(balance_cents=500_000, goal=goal, saved_to_date_cents=2_000_000)
    assert out.saved_this_month_cents == 500_000
    assert out.progress_pct == 20.0
    assert out.projected_months == 16  # (1000万-200万)/50万 = 16
    assert out.on_track is None  # 没有计划值，不硬判


def test_goal_on_track_from_planned():
    goal = GoalAlignment(goal="x", target_cents=1_000_000, planned_this_month_cents=400_000)
    assert compute_goal_alignment(balance_cents=500_000, goal=goal).on_track is True
    assert compute_goal_alignment(balance_cents=200_000, goal=goal).on_track is False


def test_goal_projection_none_without_savings():
    goal = GoalAlignment(goal="x", target_cents=1_000_000)
    out = compute_goal_alignment(balance_cents=-50_000, goal=goal, saved_to_date_cents=0)
    assert out.projected_months is None
