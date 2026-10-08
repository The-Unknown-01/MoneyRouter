"""用假模型（脚本化 runner）跑图：分支、收尾、确认三分支、0% 亏损。"""

from __future__ import annotations

from typing import Any

import pytest

from moneyrouter_agent.agent import ProfileAgent
from moneyrouter_agent.config import Settings
from moneyrouter_agent.domain.profile import Profile, ProfileDraft, ProfileResult
from moneyrouter_agent.domain.turn import TurnDecision
from moneyrouter_agent.model.deepseek import StructuredCall


class ScriptedTurnRunner:
    """按预置顺序返回 TurnDecision，并记录每次喂进来的 messages。"""

    def __init__(self, decisions: list[TurnDecision]) -> None:
        self._decisions = list(decisions)
        self.calls: list[list[Any]] = []

    def __call__(self, messages: list[Any]) -> TurnDecision:
        self.calls.append(messages)
        if not self._decisions:
            raise AssertionError("脚本化 runner 已无更多决策，但图又调用了一次")
        return self._decisions.pop(0)


class ReasoningTurnRunner:
    """按真实 runner 的形状返回 ``StructuredCall``（含思维链）。"""

    def __init__(self, decisions: list[TurnDecision], reasoning: str | None = "先摸清身份，再问收支。") -> None:
        self._decisions = list(decisions)
        self._reasoning = reasoning
        self.calls: list[list[Any]] = []

    def __call__(self, messages: list[Any]) -> StructuredCall:
        self.calls.append(messages)
        return StructuredCall(parsed=self._decisions.pop(0), reasoning=self._reasoning)


class ScriptedFinalizeRunner:
    def __init__(self, result: ProfileResult | Exception) -> None:
        self._result = result
        self.calls: list[list[Any]] = []

    def __call__(self, messages: list[Any]) -> ProfileResult:
        self.calls.append(messages)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _student_draft() -> ProfileDraft:
    return ProfileDraft(
        occupation="大四学生",
        income_cents=150_000,
        income_basis="每月生活费",
        outcome_cents=130_000,
        feature="家庭提供生活费",
        income_stable=True,
        family_load=False,
        debt_cents=0,
        reserve_cents=500_000,
        horizon_months=36,
        max_loss_pct=0,  # 完全不接受亏损——必须是合法值
        goal="毕业后租房押金",
        experience="none",
    )


def _agent(decisions: list[TurnDecision], finalize: Any) -> ProfileAgent:
    return ProfileAgent(
        settings=Settings(api_key="fake-key"),
        turn_runner=ScriptedTurnRunner(decisions),
        finalize_runner=finalize if callable(finalize) else ScriptedFinalizeRunner(finalize),
    )


# --------------------------------------------------------------------------- #
def test_not_ready_waits_for_next_turn():
    agent = _agent(
        [TurnDecision(reply="方便说说您是做什么的吗？", understanding=ProfileDraft())],
        ProfileResult(),
    )
    result = agent.turn("t-wait", "你好")

    assert result.degraded is False
    assert result.reply == "方便说说您是做什么的吗？"
    assert result.ready_to_finalize is False
    assert result.awaiting_confirmation is False
    assert result.understanding.occupation is None


def test_ready_triggers_finalize_then_confirmation():
    agent = _agent(
        [
            TurnDecision(
                reply="我了解得差不多了，帮您整理一下。",
                understanding=_student_draft(),
                notes="用户是大四学生，靠家里给生活费。",
                ready_to_finalize=True,
                rationale="核心信息已齐",
            )
        ],
        ProfileResult(profile=Profile(**_student_draft().model_dump()), articulation="用户是一名大四学生。"),
    )
    result = agent.turn("t-ready", "就是这些")

    assert result.ready_to_finalize is True
    assert result.awaiting_confirmation is True
    assert result.confirmation_payload is not None
    assert result.confirmation_payload["options"] == ["confirm", "edit", "more"]
    assert result.final_profile is not None
    assert result.final_profile.occupation == "大四学生"
    assert result.articulation == "用户是一名大四学生。"
    assert result.confirmed is False


def test_zero_percent_loss_survives_the_graph():
    agent = _agent(
        [
            TurnDecision(
                reply="整理一下",
                understanding=_student_draft(),
                ready_to_finalize=True,
            )
        ],
        ProfileResult(profile=Profile(**_student_draft().model_dump()), articulation="x"),
    )
    result = agent.turn("t-zero", "我一点亏损都不能接受")

    assert result.understanding.max_loss_pct == 0
    assert result.final_profile is not None
    assert result.final_profile.max_loss_pct == 0


