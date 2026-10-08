"""总结 Agent 门面（后馈第二块）。

- :meth:`SummaryAgent.summarize`：对一个**已落定**的月份做复盘 → 经验包 + 画像增量 + 月度复盘。
- :meth:`SummaryAgent.snapshot` / :meth:`SummaryAgent.turn`：查看状态 / 从核对环节恢复。
- 只读口：:meth:`load_pack`、:meth:`profile_events`、:meth:`effective_profile`、:meth:`load_summary`。

**落盘在门面、不在图里**：图跑完停在 ``confirm``，用户确认之后由门面写三份产物
（与 ``MonthAgent`` 的 ``_after_turn`` 同一模式），这样图保持无副作用、恢复重跑也幂等。

产物去向：经验包 → 方案生成的 ``PlanInputs.experience``；画像增量 → 画像（append-only）；
月度复盘 → 用户。三者**互不覆盖**，写入时机同一处。
"""

from __future__ import annotations
from .checkpoints import context as checkpoint_context

from typing import Any, Callable

from langchain_core.messages import BaseMessage
from langgraph.types import Command  # noqa: F401  (供类型与恢复参考)

from .api.contract import SummaryResult
from .config import MonthSettings, Settings, SummarySettings
from .domain.experience import ExperiencePack, Lesson
from .domain.month import MonthSnapshot
from .domain.profile import Profile
from .domain.profile_delta import EffectiveProfile, ProfileEvent, effective_profile
from .domain.summary import MonthlySummary, SummaryDraft
from .graph.summary_build import build_summary_graph
from .graph.summary_nodes import SummaryCodeLayer
from .history import JsonMonthHistoryStore, MonthHistoryStore, MonthRecord
from .model.deepseek import make_schema_runner
from .summary_store import (
    ExperiencePackStore,
    InMemoryMonthlySummaryStore,
    JsonExperiencePackStore,
    JsonMonthlySummaryStore,
    JsonProfileEventStore,
    MonthlySummaryStore,
    ProfileEventStore,
    SummaryStoreError,
)

DEFAULT_RECURSION_LIMIT = 25
_UNSET = object()


def _unavailable_runner(messages: list[BaseMessage]) -> Any:
    """没有可用密钥时的 runner：直接抛错，让图走 fallback 降级。"""
    raise RuntimeError("DeepSeek 不可用：未配置 API Key")


