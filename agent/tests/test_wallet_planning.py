from langgraph.checkpoint.memory import InMemorySaver
from moneyrouter_agent.domain.month import MonthSnapshot, IncomeFact, CategorySpend, PaymentObligation, MonthlyPoint, recompute
from moneyrouter_agent.domain.profile import Profile
from moneyrouter_agent.domain.plan import PlanInputs
from moneyrouter_agent.domain.wallet import Wallet, WalletProposal, validate_wallets, compose_wallet_plan
from moneyrouter_agent.domain.plan_turn import PlanTurnDecision
from moneyrouter_agent.domain.summary import build_diff
from moneyrouter_agent.plan_agent import PlanAgent
from moneyrouter_agent.config import Settings


def test_long_plan_conversation_does_not_send_orphan_tool_results():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from moneyrouter_agent.graph.wallet_build import recent_turn_messages

    messages = [HumanMessage(content="旧方案")]
    for i in range(12):
        messages.extend([AIMessage(content="", tool_calls=[{
            "name": "evaluate_candidate", "args": {}, "id": str(i)}]),
            ToolMessage(content="校验结果", tool_call_id=str(i))])
    latest = HumanMessage(content="请重新生成下月方案")
    messages.append(latest)
    sent = recent_turn_messages(messages)
    assert sent[0] == latest
    call_ids = set()
    for message in sent:
        call_ids.update(c["id"] for c in getattr(message, "tool_calls", []))
        if isinstance(message, ToolMessage):
            assert message.tool_call_id in call_ids
    # A single long turn must also retain its calling assistant messages.
    assert recent_turn_messages(messages[:-1])[0] == messages[0]


def inputs():
    return PlanInputs(period="2026-07", as_of="2026-07-10", profile=Profile(income_cents=250000,
        horizon_months=36, max_loss_pct=10), snapshot=MonthSnapshot(period="2026-07",
        income=IncomeFact(amount_cents=250000), categories=[CategorySpend(category="餐饮", amount_cents=50000)],
        obligations_reviewed=True, obligations=[PaymentObligation(id="exam", category="学习成长", label="考试", amount_cents=20000, evidence="用户尚未支付考试费")]))


def proposal(food=90000, fun=40000):
    return WalletProposal(headline="暑假保留娱乐空间并推进目标", wallets=[
        Wallet(id="food", name="餐饮", kind="expense", category="餐饮", amount_cents=food, reason="覆盖已花及后续餐饮", execution="按剩余周数安排"),
        Wallet(id="exam", name="考试", kind="expense", category="学习成长", amount_cents=20000, reason="尚未支付义务", execution="报名时支付"),
        Wallet(id="fun", name="娱乐", kind="expense", category="娱乐社交", amount_cents=fun, reason="已确认的假期需求", execution="每周核对"),
        Wallet(id="computer", name="电脑储蓄", kind="goal", amount_cents=250000-food-20000-fun, reason="推进电脑目标", execution="单独留存", target_cents=500000),
    ])


def test_amounts_are_chosen_by_agent_not_default_ratios():
    a = compose_wallet_plan(proposal(), inputs())
    b = compose_wallet_plan(proposal(food=100000, fun=20000), inputs())
    assert a.wallets[0]["amount_cents"] == 90000
    assert b.wallets[0]["amount_cents"] == 100000
    assert a.wallets[0]["remaining_cents"] == 40000
    assert a.budget.savings_cents != b.budget.savings_cents
    assert a.schema_version == 2


def test_required_spending_balance_and_sources_cannot_be_bypassed():
    draft = proposal(food=40000)
    draft.wallets[1].amount_cents = 10000
    draft.wallets[0].source_urls = ["https://invented.example/news"]
    report = validate_wallets(draft, inputs())
    assert not report["ok"]
    assert len(report["errors"]) >= 4


def test_unknown_obligations_require_month_clarification():
    facts = inputs()
    facts.snapshot.obligations_reviewed = False
    assert not validate_wallets(proposal(), facts)["ok"]


def test_model_revises_rejected_candidate_then_user_confirms_and_edits():
    results = iter([proposal(food=40000), proposal(), proposal(food=100000, fun=20000)])
    def decide(messages):
        return PlanTurnDecision(reply="请核对", status="finalize", proposal=next(results))
    agent = PlanAgent(inputs=inputs(), settings=Settings(api_key=None, key_file=None),
        decide_runner=decide, checkpointer=InMemorySaver())
    result = agent.plan("wallet")
    assert result.awaiting_confirmation and result.validation.ok
    assert result.turn_count == 2
    edited = agent.plan("wallet", resume={"action": "edit", "message": "少安排娱乐，多存一些"})
    assert edited.plan.wallets[2]["amount_cents"] == 20000
    confirmed = agent.plan("wallet", resume={"action": "confirm"})
    assert confirmed.confirmed and not confirmed.awaiting_confirmation


def test_offline_never_generates_default_allocations():
    agent = PlanAgent(inputs=inputs(), settings=Settings(api_key=None, key_file=None), checkpointer=InMemorySaver())
    result = agent.plan("offline")
    assert result.plan is None and result.degraded and not result.awaiting_confirmation


