"""与 Go 侧对接的接口契约（本步只定义，不启用服务）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..domain.experience import ExperiencePack
from ..domain.finance import FinanceBriefing, FinanceBriefingResult
from ..domain.month import MonthResult, MonthSnapshot
from ..domain.plan import CashflowSummary, Plan, ValidationReport
from ..domain.profile import Profile
from ..domain.profile_delta import ProfileDelta
from ..domain.probe import Probe
from ..domain.summary import MonthlySummary


class TurnRequest(BaseModel):
    """一次对话回合的请求体。"""

    thread_id: str = Field(description="会话/用户标识，与 LangGraph 的 thread_id 对齐。")
    user_message: str = Field(default="", description="用户这一轮说的话；首轮可留空。")
    resume: dict[str, Any] | str | None = Field(
        default=None,
        description="当会话停在确认环节时，用 {action: confirm|edit|more, message?: str} 恢复。",
    )


class TurnResult(BaseModel):
    """一次对话回合的结果（也是快照的返回形状）。"""

    model_config = ConfigDict(extra="ignore")

    thread_id: str
    reply: str = ""
    understanding: Profile = Field(default_factory=Profile, description="当前理解快照。")
    notes: str = ""
    ready_to_finalize: bool = False
    rationale: str = Field(default="", description="模型给的收尾理由，仅供观测。")
    awaiting_confirmation: bool = False
    confirmation_payload: dict[str, Any] | None = Field(
        default=None, description="停在确认环节时，交给用户核对的画像内容。"
    )
    confirmed: bool = False
    final_profile: Profile | None = None
    articulation: str | None = None
    degraded: bool = False
    error: str | None = None
    turn_count: int = 0
    reasoning: str | None = Field(
        default=None,
        description="模型最近一轮的思维链（reasoning_content），仅供观测与调优，不参与后续对话。",
    )


class FinanceContextRequest(BaseModel):
    """生成一份金融简报的请求体。"""

    model_config = ConfigDict(extra="ignore")

    request: str = Field(description="需求描述：给谁、做什么样的规划，模型据此决定检索方向与叙述重点。")
    as_of: str | None = Field(default=None, description="信息基准日期 YYYY-MM-DD，默认今天。")
    period: str | None = Field(default=None, description="统计期间 YYYY-MM，默认取 as_of 所在月份。")
    focus: list[str] | None = Field(
        default=None,
        description="本次关注的检索方面；留空表示「宏观与市场环境 / 财经新闻与政策 / 跨资产行情」全要。",
    )


class FinanceBriefingResponse(BaseModel):
    """响应体：指标表 + 当月分析 + 来源 + 观测轨迹。

    ``trace`` / ``reasoning`` 只用于本地调优，接 Go 时可以不传。
    """

    model_config = ConfigDict(extra="ignore")

    request: str = ""
    briefing: FinanceBriefing
    trace: list[str] = Field(default_factory=list)
    reasoning: list[str] = Field(default_factory=list)


class MonthTurnRequest(BaseModel):
    """本月实况的一次对话回合请求体。"""

    model_config = ConfigDict(extra="ignore")

    thread_id: str = Field(description="会话/用户标识，与 LangGraph 的 thread_id 对齐。")
    user_message: str = Field(default="", description="用户这一轮说的话；首轮可留空。")
    resume: dict[str, Any] | str | None = Field(
        default=None,
        description="当会话停在核对环节时，用 {action: confirm|edit|more, message?: str} 恢复。",
    )
    bill: str | None = Field(
        default=None,
        description="首轮可选的导入内容：规范文档 JSON（推荐）或 CSV 宽表文本；自动识别。",
    )
    period: str | None = Field(default=None, description="统计期间 YYYY-MM。")


class MonthTurnResult(BaseModel):
    """本月实况的一次对话回合结果（也是快照的返回形状）。"""

    model_config = ConfigDict(extra="ignore")

    thread_id: str
    reply: str = ""
    snapshot: MonthSnapshot = Field(default_factory=MonthSnapshot, description="当前月度实况快照。")
    probes: list[Probe] = Field(default_factory=list, description="关注点清单（含状态与归因）。")
    notes: str = ""
    ready_to_finalize: bool = False
    rationale: str = Field(default="", description="模型给的收尾理由，仅供观测。")
    awaiting_confirmation: bool = False
    confirmation_payload: dict[str, Any] | None = Field(
        default=None, description="停在核对环节时，交给用户核对的快照内容。"
    )
    confirmed: bool = False
    final_result: MonthResult | None = None
    articulation: str | None = None
    parse_warnings: list[str] = Field(default_factory=list, description="账单解析告警。")
    recorded: bool = Field(default=False, description="本轮落定后是否已写入历史留档。")
    history_periods: list[str] = Field(
        default_factory=list, description="已有的历史月份（升序），供前端展示「已有几个月历史」。"
    )
    history_location: str | None = Field(
        default=None, description="本轮历史留档的写入位置（字符串标识），仅供观测。"
    )
    history_warnings: list[str] = Field(
        default_factory=list, description="历史读取时跳过的坏记录等，如实回报。"
    )
    degraded: bool = False
    error: str | None = None
    turn_count: int = 0
    reasoning: str | None = Field(
        default=None,
        description="模型最近一轮的思维链（reasoning_content），仅供观测与调优，不参与后续对话。",
    )


class PlanRequest(BaseModel):
    """方案生成的一次对话回合请求体。

    输入（画像 / 金融简报 / 账单快照 / 经验包）由服务端在**进程内**注入 ``PlanAgent``，
    不走 HTTP；这里只承载多轮对话本身。
    """

    model_config = ConfigDict(extra="ignore")

    thread_id: str = Field(description="会话/用户标识，与 LangGraph 的 thread_id 对齐。")
    user_message: str = Field(default="", description="用户这一轮说的话；首轮可留空。")
    resume: dict[str, Any] | str | None = Field(
        default=None,
        description="当会话停在确认环节时，用 {action: confirm|edit|more, message?: str} 恢复。",
    )
    period: str | None = Field(default=None, description="方案所属期间 YYYY-MM。")


class PlanResult(BaseModel):
    """方案生成的一次对话回合结果（也是快照的返回形状）。"""

    model_config = ConfigDict(extra="ignore")

    thread_id: str
    reply: str = ""
    plan: Plan | None = Field(default=None, description="当前方案（含金额、风险、配置与叙述）。")
    validation: ValidationReport | None = Field(default=None, description="独立复核结论。")
    cashflow: CashflowSummary | None = Field(default=None, description="现金流概览。")
    ready_to_finalize: bool = False
    awaiting_confirmation: bool = False
    confirmation_payload: dict[str, Any] | None = Field(
        default=None, description="停在确认环节时，交给用户核对的方案内容。"
    )
    confirmed: bool = False
    rationale: str = Field(default="", description="模型给的判断理由，仅供观测。")
    notes: str = ""
    degraded: bool = False
    error: str | None = None
    turn_count: int = 0
    reasoning: str | None = Field(
        default=None,
        description="模型最近一轮的思维链（reasoning_content），仅供观测与调优，不参与后续对话。",
    )
    trace: list[str] = Field(default_factory=list, description="各步轨迹，便于展示「方案如何产生」。")


class SummaryRequest(BaseModel):
    """一次月度复盘的请求体。"""

    model_config = ConfigDict(extra="ignore")

    thread_id: str = Field(description="会话/用户标识，与 LangGraph 的 thread_id 对齐。")
    period: str = Field(default="", description="要复盘哪个月，YYYY-MM；留空取最近一个已落定的月份。")
    user_id: str = Field(default="default", description="落档用的用户标识（决定文件名）。")
    resume: dict[str, Any] | str | None = Field(
        default=None,
        description="会话停在核对环节时，用 {action: confirm|edit, message?: str} 恢复。",
    )
    plan: Plan | None = Field(
        default=None, description="上一版方案；用于做「计划 vs 实际」的对照，缺则只谈实际。"
    )


class SummaryResult(BaseModel):
    """一次月度复盘的结果：三份产物 + 核对状态。

    三份产物的消费方各不相同——经验包喂方案生成，画像增量喂画像，月度复盘给用户看。
    """

    model_config = ConfigDict(extra="ignore")

    thread_id: str
    period: str = ""
    summary: MonthlySummary = Field(default_factory=MonthlySummary, description="给用户看的月度复盘。")
    pack: ExperiencePack = Field(default_factory=ExperiencePack, description="经验包（喂方案生成）。")
    profile_delta: ProfileDelta = Field(
        default_factory=ProfileDelta, description="本期画像增量（append-only 追加）。"
    )
    awaiting_confirmation: bool = False
    confirmation_payload: dict[str, Any] | None = Field(
        default=None, description="停在核对环节时，交给用户确认的内容。"
    )
    confirmed: bool = False
    written: bool = Field(default=False, description="确认之后三份产物是否已写盘。")
    write_location: str | None = Field(default=None, description="写盘位置标识，仅供观测。")
    write_warnings: list[str] = Field(default_factory=list, description="写盘/读取时如实回报的问题。")
    degraded: bool = False
    error: str | None = None
    reasoning: str | None = Field(
        default=None, description="模型最近一轮的思维链（reasoning_content），仅供观测。"
    )
    trace: list[str] = Field(default_factory=list, description="各步轨迹。")


__all__ = [
    "FinanceBriefingResponse",
    "FinanceBriefingResult",
    "FinanceContextRequest",
    "MonthTurnRequest",
    "MonthTurnResult",
    "PlanRequest",
    "PlanResult",
    "SummaryRequest",
    "SummaryResult",
    "TurnRequest",
    "TurnResult",
]
