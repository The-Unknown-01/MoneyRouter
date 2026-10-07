"""方案生成 — 离线（零注入）降级测试：没有模型也要能出方案并确认。"""

from __future__ import annotations

from moneyrouter_agent.config import Settings
from moneyrouter_agent.domain.month import CategorySpend, IncomeFact, MonthSnapshot
from moneyrouter_agent.domain.plan import PlanInputs
from moneyrouter_agent.domain.profile import Profile
from moneyrouter_agent.plan_agent import PlanAgent
from moneyrouter_agent.prompts.plan_rules import PLAN_DEGRADED_NOTICE


def _inputs() -> PlanInputs:
    snapshot = MonthSnapshot(
        period="2026-09",
        income=IncomeFact(amount_cents=1_000_000, source="file"),
        categories=[
            CategorySpend(category="居住", amount_cents=200_000, source="file"),
            CategorySpend(category="购物", amount_cents=200_000, source="file"),
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


def _offline_agent() -> PlanAgent:
    return PlanAgent(
        settings=Settings(api_key=None, key_file=None),
        inputs=_inputs(),
    )


def test_offline_still_produces_plan_and_awaits_confirmation() -> None:
    agent = _offline_agent()
    result = agent.plan("offline-1")

    assert result.degraded is True
    assert result.plan is not None
    assert result.plan.degraded is True
    assert "可复算" in result.plan.narrative or PLAN_DEGRADED_NOTICE in result.plan.narrative
    assert result.awaiting_confirmation is True
    assert result.confirmation_payload is not None
    assert result.confirmation_payload["type"] == "plan_confirmation"


def test_offline_plan_is_valid_and_confirmable() -> None:
    agent = _offline_agent()
    agent.plan("offline-2")
    confirmed = agent.plan("offline-2", resume={"action": "confirm"})
    assert confirmed.confirmed is True
    assert confirmed.awaiting_confirmation is False


def test_offline_reply_has_no_chatty_filler() -> None:
    agent = _offline_agent()
    result = agent.plan("offline-3")
    text = (result.plan.narrative if result.plan else "") + result.reply
    for banned in ("咱们", "好呀", "～"):
        assert banned not in text
