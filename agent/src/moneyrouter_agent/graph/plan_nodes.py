"""方案生成图节点：ingest / agent / tools / decide / compose / validate / adjust / confirm / fallback。

核心分工：

- **agent（模型）**：``bind_tools`` 自主调用计算工具，可追问、可收尾；由 ``ToolNode`` 执行工具。
- **decide（模型）**：把「追问还是出方案」连同少量策略旋钮，交给结构化输出。
- **compose / validate（纯代码）**：按输入与旋钮跑完七步得到规范方案，再独立复核——**数字唯一来源**。
- **confirm（纯代码）**：``interrupt`` 让用户确认；不同意则 ``edit`` 交模型解析成调整参数后重算，或 ``more`` 补充信息。

纪律：任何一步出错都**就地降级**（写 ``error`` / 走 ``fallback``），绝不抛给调用方。
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END
from langgraph.types import Command, interrupt

from ..domain.money import GOAL_MAX_CHARS, format_yuan, valid_horizon_months, valid_max_loss_pct
from ..domain.plan import (
    WANTS_RATIO_CAP_PCT,
    Plan,
    PlanContext,
    PlanInputs,
    PlanStrategy,
    ValidationReport,
    build_plan,
    summarize_cashflow,
    validate_plan,
)
from ..domain.plan_turn import PlanAdjustment, PlanTurnDecision
from ..model.deepseek import StructuredCall
from ..prompts.plan import (
    PLAN_ADJUST_SYSTEM,
    PLAN_DECIDE_SYSTEM,
    PLAN_SYSTEM,
    build_adjust_human,
    build_context_block,
    build_decide_human,
)
from ..prompts.plan_rules import PLAN_DEGRADED_NOTICE, render_plan_narrative

AgentRunner = Callable[[list[BaseMessage]], Any]
DecideRunner = Callable[[list[BaseMessage]], Any]
AdjustRunner = Callable[[list[BaseMessage]], Any]

# 节点名（build 侧复用）
INGEST = "ingest"
AGENT = "agent"
TOOLS = "tools"
DECIDE = "decide"
COMPOSE = "compose"
VALIDATE = "validate"
ADJUST = "adjust"
CONFIRM = "confirm"
FALLBACK = "fallback"

CONFIRM_ACTIONS = ("confirm", "edit", "more")


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _split_runner_result(result: Any) -> tuple[Any, str | None]:
    """兼容真实模型的 ``StructuredCall`` 与脚本化假模型直接返回的领域对象。"""
    if isinstance(result, StructuredCall):
        return result.parsed, result.reasoning
    return result, None


def _normalize_resume(resume: Any) -> dict[str, Any]:
    """把 ``interrupt`` 的恢复值统一成 ``{"action": ..., "message": ...}``。"""
    if isinstance(resume, str):
        return {"action": resume.strip().lower() or "confirm", "message": None}
    if isinstance(resume, dict):
        action = str(resume.get("action") or resume.get("type") or "confirm").strip().lower()
        return {"action": action or "confirm", "message": resume.get("message")}
    return {"action": "confirm", "message": None}


# --------------------------------------------------------------------------- #
# 渲染（上下文组装属节点职责，不写进 prompt 常量）
# --------------------------------------------------------------------------- #
def _render_profile_lines(profile: Any) -> str:
    if profile is None:
        return "（暂无画像）"
    lines: list[str] = []
    if profile.occupation:
        lines.append(f"职业：{profile.occupation}")
    if profile.income_cents is not None:
        lines.append(f"税后月收入：{format_yuan(profile.income_cents)} 元（{profile.income_basis or '口径未说明'}）")
    if profile.income_stable is not None:
        lines.append(f"收入是否稳定：{'是' if profile.income_stable else '否'}")
    if profile.family_load is not None:
        lines.append(f"家庭负担：{'有' if profile.family_load else '无'}")
    if profile.debt_cents is not None:
        lines.append(f"负债总额：{format_yuan(profile.debt_cents)} 元")
    if profile.reserve_cents is not None:
        lines.append(f"现有可动用储蓄/应急金：{format_yuan(profile.reserve_cents)} 元")
    if profile.horizon_months is not None:
        lines.append(f"资金使用期限：约 {profile.horizon_months} 个月")
    if profile.max_loss_pct is not None:
        lines.append(f"可接受最大亏损：{profile.max_loss_pct}%")
    if profile.experience:
        lines.append(f"投资经验：{profile.experience}")
    if profile.goal:
        lines.append(f"理财目标：{profile.goal}")
    return "\n".join(lines) or "（暂无画像）"


def _render_cashflow_lines(cashflow: Any) -> str:
    if cashflow is None:
        return "（暂无）"
    return (
        f"月均收入 {format_yuan(cashflow.income_cents)} 元；"
        f"必要开支 {format_yuan(cashflow.necessary_cents)} 元；"
        f"可选开支 {format_yuan(cashflow.optional_cents)} 元；"
        f"债务还款 {format_yuan(cashflow.debt_cents)} 元。（{cashflow.data_quality.notes}）"
    )


def _render_briefing_lines(briefing: Any) -> str:
    if briefing is None:
        return "（未接入金融简报）"
    lines: list[str] = []
    if briefing.as_of:
        lines.append(f"截至 {briefing.as_of}")
    if briefing.analysis and briefing.analysis.headline:
        lines.append(briefing.analysis.headline)
    for metric in briefing.metrics[:6]:
        lines.append(f"- {metric.label}：{metric.value}{metric.unit}（{metric.as_of}）")
    if briefing.sources:
        lines.append(f"可引用来源 {len(briefing.sources)} 条")
    return "\n".join(lines) or "（无）"


def _render_experience_lines(pack: Any) -> str:
    if pack is None or not pack.lessons:
        return ""
    return "\n".join(f"- {lesson.statement}" for lesson in pack.lessons if lesson.statement)


def _render_plan_lines(plan: Plan | None) -> str:
    if plan is None:
        return "（暂无）"
    cashflow = plan.cashflow
    budget = plan.budget
    reserve = plan.reserve
    lines = [
        f"月均收入 {format_yuan(cashflow.income_cents)} 元，必要 {format_yuan(cashflow.necessary_cents)} 元，"
        f"可选预算 {format_yuan(budget.wants_cents)} 元，可储蓄 {format_yuan(budget.savings_cents)} 元",
        f"预备金目标 {format_yuan(reserve.target_cents)} 元（{reserve.months} 个月），缺口 "
        f"{format_yuan(reserve.gap_cents)} 元，本月补足 {format_yuan(reserve.monthly_cents)} 元，可投资 "
        f"{format_yuan(reserve.investable_cents)} 元",
        f"风险档 {plan.risk.level}（增长上限 {plan.risk.growth_cap_pct}%）",
    ]
    if plan.allocation.recommended:
        lines.append(
            "配置：" + "、".join(f"{s.category} {format_yuan(s.amount_cents)} 元" for s in plan.allocation.recommended)
        )
    return "\n".join(lines)


def _tool_notes_from_messages(messages: list[Any]) -> str:
    """把最近的工具返回结果整理成给模型看的要点。"""
    lines: list[str] = []
    for message in messages:
        if isinstance(message, ToolMessage):
            content = message.content if isinstance(message.content, str) else str(message.content)
            lines.append(f"[{message.name}] {content[:400]}")
    return "\n".join(lines[-8:])


# --------------------------------------------------------------------------- #
# 策略映射（模型旋钮 → 代码策略，一律有界）
# --------------------------------------------------------------------------- #
def _reserve_override(value: Any) -> int | None:
    return int(value) if value in (3, 6) else None


def _strategy_from_decision(decision: PlanTurnDecision | None) -> PlanStrategy:
    if decision is None:
        return PlanStrategy()
    return PlanStrategy(
        wants_ratio_pct=(
            decision.wants_ratio_pct if decision.wants_ratio_pct is not None else WANTS_RATIO_CAP_PCT
        ),
        risk_level_override=decision.risk_level_override,
        reserve_months_override=_reserve_override(decision.reserve_months),
    )


def _strategy_from_adjustment(adjustment: PlanAdjustment) -> PlanStrategy:
    return PlanStrategy(
        wants_ratio_pct=(
            adjustment.wants_ratio_pct if adjustment.wants_ratio_pct is not None else WANTS_RATIO_CAP_PCT
        ),
        risk_level_override=adjustment.risk_level_override,
        reserve_months_override=_reserve_override(adjustment.reserve_months),
        category_cap_cents=dict(adjustment.category_cap_cents or {}),
    )


def _inputs_with_adjustment(inputs: PlanInputs, adjustment: PlanAdjustment) -> PlanInputs:
    """把反馈里对画像的修改（目标/期限/可承受亏损）落到输入上。"""
    profile = inputs.profile
    if profile is not None:
        updates: dict[str, Any] = {}
        if adjustment.goal:
            updates["goal"] = adjustment.goal[:GOAL_MAX_CHARS]
        if adjustment.horizon_months is not None and valid_horizon_months(adjustment.horizon_months):
            updates["horizon_months"] = int(adjustment.horizon_months)
        if adjustment.max_loss_pct is not None and valid_max_loss_pct(adjustment.max_loss_pct):
            updates["max_loss_pct"] = int(adjustment.max_loss_pct)
        if updates:
            profile = profile.model_copy(update=updates)
    return inputs.model_copy(update={"profile": profile})


def _effective_inputs(context: PlanContext, state: dict) -> PlanInputs:
    adjustment = state.get("adjustment")
    if adjustment is None:
        return context.inputs
    return _inputs_with_adjustment(context.inputs, adjustment)


def _default_headline(plan: Plan) -> str:
    return f"本月资金安排（风险档 {plan.risk.level}）"


# --------------------------------------------------------------------------- #
# 节点：ingest（纯代码）
# --------------------------------------------------------------------------- #
def make_ingest(context: PlanContext) -> Callable[[dict], dict]:
    def ingest(state: dict) -> dict:
        cashflow = summarize_cashflow(context.inputs)
        return {
            "cashflow": cashflow,
            "period": context.inputs.period,
            "trace": [f"ingest：{cashflow.data_quality.notes}"],
        }

    return ingest


# --------------------------------------------------------------------------- #
# 节点：agent（模型 + 自主工具编排）
# --------------------------------------------------------------------------- #
def make_agent(
    agent_runner: AgentRunner | None,
    context: PlanContext,
    *,
    max_tool_rounds: int = 4,
) -> Callable[[dict], dict]:
    def agent(state: dict) -> dict:
        if agent_runner is None:
            return {
                "degraded": True,
                "error": "模型不可用",
                "trace": ["agent：模型不可用，转确定性管线"],
            }

        cashflow = state.get("cashflow") or summarize_cashflow(context.inputs)
        messages: list[BaseMessage] = [
            SystemMessage(content=PLAN_SYSTEM),
            SystemMessage(
                content=build_context_block(
                    profile_lines=_render_profile_lines(context.inputs.profile),
                    cashflow_lines=_render_cashflow_lines(cashflow),
                    briefing_lines=_render_briefing_lines(context.inputs.briefing),
                    experience_lines=_render_experience_lines(context.inputs.experience),
                )
            ),
            *state.get("messages", []),
        ]
        try:
            ai = agent_runner(messages)
        except Exception as exc:  # noqa: BLE001 - 就地降级，交给 fallback
            return {
                "degraded": True,
                "error": f"{type(exc).__name__}: {exc}",
                "trace": ["agent：模型调用失败，转确定性管线"],
            }

        calls = list(getattr(ai, "tool_calls", None) or [])
        rounds = int(state.get("tool_rounds") or 0) + (1 if calls else 0)
        if calls:
            names = "、".join(str(call.get("name")) for call in calls)
            trace = f"agent：调用工具 {names}"
        else:
            trace = "agent：本轮未再调用工具"
        return {
            "messages": [ai],
            "tool_rounds": rounds,
            "degraded": False,
            "error": None,
            "trace": [trace],
        }

    return agent


def route_after_agent(state: dict) -> str:
    """agent 之后：降级 / 去执行工具 / 进入判断。"""
    if state.get("degraded"):
        return FALLBACK
    messages = state.get("messages") or []
    last = messages[-1] if messages else None
    calls = list(getattr(last, "tool_calls", None) or [])
    return TOOLS if calls else DECIDE


def make_route_after_tools(max_tool_rounds: int) -> Callable[[dict], str]:
    """工具执行完：达到调用上限就进入判断，否则回到 agent 继续。"""

    def route_after_tools(state: dict) -> str:
        if int(state.get("tool_rounds") or 0) >= max_tool_rounds:
            return DECIDE
        return AGENT

    return route_after_tools


# --------------------------------------------------------------------------- #
# 节点：decide（模型结构化判断）
# --------------------------------------------------------------------------- #
def make_decide(decide_runner: DecideRunner | None, context: PlanContext) -> Callable[[dict], dict]:
    def decide(state: dict) -> dict:
        turn_count = int(state.get("turn_count") or 0) + 1
        if decide_runner is None:
            return {
                "degraded": True,
                "error": "模型不可用",
                "turn_count": turn_count,
                "ready_to_finalize": False,
                "trace": ["decide：模型不可用，转确定性管线"],
            }

        cashflow = state.get("cashflow") or summarize_cashflow(context.inputs)
        messages: list[BaseMessage] = [
            SystemMessage(content=PLAN_DECIDE_SYSTEM),
            HumanMessage(
                content=build_decide_human(
                    tool_notes=_tool_notes_from_messages(state.get("messages") or []),
                    cashflow_lines=_render_cashflow_lines(cashflow),
                )
            ),
        ]
        try:
            decision, reasoning = _split_runner_result(decide_runner(messages))
        except Exception as exc:  # noqa: BLE001 - 交给 fallback
            return {
                "degraded": True,
                "error": f"{type(exc).__name__}: {exc}",
                "turn_count": turn_count,
                "ready_to_finalize": False,
                "trace": ["decide：判断失败，转确定性管线"],
            }

        if not isinstance(decision, PlanTurnDecision):
            decision = PlanTurnDecision.model_validate(decision)

        update: dict[str, Any] = {
            "decision": decision,
            "ready_to_finalize": decision.status == "finalize",
            "rationale": decision.rationale,
            "notes": decision.notes or state.get("notes") or "",
            "turn_count": turn_count,
            "degraded": False,
            "error": None,
            "trace": [f"decide：{'给出方案' if decision.status == 'finalize' else '需要先问清楚'}"],
        }
        reply = (decision.reply or "").strip()
        if reply:
            update["messages"] = [AIMessage(content=reply)]
        if reasoning:
            update["reasoning"] = [reasoning]
        return update

    return decide


def route_after_decide(state: dict) -> str:
    """decide 之后：降级 / 组装方案 / 等用户回答。"""
    if state.get("degraded"):
        return FALLBACK
    if state.get("ready_to_finalize"):
        return COMPOSE
    return "wait"


# --------------------------------------------------------------------------- #
# 节点：compose（纯代码，跑完七步）
# --------------------------------------------------------------------------- #
def make_compose(context: PlanContext) -> Callable[[dict], dict]:
    def compose(state: dict) -> dict:
        adjustment = state.get("adjustment")
        if adjustment is not None:
            inputs = _inputs_with_adjustment(context.inputs, adjustment)
            strategy = _strategy_from_adjustment(adjustment)
        else:
            inputs = context.inputs
            decision = state.get("decision")
            strategy = _strategy_from_decision(decision)

        plan = build_plan(inputs, strategy)

        decision = state.get("decision")
        if adjustment is None and decision is not None:
            if decision.headline:
                plan.headline = decision.headline
            sections = [s for s in (decision.sections or []) if s]
            if sections:
                plan.narrative = "\n".join(sections)
            for caveat in decision.caveats or []:
                if caveat and caveat not in plan.warnings:
                    plan.warnings.append(caveat)
        if not plan.narrative:
            plan.narrative = render_plan_narrative(plan)
        if not plan.headline:
            plan.headline = _default_headline(plan)

        return {"plan": plan, "trace": ["compose：按输入与策略跑完七步，生成规范方案"]}

    return compose


# --------------------------------------------------------------------------- #
# 节点：validate（纯代码独立复核）
# --------------------------------------------------------------------------- #
def make_validate(context: PlanContext) -> Callable[[dict], dict]:
    def validate(state: dict) -> dict:
        plan = state.get("plan")
        if plan is None:
            report = ValidationReport(ok=False)
            return {
                "validation": report,
                "trace": ["validate：没有可复核的方案"],
            }
        inputs = _effective_inputs(context, state)
        report = validate_plan(plan, inputs)
        verdict = "通过" if report.ok else f"未通过（{len(report.issues)} 项）"
        return {"validation": report, "trace": [f"validate：{verdict}"]}

    return validate


def route_after_validate(state: dict) -> str:
    report = state.get("validation")
    if report is None or not getattr(report, "ok", False):
        return FALLBACK
    return CONFIRM


# --------------------------------------------------------------------------- #
# 节点：adjust（模型解析反馈）
# --------------------------------------------------------------------------- #
def make_adjust(adjust_runner: AdjustRunner | None, context: PlanContext) -> Callable[[dict], dict]:
    def adjust(state: dict) -> dict:
        text = state.get("adjustment_text") or ""
        if adjust_runner is None:
            return {
                "adjustment": None,
                "degraded": False,
                "error": "模型不可用，反馈未解析",
                "trace": ["adjust：模型不可用，按原方案重算"],
            }

        messages: list[BaseMessage] = [
            SystemMessage(content=PLAN_ADJUST_SYSTEM),
            HumanMessage(content=build_adjust_human(plan_lines=_render_plan_lines(state.get("plan")), feedback=text)),
        ]
        try:
            adjustment, _reasoning = _split_runner_result(adjust_runner(messages))
        except Exception as exc:  # noqa: BLE001 - 解析失败不阻断，按原方案重算
            return {
                "adjustment": None,
                "degraded": False,
                "error": f"{type(exc).__name__}: {exc}",
                "trace": ["adjust：反馈解析失败，按原方案重算"],
            }

        if not isinstance(adjustment, PlanAdjustment):
            adjustment = PlanAdjustment.model_validate(adjustment)
        return {
            "adjustment": adjustment,
            "degraded": False,
            "error": None,
            "trace": ["adjust：把反馈解析为结构化调整要求"],
        }

    return adjust


# --------------------------------------------------------------------------- #
# 节点：confirm（interrupt）与 fallback
# --------------------------------------------------------------------------- #
def confirm_node(state: dict) -> Command:
    """用户确认方案：``interrupt()`` 挂起等 resume。

    ``interrupt`` 放在首行之后、且其之前没有副作用，保证恢复重跑时幂等。
    """
    plan: Plan = state.get("plan") or Plan()
    validation: ValidationReport = state.get("validation") or ValidationReport()
    payload = {
        "type": "plan_confirmation",
        "plan": plan.model_dump(exclude_none=True),
        "validation": validation.model_dump(),
        "options": list(CONFIRM_ACTIONS),
    }
    info = _normalize_resume(interrupt(payload))

    if info["action"] == "confirm":
        return Command(goto=END, update={"confirmed": True, "awaiting_confirmation": False})

    if info["action"] == "edit":
        return Command(
            goto=ADJUST,
            update={
                "confirmed": False,
                "awaiting_confirmation": False,
                "ready_to_finalize": False,
                "adjustment_text": info.get("message") or "",
                "adjustment": None,
            },
        )

    update: dict[str, Any] = {
        "confirmed": False,
        "awaiting_confirmation": False,
        "ready_to_finalize": False,
        "adjustment": None,
    }
    message = info.get("message")
    if message:
        update["messages"] = [HumanMessage(content=str(message))]
    return Command(goto=AGENT, update=update)


def make_fallback(context: PlanContext) -> Callable[[dict], dict]:
    """模型不可用 / 复核未通过时的确定性兜底：代码跑完七步 + 模板叙述。"""

    def fallback(state: dict) -> dict:
        inputs = _effective_inputs(context, state)
        plan = build_plan(inputs, PlanStrategy())
        plan.degraded = True
        if not plan.headline:
            plan.headline = _default_headline(plan)
        if not plan.narrative:
            plan.narrative = PLAN_DEGRADED_NOTICE + render_plan_narrative(plan)
        report = validate_plan(plan, inputs)
        return {
            "plan": plan,
            "validation": report,
            "degraded": True,
            "ready_to_finalize": True,
            "awaiting_confirmation": True,
            "trace": ["fallback：模型不可用/复核未通过，改用确定性管线并标为降级"],
        }

    return fallback
