"""方案生成提示词契约：章节骨架、语气纪律、合规红线、不掺管道与字段名。"""

from __future__ import annotations

from moneyrouter_agent.domain.month import CategorySpend, IncomeFact, MonthSnapshot
from moneyrouter_agent.domain.plan import PlanInputs, build_plan
from moneyrouter_agent.domain.plan_turn import PlanAdjustment, PlanTurnDecision
from moneyrouter_agent.domain.profile import Profile
from moneyrouter_agent.model.deepseek import format_instruction
from moneyrouter_agent.prompts.plan import (
    PLAN_ADJUST_SYSTEM,
    PLAN_DECIDE_SYSTEM,
    PLAN_SYSTEM,
)
from moneyrouter_agent.prompts.plan_rules import PLAN_DEGRADED_NOTICE, render_plan_narrative

ALL_PROMPTS = (PLAN_SYSTEM, PLAN_DECIDE_SYSTEM, PLAN_ADJUST_SYSTEM)


def test_system_has_required_skeleton() -> None:
    for heading in (
        "# 你的目标",
        "# 怎么推进",
        "# 怎么说话：亲切，但有分寸",
        "# 什么时候收尾",
        "# 合规与纪律（必须遵守）",
    ):
        assert heading in PLAN_SYSTEM, heading


def test_tone_is_warm_but_measured() -> None:
    assert "亲切，但有分寸" in PLAN_SYSTEM
    assert "闲聊的朋友" in PLAN_SYSTEM
    assert "不要 emoji" in PLAN_SYSTEM
    # 点名禁用的口语化表达
    for banned in ("好呀", "咱们", "挺不错的"):
        assert banned in PLAN_SYSTEM
    assert "过度口语化" in PLAN_SYSTEM or "拉近距离" in PLAN_SYSTEM


def test_compliance_redlines() -> None:
    assert "不构成投资建议" in PLAN_SYSTEM
    assert "不代替用户决策" in PLAN_SYSTEM
    for banned in ("稳赚", "保本高息", "必赚"):
        assert banned in PLAN_SYSTEM
    assert "不推荐具体产品" in PLAN_SYSTEM
    assert "不预测涨跌" in PLAN_SYSTEM


def test_scenario_is_not_a_forecast() -> None:
    assert "情景" in PLAN_SYSTEM
    assert "不是收益预测" in PLAN_SYSTEM


def test_amounts_must_come_from_tools() -> None:
    assert "工具" in PLAN_SYSTEM
    assert "不能自己算" in PLAN_SYSTEM


def test_prompts_do_not_leak_pipeline_or_schema_names() -> None:
    forbidden = [
        "messages",
        "thread_id",
        "json",
        "JSON",
        "snapshot",
        "reply",
        "status",
        "sections",
        "headline",
        "narrative",
        "rationale",
        "wants_ratio_pct",
        "risk_level_override",
        "reserve_months",
        "category_cap_cents",
        "tool_rounds",
        "awaiting_confirmation",
        "plan_confirmation",
        "lessons_applied",
        "max_loss_pct",
        "horizon_months",
    ]
    for prompt in ALL_PROMPTS:
        for token in forbidden:
            assert token not in prompt, f"{token} 不应出现在提示词里"


def test_field_list_lives_in_the_model_layer() -> None:
    for schema in (PlanTurnDecision, PlanAdjustment):
        content = format_instruction(schema).content
        assert "json" in content
    turn = format_instruction(PlanTurnDecision).content
    assert "wants_ratio_pct" in turn
    assert "risk_level_override" in turn
    adjust = format_instruction(PlanAdjustment).content
    assert "category_cap_cents" in adjust


def test_degraded_wording_respects_tone() -> None:
    text = PLAN_DEGRADED_NOTICE
    for banned in ("咱们", "好呀", "～"):
        assert banned not in text


def test_render_plan_narrative_uses_plan_numbers_and_stays_neutral() -> None:
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
        reserve_cents=10_000_000,
        horizon_months=36,
        max_loss_pct=10,
        experience="some",
    )
    plan = build_plan(PlanInputs(profile=profile, snapshot=snapshot, period="2026-10"))
    narrative = render_plan_narrative(plan)

    assert "10,000.00" in narrative
    assert "情景" in narrative
    for banned in ("咱们", "好呀", "～"):
        assert banned not in narrative
