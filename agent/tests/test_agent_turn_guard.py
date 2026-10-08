"""门面层保护：图未停在确认环节时，``resume`` 不能被误当成用户消息（或开场白）。"""

from __future__ import annotations

from typing import Any

from moneyrouter_agent.agent import ProfileAgent
from moneyrouter_agent.config import Settings
from moneyrouter_agent.domain.profile import ProfileResult
from moneyrouter_agent.domain.turn import TurnDecision
from moneyrouter_agent.prompts.interview import OPENING_USER_TURN


class CountingRunner:
    def __init__(self, decisions: list[TurnDecision]) -> None:
        self._decisions = list(decisions)
        self.calls: list[list[Any]] = []

    def __call__(self, messages: list[Any]) -> TurnDecision:
        self.calls.append(messages)
        return self._decisions.pop(0) if self._decisions else TurnDecision(reply="继续")


class _Finalize:
    def __call__(self, messages: list[Any]) -> ProfileResult:
        return ProfileResult()


def _agent(decisions: list[TurnDecision]) -> tuple[ProfileAgent, CountingRunner]:
    runner = CountingRunner(decisions)
    agent = ProfileAgent(
        settings=Settings(api_key="fake-key"), turn_runner=runner, finalize_runner=_Finalize()
    )
    return agent, runner


def test_confirm_when_not_paused_is_a_noop():
    """误在访谈中途敲 /confirm：不能把开场白当用户消息发出去。"""
    agent, runner = _agent([TurnDecision(reply="不该被调用")])

    result = agent.turn("t-guard-1", resume={"action": "confirm"})

    assert runner.calls == []
    assert result.turn_count == 0
    assert result.reply == ""


def test_edit_when_not_paused_sends_its_text_as_user_message():
    """未暂停时 /edit、/more 携带的文本按普通用户消息处理。"""
    agent, runner = _agent([TurnDecision(reply="好的")])

    result = agent.turn("t-guard-2", resume={"action": "edit", "message": "再补充一句"})

    assert len(runner.calls) == 1
    assert runner.calls[0][-1].content == "再补充一句"
    assert result.turn_count == 1


def test_first_turn_still_sends_opening_message():
    """无 resume 的首轮仍发开场占位消息（不回归）。"""
    agent, runner = _agent([TurnDecision(reply="你好")])

    agent.turn("t-guard-3")

    assert runner.calls[0][-1].content == OPENING_USER_TURN
