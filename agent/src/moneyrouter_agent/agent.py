"""画像访谈 Agent 门面。

- ``turn(thread_id, user_message, resume=...)``：推进一轮对话（或从确认环节恢复）。
- ``snapshot(thread_id)``：只读查看当前状态。

这是将来 Go 侧调用 Python 的稳定入口（可经 FastAPI 或子进程承载）。
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.types import Command, interrupt  # noqa: F401  (interrupt 供类型参考)

from .api.contract import TurnResult
from .config import Settings
from .domain.profile import Profile
from .graph.build import build_interview_graph
from .model.deepseek import make_finalize_runner, make_turn_runner
from .prompts.interview import OPENING_USER_TURN

DEFAULT_RECURSION_LIMIT = 25


def _unavailable_runner(messages: list[BaseMessage]) -> Any:
    """没有可用密钥时的 runner：直接抛错，让图走 fallback 降级。"""
    raise RuntimeError("DeepSeek 不可用：未配置 API Key")


def _resume_to_user_message(resume: dict[str, Any] | str | None) -> str | None:
    """从 resume 载荷里取出它携带的文本（如果有）。"""
    if isinstance(resume, str):
        return resume.strip() or None
    if isinstance(resume, dict):
        text = resume.get("message")
        if text is None:
            return None
        return str(text).strip() or None
    return None


class ProfileAgent:
    """画像访谈 Agent 的门面。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        turn_runner: Callable[[list[BaseMessage]], Any] | None = None,
        finalize_runner: Callable[[list[BaseMessage]], Any] | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()

        if turn_runner is None:
            turn_runner = (
                _unavailable_runner if self.settings.degraded else make_turn_runner(self.settings)
            )
        if finalize_runner is None:
            finalize_runner = (
                _unavailable_runner if self.settings.degraded else make_finalize_runner(self.settings)
            )

        self.graph = build_interview_graph(
            turn_runner=turn_runner,
            finalize_runner=finalize_runner,
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

    def turn(
        self,
        thread_id: str,
        user_message: str = "",
        *,
        resume: dict[str, Any] | str | None = None,
    ) -> TurnResult:
        """推进一轮；若会话停在确认环节，则用 ``resume`` 恢复。"""
        config = self._config(thread_id)
        try:
            paused = bool(self.graph.get_state(config).next)
            if paused:
                payload = resume if resume is not None else {"action": "confirm"}
                self.graph.invoke(Command(resume=payload), config)
            else:
                if resume is not None:
                    # 会话并未停在确认环节：resume 不能被当成动作，
                    # 只取它携带的文本当普通用户消息；没有文本就什么也不做。
                    carried = _resume_to_user_message(resume)
                    if not carried:
                        return self.snapshot(thread_id)
                    user_message = carried
                self.graph.invoke(
                    {"messages": [HumanMessage(content=user_message or OPENING_USER_TURN)]},
                    config,
                )
        except Exception as exc:  # noqa: BLE001 - 不抛给调用方，转为可读状态
            snapshot = self.snapshot(thread_id)
            return snapshot.model_copy(
                update={"error": f"{type(exc).__name__}: {exc}", "degraded": True}
            )
        return self.snapshot(thread_id)

    def snapshot(self, thread_id: str) -> TurnResult:
        """只读快照。"""
        config = self._config(thread_id)
        state = self.graph.get_state(config)
        values: dict[str, Any] = dict(state.values or {})

        reply = ""
        for message in reversed(values.get("messages") or []):
            if isinstance(message, AIMessage):
                content = message.content
                reply = content if isinstance(content, str) else str(content)
                break

        draft = values.get("understanding") or Profile()

        confirmation_payload: dict[str, Any] | None = None
        for task in getattr(state, "tasks", ()) or ():
            for itr in getattr(task, "interrupts", ()) or ():
                confirmation_payload = itr.value

        return TurnResult(
            thread_id=thread_id,
            reply=reply,
            understanding=Profile.model_validate(draft.model_dump()),
            notes=values.get("notes") or "",
            ready_to_finalize=bool(values.get("ready_to_finalize")),
            rationale=values.get("rationale") or "",
            reasoning=values.get("reasoning"),
            awaiting_confirmation=confirmation_payload is not None
            or bool(values.get("awaiting_confirmation")),
            confirmation_payload=confirmation_payload,
            confirmed=bool(values.get("confirmed")),
            final_profile=values.get("final_profile"),
            articulation=values.get("articulation"),
            degraded=bool(values.get("degraded")),
            error=values.get("error"),
            turn_count=int(values.get("turn_count") or 0),
        )
