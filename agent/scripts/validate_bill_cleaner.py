"""真实文件验收：不打印个人明细，独立核对输入、产出和下游统计。"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from moneyrouter_agent.config import Settings
from moneyrouter_agent.tools.bill_classify import make_deepseek_classifier
from moneyrouter_agent.tools.bill_cleaner import clean, documents_to_text
from moneyrouter_agent.tools.bills import parse_bill


def input_totals(rows):
    """独立读取原始列，不调用清洗器的金额及方向转换函数。"""
    header_pos = next(i for i, row in enumerate(rows) if "交易时间" in row and "收/支" in row)
    header = rows[header_pos]
    direction_col = header.index("收/支")
    amount_col = header.index("金额") if "金额" in header else header.index("金额(元)")
    totals = {}
    labels = {"收入": "income", "支出": "expense", "不计收支": "transfer", "/": "transfer"}
    for row in rows[header_pos + 1:]:
        if len(row) <= max(direction_col, amount_col):
            continue
        direction = labels.get(str(row[direction_col]).strip())
        if direction is None:
            continue
        value = str(row[amount_col]).strip().replace("¥", "").replace("￥", "").replace(",", "")
        cents = int(Decimal(value) * 100)
        count, amount = totals.get(direction, (0, 0))
        totals[direction] = (count + 1, amount + cents)
    return totals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--alipay", required=True)
    parser.add_argument("--wechat", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    alipay = Path(args.alipay).read_bytes()
    wechat = Path(args.wechat).read_bytes()
    import openpyxl
    book = openpyxl.load_workbook(io.BytesIO(wechat), read_only=True, data_only=True)
    try:
        wx_rows = list(book.active.values)
    finally:
        book.close()
    for encoding in ("utf-8-sig", "gbk"):
        try:
            ali_text = alipay.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    expected = {"alipay": input_totals(list(csv.reader(io.StringIO(ali_text)))),
                "wechat": input_totals(wx_rows)}
    baseline = clean(alipay=alipay, wechat=wechat)
    classifier = make_deepseek_classifier(Settings.from_env(), thinking=False) if args.real else None
    outcome = clean(alipay=alipay, wechat=wechat, classifier=classifier,
                    classifier_name="DeepSeek" if classifier else "keyword")
    assert not outcome.warnings, f"存在 {len(outcome.warnings)} 条告警，请检查输出"
    assert all(outcome.source_totals(source) == totals for source, totals in expected.items())
    assert [(r.date, r.direction, r.amount_cents, r.transaction_id) for r in baseline.rows] == [
        (r.date, r.direction, r.amount_cents, r.transaction_id) for r in outcome.rows]
    monthly = {}
    for period, document in outcome.documents.items():
        parsed = parse_bill(json.dumps(document, ensure_ascii=False), period=period)
        rows = [r for r in outcome.rows if r.period == period]
        income = sum(r.amount_cents for r in rows if r.direction == "income")
        spend = sum(r.amount_cents for r in rows if r.direction == "expense")
        assert (parsed.income.amount_cents if parsed.income else 0) == income
        assert sum(c.amount_cents for c in parsed.categories) == spend
        monthly[period] = {"rows": len(rows), "income_cents": income, "spend_cents": spend}
    transfer_rows = [r for r in outcome.rows if r.direction == "expense" and
                     any(w in r.kind for w in ("转账", "红包", "群收款", "二维码")) and not r.product]
    assert all(r.category == "其他" and r.review_reason for r in transfer_rows)
    stats = getattr(classifier, "stats", {})
    assert not stats.get("reasoning_responses")
    report = {"ok": True, "mode": "real" if args.real else "offline", "source_totals": expected,
              "platform_totals": outcome.platform_totals, "monthly": monthly,
              "categories_count": dict(Counter(r.category for r in outcome.rows if r.direction == "expense")),
              "unclear_transfers": len(transfer_rows), "review_rows": sum(bool(r.review_reason or r.refund_like) for r in outcome.rows),
              "classifier": stats, "reused_rows": outcome.classify.reused,
              "warnings": outcome.warnings}
    for period, text in documents_to_text(outcome.documents).items():
        (output / f"{period}.json").write_text(text, encoding="utf-8")
    review = [item for doc in outcome.documents.values() for item in doc["cashflow"] if item.get("review_reason")]
    (output / "review.json").write_text(json.dumps({"items": review}, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
