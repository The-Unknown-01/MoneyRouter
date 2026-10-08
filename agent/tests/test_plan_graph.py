"""LangGraph tool execution, bounded corrections and monthly clarification."""
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from moneyrouter_agent.plan_agent import PlanAgent
from moneyrouter_agent.config import Settings
from moneyrouter_agent.domain.plan_turn import PlanTurnDecision
from test_wallet_planning import inputs, proposal


def test_model_can_read_facts_through_tools_before_proposal():
    calls = []
    def inspect(messages):
        calls.append(messages)
        if len(calls) == 1:
            return AIMessage(content="", tool_calls=[{"name":"inspect_planning_facts", "args":{}, "id":"facts", "type":"tool_call"}])
        return AIMessage(content="资料已读取")
    def decide(messages):
        assert any(getattr(m, "name", "") == "inspect_planning_facts" for m in messages)
        return PlanTurnDecision(reply="请核对", status="finalize", proposal=proposal())
    agent=PlanAgent(inputs=inputs(), settings=Settings(api_key=None, key_file=None), agent_runner=inspect,
                    decide_runner=decide, checkpointer=InMemorySaver())
    assert agent.plan("tools").awaiting_confirmation
    assert len(calls)==2


def test_missing_month_information_routes_back_to_month():
    agent=PlanAgent(inputs=inputs(), settings=Settings(api_key=None, key_file=None),
        decide_runner=lambda m: PlanTurnDecision(reply="还有房租尚未支付吗？", status="ask", questions=["还有房租尚未支付吗？"]),
        checkpointer=InMemorySaver())
    result=agent.plan("ask")
    assert result.clarification_target=="month" and result.plan is None and not result.awaiting_confirmation


def test_invalid_candidates_stop_after_three_attempts():
    calls=[]
    def decide(messages):
        calls.append(messages)
        return PlanTurnDecision(reply="请核对", status="finalize", proposal=proposal(food=40000))
    agent=PlanAgent(inputs=inputs(), settings=Settings(api_key=None, key_file=None), decide_runner=decide, checkpointer=InMemorySaver())
    result=agent.plan("invalid")
    assert len(calls)==3 and not result.awaiting_confirmation and not result.validation.ok
    assert result.plan is None
