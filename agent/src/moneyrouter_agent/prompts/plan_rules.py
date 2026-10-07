"""方案生成的降级文案。

模型不可用（无密钥 / 调用失败 / 结构化解析失败）时，仍由代码跑完七步、用这里的模板
把数字讲清楚，保证"没有 AI 也能用"。语气与主路径一致：亲切但有分寸，无口语化语气词。
"""

from __future__ import annotations

from ..domain.money import format_yuan
from ..domain.plan import Plan

PLAN_DEGRADED_NOTICE = "（详细解释暂时没能生成，先按可复算的规则给您一份安排——）"


def render_plan_narrative(plan: Plan) -> str:
    """按方案里的数字生成一段平实的说明（**只复述代码算出的结果，不新增数值**）。"""
    cf = plan.cashflow
    budget = plan.budget
    reserve = plan.reserve
    risk = plan.risk

    parts: list[str] = []
    parts.append(
        f"按您近几个月的收支，月均收入约 {format_yuan(cf.income_cents)} 元，"
        f"必要开支约 {format_yuan(cf.necessary_cents)} 元、债务还款约 {format_yuan(cf.debt_cents)} 元。"
        f"在留出可选开支 {format_yuan(budget.wants_cents)} 元后，计划每月结余约 "
        f"{format_yuan(budget.savings_cents)} 元。"
    )

    if reserve.high_interest_debt:
        parts.append("这个月的债务还款占收入偏高，建议先核对并优先偿还成本较高的债务。")

    if reserve.gap_cents > 0:
        parts.append(
            f"应急预备金的目标是 {reserve.months} 个月必要开支、约 {format_yuan(reserve.target_cents)} 元，"
            f"目前还差约 {format_yuan(reserve.gap_cents)} 元，建议每月先补 "
            f"{format_yuan(reserve.monthly_cents)} 元。"
        )
    else:
        parts.append("应急预备金已基本补足，可以开始安排结余的用途。")

    if plan.allocation.recommended:
        segments = "、".join(f"{s.category} {format_yuan(s.amount_cents)} 元" for s in plan.allocation.recommended)
        parts.append(
            f"本月可配置的资金约 {format_yuan(plan.allocation.investable_cents)} 元，"
            f"按您的风险档（{risk.level}）建议分为：{segments}。"
        )
    else:
        parts.append("本月结余优先用于保障与预备金，暂不安排新增投资。")

    if plan.scenario is not None:
        reach = "有望达到" if plan.scenario.goal_reachable else "仍未达到"
        parts.append(
            f"在假设情景下（不是收益预测），这套安排的年化约为 "
            f"{plan.scenario.net_annual_pct:.2f}%，距离 3% 的目标{reach}。"
        )

    if plan.warnings:
        parts.append("需要留意：" + "；".join(plan.warnings[:3]) + "。")

    return "".join(parts)
