"""图状态定义。

多轮对话的机制由 LangGraph 承担：``messages`` 用 ``add_messages`` 累积并自动回放，
``understanding`` / ``notes`` 等作为跨轮状态字段由节点覆盖式更新。
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from ..domain.profile import Profile, ProfileDraft


class InterviewState(TypedDict, total=False):
    """画像访谈图的共享状态。"""

    # 完整对话历史（喂回模型——这是与旧 Go 实现最大的区别）
    messages: Annotated[list[AnyMessage], add_messages]
    # 当前理解快照（覆盖式）
    understanding: ProfileDraft
    # 累积的「额外了解」（要点与结论，非对话转录）
    notes: str
    refused_fields: dict[str, str]
    pending_questions: list[str]
    # 模型判断与字段完整性检查共同决定收尾
    ready_to_finalize: bool
    rationale: str
    # 收尾产物
    final_profile: Profile | None
    articulation: str
    # 确认环节
    confirmed: bool
    awaiting_confirmation: bool
    # 观测/降级
    turn_count: int
    degraded: bool
    error: str | None
    # 模型本轮思维链（官方 reasoning_content）：**仅观测**，绝不回传模型
    reasoning: str | None
