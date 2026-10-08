"""本月实况 Agent 门面。

- ``turn(thread_id, user_message, resume=..., bill=..., period=...)``：推进一轮（或从核对环节恢复）。
- ``snapshot(thread_id)``：只读查看当前状态。
- ``parse_bill(content, period=...)``：只解析账单，供调用方即时预览解析结果与告警。

**历史留档**（:mod:`moneyrouter_agent.history`）：

- 开跑前按统计期间**自动装载更早的历史月份**，作为环比/近三月均值与"累计已攒"的基线；
- 用户核对通过（``confirmed``）之后**自动落档**，同期间覆盖；
- `history_store=None` 或 `MonthSettings.history_dir=""` 可关闭整条链路。

这是将来 Go 侧调用 Python 的稳定入口（可经 FastAPI 或子进程承载）。
"""

from __future__ import annotations
from .checkpoints import context as checkpoint_context

from typing import Any, Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.types import Command  # noqa: F401  (供类型与恢复参考)

from .api.contract import MonthTurnResult
from .config import MonthSettings, Settings
from .domain.month import GoalAlignment, MonthResult, MonthSnapshot
from .domain.month_turn import MonthTurnDecision
from .domain.probe import Probe
from .graph.month_build import build_month_graph
from .graph.month_nodes import CodeLayer
from .history import (
    HistoryError,
    JsonMonthHistoryStore,
    MonthHistoryStore,
    MonthRecord,
    now_iso,
    snapshots_from,
)
from .model.deepseek import make_schema_runner
from .prompts.month import OPENING_USER_TURN
from .tools.bills import BillParser, ParsedBill
from .tools.bills import parse_bill as _parse_bill

DEFAULT_RECURSION_LIMIT = 25

_UNSET = object()


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


def record_from_turn(result: MonthTurnResult) -> MonthRecord:
    """把一版**已落定**的回合转成留档。

    取 ``final_result``（用户核对通过的那一版）而不是过程中的快照；关注点一并留下，
    因为"为什么偏高/为什么亏"的归因正是后续复盘最有价值的部分。
    """
    final = result.final_result
    snapshot = final.snapshot if final is not None else result.snapshot
    articulation = (final.articulation if final is not None else "") or (result.articulation or "")
    return MonthRecord(
        period=(snapshot.period or result.snapshot.period or "").strip(),
        recorded_at=now_iso(),
        snapshot=snapshot,
        articulation=articulation,
        probes=list(result.probes),
    )


