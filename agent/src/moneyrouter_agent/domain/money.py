"""金额与取值范围工具。

约定：内部一律以「分」（int）存储金额，表单与对话以「元」呈现。
取值范围与语义对齐 Go 侧（``finance.go`` / ``profile.go``）。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

CENTS_PER_YUAN = Decimal(100)

# 取值范围（对齐 Go 侧）
HORIZON_MIN_MONTHS = 1
HORIZON_MAX_MONTHS = 600
MAX_LOSS_PCT_MIN = 0
MAX_LOSS_PCT_MAX = 100
GOAL_MAX_CHARS = 500


class MoneyError(ValueError):
    """金额解析或换算失败。"""


def yuan_to_cents(value: Any) -> int:
    """把「元」（int/float/str/Decimal）换算成「分」，四舍五入到分。"""
    if isinstance(value, bool):
        raise MoneyError("布尔值不是合法金额")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ArithmeticError, AttributeError) as exc:
        raise MoneyError(f"无法解析金额: {value!r}") from exc
    if amount.is_nan() or amount.is_infinite():
        raise MoneyError(f"无法解析金额: {value!r}")
    cents = (amount * CENTS_PER_YUAN).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(cents)


def cents_to_yuan(cents: int) -> Decimal:
    """把「分」换算回「元」（Decimal，保留两位小数）。"""
    if cents is None:
        raise MoneyError("金额为空")
    return (Decimal(int(cents)) / CENTS_PER_YUAN).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def format_yuan(cents: int) -> str:
    """把「分」格式化为带千分位的「元」字符串，如 1234567 -> '12,345.67'。"""
    return f"{cents_to_yuan(cents):,.2f}"


def valid_horizon_months(months: Any) -> bool:
    """期限是否落在 1-600 个月。"""
    try:
        value = int(months)
    except (TypeError, ValueError):
        return False
    return HORIZON_MIN_MONTHS <= value <= HORIZON_MAX_MONTHS


def valid_max_loss_pct(pct: Any) -> bool:
    """可承受亏损百分比是否落在 0-100。

    注意：``0`` 是合法值（完全不接受亏损），不能被当作「未回答」。
    """
    try:
        value = int(pct)
    except (TypeError, ValueError):
        return False
    return MAX_LOSS_PCT_MIN <= value <= MAX_LOSS_PCT_MAX


def valid_goal(goal: Any) -> bool:
    """目标是否为非空且不超过 500 字的字符串。"""
    return isinstance(goal, str) and 0 < len(goal.strip()) <= GOAL_MAX_CHARS
