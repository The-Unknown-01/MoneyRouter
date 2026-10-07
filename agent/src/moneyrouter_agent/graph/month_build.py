"""本月实况图装配。

    START → ingest → survey → converse ──(未收尾)──→ END（等下一句）
                                        ├──(已够)──→ finalize → confirm(interrupt) ──confirm──→ END
                                        │                                          └edit/more──→ converse
                                        └──(降级)──→ fallback → END

``confirm`` 不加静态出边，由 ``Command(goto=...)`` 决定去向，避免双跑。
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from .month_nodes import (
    CodeLayer,
    FinalizeRunner,
    TurnRunner,
    confirm_node,
    fallback_node,
    make_converse,
    make_finalize,
    make_ingest,
    make_survey,
    route_after_converse,
)
from .month_state import MonthState

INGEST = "ingest"
SURVEY = "survey"
CONVERSE = "converse"
FINALIZE = "finalize"
CONFIRM = "confirm"
FALLBACK = "fallback"

# 状态里存的是领域模型，需显式允许反序列化（否则未来版本会被拦下）
ALLOWED_MSGPACK_MODULES: list[tuple[str, ...]] = [
    ("moneyrouter_agent.domain.month", "MonthSnapshot"),
    ("moneyrouter_agent.domain.month", "MonthResult"),
    ("moneyrouter_agent.domain.month", "CategorySpend"),
    ("moneyrouter_agent.domain.month", "IncomeFact"),
    ("moneyrouter_agent.domain.month", "Baseline"),
    ("moneyrouter_agent.domain.month", "OneOffItem"),
    ("moneyrouter_agent.domain.month", "MonthlyPoint"),
    ("moneyrouter_agent.domain.month", "GoalAlignment"),
    ("moneyrouter_agent.domain.month", "FundAllocation"),
    ("moneyrouter_agent.domain.month", "HoldingReturn"),
    ("moneyrouter_agent.domain.month", "InvestmentSnapshot"),
    ("moneyrouter_agent.domain.probe", "Probe"),
]


def default_checkpointer() -> InMemorySaver:
    """默认的内存检查点（允许我们的领域模型参与序列化）。"""
    return InMemorySaver(
        serde=JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MSGPACK_MODULES)
    )


def build_month_graph(
    *,
    turn_runner: TurnRunner,
    finalize_runner: FinalizeRunner,
    code: CodeLayer,
    one_off_min_cents: int,
    parser: Any | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译本月实况图（默认内存检查点）。"""
    builder = StateGraph(MonthState)
    builder.add_node(INGEST, make_ingest(one_off_min_cents=one_off_min_cents, parser=parser))
    builder.add_node(SURVEY, make_survey(code))
    builder.add_node(CONVERSE, make_converse(turn_runner, code))
    builder.add_node(FINALIZE, make_finalize(finalize_runner, code))
    builder.add_node(CONFIRM, confirm_node)
    builder.add_node(FALLBACK, fallback_node)

    builder.add_edge(START, INGEST)
    builder.add_edge(INGEST, SURVEY)
    builder.add_edge(SURVEY, CONVERSE)
    builder.add_conditional_edges(
        CONVERSE,
        route_after_converse,
        {FINALIZE: FINALIZE, FALLBACK: FALLBACK, "wait": END},
    )
    builder.add_edge(FINALIZE, CONFIRM)
    builder.add_edge(FALLBACK, END)

    return builder.compile(checkpointer=checkpointer if checkpointer is not None else default_checkpointer())
