"""图装配。

    START -> converse -> (wait: END) / (finalize) / (fallback)
    finalize -> confirm -> (Command: END 或 converse)
    fallback -> END

``confirm`` 不加静态出边，由 ``Command(goto=...)`` 决定去向，避免双跑。
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from .nodes import (
    FinalizeRunner,
    TurnRunner,
    confirm_node,
    fallback_node,
    make_converse,
    make_finalize,
    route_after_converse,
)
from .state import InterviewState

CONVERSE = "converse"
FINALIZE = "finalize"
CONFIRM = "confirm"
FALLBACK = "fallback"

# 状态里存的是领域模型，需显式允许反序列化（否则未来版本会被拦下）
ALLOWED_MSGPACK_MODULES: list[tuple[str, ...]] = [
    ("moneyrouter_agent.domain.profile", "ProfileDraft"),
    ("moneyrouter_agent.domain.profile", "Profile"),
]


def default_checkpointer() -> InMemorySaver:
    """默认的内存检查点（允许我们的领域模型参与序列化）。"""
    return InMemorySaver(
        serde=JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MSGPACK_MODULES)
    )


def build_interview_graph(
    *,
    turn_runner: TurnRunner,
    finalize_runner: FinalizeRunner,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译画像访谈图（默认内存检查点）。"""
    builder = StateGraph(InterviewState)
    builder.add_node(CONVERSE, make_converse(turn_runner))
    builder.add_node(FINALIZE, make_finalize(finalize_runner))
    builder.add_node(CONFIRM, confirm_node)
    builder.add_node(FALLBACK, fallback_node)

    builder.add_edge(START, CONVERSE)
    builder.add_conditional_edges(
        CONVERSE,
        route_after_converse,
        {FINALIZE: FINALIZE, FALLBACK: FALLBACK, "wait": END},
    )
    builder.add_edge(FINALIZE, CONFIRM)
    builder.add_edge(FALLBACK, END)

    return builder.compile(checkpointer=checkpointer if checkpointer is not None else default_checkpointer())
