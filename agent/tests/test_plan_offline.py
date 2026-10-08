"""Model failure must not fabricate a rule-based plan."""
from langgraph.checkpoint.memory import InMemorySaver
from moneyrouter_agent.plan_agent import PlanAgent
from moneyrouter_agent.config import Settings
from test_wallet_planning import inputs


def test_offline_does_not_generate_or_confirm_allocations():
    agent = PlanAgent(settings=Settings(api_key=None, key_file=None), inputs=inputs(), checkpointer=InMemorySaver())
    result = agent.plan("offline")
    assert result.degraded and result.plan is None and not result.awaiting_confirmation
    result = agent.plan("offline", resume={"action": "confirm"})
    assert not result.confirmed and result.plan is None


def test_failure_is_readable_and_retains_input_context():
    agent = PlanAgent(settings=Settings(api_key=None, key_file=None), inputs=inputs(), checkpointer=InMemorySaver())
    result = agent.plan("offline")
    assert "重试" in result.reply
    assert agent.context.inputs.snapshot.income.amount_cents == 250000
