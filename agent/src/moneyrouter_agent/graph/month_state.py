"""本月实况图的共享状态。

多轮机制由 LangGraph 承担：``messages`` 用 ``add_messages`` 累积并自动回放；
``snapshot`` / ``probes`` 等跨轮字段由节点覆盖式更新。

注意：探测阈值、注入的预算/历史/目标**不放进状态**——它们在装配图时由闭包捕获，
以免把非序列化对象塞进检查点。
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from ..domain.month import MonthResult, MonthSnapshot
from ..domain.probe import Probe


class MonthState(TypedDict, total=False):
    """本月实况图的共享状态。"""

    # 完整对话历史（喂回模型）
    messages: Annotated[list[AnyMessage], add_messages]
    # 输入
    period: str
    bill_text: str | None
    imported: bool
    parse_warnings: list[str]
    # 本月实况（代码层 + 口述层合并后）
    snapshot: MonthSnapshot
    # 关注点（覆盖式，按 id 合并后）与待问 id
    probes: list[Probe]
    open_probe_ids: list[str]
    # 累积的额外了解（要点，非转录）
    notes: str
    # 模型对是否可收尾的判断（供观测，不作代码门槛）
    ready_to_finalize: bool
    rationale: str
    # 收尾产物
    final_result: MonthResult | None
    articulation: str
    # 核对环节
    confirmed: bool
    awaiting_confirmation: bool
    # 观测 / 降级
    turn_count: int
    degraded: bool
    error: str | None
    reasoning: str | None