class SummaryAgent:
    """「月度复盘」子能力的门面。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        summary_settings: SummarySettings | None = None,
        turn_settings: MonthSettings | None = None,
        reflect_runner: Callable[[list[BaseMessage]], Any] | None = None,
        checkpointer: Any | None = None,
        history_store: MonthHistoryStore | None | object = _UNSET,
        experience_store: ExperiencePackStore | None | object = _UNSET,
        event_store: ProfileEventStore | None | object = _UNSET,
        summary_store: MonthlySummaryStore | None | object = _UNSET,
        plan: Any | None = None,
        autosave: bool = True,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.summary_settings = summary_settings or SummarySettings.from_env()
        self.month_settings = turn_settings or MonthSettings.from_env()
        self.user = self.summary_settings.user
        self.plan = plan
        self.autosave = autosave

        # 读「本月实况」留档：不传就按 MONTH_HISTORY_DIR 建默认 JSON 库
        if history_store is _UNSET:
            directory = self.month_settings.history_dir
            self.history_store: MonthHistoryStore | None = (
                JsonMonthHistoryStore(directory) if directory else None
            )
        else:
            self.history_store = history_store  # type: ignore[assignment]

        if experience_store is _UNSET:
            directory = self.summary_settings.experience_dir
            self.experience_store: ExperiencePackStore | None = (
                JsonExperiencePackStore(directory, user=self.user) if directory else None
            )
        else:
            self.experience_store = experience_store  # type: ignore[assignment]

        if event_store is _UNSET:
            directory = self.summary_settings.event_dir
            self.event_store: ProfileEventStore | None = (
                JsonProfileEventStore(directory, user=self.user) if directory else None
            )
        else:
            self.event_store = event_store  # type: ignore[assignment]

        if summary_store is _UNSET:
            directory = self.summary_settings.summary_dir
            self.summary_store: MonthlySummaryStore | None = (
                JsonMonthlySummaryStore(directory, user=self.user) if directory else None
            )
        else:
            self.summary_store = summary_store  # type: ignore[assignment]

        if reflect_runner is None:
            reflect_runner = (
                _unavailable_runner
                if self.settings.degraded
                else make_schema_runner(self.settings, SummaryDraft)
            )
        self.reflect_runner = reflect_runner

        self.code = SummaryCodeLayer()
        self.graph = build_summary_graph(
            reflect_runner=reflect_runner, code=self.code, checkpointer=checkpointer
        )
        self.write_warnings: list[str] = []

    # ------------------------------------------------------------------ #
    def _config(self, thread_id: str) -> dict[str, Any]:
        if not thread_id:
            raise ValueError("thread_id 不能为空")
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": DEFAULT_RECURSION_LIMIT,
        }

    # ------------------------------------------------------------------ #
    # 只读口
    # ------------------------------------------------------------------ #
    def history_periods(self) -> list[str]:
        """已有「本月实况」留档的月份（升序）。"""
        if self.history_store is None:
            return []
        try:
            return list(self.history_store.list_periods())
        except Exception:  # noqa: BLE001 - 只读展示，不该拖垮会话
            return []

    def load_record(self, period: str) -> MonthRecord | None:
        """读某个月的实况留档（复盘的事实来源）。"""
        if self.history_store is None:
            return None
        return self.history_store.load(period)

    def load_pack(self) -> ExperiencePack | None:
        """读当前经验包（喂方案生成的那份）。"""
        if self.experience_store is None:
            return None
        return self.experience_store.load()

    def profile_events(self) -> list[ProfileEvent]:
        """读画像事件日志（append-only）。"""
        if self.event_store is None:
            return []
        return self.event_store.load()

    def effective_profile(self, base: Profile | None = None, *, as_of_period: str | None = None) -> EffectiveProfile:
        """把生效中的画像变化叠加到原画像上（只读投影，不改原画像）。"""
        return effective_profile(base, self.profile_events(), as_of_period=as_of_period)

    def load_summary(self, period: str) -> MonthlySummary | None:
        """读某个月的复盘留档。"""
        if self.summary_store is None:
            return None
        return self.summary_store.load(period)

    def summaries(self) -> list[str]:
        """已有复盘的月份（升序）。"""
        if self.summary_store is None:
            return []
        try:
            return list(self.summary_store.list_periods())
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------ #
    def _resolve_period(self, period: str, record: MonthRecord | None) -> str:
        if period:
            return period
        if record is not None:
            return record.period or getattr(record.snapshot, "period", "") or ""
        periods = self.history_periods()
        return periods[-1] if periods else ""

    def summarize(
        self,
        thread_id: str,
        *,
        period: str = "",
        plan: Any | None = None,
        record: MonthRecord | None = None,
        user_id: str | None = None,
        resume: dict[str, Any] | str | None = None,
    ) -> SummaryResult:
        """对一个已落定的月份做复盘；会话停在核对环节时用 ``resume`` 恢复。"""
        config = self._config(thread_id)
        target = self._resolve_period(period, record)
        try:
            if user_id:
                self.user = user_id

            paused = bool(self.graph.get_state(config).next)
            if paused:
                saved = checkpoint_context(self.graph.checkpointer, thread_id)
                if saved:
                    from .domain.plan import Plan
                    from .domain.experience import ExperiencePack
                    from .domain.profile_delta import ProfileEvent
                    self.code.set_inputs(
                        record=MonthRecord.model_validate(saved["record"]) if saved["record"] else None,
                        plan=Plan.model_validate(saved["plan"]) if saved["plan"] else None,
                        prior_pack=ExperiencePack.model_validate(saved["pack"]) if saved["pack"] else None,
                        prior_events=[ProfileEvent.model_validate(e) for e in saved["events"]],
                    )
                payload = resume if resume is not None else {"action": "confirm"}
                self.graph.invoke(Command(resume=payload), config)
            else:
                if record is None and target and self.history_store is not None:
                    record = self.history_store.load(target)
                if record is not None and self.history_store is not None:
                    from .domain.month import recompute
                    history = self.history_store.load_records(before=target)
                    budget = {b.category: b.amount_cents for b in record.snapshot.baselines if b.metric == "budget"}
                    record = record.model_copy(update={"snapshot": recompute(record.snapshot,
                        history=[r.snapshot for r in history], budget=budget)})
                # 每期开跑前把"这一期的事实"装进代码层（不进图状态）
                self.code.set_inputs(
                    record=record,
                    plan=plan if plan is not None else self.plan,
                    prior_pack=self.load_pack(),
                    prior_events=self.profile_events(),
                )
                if not target and self.code.record is not None:
                    target = self.code.period
                checkpoint_context(self.graph.checkpointer, thread_id, {
                    "record": self.code.record.model_dump(mode="json") if self.code.record else None,
                    "plan": self.code.plan.model_dump(mode="json") if self.code.plan else None,
                    "pack": self.code.prior_pack.model_dump(mode="json") if self.code.prior_pack else None,
                    "events": [e.model_dump(mode="json") for e in self.code.prior_events],
                })
                self.graph.invoke({"period": target, "feedback": ""}, config)
        except Exception as exc:  # noqa: BLE001 - 不抛给调用方，转为可读状态
            snapshot = self.snapshot(thread_id)
            return snapshot.model_copy(
                update={"error": f"{type(exc).__name__}: {exc}", "degraded": True}
            )
        return self._after_confirm(self.snapshot(thread_id))

    def turn(
        self, thread_id: str, *, resume: dict[str, Any] | str | None = None, period: str = ""
    ) -> SummaryResult:
        """从核对环节恢复（``confirm`` / ``edit``），或在没有会话时先跑一遍。"""
        if resume is None and not self.graph.get_state(self._config(thread_id)).next:
            return self.summarize(thread_id, period=period)
        return self.summarize(thread_id, period=period, resume=resume)

    # ------------------------------------------------------------------ #
    def _after_confirm(self, result: SummaryResult) -> SummaryResult:
        """确认之后写三份产物。顺序：画像事件（最易失败）→ 经验包 → 月度复盘。"""
        update: dict[str, Any] = {"write_warnings": list(self.write_warnings)}
        if not (self.autosave and result.confirmed):
            return result.model_copy(update=update)

        locations: list[str] = []
        try:
            if result.summary.review_mode == "final" and self.event_store is not None and result.profile_delta.events:
                self.event_store.append(result.profile_delta.events)
                locations.append("画像事件")
            if result.summary.review_mode == "final" and self.experience_store is not None:
                self.experience_store.save(result.pack)
                locations.append("经验包")
            if self.summary_store is not None and result.summary.period:
                self.summary_store.save(result.summary)
                locations.append("月度复盘")
        except SummaryStoreError as exc:
            update["written"] = False
            update["error"] = f"复盘产物写入失败：{exc}"
            return result.model_copy(update=update)

        update["written"] = bool(locations)
        update["write_location"] = "、".join(locations) if locations else None
        return result.model_copy(update=update)

    # ------------------------------------------------------------------ #
    def snapshot(self, thread_id: str) -> SummaryResult:
        """只读快照。"""
        config = self._config(thread_id)
        state = self.graph.get_state(config)
        values: dict[str, Any] = dict(state.values or {})

        confirmation_payload: dict[str, Any] | None = None
        for task in getattr(state, "tasks", ()) or ():
            for itr in getattr(task, "interrupts", ()) or ():
                confirmation_payload = itr.value

        delta = values.get("profile_delta")
        return SummaryResult(
            thread_id=thread_id,
            period=str(values.get("period") or ""),
            summary=values.get("summary") or MonthlySummary(period=str(values.get("period") or "")),
            pack=values.get("pack") or ExperiencePack(),
            profile_delta=delta if delta is not None else _empty_delta(values.get("period") or ""),
            awaiting_confirmation=confirmation_payload is not None
            or bool(values.get("awaiting_confirmation")),
            confirmation_payload=confirmation_payload,
            confirmed=bool(values.get("confirmed")),
            degraded=bool(values.get("degraded")),
            error=values.get("error"),
            reasoning=values.get("reasoning"),
            trace=list(values.get("trace") or []),
            write_warnings=list(self.write_warnings),
            event_warnings=list(values.get("event_warnings") or []),
        )


def _empty_delta(period: str) -> Any:
    from .domain.profile_delta import ProfileDelta

    return ProfileDelta(period=period)


def lessons_from_pack(pack: ExperiencePack | None) -> list[Lesson]:
    """取经验包里的教训（便于调用方直接喂方案生成）。"""
    return list(pack.lessons) if pack is not None else []


def experience_for_plan(agent: SummaryAgent) -> ExperiencePack | None:
    """把经验包取出来注入 ``PlanInputs.experience`` —— 方案侧的对接点。"""
    return agent.load_pack()


__all__ = [
    "InMemoryMonthlySummaryStore",
    "SummaryAgent",
    "experience_for_plan",
    "lessons_from_pack",
]
