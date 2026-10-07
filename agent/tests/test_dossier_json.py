"""规范输入文档（「最干净的格式」）：正例、别名、异常、自动识别、快照携带。"""

from __future__ import annotations

import json

from moneyrouter_agent.tools.bills import looks_like_json, parse_bill
from moneyrouter_agent.tools.dossier import sample_document, sample_dossier

PERIOD = "2026-09"


def _docs(**sections) -> str:
    """按段拼一份文档（默认用样例的各段，便于局部覆盖）。"""
    payload = sample_document(PERIOD)
    payload.update(sections)
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 正例
# --------------------------------------------------------------------------- #
def test_canonical_document_parses_into_facts():
    parsed = parse_bill(sample_dossier(PERIOD), period=PERIOD)

    assert parsed.period == PERIOD
    assert parsed.warnings == []
    by_cat = {c.category: c.amount_cents for c in parsed.categories}
    assert by_cat == {"居住": 250_000, "购物": 200_000, "餐饮": 25_050}
    assert all(c.source == "file" for c in parsed.categories)
    assert parsed.income is not None and parsed.income.amount_cents == 800_000
    assert [item.label for item in parsed.one_offs] == ["换手机"]
    # 「还信用卡」被排除在收支之外，但要计数
    assert parsed.skipped_rows == 1


def test_document_carries_savings_and_investments_as_file_facts():
    parsed = parse_bill(sample_dossier(PERIOD), period=PERIOD)

    assert parsed.allocation is not None
    assert parsed.allocation.non_invested_cents == 324_950
    assert parsed.allocation.invested_cents == 0
    assert parsed.allocation.source == "file"

    assert parsed.investments is not None
    assert parsed.investments.has_investments is True
    assert parsed.investments.source == "file"
    holding = parsed.investments.holdings[0]
    assert holding.name == "沪深300指数基金"
    assert holding.cost_cents == 2_000_000
    assert holding.market_value_cents == 2_080_000
    assert holding.month_return_cents == 8_000
    assert holding.source == "file"


def test_as_snapshot_brings_file_facts_together():
    snapshot = parse_bill(sample_dossier(PERIOD), period=PERIOD).as_snapshot()
    assert snapshot.period == PERIOD
    assert snapshot.allocation.source == "file"
    assert snapshot.investments.source == "file"
    assert snapshot.income is not None


# --------------------------------------------------------------------------- #
# 别名与容错
# --------------------------------------------------------------------------- #
def test_chinese_keys_and_currency_decoration_are_accepted():
    doc = json.dumps(
        {
            "期间": PERIOD,
            "流水": [
                {"日期": f"{PERIOD}-01", "收/支": "支出", "金额": "¥50.00", "分类": "餐饮"},
                {"日期": f"{PERIOD}-02", "收/支": "收入", "金额": 8000, "分类": "工资"},
                {"日期": f"{PERIOD}-03", "收/支": "不计收支", "金额": 1000, "摘要": "还信用卡"},
            ],
        },
        ensure_ascii=False,
    )
    parsed = parse_bill(doc, period=PERIOD)

    assert {c.category: c.amount_cents for c in parsed.categories} == {"餐饮": 5_000}
    assert parsed.income is not None and parsed.income.amount_cents == 800_000
    assert parsed.skipped_rows == 1
    assert parsed.warnings == []


def test_bare_array_of_transactions_is_accepted():
    doc = json.dumps(
        [{"direction": "expense", "amount": 10, "category": "餐饮", "date": f"{PERIOD}-02"}],
        ensure_ascii=False,
    )
    parsed = parse_bill(doc, period=PERIOD)
    assert {c.category: c.amount_cents for c in parsed.categories} == {"餐饮": 1_000}


def test_holdings_can_declare_absent_investments():
    parsed = parse_bill(_docs(investments={"has_investments": False}), period=PERIOD)
    assert parsed.investments is not None
    assert parsed.investments.has_investments is False
    assert parsed.investments.holdings == []


# --------------------------------------------------------------------------- #
# 异常：**永不抛，但要说清楚是第几条**
# --------------------------------------------------------------------------- #
def test_bad_rows_report_index_and_reason():
    doc = json.dumps(
        {
            "period": PERIOD,
            "cashflow": [
                {"date": f"{PERIOD}-01", "direction": "支出", "amount": 50, "category": "餐饮"},
                {"date": f"{PERIOD}-02", "direction": "乱七八糟", "amount": 10},
                {"date": f"{PERIOD}-03", "direction": "支出", "amount": "abc"},
                {"date": f"{PERIOD}-04", "direction": "支出", "amount": -30},
            ],
        },
        ensure_ascii=False,
    )
    parsed = parse_bill(doc, period=PERIOD)
    joined = " ".join(parsed.warnings)

    assert "第 2 条流水" in joined and "无法识别" in joined
    assert "第 3 条流水" in joined and "不是有效数字" in joined
    assert "第 4 条流水" in joined and "金额为负" in joined
    # 能用的一行照常产出
    assert {c.category: c.amount_cents for c in parsed.categories} == {"餐饮": 5_000}


def test_invalid_json_degrades_with_location_and_never_raises():
    parsed = parse_bill('{ "period": "2026-09", oops }', period=PERIOD)
    assert parsed.categories == []
    assert "cashflow" in parsed.missing
    assert any("不是合法 JSON" in w for w in parsed.warnings)


def test_missing_cashflow_is_reported():
    parsed = parse_bill(json.dumps({"period": PERIOD}), period=PERIOD)
    assert "cashflow" in parsed.missing
    assert any("cashflow" in w for w in parsed.warnings)


def test_empty_and_whitespace_documents_do_not_raise():
    for text in ("", "   ", "\n"):
        parsed = parse_bill(text, period=PERIOD)
        assert parsed.warnings


# --------------------------------------------------------------------------- #
# 自动识别
# --------------------------------------------------------------------------- #
def test_looks_like_json_distinguishes_formats():
    assert looks_like_json(sample_dossier(PERIOD))
    assert looks_like_json("  [ {} ]")
    assert not looks_like_json("日期,分类,金额\n2026-09-01,餐饮,-1\n")
    assert not looks_like_json("")


def test_parse_bill_auto_detects_json_and_still_handles_csv():
    # JSON 文档会自动带上「结余去向」——CSV 永远不会有，据此判断走了哪条路
    assert parse_bill(sample_dossier(PERIOD), period=PERIOD).allocation is not None

    csv_text = "日期,分类,金额,摘要\n2026-09-01,餐饮,-88.00,聚餐\n"
    from_csv = parse_bill(csv_text, period=PERIOD)
    assert from_csv.allocation is None
    assert {c.category: c.amount_cents for c in from_csv.categories} == {"餐饮": 8_800}
