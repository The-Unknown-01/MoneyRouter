"""Tools expose facts and validate candidates without default allocations."""
from moneyrouter_agent.tools.plan_tools import make_plan_tools, TOOL_NAMES
from moneyrouter_agent.domain.plan import PlanContext
from test_wallet_planning import inputs, proposal


def test_tools_do_not_expose_legacy_allocation_algorithms():
    tools = {t.name: t for t in make_plan_tools(PlanContext(inputs()))}
    assert set(tools) == set(TOOL_NAMES)
    facts = tools["inspect_planning_facts"].invoke({})
    assert facts["month"]["income"]["amount_cents"] == 250000
    assert "allocation" not in facts


def test_candidate_tool_accepts_model_amounts_and_returns_errors():
    tools = {t.name: t for t in make_plan_tools(PlanContext(inputs()))}
    assert tools["evaluate_candidate"].invoke({"proposal": proposal().model_dump()})["ok"]
    invalid = proposal(food=40000)
    assert not tools["evaluate_candidate"].invoke({"proposal": invalid.model_dump()})["ok"]


def test_missing_news_stays_unavailable():
    tools = {t.name: t for t in make_plan_tools(PlanContext(inputs()))}
    assert tools["find_sources"].invoke({}) == {"available": False}
