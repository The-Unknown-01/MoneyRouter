"""Calendar gaps, actual/expected accounting, and month lifecycle regressions."""
from datetime import datetime, timezone
import json
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from moneyrouter_agent.periods import business_today, shift_period, history_window, period_context, valid_period
from moneyrouter_agent.domain.month import (
    MonthSnapshot, IncomeFact, CategorySpend, MonthlyPoint, GoalAlignment,
    recompute, build_baselines, review_mode,
)
from moneyrouter_agent.domain.plan import PlanInputs, Plan, BudgetBaseline
from moneyrouter_agent.domain.wallet import Wallet, WalletProposal, compose_wallet_plan, validate_wallets
from moneyrouter_agent.domain.summary import build_diff, build_candidates
from moneyrouter_agent.domain.summary_insights import build_insights
from moneyrouter_agent.history import InMemoryMonthHistoryStore, JsonMonthHistoryStore, MonthRecord
from moneyrouter_agent.month_agent import MonthAgent
from moneyrouter_agent.summary_agent import SummaryAgent
from moneyrouter_agent.config import Settings
from moneyrouter_agent.summary_store import InMemoryExperiencePackStore, InMemoryMonthlySummaryStore
from moneyrouter_agent.api.service import Service, Command


def snapshot(period, amount=10000, complete=True):
    return recompute(MonthSnapshot(period=period, coverage_complete=complete,
        income=IncomeFact(amount_cents=800000), categories=[CategorySpend(category="餐饮", amount_cents=amount)]))


def test_calendar_anchor_and_china_boundary():
    assert shift_period("2027-01", -1) == "2026-12"
    assert shift_period("2026-12", 1) == "2027-01"
    assert business_today(datetime(2026, 9, 30, 16, 30, tzinfo=timezone.utc)).isoformat() == "2026-10-01"
    assert period_context("2026-09")["next_period"] == "2026-10"
    assert period_context("2026-09")["current_period"] == "2026-10"
    assert history_window("2026-10") == ["2026-07", "2026-08", "2026-09"]
    assert not valid_period("0000-01") and not valid_period("2026-13")


def test_missing_adjacent_month_never_falls_back_to_august():
    result = recompute(snapshot("2026-09"), history=[snapshot("2026-07")])
    assert not any(b.metric == "last_month" for b in result.baselines)
    insights = build_insights(result)
    assert insights["previous_period"] == "2026-08"
    assert not insights["previous_available"] and not insights["category_changes"]


def test_fixed_three_month_window_and_known_zero():
    july = snapshot("2026-07", 10000)
    august = snapshot("2026-08", 0)
    august.categories = []  # Known zero, not an unknown missing category.
    june = snapshot("2026-06", 20000)
    result = recompute(snapshot("2026-09"), history=[july, june, august])
    average = next(b for b in result.baselines if b.metric == "trailing_3m_avg")
    assert average.amount_cents == 10000
    assert average.periods == ["2026-06", "2026-07", "2026-08"]
    incomplete = recompute(snapshot("2026-09"), history=[july, august, snapshot("2026-04")])
    assert not any(b.metric == "trailing_3m_avg" for b in incomplete.baselines)
    unknown = recompute(snapshot("2026-09"), history=[june, july, snapshot("2026-08", complete=None)])
    assert not any(b.metric in {"last_month", "trailing_3m_avg"} for b in unknown.baselines)


def test_month_to_date_not_compared_with_full_month():
    current = recompute(snapshot("2026-10", 1000), history=[snapshot("2026-09")])
    assert review_mode(current) == "stage"
    assert not current.baselines
    insights = build_insights(current)
    assert insights["previous_available"] and not insights["comparable"]
    assert all(m["delta_cents"] is None for m in insights["metrics"])


def test_expected_income_does_not_inflate_actual_surplus_or_double_count():
    current = recompute(MonthSnapshot(period="2026-10", income=IncomeFact(amount_cents=200000),
        expected_income=IncomeFact(amount_cents=800000, role="expected"),
        categories=[CategorySpend(category="餐饮", amount_cents=100000)], obligations_reviewed=True,
        goal_alignment=GoalAlignment(planned_this_month_cents=600000)))
    assert current.balance_cents == 100000
    assert current.goal_alignment.on_track is None
    inputs = PlanInputs(period="2026-10", as_of="2026-10-08", snapshot=current)
    candidate = WalletProposal(headline="十月安排", wallets=[
        Wallet(id="food", name="餐饮", kind="expense", category="餐饮", amount_cents=300000, reason="生活", execution="核对支出"),
        Wallet(id="goal", name="目标", kind="goal", amount_cents=500000, reason="目标", execution="到账后留存"),
    ])
    plan = compose_wallet_plan(candidate, inputs)
    assert plan.funding["total_cents"] == 800000
    diff = build_diff(current, plan=plan)
    assert next(x.actual_cents for x in diff.layers if x.layer == "income") == 200000
    assert next(x.actual_cents for x in diff.layers if x.layer == "savings") == 100000
    assert not build_candidates(diff)


