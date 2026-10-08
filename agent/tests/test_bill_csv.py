"""账单解析器：表头识别、金额换算、类目归一、一次性大额、多月趋势、告警。"""

from __future__ import annotations

from moneyrouter_agent.tools.bills import ParsedBill, parse_bill

SAMPLE = """日期,分类,金额,摘要
2026-09-01,餐饮,-88.00,朋友聚餐
2026-09-03,外卖,-42.50,
2026-09-05,工资,8000.00,9月工资
2026-09-08,房租,-2500.00,9月房租
2026-09-12,数码,-2000.00,换手机
2026-08-20,餐饮,-60.00,上月聚餐
"""


def test_header_amounts_and_category_mapping():
    parsed = parse_bill(SAMPLE, period="2026-09")
    by_cat = {c.category: c.amount_cents for c in parsed.categories}

    assert parsed.period == "2026-09"
    assert by_cat["餐饮"] == 13_050  # 88 + 42.5 元 = 130.5 元
    assert by_cat["居住"] == 250_000
    assert by_cat["购物"] == 200_000  # 数码 -> 购物
    assert parsed.income is not None and parsed.income.amount_cents == 800_000
    assert all(c.source == "file" for c in parsed.categories)


def test_one_off_flag_excludes_recurring_categories():
    parsed = parse_bill(SAMPLE, period="2026-09")
    labels = [item.label for item in parsed.one_offs]
    assert "换手机" in labels
    # 房租属固定支出，不该被当作一次性大额
    assert all("房租" not in label for label in labels)


def test_multi_month_produces_trend_points():
    parsed = parse_bill(SAMPLE, period="2026-09")
    periods = [point.period for point in parsed.monthly_points]
    assert "2026-08" in periods and "2026-09" in periods


def test_missing_amount_column_degrades_cleanly():
    parsed = parse_bill("日期,分类,摘要\n2026-09-01,餐饮,午饭\n", period="2026-09")
    assert "amount" in parsed.missing
    assert parsed.warnings


def test_unknown_category_warns_but_is_kept():
    parsed = parse_bill("日期,分类,金额\n2026-09-01,神秘项目,-30.00\n", period="2026-09")
    assert any(c.category == "其他" for c in parsed.categories)
    assert any("未知类目" in w for w in parsed.warnings)


def test_bad_amount_row_is_skipped_with_warning():
    parsed = parse_bill("日期,分类,金额\n2026-09-01,餐饮,不是数字\n", period="2026-09")
    assert parsed.categories == []
    assert any("无法解析" in w for w in parsed.warnings)


def test_empty_file_does_not_raise():
    parsed = parse_bill("", period="2026-09")
    assert isinstance(parsed, ParsedBill)
    assert parsed.warnings


def test_bytes_with_bom_are_accepted():
    payload = SAMPLE.encode("utf-8-sig")
    parsed = parse_bill(payload, period="2026-09")
    assert any(c.category == "餐饮" for c in parsed.categories)
