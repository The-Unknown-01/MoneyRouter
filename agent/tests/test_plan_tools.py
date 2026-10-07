"""方案工具层测试：工具契约、`ToolNode` 可执行、模型无法注入金额。"""

from __future__ import annotations

import json
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from moneyrouter_agent.domain.finance import FinanceBriefing, MonthlyAnalysis
from moneyrouter_agent.domain.month import CategorySpend, IncomeFact, MonthSnapshot
from moneyrouter_agent.domain.plan import PlanContext, PlanInputs
from moneyrouter_agent.domain.profile import Profile
from moneyrouter_agent.tools.plan_tools import TOOL_NAMES, make_plan_tools


def _context() -> PlanContext:
    snapshot = MonthSnapshot(
        period="2026-09",
        income=IncomeFact(amount_cents=1_000_000, source="file"),
        categories=[
            CategorySpend(category="居住", amount_cents=200_000, source="file"),
            CategorySpend(category="购物", amount_cents=300_000, source="file"),
        ],
    )
    profile = Profile(
        income_cents=1_000_000,
        income_stable=True,
        reserve_cents=10_000_000,
        horizon_months=36,
        max_loss_pct=10,
        experience="some",
    )
    return PlanContext(PlanInputs(profile=profile, snapshot=snapshot, period="2026-10"))


def test_exposes_the_seven_tools() -> None:
    tools = make_plan_tools(_context())
    assert [t.name for t in tools] == list(TOOL_NAMES)
    for t in tools:
        assert t.description


def test_every_tool_is_callable_and_json_serializable() -> None:
    tools = {t.name: t for t in make_plan_tools(_context())}
    for name, args in (
        ("summarize_cashflow", {}),
        ("allocate_budget", {"wants_ratio_pct": 20}),
        ("calculate_reserve", {"reserve_months": 6}),
        ("assess_risk", {"risk_level_override": "低"}),
        ("propose_allocation", {}),
        ("find_sources", {"max_items": 3}),
        ("check_goal", {}),
    ):
        result = tools[name].invoke(args)
        assert isinstance(result, dict)
        json.dumps(result)  # 必须可序列化


def test_model_cannot_inject_amounts() -> None:
    """工具入参只应是旋钮，不应有金额字段。"""
    tools = {t.name: t for t in make_plan_tools(_context())}
    for name, tool in tools.items():
        props = tool.args_schema.model_json_schema().get("properties", {})
        assert "amount_cents" not in props, name
        assert "income_cents" not in props, name


def test_reserve_month_knob_affects_result() -> None:
    tools = {t.name: t for t in make_plan_tools(_context())}
    three = tools["calculate_reserve"].invoke({"reserve_months": 3})
    six = tools["calculate_reserve"].invoke({"reserve_months": 6})
    assert three["months"] == 3
    assert six["months"] == 6
    assert six["target_cents"] > three["target_cents"]


def test_find_sources_without_briefing() -> None:
    tools = {t.name: t for t in make_plan_tools(_context())}
    result = tools["find_sources"].invoke({})
    assert result["available"] is False


def test_tool_node_executes_calls() -> None:
    class _State(TypedDict):
        messages: Annotated[list, add_messages]

    tools = make_plan_tools(_context())
    builder = StateGraph(_State)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "tools")
    app = builder.compile()
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "summarize_cashflow", "args": {}, "id": "call-1", "type": "tool_call"},
        ],
    )
    out = app.invoke({"messages": [message]})
    tool_messages = [m for m in out["messages"] if getattr(m, "name", None)]
    assert len(tool_messages) == 1
    assert tool_messages[0].name == "summarize_cashflow"
    payload = json.loads(tool_messages[0].content)
    assert payload["income_cents"] == 1_000_000


def test_find_sources_returns_briefing_items() -> None:
    context = _context()
    context.inputs.briefing = FinanceBriefing(
        as_of="2026-10-07", period="2026-10", analysis=MonthlyAnalysis(headline="测试")
    )
    tools = {t.name: t for t in make_plan_tools(context)}
    result = tools["find_sources"].invoke({})
    assert result["available"] is True
    assert result["as_of"] == "2026-10-07"
