"""单轮决策 schema（converse 节点的结构化输出）。

字段语义放在 ``Field(description=...)`` 里——**schema 管格式、prompt 管判断**。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .profile import ProfileDraft


class TurnDecision(BaseModel):
    """converse 节点每轮返回的结构化决策。"""

    model_config = ConfigDict(extra="ignore")

    reply: str = Field(
        description="要对用户说的话（口语；通常是「对上一句的回应 + 一个提问」，收尾轮为收尾语）。",
    )
    understanding: ProfileDraft = Field(
        default_factory=ProfileDraft,
        description=(
            "你对这个人当前的完整理解快照（全量，不是增量）。"
            "没了解到的字段留空，不要填默认值。"
            "income_cents 以「分」为单位；income_basis 写清口径（如「每月生活费」「税后月薪」）；"
            "max_loss_pct=0 表示完全不接受亏损，是合法值。"
        ),
    )
    notes: str = Field(
        default="",
        description=(
            "累积的「额外了解」：有价值但装不进标准字段的信息"
            "（消费习惯、顾虑、家庭、对钱的态度……）。记要点与结论即可——"
            "不要逐句转录对话，也不要复述你是怎么提问的；跨轮累积。"
        ),
    )
    ready_to_finalize: bool = Field(
        default=False,
        description="你认为现在是否已经可以收尾（够了解这个人了）。",
    )
    rationale: str = Field(
        default="",
        description="一句话说明本轮为何收尾 / 为何这样问。仅供调试观测，不展示给用户。",
    )
