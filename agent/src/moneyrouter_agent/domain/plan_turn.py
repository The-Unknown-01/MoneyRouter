"""与模型交互的 schema（方案生成的"模型可写面"）。

字段语义放在 ``Field(description=...)`` 里——**schema 管格式、prompt 管判断**。
金额计算字段不在这里：它们全部由 :mod:`moneyrouter_agent.domain.plan` 的纯函数写入。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .wallet import WalletProposal

RiskLevel = Literal["低", "中", "高"]


class WalletTurnDecision(BaseModel):
    """判断节点每轮的结构化决策：给出方案，还是先问清楚。"""

    model_config = ConfigDict(extra="ignore")

    reply: str = Field(
        description="要对用户说的话（口语）：追问时是一个关键问题；给方案时是简短的引入语。"
    )
    status: Literal["ask", "finalize"] = Field(
        description="ask=资料还不够、需要先问清楚；finalize=可以给出方案了。"
    )
    questions: list[str] = Field(
        default_factory=list, description="status=ask 时要问的具体问题（最多两三个，问最关键的）。"
    )
    missing: list[str] = Field(
        default_factory=list, description="还缺哪些关键资料（如收入、债务还款、可承受风险）。"
    )
    proposal: WalletProposal | None = Field(default=None, description="finalize 必须给完整自主钱包方案")
    clarification_target: Literal["month"] = "month"
    notes: str = Field(default="", description="累积的额外了解：值得留意的偏好或约束，不复述对话过程。")


class PlanTurnDecision(WalletTurnDecision):
    """Read compatibility for archived decisions; new model calls use WalletTurnDecision."""

    # Read compatibility for older test/archived decisions; v2 ignores these fields.
    wants_ratio_pct: float | None = Field(default=None, description="历史兼容字段，v2 不使用，无默认预算比例")
    risk_level_override: RiskLevel | None = Field(default=None, description="历史兼容字段，v2 不使用固定风险配比")
    reserve_months: int | None = Field(default=None, description="历史兼容字段，v2 不使用固定预备金月数")
    headline: str = Field(default="", description="status=finalize 时的一句话总述。")
    sections: list[str] = Field(
        default_factory=list,
        description="status=finalize 时的分段解释：现金流、预备金、风险与配置、目标前景。"
        "只复述工具给出的数字，不要自己计算、不要新增数值。",
    )
    caveats: list[str] = Field(
        default_factory=list, description="需要提醒用户的前提与风险（如情景不是预测、信息缺口）。"
    )
    notes: str = Field(
        default="",
        description="累积的额外了解（要点）：值得留意的偏好或约束。不要复述对话过程。",
    )
    rationale: str = Field(default="", description="一句话说明本轮为何这样判断。仅供内部观测。")


class PlanAdjustment(BaseModel):
    """把用户对方案的修改意见，提炼成结构化调整要求（不直接给金额结论）。"""

    model_config = ConfigDict(extra="ignore")

    category_cap_cents: dict[str, int] = Field(
        default_factory=dict, description="某类支出的上限（分）；只对可选类生效。"
    )
    wants_ratio_pct: float | None = Field(
        default=None, description="可选支出上限占收入的比例（%），覆盖默认值。"
    )
    risk_level_override: RiskLevel | None = Field(
        default=None, description="用户明确要求调整的风险档；最终只会更保守。"
    )
    reserve_months: int | None = Field(default=None, description="应急预备金目标月数，只接受 3 或 6。")
    goal: str | None = Field(default=None, description="目标发生变化时的新描述。")
    horizon_months: int | None = Field(default=None, description="资金使用期限的变化（月），1-600。")
    max_loss_pct: int | None = Field(default=None, description="可承受最大亏损的变化，0-100；0 是合法值。")
    notes: str = Field(default="", description="无法结构化但值得保留的要点。")