def test_tool_loop_text_is_private_even_without_tool_calls():
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from moneyrouter_agent.api.service import plan_conversation

    @tool
    def read_facts():
        """Read the facts used for planning."""
        return "confirmed facts"

    inspections = iter([
        AIMessage(content="I'll start by reading the facts.", tool_calls=[
            {"name": "read_facts", "args": {}, "id": "facts-1"}]),
        AIMessage(content="Let me test a minimal plan within the cap."),
    ])
    agent = PlanAgent(inputs=inputs(), settings=Settings(api_key=None, key_file=None),
        agent_runner=lambda messages: next(inspections), tools=[read_facts],
        decide_runner=lambda messages: PlanTurnDecision(status="ask", reply="请核对本月可用收入。"),
        checkpointer=InMemorySaver())
    result = agent.plan("private", user_message="生成本月方案")
    state = agent.graph.get_state(agent._config("private"))
    assert result.reply == "请核对本月可用收入。"
    assert result.clarification_target == "month"
    assert plan_conversation(state.values["messages"]) == [
        {"role": "user", "content": "生成本月方案"},
        {"role": "assistant", "content": result.reply},
    ]
    assert any(isinstance(m, AIMessage) and "Let me" in m.content for m in state.values["messages"])
    # An unmarked/internal AI message can never become the snapshot reply.
    agent.graph.update_state(agent._config("private"), {"messages": [AIMessage(content="internal only")]})
    assert agent.snapshot("private").reply == result.reply


def test_summary_keeps_previous_month_and_wallet_execution_separate():
    facts = inputs()
    facts.snapshot.coverage_complete = True
    facts.snapshot = recompute(facts.snapshot)
    facts.snapshot.trailing = [MonthlyPoint(period="2026-06", coverage_complete=True, income_cents=240000, spend_total_cents=60000, balance_cents=180000)]
    plan = compose_wallet_plan(proposal(), facts)
    result = build_diff(facts.snapshot, plan=plan)
    assert result.insights["previous_available"]
    assert result.insights["metrics"][0]["delta_cents"] == 10000
    assert result.insights["wallet_execution"][0]["actual_cents"] == 50000
    assert result.insights["wallet_execution"][-1]["actual_cents"] is None
    assert result.categories[0].planned_cents is not None


def test_first_month_is_unknown_not_zero():
    report = build_diff(inputs().snapshot).insights
    assert not report["previous_available"]
    assert all(item["previous_cents"] is None for item in report["metrics"])


def test_investment_schedule_only_allocates_unexecuted_money():
    from moneyrouter_agent.domain.month import WalletExecution
    from moneyrouter_agent.domain.wallet import InvestmentStep
    facts = inputs()
    facts.snapshot.allocation.invested_cents = 20000
    facts.snapshot.wallet_execution = [WalletExecution(wallet_id="computer", amount_cents=20000, evidence="用户确认已投入")]
    candidate = proposal()
    investment = candidate.wallets[-1]
    investment.kind = "investment"
    investment.asset_class = "steady"
    investment.asset_scope = "分散的债券类资产"
    investment.horizon_months = 24
    investment.liquidity = "保留其他日常流动资金"
    investment.risks = ["净值会波动"]
    investment.review_conditions = ["用户资金期限变化时重新评估"]
    investment.steps = [InvestmentStep(date="2026-07-15", amount_cents=80000)]
    assert validate_wallets(candidate, facts)["ok"]
    plan = compose_wallet_plan(candidate, facts)
    assert plan.wallets[-1]["remaining_cents"] == 80000
    investment.steps[0].amount_cents = 100000
    assert not validate_wallets(candidate, facts)["ok"]


def test_unknown_risk_cannot_be_replaced_by_default_allocation():
    from moneyrouter_agent.domain.wallet import InvestmentStep
    candidate = proposal()
    investment = candidate.wallets[-1]
    investment.kind = "investment"
    investment.asset_class = "growth"
    investment.asset_scope = "分散权益类别"
    investment.horizon_months = 36
    investment.liquidity = "长期持有"
    investment.risks = ["可能亏损"]
    investment.review_conditions = ["期限变化"]
    investment.steps = [InvestmentStep(date="2026-07-15", amount_cents=100000)]
    facts = inputs()
    facts.profile.max_loss_pct = None
    assert not validate_wallets(candidate, facts)["ok"]


def test_v2_validation_rejects_tampered_display_aggregates():
    from moneyrouter_agent.domain.plan import validate_plan
    facts = inputs()
    plan = compose_wallet_plan(proposal(), facts)
    assert validate_plan(plan, facts).ok
    plan.budget.savings_cents += 1
    assert not validate_plan(plan, facts).ok


def test_existing_reserve_is_not_automatically_spendable():
    facts = inputs()
    facts.profile.reserve_cents = 1000000
    assert validate_wallets(proposal(), facts)["funding_cents"] == 250000
    facts.snapshot.additional_funds_cents = 10000
    candidate = proposal()
    candidate.wallets[-1].amount_cents += 10000
    assert not validate_wallets(candidate, facts)["ok"]
    facts.snapshot.additional_funds_evidence = "用户明确允许本月动用一百元已有余额"
    plan = compose_wallet_plan(candidate, facts)
    assert plan.funding["total_cents"] == 260000
    assert plan.funding["income_cents"] == 250000
    assert next(row for row in build_diff(facts.snapshot, plan=plan).layers if row.layer == "savings").planned_cents == 100000
