"""总结图的节点：collect / diff / reflect / fallback / compose / confirm。

两条纪律贯穿全图：

- **代码确定性产数据**：差异表、候选经验、事件排号、经验包归并、复盘要点都在代码里；
  模型只在 ``reflect`` 里写叙述（``SummaryDraft``），交回来之后由 :func:`compose_node` 覆盖合成。
- **失败不抛**：``reflect`` 出错就置 ``degraded`` 并转到 ``fallback``，用规则文案继续走完，
  最终仍能确认、仍能落盘——"没有 AI 也能用"。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END
from langgraph.types import Command, interrupt

from ..domain.experience import ExperiencePack, Lesson
from ..domain.profile_delta import ProfileEvent, active_events
from ..domain.summary import (
    LessonDraft,
    MonthlySummary,
    PlanActualDiff,
    SummaryDraft,
    build_candidates,
    build_diff,
    build_profile_delta,
    collect_lesson_statements,
    merge_pack,
)
from ..model.deepseek import StructuredCall
from ..prompts.summary import (
    SUMMARY_SYSTEM,
    build_machine_context,
    render_candidates,
    render_open_events,
)
from ..prompts.summary_rules import (
    SUMMARY_DEGRADED_NOTICE,
    lesson_statement,
    render_headline,
    render_sections,
)

ReflectRunner = Callable[[list[BaseMessage]], Any]

CONFIRM_ACTIONS = ("confirm", "edit")


def _split_runner_result(result: Any) -> tuple[Any, str | None]:
    """兼容真实模型的 ``StructuredCall`` 与脚本化假模型的领域对象。"""
    if isinstance(result, StructuredCall):
        return result.parsed, result.reasoning
    return result, None


def _normalize_resume(resume: Any) -> dict[str, Any]:
    """把 ``Command(resume=...)`` 的载荷归一成 ``{"action", "message"}``。"""
    if isinstance(resume, str):
        action = resume.strip().lower()
        return {"action": action or "confirm", "message": None}
    if isinstance(resume, dict):
        action = str(resume.get("action") or resume.get("type") or "confirm").strip().lower()
        return {"action": action or "confirm", "message": resume.get("message")}
    return {"action": "confirm", "message": None}


class SummaryCodeLayer:
    """闭包捕获的代码侧依赖：该月留档、上一版方案、既有经验包与画像事件。

    这些是**每一期都会变的输入**，由门面在开跑前调 :meth:`set_inputs` 装载（不进图状态）。
    """

    def __init__(
        self,
        *,
        record: Any | None = None,
        plan: Any | None = None,
        prior_pack: ExperiencePack | None = None,
        prior_events: list[ProfileEvent] | None = None,
    ) -> None:
        self.set_inputs(record=record, plan=plan, prior_pack=prior_pack, prior_events=prior_events)

    def set_inputs(
        self,
        *,
        record: Any | None = None,
        plan: Any | None = None,
        prior_pack: ExperiencePack | None = None,
        prior_events: list[ProfileEvent] | None = None,
    ) -> None:
        self.record = record
        self.plan = plan
        self.prior_pack = prior_pack
        self.prior_events = list(prior_events or [])

    # ------------------------------------------------------------------ #
    @property
    def period(self) -> str:
        snapshot = getattr(self.record, "snapshot", None)
        return str(getattr(snapshot, "period", "") or getattr(self.record, "period", "") or "")

    @property
    def snapshot(self) -> Any | None:
        return getattr(self.record, "snapshot", None)

    def diff(self, period: str = "") -> PlanActualDiff:
        snapshot = self.snapshot
        if snapshot is None:
            return PlanActualDiff(period=period or self.period, notes=["没有找到该月已落定的实况。"])
        return build_diff(snapshot, plan=self.plan, period=period or self.period)

    def candidates(self, diff: PlanActualDiff) -> list[Lesson]:
        return build_candidates(diff, period=self.period or diff.period)

    def evidence(self) -> list[str]:
        """依据要点——由代码从该月留档里取，模型碰不到。"""
        record = self.record
        if record is None:
            return []
        out: list[str] = []
        for probe in getattr(record, "probes", []) or []:
            if getattr(probe, "status", "") == "answered" and getattr(probe, "answer", ""):
                out.append(f"{probe.topic} → {probe.answer}")
        articulation = (getattr(record, "articulation", "") or "").strip()
        if articulation:
            out.append(articulation)
        notes = (getattr(getattr(record, "snapshot", None), "notes", "") or "").strip()
        if notes and notes != articulation:
            out.append(notes)
        return out[:8]

    def digest(self) -> str:
        """本月已归因的疑问，供模型下结论。"""
        record = self.record
        if record is None:
            return ""
        lines = [
            f"- {probe.topic}：{probe.answer}"
            for probe in getattr(record, "probes", []) or []
            if getattr(probe, "status", "") == "answered" and getattr(probe, "answer", "")
        ]
        articulation = (getattr(record, "articulation", "") or "").strip()
        if articulation:
            lines.append("本月已确认的实况结论：" + articulation)
        notes = (getattr(getattr(record, "snapshot", None), "notes", "") or "").strip()
        if notes and notes != articulation:
            lines.append("本月记录的额外事实：" + notes)
        return "\n".join(lines)

    def open_events(self) -> list[ProfileEvent]:
        """此前记录、尚未了结的事——模型据此判断本月有没有事情结束。"""
        return active_events(self.prior_events, as_of_period=self.period or None)

    def sources(self) -> list[str]:
        out: list[str] = []
        if self.record is not None:
            out.append("本月实况")
        if self.plan is not None:
            out.append("上一版方案")
        if self.prior_pack is not None and self.prior_pack.lessons:
            out.append("已有经验")
        if self.prior_events:
            out.append("已记录的画像变化")
        return out

    def build_delta(self, draft: SummaryDraft) -> tuple[Any, list[str]]:
        return build_profile_delta(
            draft.events,
            period=self.period,
            existing_events=self.prior_events,
            evidence=self.evidence(),
            notes=draft.notes,
        )


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #
def make_collect(code: SummaryCodeLayer):
    """收集节点（代码）：确认本期有据可依，缺什么如实记进说明。"""

    def collect(state: dict) -> dict:
        period = str(state.get("period") or code.period or "")
        trace: list[str] = [f"collect：本期 {period or '（缺期间）'}"]
        notes: list[str] = []
        if code.record is None:
            notes.append("本期没有已落定的月度实况，复盘只能就方案谈。")
        if code.plan is None:
            notes.append("没有注入上一版方案，无法判断偏差。")
        return {"period": period, "trace": trace, "event_warnings": notes}

    return collect


def make_diff(code: SummaryCodeLayer):
    """对照节点（代码）：算差异表 + 产出候选经验。"""

    def diff_node(state: dict) -> dict:
        period = str(state.get("period") or code.period or "")
        diff = code.diff(period)
        candidates = code.candidates(diff)
        return {
            "diff": diff,
            "candidates": candidates,
            "trace": [
                f"diff：计划可用={diff.plan_available}，候选经验 {len(candidates)} 条"
            ],
        }

    return diff_node


def make_reflect(runner: ReflectRunner, code: SummaryCodeLayer, *, system_prompt: str = SUMMARY_SYSTEM):
    """复盘节点（模型）：只写叙述、给候选填结论、抽画像变化。"""

    def reflect(state: dict) -> dict:
        diff = state.get("diff") or code.diff()
        candidates = list(state.get("candidates") or [])
        payload = build_machine_context(
            diff.model_dump_json(exclude_none=True),
            render_candidates(candidates),
            render_open_events(code.open_events()),
            code.digest(),
        )
        messages: list[BaseMessage] = [
            SystemMessage(content=system_prompt),
            SystemMessage(content=payload),
        ]
        feedback = (state.get("feedback") or "").strip()
        if feedback:
            messages.append(HumanMessage(content=f"（用户对上一版复盘的修改意见：{feedback}）"))
        try:
            draft, reasoning = _split_runner_result(runner(messages))
        except Exception as exc:  # noqa: BLE001 - 交给 fallback 节点降级
            return {"degraded": True, "error": f"{type(exc).__name__}: {exc}", "reasoning": None}

        if not isinstance(draft, SummaryDraft):
            draft = SummaryDraft.model_validate(draft)
        return {"draft": draft, "degraded": False, "error": None, "reasoning": reasoning}

    return reflect


def make_fallback(code: SummaryCodeLayer):
    """降级节点（代码）：用模板产草稿，之后照常进 compose。"""

    def fallback(state: dict) -> dict:
        diff = state.get("diff") or code.diff()
        candidates = list(state.get("candidates") or [])
        draft = SummaryDraft(
            headline=render_headline(diff),
            sections=render_sections(diff),
            lessons=[LessonDraft(id=item.id, statement=lesson_statement(item.kind)) for item in candidates],
            notes=SUMMARY_DEGRADED_NOTICE,
        )
        return {"draft": draft, "degraded": True}

    return fallback


def make_compose(code: SummaryCodeLayer):
    """合成节点（代码）。"""

    def compose(state: dict) -> dict:
        diff = state.get("diff") or code.diff()
        candidates = list(state.get("candidates") or [])
        draft = state.get("draft") or SummaryDraft()
        written = {item.id: (item.statement or "").strip() for item in draft.lessons}
        filled = [
            item.model_copy(update={"statement": written.get(item.id) or lesson_statement(item.kind)})
            for item in candidates
        ]

        pack = merge_pack(code.prior_pack, filled, period=code.period or diff.period)
        delta, warnings = code.build_delta(draft)

        sections = [text.strip() for text in draft.sections if text.strip()]
        summary = MonthlySummary(
            period=code.period or diff.period,
            generated_at=date.today().isoformat(),
            headline=(draft.headline or "").strip() or render_headline(diff),
            sections=sections or render_sections(diff),
            diff=diff,
            lessons=collect_lesson_statements(filled),
            notes=draft.notes,
            sources=code.sources(),
            degraded=bool(state.get("degraded")),
        )
        return {
            "pack": pack,
            "profile_delta": delta,
            "summary": summary,
            "event_warnings": warnings,
            "awaiting_confirmation": True,
            "trace": [
                f"compose：经验包 v{pack.version}（{len(pack.lessons)} 条），"
                f"画像变化 {len(delta.events)} 条"
            ],
        }

    return compose


def _dump(model: Any) -> dict[str, Any]:
    """把领域模型导成可 JSON 序列化的 dict（``interrupt`` 载荷的硬要求）。"""
    if model is None:
        return {}
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return {}


def confirm_node(state: dict) -> Command:
    """用户核对：``interrupt()`` 挂起等 resume。

    ``interrupt`` 放在首行之后、之前没有副作用，保证恢复重跑幂等。
    只支持「确认」与「带意见重来」两种动作——这是一次性任务，不做多轮追问。
    """
    payload = {
        "type": "summary_confirmation",
        "summary": _dump(state.get("summary")),
        "pack": _dump(state.get("pack")),
        "profile_delta": _dump(state.get("profile_delta")),
        "options": list(CONFIRM_ACTIONS),
    }
    info = _normalize_resume(interrupt(payload))

    if info["action"] == "confirm":
        return Command(goto=END, update={"confirmed": True, "awaiting_confirmation": False})

    update: dict[str, Any] = {
        "confirmed": False,
        "awaiting_confirmation": False,
        "feedback": str(info.get("message") or ""),
    }
    return Command(goto="reflect", update=update)


def route_after_reflect(state: dict) -> str:
    """reflect 之后：降级去 fallback，正常直接合成。"""
    return "fallback" if state.get("degraded") else "compose"
