"""单轮决策 schema（converse 节点的结构化输出）。

字段语义放在 ``Field(description=...)`` 里——**schema 管格式、prompt 管判断**。
这是本模块里**唯一"模型可写面"**：文件来源字段与计算字段随后由代码层覆盖（见 ``month_nodes._overlay_code_layer``）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .month import MonthSnapshot


class ProbeAnswer(BaseModel):
    """用户对某条关注点给出的解释。"""

    model_config = ConfigDict(extra="ignore")

    probe_id: str = Field(description="对应哪条关注点，用系统给出的标识原样填。")
    answer: str = Field(description="用户对这个关注点给出的解释/归因，一两句话要点即可。")


class MonthTurnDecision(BaseModel):
    """converse 节点每轮返回的结构化决策。"""

    model_config = ConfigDict(extra="ignore")

    reply: str = Field(
        description="要对用户说的话（口语；通常是「对上一句的回应 + 一个提问」，收尾轮为收尾语）。"
    )
    snapshot: MonthSnapshot = Field(
        default_factory=MonthSnapshot,
        description=(
            "你当前掌握的**本月实况全量快照**（全量，不是增量）。"
            "你只填从对话里了解到的口述事实；账单来源的分类与合计、结余、基线等由系统维护，不用你管。"
            "没了解到的留空，不要填默认值、不要推测。金额一律用「分」；0 是合法值。"
        ),
    )
    probe_answers: list[ProbeAnswer] = Field(
        default_factory=list,
        description="本轮用户对系统给出的关注点作出的解释，逐条对应；没解释到的不填。",
    )
    notes: str = Field(
        default="",
        description=(
            "累积的「额外了解」：有价值但装不进分类的信息（消费习惯、顾虑、一次性原因……）。"
            "记要点与结论即可，不要逐句转录对话、不要复述你是怎么提问的；跨轮累积。"
        ),
    )
    ready_to_finalize: bool = Field(
        default=False, description="你认为现在是否已足以还原这个月的真实情况，可以收尾。"
    )
    rationale: str = Field(
        default="", description="一句话说明本轮为何这样问 / 为何收尾。仅供调试观测，不展示给用户。"
    )
