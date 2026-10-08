"""方案生成 Agent 门面。

用法::

    agent = PlanAgent(inputs=PlanInputs(profile=..., snapshot=..., briefing=..., experience=...))
    result = agent.plan("user-1")

    result.plan          # 规范方案（金额/风险/配置/叙述）
    result.validation    # 独立复核结论
    result.reply         # 本轮要对用户说的话

输入（画像 / 金融简报 / 账单快照 / 经验包）由调用方**进程内注入**，不走 HTTP；
``plan`` 承载多轮对话与确认：停在确认环节时用 ``resume={"action": ...}`` 恢复。

模型不可用或候选无法校验时不生成默认分配。旧正式方案仍保留；新图使用钱包 v2 契约。
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from .api.contract import PlanResult
from .config import PlanSettings, Settings
from .checkpoints import context as checkpoint_context
from .domain.plan import CashflowSummary, Plan, PlanContext, PlanInputs, ValidationReport
from .domain.plan_turn import PlanAdjustment, PlanTurnDecision
from .graph.wallet_build import build_wallet_graph as build_plan_graph
from .model.deepseek import StructuredCall, build_chat_model, make_schema_runner
from .prompts.plan import OPENING_USER_TURN
from .tools.plan_tools import make_plan_tools

DEFAULT_RECURSION_LIMIT = 40


def _unavailable_runner(messages: list[Any]) -> Any:
    """没有可用模型密钥时的 runner：抛错，让节点走降级。"""
    raise RuntimeError("DeepSeek 不可用：未配置 API Key")


class PlanAgent:
    """「方案生成」子能力的门面。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        plan_settings: PlanSettings | None = None,
        inputs: PlanInputs | None = None,
        experience: Any = None,
        agent_model: Any = None,
        agent_runner: Callable[[list[Any]], Any] | None = None,
        tools: list[Any] | None = None,
        decide_runner: Callable[[list[Any]], Any] | None = None,
        adjust_runner: Callable[[list[Any]], Any] | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.plan_settings = plan_settings or PlanSettings.from_env()

        if inputs is None:
            inputs = PlanInputs(experience=experience)
        elif experience is not None and inputs.experience is None:
            inputs = inputs.model_copy(update={"experience": experience})
        self.context = PlanContext(inputs)

        if tools is None:
            tools = make_plan_tools(self.context)
        self.tools = tools

        degraded = self.settings.degraded
        if agent_runner is None:
            if agent_model is None and not degraded:
                agent_model = build_chat_model(
                    self.settings.with_overrides(
                        thinking_enabled=self.plan_settings.tools_thinking_enabled
                    )
                )
            if agent_model is None:
                agent_runner = None
            else:
                agent_runner = agent_model.bind_tools(tools).invoke if tools else agent_model.invoke
        self.agent_runner = agent_runner

        if decide_runner is None:
            decide_runner = (
                _unavailable_runner
                if degraded
                else make_schema_runner(self.settings, PlanTurnDecision)
            )
        if adjust_runner is None:
            adjust_runner = (
                _unavailable_runner
                if degraded
                else make_schema_runner(self.settings, PlanAdjustment)
            )

        self.graph = build_plan_graph(
            agent_runner=agent_runner,
            tools=tools,
            decide_runner=decide_runner,
            adjust_runner=adjust_runner,
            context=self.context,
            max_tool_rounds=self.plan_settings.max_tool_rounds,
            checkpointer=checkpointer,
        )

    # ------------------------------------------------------------------ #
    def _config(self, thread_id: str) -> dict[str, Any]:
        if not thread_id:
            raise ValueError("thread_id 不能为空")
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": DEFAULT_RECURSION_LIMIT,
        }

    def plan(
        self,
        thread_id: str,
        *,
        inputs: PlanInputs | None = None,
        user_message: str = "",
        resume: dict[str, Any] | str | None = None,
    ) -> PlanResult:
        """推进一轮；若会话停在确认环节，则用 ``resume`` 恢复。"""
        if inputs is not None:
            self.context.set_inputs(inputs)
        config = self._config(thread_id)
        try:
            saved = checkpoint_context(self.graph.checkpointer, thread_id)
            if saved and inputs is None:
                self.context.set_inputs(PlanInputs.model_validate(saved["inputs"]))
            checkpoint_context(self.graph.checkpointer, thread_id, {"inputs": self.context.inputs.model_dump(mode="json")})
            paused = bool(self.graph.get_state(config).next)
            if paused:
                payload = resume if resume is not None else {"action": "confirm"}
                self.graph.invoke(Command(resume=payload), config)
            else:
                update: dict[str, Any] = {
                    "messages": [HumanMessage(content=user_message or OPENING_USER_TURN)],
                    "period": self.context.inputs.period,
                }
                self.graph.invoke(update, config)
            checkpoint_context(self.graph.checkpointer, thread_id, {"inputs": self.context.inputs.model_dump(mode="json")})
        except Exception as exc:  # noqa: BLE001 - 不抛给调用方，转为可读状态
            result = self.snapshot(thread_id)
            return result.model_copy(
                update={"error": f"{type(exc).__name__}: {exc}", "degraded": True}
            )
        return self.snapshot(thread_id)

    def snapshot(self, thread_id: str) -> PlanResult:
        """只读快照。"""
        config = self._config(thread_id)
        state = self.graph.get_state(config)
        values: dict[str, Any] = dict(state.values or {})

        reply = ""
        for message in reversed(values.get("messages") or []):
            if isinstance(message, AIMessage) and message.additional_kwargs.get("public_reply") is True:
                content = message.content
                reply = content if isinstance(content, str) else str(content)
                break

        confirmation_payload: dict[str, Any] | None = None
        for task in getattr(state, "tasks", ()) or ():
            for itr in getattr(task, "interrupts", ()) or ():
                confirmation_payload = itr.value

        reasoning_list = values.get("reasoning") or []
        reasoning = reasoning_list[-1] if reasoning_list else None

        plan = values.get("plan")
        validation = values.get("validation")
        cashflow = values.get("cashflow")
        return PlanResult(
            thread_id=thread_id,
            clarification_target=values.get("clarification_target") or "",
            reply=reply,
            plan=Plan.model_validate(plan.model_dump()) if isinstance(plan, Plan) else None,
            validation=(
                ValidationReport.model_validate(validation.model_dump())
                if isinstance(validation, ValidationReport)
                else None
            ),
            cashflow=(
                CashflowSummary.model_validate(cashflow.model_dump())
                if isinstance(cashflow, CashflowSummary)
                else None
            ),
            ready_to_finalize=bool(values.get("ready_to_finalize")),
            awaiting_confirmation=confirmation_payload is not None
            or bool(values.get("awaiting_confirmation")),
            confirmation_payload=confirmation_payload,
            confirmed=bool(values.get("confirmed")),
            rationale=values.get("rationale") or "",
            notes=values.get("notes") or "",
            degraded=bool(values.get("degraded")),
            error=values.get("error"),
            turn_count=int(values.get("turn_count") or 0),
            reasoning=reasoning if isinstance(reasoning, str) else None,
            trace=list(values.get("trace") or []),
        )


__all__ = ["PlanAgent", "StructuredCall"]
