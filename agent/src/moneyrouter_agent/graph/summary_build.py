"""总结图装配。

    START → collect → diff → reflect ──(降级)──→ fallback ──┐
                                └──(正常)───────────────────┴→ compose → confirm(interrupt)
                                                                          ├confirm→ END（门面写盘）
                                                                          └edit───→ reflect

``confirm`` 不加静态出边，由 ``Command(goto=...)`` 决定去向，避免双跑。
"""

from __future__ import annotations

from typing import Any

from ..checkpoints import make_checkpointer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from .summary_nodes import (
    ReflectRunner,
    SummaryCodeLayer,
    confirm_node,
    make_collect,
    make_compose,
    make_diff,
    make_fallback,
    make_reflect,
    route_after_reflect,
)
from .summary_state import SummaryState

COLLECT = "collect"
DIFF = "diff"
REFLECT = "reflect"
FALLBACK = "fallback"
COMPOSE = "compose"
CONFIRM = "confirm"

# 状态里存的是领域模型，需显式允许反序列化（否则未来版本会被拦下）
ALLOWED_MSGPACK_MODULES: list[tuple[str, ...]] = [
    ("moneyrouter_agent.domain.summary", "PlanActualDiff"),
    ("moneyrouter_agent.domain.summary", "LayerDiff"),
    ("moneyrouter_agent.domain.summary", "CategoryDiff"),
    ("moneyrouter_agent.domain.summary", "MonthlySummary"),
    ("moneyrouter_agent.domain.summary", "SummaryDraft"),
    ("moneyrouter_agent.domain.summary", "LessonDraft"),
    ("moneyrouter_agent.domain.summary", "EventDraft"),
    ("moneyrouter_agent.domain.experience", "ExperiencePack"),
    ("moneyrouter_agent.domain.experience", "Lesson"),
    ("moneyrouter_agent.domain.profile_delta", "ProfileEvent"),
    ("moneyrouter_agent.domain.profile_delta", "ProfileFieldUpdate"),
    ("moneyrouter_agent.domain.profile_delta", "EventEffect"),
    ("moneyrouter_agent.domain.profile_delta", "ProfileDelta"),
    ("moneyrouter_agent.domain.month", "MonthSnapshot"),
    ("moneyrouter_agent.domain.month", "CategorySpend"),
    ("moneyrouter_agent.domain.month", "IncomeFact"),
    ("moneyrouter_agent.domain.month", "Baseline"),
    ("moneyrouter_agent.domain.month", "OneOffItem"),
    ("moneyrouter_agent.domain.month", "MonthlyPoint"),
    ("moneyrouter_agent.domain.month", "GoalAlignment"),
    ("moneyrouter_agent.domain.month", "FundAllocation"),
    ("moneyrouter_agent.domain.month", "HoldingReturn"),
    ("moneyrouter_agent.domain.month", "InvestmentSnapshot"),
    ("moneyrouter_agent.domain.month", "MonthResult"),
    ("moneyrouter_agent.domain.probe", "Probe"),
    ("moneyrouter_agent.history", "MonthRecord"),
]


def default_checkpointer() -> Any:
    """默认持久化检查点（允许我们的领域模型参与序列化）。"""
    return make_checkpointer('summary', ALLOWED_MSGPACK_MODULES)


def build_summary_graph(
    *,
    reflect_runner: ReflectRunner,
    code: SummaryCodeLayer,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译总结图（默认持久化检查点）。"""
    builder = StateGraph(SummaryState)
    builder.add_node(COLLECT, make_collect(code))
    builder.add_node(DIFF, make_diff(code))
    builder.add_node(REFLECT, make_reflect(reflect_runner, code))
    builder.add_node(FALLBACK, make_fallback(code))
    builder.add_node(COMPOSE, make_compose(code))
    builder.add_node(CONFIRM, confirm_node)

    builder.add_edge(START, COLLECT)
    builder.add_edge(COLLECT, DIFF)
    builder.add_edge(DIFF, REFLECT)
    builder.add_conditional_edges(
        REFLECT,
        route_after_reflect,
        {FALLBACK: FALLBACK, COMPOSE: COMPOSE},
    )
    builder.add_edge(FALLBACK, COMPOSE)
    builder.add_edge(COMPOSE, CONFIRM)

    return builder.compile(
        checkpointer=checkpointer if checkpointer is not None else default_checkpointer()
    )
