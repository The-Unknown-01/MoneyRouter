"""Visualization-ready evidence, separate cash flows from market returns. No UI here."""
from .month import category_map, actual_income_cents, complete_month, review_mode
from ..periods import shift_period, valid_period


def previous_period(period):
    return shift_period(period, -1) if valid_period(period) and period != "0001-01" else ""


def build_insights(snapshot, plan=None):
    prior_period = previous_period(snapshot.period) if snapshot.period else ""
    prior = next((point for point in snapshot.trailing if point.period == prior_period and point.coverage_complete is True), None)
    comparable = prior is not None and complete_month(snapshot)
    metrics = []
    current = {
        "income_cents": actual_income_cents(snapshot),
        "spend_total_cents": snapshot.spend_total_cents if snapshot.categories or snapshot.coverage_complete else None,
        "balance_cents": snapshot.balance_cents,
        "investment_return_cents": snapshot.investments.month_return_cents,
    }
    for key, value in current.items():
        old = getattr(prior, key, None) if prior else None
        delta = value-old if comparable and value is not None and old is not None else None
        metrics.append({"id": key, "current_cents": value, "previous_cents": old,
                        "delta_cents": delta, "delta_pct": round(delta/old*100, 2) if delta is not None and old else None})
    categories = [{"id": b.category, "previous_cents": b.amount_cents,
                   "current_cents": b.amount_cents+b.delta_cents, "delta_cents": b.delta_cents,
                   "delta_pct": b.delta_pct} for b in snapshot.baselines if comparable and b.metric == "last_month" and b.periods == [prior_period] and b.category]
    actuals = category_map(snapshot.categories)
    wallets = []
    milestones = []
    for wallet in getattr(plan, "wallets", []) if plan and plan.period == snapshot.period else []:
        actual = actuals.get(wallet["category"]) if wallet["kind"] == "expense" else next((x.amount_cents for x in snapshot.wallet_execution if x.wallet_id == wallet["id"]), None)
        if wallet["kind"] == "expense" and actual is None and snapshot.coverage_complete:
            actual = 0
        budget = wallet["amount_cents"]
        wallets.append({"id": wallet["id"], "name": wallet["name"], "kind": wallet["kind"],
                        "planned_cents": budget, "actual_cents": actual,
                        "delta_cents": actual-budget if actual is not None else None,
                        "status": "unknown" if actual is None else "over_budget" if actual > budget else "within_budget"})
    goal = snapshot.goal_alignment
    if goal.on_track is True:
        milestones.append({"id": "goal_on_track:"+snapshot.period, "kind": "goal_progress",
                           "label": "本月目标储蓄进度符合计划", "evidence": {
                               "planned_cents": goal.planned_this_month_cents,
                               "saved_cents": goal.saved_this_month_cents}})
    if goal.target_cents and goal.saved_to_date_cents is not None and goal.saved_to_date_cents >= goal.target_cents:
        milestones.append({"id": "goal_complete:"+snapshot.period, "kind": "goal_complete",
                           "label": "目标累计金额已达到", "evidence": {
                               "target_cents": goal.target_cents, "saved_to_date_cents": goal.saved_to_date_cents}})
    return {"schema_version": 1, "period": snapshot.period, "previous_period": prior_period,
            "previous_available": prior is not None, "comparable": comparable, "review_mode": review_mode(snapshot), "as_of": snapshot.as_of,
            "metrics": metrics, "category_changes": categories,
            "wallet_execution": wallets, "milestones": milestones,
            "goal": goal.model_dump(mode="json"),
            "confirmed_environment": [event.model_dump(mode="json") for event in snapshot.environment],
            "accounting": {"surplus_cents": snapshot.balance_cents,
                "retained_cents": snapshot.allocation.non_invested_cents,
                "invested_cents": snapshot.allocation.invested_cents,
                "market_return_cents": snapshot.investments.month_return_cents,
                "notice": "结余、储蓄去向和投资盈亏分别展示，不能相加重复称为攒下的钱；钱包执行未知不补零"}}