def test_future_and_past_plan_guards():
    future = MonthSnapshot(period="2026-11", expected_income=IncomeFact(amount_cents=800000, role="expected"), obligations_reviewed=True)
    candidate = WalletProposal(headline="预案", wallets=[Wallet(id="goal", name="目标", kind="goal", amount_cents=800000, reason="目标", execution="到账后执行")])
    inputs = PlanInputs(period="2026-11", as_of="2026-10-08", snapshot=future)
    assert validate_wallets(candidate, inputs)["ok"]
    future.income = IncomeFact(amount_cents=800000)
    assert not validate_wallets(candidate, inputs)["ok"]
    inputs.period = future.period = "2026-09"
    assert not validate_wallets(candidate, inputs)["ok"]


def test_expected_income_can_coexist_with_file_actual():
    agent = MonthAgent(settings=Settings(api_key=None, key_file=None), checkpointer=InMemorySaver(), history_store=None)
    agent.turn("income", period="2026-10", bill=json.dumps({"period":"2026-10", "cashflow":[
        {"date":"2026-10-01","direction":"income","amount":2000,"category":"工资"}]}))
    result = agent.prepare_confirmation("income", supplements={"expected_income_cents":800000, "obligations_reviewed":True})
    assert result.snapshot.income.amount_cents == 200000
    assert result.snapshot.expected_income.amount_cents == 800000
    assert result.snapshot.balance_cents == 200000


def test_stage_confirmation_does_not_save_next_month_lessons():
    history = InMemoryMonthHistoryStore()
    history.save(MonthRecord(period="2026-10", snapshot=snapshot("2026-10", 500000)))
    pack, summaries = InMemoryExperiencePackStore(), InMemoryMonthlySummaryStore()
    agent = SummaryAgent(settings=Settings(api_key=None,key_file=None), checkpointer=InMemorySaver(),
        history_store=history, experience_store=pack, summary_store=summaries)
    first = agent.summarize("stage",period="2026-10",plan=Plan(period="2026-10", budget=BudgetBaseline(wants_cents=100000)))
    assert first.summary.review_mode == "stage"
    result = agent.summarize("stage",resume={"action":"confirm"})
    assert result.confirmed and pack.load() is None
    assert summaries.load("2026-10").review_mode == "stage"


def test_reloaded_summary_uses_late_added_adjacent_month():
    history = InMemoryMonthHistoryStore()
    history.save(MonthRecord(period="2026-09", snapshot=snapshot("2026-09", 20000)))
    agent = SummaryAgent(settings=Settings(api_key=None,key_file=None), checkpointer=InMemorySaver(), history_store=history)
    assert not agent.summarize("before",period="2026-09").summary.insights["previous_available"]
    history.save(MonthRecord(period="2026-08", snapshot=snapshot("2026-08", 10000)))
    result = agent.summarize("after",period="2026-09")
    assert result.summary.insights["previous_available"]
    assert result.summary.insights["category_changes"][0]["delta_cents"] == 10000


def test_legacy_stated_income_keeps_amount_but_requires_role_reconfirmation(tmp_path):
    store = JsonMonthHistoryStore(tmp_path)
    store.path_for("2026-09").write_text(json.dumps({"period":"2026-09","snapshot":{
        "period":"2026-09","income":{"amount_cents":800000,"source":"stated"}}}),encoding="utf-8")
    record = store.load("2026-09")
    assert record.snapshot.income.amount_cents == 800000 and record.snapshot.income.role == "unknown"
    assert recompute(record.snapshot).balance_cents is None


def test_service_review_version_and_dependency_thread(tmp_path):
    service = Service(tmp_path / "service")
    try:
        plan = Plan(period="2026-09").model_dump(mode="json")
        service.put("u1", "versions", [{"version":1,"plan":plan}, {"version":2,"plan":plan}], "2026-09")
        c = Command(user_id="u1",kind="summary",period="2026-09",request_id="request-month")
        assert service.review_plan(c)["version"] == 1
        assert service.review_plan(c.model_copy(update={"payload":{"plan_version":2}}))["version"] == 2
        before = service.thread(c)
        history = service.stores("u1")[0]
        history.save(MonthRecord(period="2026-08", snapshot=snapshot("2026-08")))
        assert service.thread(c) != before
        with pytest.raises(ValueError,match="历史月份"):
            service.execute(c.model_copy(update={"kind":"plan"}))
        with pytest.raises(ValueError,match="未来月份"):
            service.execute(c.model_copy(update={"period":"2026-11"}))
    finally:
        service.close()
