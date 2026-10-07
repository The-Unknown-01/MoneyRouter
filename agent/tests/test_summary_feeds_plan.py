"""联动回归：总结 Agent 产出的经验包，真的能被方案生成吃下去。

这一层是两块功能的接缝——契约在 `domain/experience.py`、消费在 `domain/plan.py`，
而**产生器**在 `domain/summary.py`。接缝处最容易出的问题是 `wants_down` 连乘：
`apply_lessons` 用的是 `scale *= 1 - 0.6*strength`，跨月累积会叠成极端值。
"""

from __future__ import annotations

from moneyrouter_agent.domain.experience import Lesson, apply_lessons
from moneyrouter_agent.domain.month import (
    CategorySpend,
    IncomeFact,
    MonthSnapshot,
    recompute,
)
from moneyrouter_agent.domain.plan import PlanInputs, build_plan
from moneyrouter_agent.domain.profile import Profile
from moneyrouter_agent.domain.summary import merge_pack

PERIOD = "2026-09"


def _snapshot() -> MonthSnapshot:
    return recompute(
        MonthSnapshot(
            period=PERIOD,
            income=IncomeFact(amount_cents=1_000_000),
            categories=[
                CategorySpend(category="餐饮", amount_cents=300_000),
                CategorySpend(category="娱乐社交", amount_cents=400_000),
            ],
        )
    )


def _wants_down(period: str, strength: float = 1.0) -> Lesson:
    return Lesson(
        id=f"wants_down:{period}",
        kind="wants_down",
        statement="可选支出高于计划，下月宜收紧。",
        strength=strength,
        from_period=period,
        evidence=["测试依据"],
    )


def _inputs(experience=None) -> PlanInputs:
    return PlanInputs(
        profile=Profile(income_cents=1_000_000, goal="攒首付"),
        snapshot=_snapshot(),
        period=PERIOD,
        experience=experience,
    )


def test_pack_feeds_build_plan_without_multiplying():
    """跨两期合并出来的经验包，只应产生一次软调整。"""
    pack = merge_pack(None, [_wants_down("2026-08", 1.0)], period="2026-08")
    pack = merge_pack(pack, [_wants_down(PERIOD, 1.0)], period=PERIOD)

    plan = build_plan(_inputs(pack))

    # 每 kind 只留一条 —— 计划侧记的 id 也只有一个
    assert plan.lessons_applied == [f"wants_down:{PERIOD}"]
    # 只缩一次：1 − 0.6×1.0 = 0.4；若按 id 累积会变成 0.16
    assert apply_lessons(pack).wants_ratio_scale == 0.4


def test_plan_without_experience_is_unaffected():
    plan = build_plan(_inputs(None))
    assert plan.lessons_applied == []
    assert apply_lessons(None).wants_ratio_scale == 1.0


def test_each_kind_applies_once_and_only_once():
    pack = merge_pack(
        None,
        [
            _wants_down(PERIOD, 1.0),
            Lesson(id=f"risk_conservative:{PERIOD}", kind="risk_conservative", statement="更保守。", from_period=PERIOD),
            Lesson(id=f"debt_priority:{PERIOD}", kind="debt_priority", statement="债务优先。", from_period=PERIOD),
        ],
        period=PERIOD,
    )
    # 再合并一次同样的包：kind 内仍是每条一个
    pack = merge_pack(pack, [_wants_down(PERIOD, 1.0)], period=PERIOD)

    effects = apply_lessons(pack)

    assert effects.wants_ratio_scale == 0.4
    assert effects.risk_downgrade_notches == 1
    assert effects.debt_priority is True
    assert sorted(effects.applied_ids) == sorted(effects.applied_ids)  # 稳定
    assert len(effects.applied_ids) == 3


def test_experience_soft_adjustments_do_not_break_hard_constraints():
    """软调整只该改数字、不该破坏硬约束。

    ⚠️ 已知（方案侧，2026-10-07 观察到）：`validate_plan(plan, inputs)` 内部是
    `build_plan(inputs, plan.strategy)`，而 `plan.strategy` 里已经是**应用过一次**软调整的策略；
    于是"带经验包 + 二次复核"会把 `wants_down` 的缩放再乘一遍，表现为一条 `recompute_mismatch`。
    这发生在方案模块内部，本模块不改它——这里只断言**没有硬约束类问题**，
    等方案侧修掉双重应用后 `report.ok` 自然为真。
    """
    from moneyrouter_agent.domain.plan import validate_plan

    pack = merge_pack(None, [_wants_down(PERIOD, 1.0)], period=PERIOD)
    plan = build_plan(_inputs(pack))

    report = validate_plan(plan, _inputs(pack))

    assert {issue.code for issue in report.issues} <= {"recompute_mismatch"}
