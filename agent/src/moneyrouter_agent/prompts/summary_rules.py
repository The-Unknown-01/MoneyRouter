"""总结环节的降级文案与规则回退。

模型不可用（无密钥 / 调用失败 / 结构化输出解析失败）时使用，保证"没有 AI 也能用"：
直接从差异表产复盘要点、用模板给每条候选填结论。语气与主路径一致——
不用「咱们」「好呀」这类过度口语化表达。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.money import format_yuan

if TYPE_CHECKING:  # 仅为类型提示，避免运行期循环导入
    from ..domain.experience import Lesson
    from ..domain.summary import PlanActualDiff, SummaryDraft

SUMMARY_DEGRADED_NOTICE = "（这次没能生成详细复盘，先用规则把关键差异理一遍——）"

LESSON_STATEMENT_TEMPLATES: dict[str, str] = {
    "wants_down": "本月可选支出高于计划，下个月宜适当收紧可选支出上限。",
    "risk_conservative": "本月投资出现亏损，下个月宜更稳妥地控制风险。",
    "reserve_priority": "本月在应急金尚未补足时仍在新增投资，下个月宜优先留足应急金。",
    "debt_priority": "本月存在成本偏高的债务，下个月宜优先偿还、暂缓新增投资。",
    "goal_pace": "本月攒钱进度落后于计划，下个月宜向目标多留一些。",
    "custom": "本月该方面出现变化，暂作记录、下月继续观察。",
}

LAYER_LABELS: dict[str, str] = {
    "income": "收入",
    "necessary": "必要支出",
    "wants": "可选支出",
    "savings": "可储蓄",
    "growth": "增长类配置",
}


def lesson_statement(kind: str) -> str:
    """某类经验的兜底结论（模型没写时用）。"""
    return LESSON_STATEMENT_TEMPLATES.get(kind, LESSON_STATEMENT_TEMPLATES["custom"])


def render_headline(diff: "PlanActualDiff") -> str:
    """一句话结论——只复述代码算出来的数字。"""
    if not diff.plan_available:
        return f"{diff.period} 的实际情况已整理，本月没有可对照的方案。"
    overs = [
        item
        for item in diff.layers
        if item.delta_cents is not None and item.delta_cents > 0 and item.layer in ("wants", "necessary")
    ]
    if overs:
        names = "、".join(LAYER_LABELS.get(item.layer, item.layer) for item in overs)
        return f"本月 {names} 高于计划，其余科目基本按计划执行。"
    return "本月各项基本按计划执行。"


def render_sections(diff: "PlanActualDiff") -> list[str]:
    """复盘要点——全部由代码按差异表生成，模型不可写数字。"""
    lines: list[str] = []
    for item in diff.layers:
        label = LAYER_LABELS.get(item.layer, item.layer)
        if item.planned_cents is None:
            if item.actual_cents is not None:
                lines.append(f"{label}实际 {format_yuan(item.actual_cents)} 元。")
            continue
        if item.actual_cents is None:
            lines.append(f"{label}计划 {format_yuan(item.planned_cents)} 元，实际未了解到。")
            continue
        delta = item.delta_cents or 0
        if delta > 0:
            lines.append(
                f"{label}计划 {format_yuan(item.planned_cents)} 元，实际 {format_yuan(item.actual_cents)} 元，"
                f"高出 {format_yuan(delta)} 元。"
            )
        elif delta < 0:
            lines.append(
                f"{label}计划 {format_yuan(item.planned_cents)} 元，实际 {format_yuan(item.actual_cents)} 元，"
                f"低于计划 {format_yuan(abs(delta))} 元。"
            )
        else:
            lines.append(
                f"{label}计划与实际均为 {format_yuan(item.actual_cents)} 元，按计划执行。"
            )

    if diff.reserve_gap_cents:
        lines.append(f"应急金尚有 {format_yuan(diff.reserve_gap_cents)} 元缺口。")
    if diff.investment_return_cents:
        direction = "盈利" if diff.investment_return_cents > 0 else "亏损"
        lines.append(f"已有投资本月{direction} {format_yuan(abs(diff.investment_return_cents))} 元。")
    if not diff.plan_available:
        lines.append("本月缺少可对照的方案，偏差未作判断。")
    return lines


def degraded_notice() -> str:
    return SUMMARY_DEGRADED_NOTICE
