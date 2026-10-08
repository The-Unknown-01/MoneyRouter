"""方案生成图状态。

**输入（画像 / 金融简报 / 账单快照 / 经验包）不进状态**——它们在装配图时由 ``PlanContext``
闭包捕获（见 :mod:`moneyrouter_agent.domain.plan`），以免把非序列化对象塞进检查点。
状态里只放：对话消息、代码算出的方案与复核结论、模型的结构化决策、以及观测用的轨迹。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from ..domain.plan import CashflowSummary, Plan, ValidationReport
from ..domain.plan_turn import PlanAdjustment, PlanTurnDecision


class PlanState(TypedDict, total=False):
    """方案生成图的状态。除累积字段外一律**覆盖式**（写了就整块替换，没写保留旧值）。"""

    messages: Annotated[list[AnyMessage], add_messages]  # 累积 + 自动回放
    proposal_attempts: int
    validation_feedback: list[str]
    clarification_target: str
    period: str
    cashflow: CashflowSummary
    decision: PlanTurnDecision | None
    adjustment: PlanAdjustment | None
    adjustment_text: str
    plan: Plan | None
    validation: ValidationReport | None
    tool_rounds: int
    notes: str
    ready_to_finalize: bool
    rationale: str
    confirmed: bool
    awaiting_confirmation: bool
    turn_count: int
    degraded: bool
    error: str | None
    trace: Annotated[list[str], operator.add]  # 每步追加，供展示「方案如何产生」
    reasoning: Annotated[list[str], operator.add]  # 仅观测，绝不回传模型


__all__: list[Any] = ["PlanState"]
