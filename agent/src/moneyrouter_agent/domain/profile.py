"""画像领域模型。

设计要点（对齐计划第三节）：

- ``ProfileDraft`` 是访谈过程中的「活理解」快照，``None`` 表示尚未了解到；
- **不做必需字段门槛**——完成与否完全由模型判断，这里只是承载理解的容器；
- 字段语义写在 ``Field(description=...)`` 里，交给结构化输出（schema 管格式、prompt 管判断）；
- ``max_loss_pct=0`` 是合法值（完全不接受亏损），**不能**用 0 表示「未回答」。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .money import GOAL_MAX_CHARS, valid_horizon_months, valid_max_loss_pct

Experience = Literal["none", "some", "experienced"]


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class ProfileDraft(BaseModel):
    """访谈过程中的活理解；未了解到的字段一律留 ``None``，不填默认值、不推测。"""

    model_config = ConfigDict(extra="ignore")

    occupation: str | None = Field(
        default=None,
        description="职业，如「学生」「程序员」「自由职业者」。未识别到就留空。",
    )
    income_cents: int | None = Field(
        default=None,
        description="收入金额，以「分」为单位（8000 元 = 800000）。学生场景填「每月生活费」。",
    )
    income_basis: str | None = Field(
        default=None,
        description="收入口径说明，如「税后月薪」「每月生活费」「接单收入」。",
    )
    income_stable: bool | None = Field(default=None, description="收入是否稳定。")
    family_load: bool | None = Field(default=None, description="是否需要承担家庭负担（养家）。")
    debt_cents: int | None = Field(default=None, description="负债金额（分）；没有负债填 0。")
    reserve_cents: int | None = Field(default=None, description="现有可动用的储蓄/应急金（分）。")
    horizon_months: int | None = Field(
        default=None, description="这笔钱预计多久后会用，单位「月」，1-600。"
    )
    max_loss_pct: int | None = Field(
        default=None,
        description="可承受的最大亏损百分比 0-100；0 表示完全不接受亏损，是合法值。",
    )
    experience: Experience | None = Field(
        default=None,
        description="投资经验：none 没有 / some 有一点 / experienced 比较熟。",
    )
    goal: str | None = Field(
        default=None, description="理财目标，用户自己的说法，不超过 500 字。"
    )

    @model_validator(mode="after")
    def _sanitize(self) -> "ProfileDraft":
        """把空串归零、把越界值清空——但不给出任何默认值。"""
        for name in ("occupation", "income_basis", "goal"):
            value = _blank_to_none(getattr(self, name))
            if isinstance(value, str):
                value = value.strip()
            setattr(self, name, value)

        if self.goal is not None:
            self.goal = self.goal[:GOAL_MAX_CHARS]

        for name in ("income_cents", "debt_cents", "reserve_cents"):
            value = getattr(self, name)
            if value is not None and value < 0:
                setattr(self, name, None)

        if self.horizon_months is not None and not valid_horizon_months(self.horizon_months):
            self.horizon_months = None
        if self.max_loss_pct is not None and not valid_max_loss_pct(self.max_loss_pct):
            self.max_loss_pct = None
        return self


class Profile(ProfileDraft):
    """收尾后的正式画像（字段与 ``ProfileDraft`` 一致，允许仍为 ``None``）。"""


class ProfileResult(BaseModel):
    """最终输出：规整的结构化基础信息 + 一段自由表述。"""

    model_config = ConfigDict(extra="ignore")

    profile: Profile = Field(default_factory=Profile, description="规整的结构化基础信息。")
    articulation: str = Field(
        default="",
        description=(
            "用户的结论性画像（第三人称客观口吻）：是谁、经济状况如何、攒钱为了什么、多久要用、"
            "能承受多大波动、投资经验如何、哪些地方需要再解释。"
            "只写结论不写过程——不复述访谈怎么进行、不引用用户原话、不逐条清点细节。"
        ),
    )
