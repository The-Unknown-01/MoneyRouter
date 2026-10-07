"""方案生成图装配。

    START → ingest → agent ⇄ tools → decide ──(ask)──→ END（等下一句）
                                            └─(finalize)─→ compose → validate ──ok──→ confirm
                                                                        └─fail─→ fallback → confirm
    confirm ──confirm──→ END
            ├─edit────→ adjust → compose（重算 + 复核 → 回 confirm）
            └─more────→ agent

``confirm`` **不加静态出边**，由 ``Command(goto=...)`` 决定去向，避免双跑。
工具循环有界（``tool_rounds`` + ``recursion_limit``），适配 1 核 VPS，避免死循环。
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from .plan_nodes import (
    ADJUST,
    AGENT,
    COMPOSE,
    CONFIRM,
    DECIDE,
    FALLBACK,
    INGEST,
    TOOLS,
    VALIDATE,
    AdjustRunner,
    AgentRunner,
    DecideRunner,
    confirm_node,
    make_adjust,
    make_agent,
    make_compose,
    make_decide,
    make_fallback,
    make_ingest,
    make_route_after_tools,
    make_validate,
    route_after_agent,
    route_after_decide,
    route_after_validate,
)
from .plan_state import PlanState

# 状态里出现的领域模型，需显式允许反序列化（否则未来版本会被拦下）。
ALLOWED_MSGPACK_MODULES: list[tuple[str, ...]] = [
    ("moneyrouter_agent.domain.plan", "Plan"),
    ("moneyrouter_agent.domain.plan", "PlanStrategy"),
    ("moneyrouter_agent.domain.plan", "CashflowSummary"),
    ("moneyrouter_agent.domain.plan", "DataQuality"),
    ("moneyrouter_agent.domain.plan", "BudgetBaseline"),
    ("moneyrouter_agent.domain.plan", "ReservePlan"),
    ("moneyrouter_agent.domain.plan", "RiskAssessment"),
    ("moneyrouter_agent.domain.plan", "AllocationProposal"),
    ("moneyrouter_agent.domain.plan", "AllocationSlice"),
    ("moneyrouter_agent.domain.plan", "ScenarioCheck"),
    ("moneyrouter_agent.domain.plan", "ValidationReport"),
    ("moneyrouter_agent.domain.plan", "ValidationIssue"),
    ("moneyrouter_agent.domain.plan_turn", "PlanTurnDecision"),
    ("moneyrouter_agent.domain.plan_turn", "PlanAdjustment"),
    ("moneyrouter_agent.domain.experience", "ExperiencePack"),
    ("moneyrouter_agent.domain.experience", "Lesson"),
]


def default_checkpointer() -> InMemorySaver:
    """默认的内存检查点（允许我们的领域模型参与序列化）。"""
    return InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MSGPACK_MODULES))


def build_plan_graph(
    *,
    agent_runner: AgentRunner | None,
    tools: list[Any],
    decide_runner: DecideRunner | None,
    adjust_runner: AdjustRunner | None,
    context: Any,
    max_tool_rounds: int = 4,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译方案生成图（默认内存检查点）。"""
    builder = StateGraph(PlanState)
    builder.add_node(INGEST, make_ingest(context))
    builder.add_node(AGENT, make_agent(agent_runner, context, max_tool_rounds=max_tool_rounds))
    builder.add_node(TOOLS, ToolNode(tools))
    builder.add_node(DECIDE, make_decide(decide_runner, context))
    builder.add_node(COMPOSE, make_compose(context))
    builder.add_node(VALIDATE, make_validate(context))
    builder.add_node(ADJUST, make_adjust(adjust_runner, context))
    builder.add_node(CONFIRM, confirm_node)
    builder.add_node(FALLBACK, make_fallback(context))

    builder.add_edge(START, INGEST)
    builder.add_edge(INGEST, AGENT)
    builder.add_conditional_edges(
        AGENT,
        route_after_agent,
        {TOOLS: TOOLS, DECIDE: DECIDE, FALLBACK: FALLBACK},
    )
    builder.add_conditional_edges(
        TOOLS,
        make_route_after_tools(max_tool_rounds),
        {AGENT: AGENT, DECIDE: DECIDE},
    )
    builder.add_conditional_edges(
        DECIDE,
        route_after_decide,
        {COMPOSE: COMPOSE, FALLBACK: FALLBACK, "wait": END},
    )
    builder.add_edge(COMPOSE, VALIDATE)
    builder.add_conditional_edges(
        VALIDATE,
        route_after_validate,
        {CONFIRM: CONFIRM, FALLBACK: FALLBACK},
    )
    builder.add_edge(ADJUST, COMPOSE)
    builder.add_edge(FALLBACK, CONFIRM)
    # CONFIRM 无静态出边：去向由 Command(goto=...) 决定（END / ADJUST / AGENT）

    return builder.compile(checkpointer=checkpointer if checkpointer is not None else default_checkpointer())
