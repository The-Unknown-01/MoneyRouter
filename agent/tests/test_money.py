"""金额换算与取值范围单测。"""

from decimal import Decimal

import pytest

from moneyrouter_agent.domain.money import (
    GOAL_MAX_CHARS,
    MoneyError,
    cents_to_yuan,
    format_yuan,
    valid_goal,
    valid_horizon_months,
    valid_max_loss_pct,
    yuan_to_cents,
)


def test_yuan_to_cents_basic():
    assert yuan_to_cents(8000) == 800_000
    assert yuan_to_cents("8000.5") == 800_050
    assert yuan_to_cents(0.01) == 1
    assert yuan_to_cents(Decimal("1234.56")) == 123_456


def test_yuan_to_cents_half_up_rounding():
    assert yuan_to_cents("0.005") == 1
    assert yuan_to_cents("0.004") == 0
    assert yuan_to_cents("1.005") == 101


def test_yuan_to_cents_rejects_garbage():
    with pytest.raises(MoneyError):
        yuan_to_cents("一万")
    with pytest.raises(MoneyError):
        yuan_to_cents(True)
    with pytest.raises(MoneyError):
        yuan_to_cents(float("nan"))


def test_cents_to_yuan_and_format():
    assert cents_to_yuan(800_000) == Decimal("8000.00")
    assert format_yuan(1_234_567) == "12,345.67"


def test_valid_horizon_months():
    assert valid_horizon_months(1) and valid_horizon_months(600)
    assert not valid_horizon_months(0)
    assert not valid_horizon_months(601)
    assert not valid_horizon_months("x")


def test_valid_max_loss_pct_zero_is_legitimate():
    # 关键：0 表示「完全不接受亏损」，必须是合法值
    assert valid_max_loss_pct(0)
    assert valid_max_loss_pct(100)
    assert not valid_max_loss_pct(101)
    assert not valid_max_loss_pct(-1)


def test_valid_goal():
    assert valid_goal("攒首付")
    assert not valid_goal("")
    assert not valid_goal("   ")
    assert not valid_goal("x" * (GOAL_MAX_CHARS + 1))
    assert not valid_goal(None)