def test_confirm_branch_sets_confirmed():
    agent = _agent(
        [
            TurnDecision(reply="整理一下", understanding=_student_draft(), ready_to_finalize=True),
        ],
        ProfileResult(profile=Profile(**_student_draft().model_dump()), articulation="x"),
    )
    agent.turn("t-confirm", "就是这些")
    result = agent.turn("t-confirm", resume={"action": "confirm"})

    assert result.confirmed is True
    assert result.awaiting_confirmation is False


def test_edit_branch_goes_back_to_converse():
    agent = _agent(
        [
            TurnDecision(reply="整理一下", understanding=_student_draft(), ready_to_finalize=True),
            TurnDecision(
                reply="好的，那我把期限改成 60 个月。",
                understanding=ProfileDraft(horizon_months=60),
                ready_to_finalize=False,
            ),
        ],
        ProfileResult(profile=Profile(**_student_draft().model_dump()), articulation="x"),
    )
    agent.turn("t-edit", "就是这些")
    result = agent.turn("t-edit", resume={"action": "edit", "message": "期限其实是 5 年"})

    assert result.confirmed is False
    assert result.awaiting_confirmation is False
    assert result.ready_to_finalize is False
    # 编辑意见作为用户消息进入历史；旧快照的字段被保留，新字段被更新
    assert result.understanding.horizon_months == 60
    assert result.understanding.occupation == "大四学生"


def test_finalize_failure_falls_back_to_current_understanding():
    agent = _agent(
        [
            TurnDecision(reply="整理一下", understanding=_student_draft(), ready_to_finalize=True),
        ],
        ScriptedFinalizeRunner(RuntimeError("boom")),
    )
    result = agent.turn("t-finalize-fail", "就是这些")

    assert result.awaiting_confirmation is True
    assert result.degraded is True
    assert result.final_profile is not None
    assert result.final_profile.occupation == "大四学生"


def test_empty_thread_id_rejected():
    agent = _agent([TurnDecision(reply="hi")], ProfileResult())
    with pytest.raises(ValueError):
        agent.turn("", "你好")


def test_partial_snapshot_does_not_lose_previous_fields():
    """模型若只回增量，节点层应把旧值补齐。"""
    agent = _agent(
        [
            TurnDecision(reply="a", understanding=ProfileDraft(occupation="程序员")),
            TurnDecision(reply="b", understanding=ProfileDraft(income_cents=2_000_000)),
        ],
        ProfileResult(),
    )
    agent.turn("t-merge", "我是程序员")
    result = agent.turn("t-merge", "月薪两万")

    assert result.understanding.occupation == "程序员"
    assert result.understanding.income_cents == 2_000_000


# --------------------------------------------------------------------------- #
# 思维链：只做观测，绝不进对话历史
# --------------------------------------------------------------------------- #
def test_reasoning_surfaces_to_turn_result():
    runner = ReasoningTurnRunner([TurnDecision(reply="方便说说您是做什么的吗？")])
    agent = ProfileAgent(
        settings=Settings(api_key="fake-key"),
        turn_runner=runner,
        finalize_runner=ScriptedFinalizeRunner(ProfileResult()),
    )
    result = agent.turn("t-reason", "你好")

    assert result.reasoning == "先摸清身份，再问收支。"


def test_scripted_runner_without_structured_call_yields_no_reasoning():
    """兼容直接返回领域对象的旧式 runner。"""
    agent = _agent([TurnDecision(reply="hi")], ProfileResult())
    assert agent.turn("t-no-reason", "你好").reasoning is None


def test_reasoning_is_never_written_back_into_conversation():
    """思维链不能出现在后续任何一轮喂给模型的消息里。"""
    runner = ReasoningTurnRunner(
        [TurnDecision(reply="a"), TurnDecision(reply="b")], reasoning="仅供观测的思维链"
    )
    agent = ProfileAgent(
        settings=Settings(api_key="fake-key"),
        turn_runner=runner,
        finalize_runner=ScriptedFinalizeRunner(ProfileResult()),
    )
    agent.turn("t-reason-2", "第一句")
    agent.turn("t-reason-2", "第二句")

    for payload in runner.calls:
        assert all("仅供观测的思维链" not in str(message.content) for message in payload)