class MonthAgent:
    """「本月实况」子能力的门面。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        month_settings: MonthSettings | None = None,
        bill_parser: BillParser | Any | None = None,
        turn_runner: Callable[[list[BaseMessage]], Any] | None = None,
        finalize_runner: Callable[[list[BaseMessage]], Any] | None = None,
        checkpointer: Any | None = None,
        profile_context: dict | None = None,
        budget: dict[str, int] | None = None,
        history: list[MonthSnapshot] | None = None,
        goal: GoalAlignment | None = None,
        one_off_min_cents: int | None = None,
        history_store: MonthHistoryStore | None | object = _UNSET,
        history_limit: int | None = None,
        autoload_history: bool = True,
        autosave_history: bool = True,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.month_settings = month_settings or MonthSettings.from_env()
        self.bill_parser = bill_parser
        self._one_off_min_cents = (
            one_off_min_cents
            if one_off_min_cents is not None
            else self.month_settings.one_off_min_cents
        )

        # 历史留档：不传就按配置建一个 JSON 文件库；显式传 None 表示本次完全不用历史。
        if history_store is _UNSET:
            directory = self.month_settings.history_dir
            self.history_store: MonthHistoryStore | None = (
                JsonMonthHistoryStore(directory) if directory else None
            )
        else:
            self.history_store = history_store  # type: ignore[assignment]
        self.history_limit = (
            history_limit if history_limit is not None else self.month_settings.history_limit
        )
        self.autoload_history = autoload_history
        self.autosave_history = autosave_history
        self.history_warnings: list[str] = []
        # 已经按哪个期间装载过历史——同一期间不重复读盘
        self._history_loaded_for: str | None = None

        if turn_runner is None:
            turn_runner = (
                _unavailable_runner
                if self.settings.degraded
                else make_schema_runner(self.settings, MonthTurnDecision)
            )
        if finalize_runner is None:
            finalize_runner = (
                _unavailable_runner
                if self.settings.degraded
                else make_schema_runner(self.settings, MonthResult)
            )

        if profile_context:
            from langchain_core.messages import SystemMessage
            import json
            context_message = SystemMessage(content="基础画像仅作背景，不自动认定为本月事实：" + json.dumps(profile_context, ensure_ascii=False))
            original_turn, original_finalize = turn_runner, finalize_runner
            turn_runner = lambda messages: original_turn([context_message, *messages])
            finalize_runner = lambda messages: original_finalize([context_message, *messages])
        self.code = CodeLayer(
            settings=self.month_settings, budget=budget, history=history, goal=goal
        )
        self.graph = build_month_graph(
            turn_runner=turn_runner,
            finalize_runner=finalize_runner,
            code=self.code,
            one_off_min_cents=self._one_off_min_cents,
            parser=self.bill_parser,
            checkpointer=checkpointer,
        )

        # 调用方显式给了 history（含空列表）就照用；``None`` 时才让留档来喂。
        if history is None and self.autoload_history and self.history_store is not None:
            self.code.set_history(
                snapshots_from(
                    self.history_store.load_records(
                        limit=self.history_limit, warnings=self.history_warnings
                    )
                )
            )

    # ------------------------------------------------------------------ #
    # 历史留档
    # ------------------------------------------------------------------ #
    def history_periods(self) -> list[str]:
        """已有历史月份（升序，形如 ``['2026-07', '2026-08']``）。"""
        if self.history_store is None:
            return []
        try:
            return list(self.history_store.list_periods())
        except Exception:  # noqa: BLE001 - 只读展示，不该拖垮会话
            return []

    def history_records(
        self, *, limit: int | None = None, before: str | None = None
    ) -> list[MonthRecord]:
        """读取历史留档（升序，最近的在后）；坏记录跳过并记进 :attr:`history_warnings`。"""
        if self.history_store is None:
            return []
        return self.history_store.load_records(
            limit=limit, before=before, warnings=self.history_warnings
        )

    def load_record(self, period: str) -> MonthRecord | None:
        """点名读取某个月的留档；没有返回 ``None``，读不出来抛
        :class:`~moneyrouter_agent.history.HistoryError`。"""
        if self.history_store is None:
            return None
        return self.history_store.load(period)

    def _refresh_history(self, config: dict[str, Any], period: str | None) -> None:
        """按当前统计期间装载历史——**只取更早的月份**，避免拿本月跟自己比。"""
        if not self.autoload_history or self.history_store is None:
            return
        target = (period or "").strip()
        if not target:
            target = str((self.graph.get_state(config).values or {}).get("period") or "").strip()
        records = self.history_store.load_records(
            limit=self.history_limit,
            before=target or None,
            warnings=self.history_warnings,
        )
        self.code.set_history(snapshots_from(records))
        self._history_loaded_for = target

    def _after_turn(self, result: MonthTurnResult) -> MonthTurnResult:
        """落定之后写历史（同期间覆盖），并把历史概况回报给调用方。"""
        update: dict[str, Any] = {}
        if (
            self.autosave_history
            and self.history_store is not None
            and result.confirmed
            and result.final_result is not None
        ):
            record = record_from_turn(result)
            if not record.period:
                update["recorded"] = False
                update["error"] = "历史记录未写入：快照缺少统计期间。"
            else:
                try:
                    update["history_location"] = self.history_store.save(record)
                except HistoryError as exc:
                    update["recorded"] = False
                    update["error"] = f"历史记录写入失败：{exc}"
                else:
                    update["recorded"] = True

        update["history_periods"] = self.history_periods()
        update["history_warnings"] = list(self.history_warnings)
        return result.model_copy(update=update)

    # ------------------------------------------------------------------ #
    def _config(self, thread_id: str) -> dict[str, Any]:
        if not thread_id:
            raise ValueError("thread_id 不能为空")
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": DEFAULT_RECURSION_LIMIT,
        }

    def parse_bill(self, content: str | bytes, *, period: str) -> ParsedBill:
        """只解析账单（不改会话状态），供调用方即时预览。"""
        return _parse_bill(
            content,
            period=period,
            one_off_min_cents=self._one_off_min_cents,
            parser=self.bill_parser,
        )

    def turn(
        self,
        thread_id: str,
        user_message: str = "",
        *,
        resume: dict[str, Any] | str | None = None,
        bill: str | bytes | None = None,
        period: str | None = None,
    ) -> MonthTurnResult:
        """推进一轮；若会话停在核对环节，则用 ``resume`` 恢复。首轮可带 ``bill`` / ``period``。"""
        config = self._config(thread_id)
        try:
            saved = checkpoint_context(self.graph.checkpointer, thread_id)
            if saved:
                self.code.budget = saved["budget"]
                self.code.goal = GoalAlignment.model_validate(saved["goal"]) if saved["goal"] else None
                self.code.history = [MonthSnapshot.model_validate(s) for s in saved["history"]]
            else:
                checkpoint_context(self.graph.checkpointer, thread_id, {
                    "budget": self.code.budget,
                    "goal": self.code.goal.model_dump(mode="json") if self.code.goal else None,
                    "history": [s.model_dump(mode="json") for s in self.code.history],
                })
            # 先按当前期间把更早的月份装成基线，再让图跑
            self._refresh_history(config, period)
            paused = bool(self.graph.get_state(config).next)
            if paused:
                if bill is not None:
                    raise ValueError("会话正在核对，请先完成核对或使用新会话导入更新账单")
                payload = resume if resume is not None else {"action": "confirm"}
                self.graph.invoke(Command(resume=payload), config)
            else:
                if resume is not None:
                    carried = _resume_to_user_message(resume)
                    if not carried:
                        return self.snapshot(thread_id)
                    user_message = carried
                update: dict[str, Any] = {
                    "messages": [HumanMessage(content=user_message or OPENING_USER_TURN)],
                    "confirmed": False, "final_result": None, "ready_to_finalize": False
                }
                existing = dict(self.graph.get_state(config).values or {})
                if bill is not None and existing.get("period") and period and period != existing["period"]:
                    raise ValueError("不同月份账单请使用不同会话")
                if bill is not None:
                    update.update(imported=False, confirmed=False, final_result=None)
                if bill is not None or not existing.get("imported"):
                    if bill is not None:
                        update["bill_text"] = bill
                    if period:
                        update["period"] = period
                self.graph.invoke(update, config)
        except Exception as exc:  # noqa: BLE001 - 不抛给调用方，转为可读状态
            snapshot = self.snapshot(thread_id)
            return snapshot.model_copy(
                update={"error": f"{type(exc).__name__}: {exc}", "degraded": True}
            )
        return self._after_turn(self.snapshot(thread_id))

    def prepare_confirmation(self, thread_id: str, *, supplements: dict | None = None) -> MonthTurnResult:
        """Explicit user request to review available facts, including offline mode.

        Reuses the existing finalize -> interrupt path. Unknown facts remain unknown.
        """
        from .domain.month import FundAllocation, InvestmentSnapshot
        config = self._config(thread_id)
        state = self.graph.get_state(config)
        if state.next:
            raise ValueError("会话已经等待核对")
        snapshot = self.snapshot(thread_id).snapshot.model_copy(deep=True)
        values = supplements or {}
        if values.get("income_cents") is not None:
            from .domain.month import IncomeFact
            amount = values["income_cents"]
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise ValueError("收入必须为非负整数分")
            if snapshot.income and snapshot.income.source == "file" and snapshot.income.amount_cents != amount:
                raise ValueError("账单收入与手填冲突，请核对账单后更新")
            snapshot.income = IncomeFact(amount_cents=amount, evidence="用户在核对卡片明确填写", source="stated")
        if values.get("expected_income_cents") is not None:
            from .domain.month import IncomeFact
            amount = values["expected_income_cents"]
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise ValueError("预计收入必须为非负整数分")
            snapshot.expected_income = IncomeFact(amount_cents=amount, role="expected", source="stated", evidence="用户明确填写预计整月总收入，包含已到账部分")
        if values.get("coverage_complete") is not None:
            from .periods import business_today, period_bounds
            if not isinstance(values["coverage_complete"], bool):
                raise ValueError("整月资料核对状态无效")
            if values["coverage_complete"] and snapshot.period >= business_today().strftime("%Y-%m"):
                raise ValueError("进行中或未来月份不能确认整月已结账")
            snapshot.coverage_complete = values["coverage_complete"]
            if snapshot.coverage_complete:
                start, end = period_bounds(snapshot.period)
                snapshot.coverage_start, snapshot.coverage_end = start.isoformat(), end.isoformat()
        if values.get("obligations_reviewed") is not None:
            if not isinstance(values["obligations_reviewed"], bool):
                raise ValueError("待支付核对状态无效")
            snapshot.obligations_reviewed = values["obligations_reviewed"]
        for key in ("non_invested_cents", "invested_cents"):
            if values.get(key) is not None:
                value = values[key]
                if not isinstance(value, int) or value < 0:
                    raise ValueError("储蓄去向必须为非负整数分")
                setattr(snapshot.allocation, key, value)
        if values.get("has_investments") is not None:
            if not isinstance(values["has_investments"], bool):
                raise ValueError("投资情况无效")
            snapshot.investments.has_investments = values["has_investments"]
        self.graph.update_state(config, {"snapshot": self.code.recompute(snapshot),
            "ready_to_finalize": True, "degraded": False, "error": None}, as_node="converse")
        self.graph.invoke(None, config)
        return self._after_turn(self.snapshot(thread_id))

    def snapshot(self, thread_id: str) -> MonthTurnResult:
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

        snapshot = values.get("snapshot") or MonthSnapshot()
        probes = [p for p in (values.get("probes") or []) if isinstance(p, Probe)]

        confirmation_payload: dict[str, Any] | None = None
        for task in getattr(state, "tasks", ()) or ():
            for itr in getattr(task, "interrupts", ()) or ():
                confirmation_payload = itr.value

        return MonthTurnResult(
            thread_id=thread_id,
            reply=reply,
            snapshot=MonthSnapshot.model_validate(snapshot.model_dump()),
            probes=[Probe.model_validate(p.model_dump()) for p in probes],
            notes=values.get("notes") or "",
            ready_to_finalize=bool(values.get("ready_to_finalize")),
            rationale=values.get("rationale") or "",
            reasoning=values.get("reasoning"),
            awaiting_confirmation=confirmation_payload is not None
            or bool(values.get("awaiting_confirmation")),
            confirmation_payload=confirmation_payload,
            confirmed=bool(values.get("confirmed")),
            final_result=values.get("final_result"),
            articulation=values.get("articulation"),
            parse_warnings=list(values.get("parse_warnings") or []),
            degraded=bool(values.get("degraded")),
            error=values.get("error"),
            turn_count=int(values.get("turn_count") or 0),
            history_periods=self.history_periods(),
            history_warnings=list(self.history_warnings),
        )
