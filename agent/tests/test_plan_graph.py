"""方案生成图 — 脚本化假模型测试：工具编排、追问、确认三分支、轮数上限。"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage

from moneyrouter_agent.config import PlanSettings, Settings
from moneyrouter_agent.domain.month import CategorySpend, IncomeFact, MonthSnapshot
from moneyrouter_agent.domain.plan import PlanInputs
from moneyrouter_agent.domain.plan_turn import PlanAdjustment, PlanTurnDecision
from moneyrouter_agent.domain.profile import Profile
from moneyrouter_agent.model.deepseek import StructuredCall
from moneyrouter_agent.plan_agent import PlanAgent


class ScriptedAgentModel:
    """按预置列表返回 ``AIMessage``；``bind_tools`` 直接返回自身（模拟已绑定工具）。"""

    def __init__(self, messages: list[AIMessage]) -> None:
        self._messages = list(messages)
        self.calls = 0
        self.seen: list[list[Any]] = []

    def bind_tools(self, tools: list[Any]) -> "ScriptedAgentModel":
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.calls += 1
        self.seen.append(list(messages))
        if not self._messages:
            return AIMessage(content="")
        return self._messages.pop(0)


class ScriptedRunner:
    """按预置列表返回；元素为 ``Exception`` 时抛出（模拟失败）。"""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.calls = 0

    def __call__(self, messages: list[Any]) -> Any:
        self.calls += 1
        if not self._results:
            raise AssertionError("没有更多预置结果了")
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _inputs() -> PlanInputs:
    snapshot = MonthSnapshot(
        period="2026-09",
        income=IncomeFact(amount_cents=1_000_000, source="file"),
        categories=[
            CategorySpend(category="居住", amount_cents=200_000, source="file"),
            CategorySpend(category="购物", amount_cents=300_000, source="file"),
        ],
    )
    profile = Profile(
        income_cents=1_000_000,
        income_stable=True,
        reserve_cents=2_000_000,
        horizon_months=36,
        max_loss_pct=10,
        experience="some",
        goal="攒钱",
    )
    return PlanInputs(profile=profile, snapshot=snapshot, period="2026-10")


def _tool_call(name: str, call_id: str) -> dict:
    return {"name": name, "args": {}, "id": call_id, "type": "tool_call"}


def _agent(
    agent_messages: list[AIMessage],
    decide_results: list[Any],
    adjust_results: list[Any] | None = None,
    *,
    plan_settings: PlanSettings | None = None,
) -> tuple[PlanAgent, ScriptedAgentModel]:
    model = ScriptedAgentModel(agent_messages)
    agent = PlanAgent(
        settings=Settings(api_key="dummy"),
        plan_settings=plan_settings,
        inputs=_inputs(),
        agent_model=model,
        decide_runner=ScriptedRunner(decide_results),
        adjust_runner=ScriptedRunner(adjust_results or []),
    )
    return agent, model


# --------------------------------------------------------------------------- #
# 工具循环 + 收尾
# --------------------------------------------------------------------------- #
def test_tool_loop_then_finalize() -> None:
    agent, model = _agent(
        [
            AIMessage(content="", tool_calls=[_tool_call("summarize_cashflow", "c1")]),
            AIMessage(content="资料够了。"),
        ],
        [
            PlanTurnDecision(
                status="finalize",
                reply="这是我给您的安排。",
                headline="本月资金安排",
                sections=["现金流部分……", "预备金部分……"],
            )
        ],
    )
    result = agent.plan("g-tools")

    assert model.calls == 2  # 一次调工具、一次收尾
    assert result.plan is not None
    assert result.plan.headline == "本月资金安排"
    assert "现金流部分" in result.plan.narrative
    assert result.awaiting_confirmation is True
    assert result.reply == "这是我给您的安排。"


# --------------------------------------------------------------------------- #
# 追问
# --------------------------------------------------------------------------- #
def test_ask_path_waits_for_user() -> None:
    agent, model = _agent(
        [AIMessage(content="先看看资料。")],
        [PlanTurnDecision(status="ask", reply="方便说一下您的收入情况吗？", questions=["收入"])],
    )
    result = agent.plan("g-ask")

    assert model.calls == 1
    assert result.ready_to_finalize is False
    assert result.awaiting_confirmation is False
    assert result.plan is None
    assert "收入" in result.reply


# --------------------------------------------------------------------------- #
# 反馈重算（edit）
# --------------------------------------------------------------------------- #
def test_edit_feedback_recomputes_plan() -> None:
    agent, _model = _agent(
        [AIMessage(content="资料够了。")],
        [PlanTurnDecision(status="finalize", reply="初版方案", headline="初版")],
        [PlanAdjustment(wants_ratio_pct=10.0, notes="餐饮再省一点")],
    )
    first = agent.plan("g-edit")
    assert first.plan is not None
    baseline_ratio = first.plan.budget.wants_ratio_pct
    assert baseline_ratio == 30.0

    revised = agent.plan("g-edit", resume={"action": "edit", "message": "餐饮想再省一点"})
    assert revised.plan is not None
    assert revised.plan.budget.wants_ratio_pct == 10.0
    assert revised.plan.budget.wants_cents < first.plan.budget.wants_cents
    assert revised.awaiting_confirmation is True
    assert revised.confirmed is False


# --------------------------------------------------------------------------- #
# 补充信息（more）
# --------------------------------------------------------------------------- #
def test_more_returns_to_agent() -> None:
    agent, model = _agent(
        [AIMessage(content="资料够了。"), AIMessage(content="收到补充。")],
        [
            PlanTurnDecision(status="finalize", reply="初版", headline="初版"),
            PlanTurnDecision(status="finalize", reply="更新版", headline="更新版"),
        ],
    )
    agent.plan("g-more")
    result = agent.plan("g-more", resume={"action": "more", "message": "还有一笔兼职收入"})

    assert model.calls == 2
    assert result.plan is not None
    assert result.plan.headline == "更新版"


# --------------------------------------------------------------------------- #
# 工具轮数上限
# --------------------------------------------------------------------------- #
def test_tool_round_cap_forces_decide() -> None:
    agent, model = _agent(
        [
            AIMessage(content="", tool_calls=[_tool_call("summarize_cashflow", "c1")]),
            AIMessage(content="", tool_calls=[_tool_call("propose_allocation", "c2")]),
            AIMessage(content="", tool_calls=[_tool_call("check_goal", "c3")]),
        ],
        [PlanTurnDecision(status="ask", reply="还差一点信息")],
        plan_settings=PlanSettings(max_tool_rounds=2),
    )
    result = agent.plan("g-cap")

    assert model.calls == 2  # 达到上限就不再回 agent
    assert result.awaiting_confirmation is False


# --------------------------------------------------------------------------- #
# 思维链捕获（仅观测）
# --------------------------------------------------------------------------- #
def test_reasoning_is_captured_but_not_replayed() -> None:
    agent, model = _agent(
        [AIMessage(content="先看看。")],
        [
            StructuredCall(
                parsed=PlanTurnDecision(status="ask", reply="先问一下收入"),
                reasoning="我需要先了解收入才能判断",
            )
        ],
    )
    result = agent.plan("g-reason")
    assert result.reasoning == "我需要先了解收入才能判断"
    # 思维链不回写进对话历史
    last_seen = model.seen[-1]
    assert all("我需要先了解收入" not in str(getattr(m, "content", "")) for m in last_seen)


# --------------------------------------------------------------------------- #
# 判断失败 → 降级
# --------------------------------------------------------------------------- #
def test_decide_failure_falls_back() -> None:
    agent, _model = _agent(
        [AIMessage(content="看看资料。")],
        [RuntimeError("boom")],
    )
    result = agent.plan("g-fallback")
    assert result.degraded is True
    assert result.plan is not None
    assert result.plan.degraded is True
    assert result.awaiting_confirmation is True
