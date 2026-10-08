"""图节点：converse / finalize / confirm / fallback。

上下文组装（把上一轮的结构化状态带回模型）属于节点职责，不是 prompt 内容。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END
from langgraph.types import Command, interrupt

from ..domain.profile import Profile, ProfileDraft, ProfileResult
from ..domain.turn import TurnDecision
from ..model.deepseek import StructuredCall
from ..prompts.interview import FINALIZE_SYSTEM, INTERVIEW_SYSTEM, build_finalize_human
from ..prompts.rules import degraded_reply

TurnRunner = Callable[[list[BaseMessage]], Any]
FinalizeRunner = Callable[[list[BaseMessage]], Any]

CONFIRM_ACTIONS = ("confirm", "edit", "more")
PROFILE_QUESTIONS = {
    "occupation": "您的职业是什么？", "income_cents": "每月收入或生活费大约多少元？",
    "income_basis": "这笔收入来自工资、生活费还是其他来源？", "outcome_cents": "通常每月开销大约多少元？",
    "feature": "平时的生活情况和家庭支持是怎样的？", "income_stable": "收入或生活费是否稳定？",
    "family_load": "您是否需要承担养家、赡养或抚养的支出？", "debt_cents": "目前负债总额大约多少元？没有可以说零。",
    "reserve_cents": "现有可动用储蓄大约多少元？", "horizon_months": "预计多久后用这笔钱？大约几个月或几年都可以。",
    "max_loss_pct": "最多能接受多少百分比的短期亏损？完全不能接受可以说零。",
    "experience": "以前接触过哪些投资，熟悉程度如何？", "goal": "您希望攒钱实现什么目标？",
}


def _merge_notes(old: str, new: str) -> str:
    if old and old in new:
        return new
    if new and new in old:
        return old
    return "\n".join(dict.fromkeys(x.strip() for x in (old, new) if x and x.strip()))



# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _split_runner_result(result: Any) -> tuple[Any, str | None]:
    """兼容两种 runner 返回：真实模型的 ``StructuredCall`` 或脚本化假模型的领域对象。"""
    if isinstance(result, StructuredCall):
        return result.parsed, result.reasoning
    return result, None


def _fill_missing(new: ProfileDraft, old: ProfileDraft | None, cls: type = ProfileDraft):
    """新快照里为 None 的字段，用旧快照补齐——防止模型只回增量导致信息丢失。

    注意 ``0`` 是合法值，不会被当成空。
    """
    data = new.model_dump()
    if old is not None:
        old_data = old.model_dump()
        for key, value in data.items():
            if value is None and old_data.get(key) is not None:
                data[key] = old_data[key]
    return cls.model_validate(data)


def _state_context(state: dict) -> list[BaseMessage]:
    """把上一轮的机器状态作为一条 system 消息带回模型。"""
    draft = state.get("understanding")
    notes = (state.get("notes") or "").strip()
    if draft is None and not notes:
        return []
    lines = ["【机器记录的当前状态，供你更新；不要照抄给用户】"]
    if draft is not None:
        lines.append("已知信息：" + draft.model_dump_json(exclude_none=True))
    if notes:
        lines.append("额外了解：" + notes)
    lines.append("已明确拒答：" + str(state.get("refused_fields") or {}))
    lines.append("仍需追问的问题：" + str(state.get("pending_questions") or []))
    if draft is not None:
        lines.append("未收集且未拒答的字段：" + str([k for k in PROFILE_QUESTIONS if getattr(draft, k) is None and k not in (state.get("refused_fields") or {})]))
    return [SystemMessage(content="\n".join(lines))]


def _render_transcript(messages: list[BaseMessage], *, limit: int = 40) -> str:
    rows: list[str] = []
    for message in messages[-limit:]:
        if isinstance(message, HumanMessage):
            rows.append(f"用户：{message.content}")
        elif isinstance(message, AIMessage):
            rows.append(f"助理：{message.content}")
    return "\n".join(rows)


def _normalize_resume(resume: Any) -> dict[str, Any]:
    """把 ``Command(resume=...)`` 的载荷归一成 ``{"action", "message"}``。"""
    if isinstance(resume, str):
        action = resume.strip().lower()
        return {"action": action or "confirm", "message": None}
    if isinstance(resume, dict):
        action = str(resume.get("action") or resume.get("type") or "confirm").strip().lower()
        message = resume.get("message")
        return {"action": action or "confirm", "message": message}
    return {"action": "confirm", "message": None}


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #
def make_converse(turn_runner: TurnRunner, *, system_prompt: str = INTERVIEW_SYSTEM):
    """一次结构化 LLM 调用：产出回复 + 更新理解 + 判断能否收尾。"""

    def converse(state: dict) -> dict:
        turn_count = int(state.get("turn_count") or 0) + 1
        messages = [
            SystemMessage(content=system_prompt),
            *_state_context(state),
            *state.get("messages", []),
        ]
        try:
            decision, reasoning = _split_runner_result(turn_runner(messages))
        except Exception as exc:  # noqa: BLE001 - 交给 fallback 节点降级
            return {
                "degraded": True,
                "error": f"{type(exc).__name__}: {exc}",
                "turn_count": turn_count,
                "ready_to_finalize": False,
                "reasoning": None,
            }

        if not isinstance(decision, TurnDecision):
            decision = TurnDecision.model_validate(decision)

        draft = _fill_missing(decision.understanding or ProfileDraft(), state.get("understanding"))
        notes = _merge_notes(state.get("notes") or "", decision.notes or "")
        draft.notes = _merge_notes(draft.notes or "", notes) or None
        user_texts = [str(m.content) for m in state.get("messages", []) if isinstance(m, HumanMessage)]
        refused = dict(state.get("refused_fields") or {})
        for key, quote in decision.refused_fields.items():
            explicit_refusal = re.search(r"不(?:想|愿|方便|回答|告诉|提供|透露|说)|拒绝|跳过|保密|隐私|略过", quote)
            if key in PROFILE_QUESTIONS and explicit_refusal and any(quote.strip() in text for text in user_texts):
                refused[key] = quote.strip()
        missing = [key for key in PROFILE_QUESTIONS if getattr(draft, key) is None and key not in refused]
        pending = [q.strip() for q in decision.pending_questions if q.strip()]
        ready = bool(decision.ready_to_finalize) and not missing and not pending
        reply = decision.reply or ""
        if decision.ready_to_finalize and not ready:
            questions = [PROFILE_QUESTIONS[k] for k in missing[:2]] if missing else pending[:2]
            reply = "还需要补充了解一些信息，才能整理画像。" + " ".join(questions) + " 如果不愿回答，可以明确告诉我。"
        return {
            "messages": [AIMessage(content=reply)],
            "understanding": draft,
            "notes": notes,
            "ready_to_finalize": ready,
            "refused_fields": refused,
            "pending_questions": pending,
            "rationale": decision.rationale or "",
            "reasoning": reasoning,
            "turn_count": turn_count,
            "degraded": False,
            "error": None,
        }

    return converse


def make_finalize(finalize_runner: FinalizeRunner, *, system_prompt: str = FINALIZE_SYSTEM):
    """收尾：合成结构化画像 + 一段自由表述。"""

    def finalize(state: dict) -> dict:
        draft = state.get("understanding") or ProfileDraft()
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(
                content=build_finalize_human(
                    _render_transcript(state.get("messages", [])),
                    draft.model_dump_json(exclude_none=True),
                    state.get("notes") or "",
                )
            ),
        ]
        try:
            result, _ = _split_runner_result(finalize_runner(messages))
            if not isinstance(result, ProfileResult):
                result = ProfileResult.model_validate(result)
            profile = _fill_missing(result.profile, draft, Profile)
            for key in state.get("refused_fields") or {}:
                if key in PROFILE_QUESTIONS and getattr(draft, key) is None:
                    setattr(profile, key, None)
            profile.notes = _merge_notes(profile.notes or "", state.get("notes") or "") or None
            return {
                "final_profile": profile,
                "articulation": (result.articulation or "").strip() or (state.get("notes") or ""),
                "awaiting_confirmation": True,
                "degraded": False,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - 用当前理解兜底
            return {
                "final_profile": _fill_missing(draft, draft, Profile),
                "articulation": state.get("notes") or "",
                "awaiting_confirmation": True,
                "degraded": True,
                "error": f"{type(exc).__name__}: {exc}",
            }

    return finalize


def confirm_node(state: dict) -> Command:
    """用户核对画像：``interrupt()`` 挂起等 resume。

    ``interrupt`` 放在首行之后、且其之前没有副作用，保证恢复重跑时幂等。
    """
    profile = state.get("final_profile") or Profile()
    payload = {
        "type": "profile_confirmation",
        "profile": profile.model_dump(exclude_none=True),
        "articulation": state.get("articulation") or "",
        "options": list(CONFIRM_ACTIONS),
    }
    info = _normalize_resume(interrupt(payload))

    if info["action"] == "confirm":
        return Command(goto=END, update={"confirmed": True, "awaiting_confirmation": False})

    update: dict[str, Any] = {
        "confirmed": False,
        "awaiting_confirmation": False,
        "ready_to_finalize": False,
    }
    message = info.get("message")
    if message:
        update["messages"] = [HumanMessage(content=str(message))]
    return Command(goto="converse", update=update)


def fallback_node(state: dict) -> dict:
    """模型不可用时的规则引导。"""
    return {
        "messages": [AIMessage(content=degraded_reply(state.get("understanding")))],
        "degraded": True,
        "ready_to_finalize": False,
        "awaiting_confirmation": False,
    }


def route_after_converse(state: dict) -> str:
    """converse 之后的分支：降级 / 收尾 / 等下一句。"""
    if state.get("degraded"):
        return "fallback"
    if state.get("ready_to_finalize"):
        return "finalize"
    return "wait"
