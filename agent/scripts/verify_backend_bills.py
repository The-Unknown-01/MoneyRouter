"""Local-only acceptance of real uploads, net refunds, replay and coverage."""
import argparse
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from moneyrouter_agent.bill_imports import BillImportStore, import_bills
from moneyrouter_agent.tools.bills import parse_bill

parser = argparse.ArgumentParser()
parser.add_argument("--alipay", type=Path, required=True)
parser.add_argument("--wechat", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True,exist_ok=False)
store = BillImportStore(args.output / "ledger.sqlite",user="acceptance")
kwargs = {"alipay":args.alipay.read_bytes(),"wechat":args.wechat.read_bytes(),"refund_policy":"net"}
outcome, documents, first = import_bills(store, **kwargs)
assert not outcome.warnings
_, repeated, second = import_bills(store, **kwargs)
assert documents == repeated and first["added"] == 362 and second["added"] == 0
assert BillImportStore(store.path,user="another-user").documents() == {}
assert {m:d["coverage"]["complete"] for m,d in documents.items()} == {
    "2026-07":False,"2026-08":True,"2026-09":True,"2026-10":False}
assert sum(r["amount"] for d in documents.values() for r in d["raw_cashflow"] if r["direction"]=="expense") > sum(
    r["amount"] for d in documents.values() for r in d["cashflow"] if r["direction"]=="expense")
report = {"ok":True,"first_import":first,"repeat_import":second,"refund_policy":"net","months":{}}
for month,doc in documents.items():
    (args.output / f"{month}.json").write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding="utf-8")
    parsed = parse_bill(json.dumps(doc,ensure_ascii=False),period=month)
    expected_income = sum(round(r["amount"] * 100) for r in doc["cashflow"] if r["direction"]=="income")
    expected_spend = sum(round(r["amount"] * 100) for r in doc["cashflow"] if r["direction"]=="expense")
    assert (parsed.income.amount_cents if parsed.income else 0) == expected_income
    assert sum(c.amount_cents for c in parsed.categories) == expected_spend
    assert parsed.as_snapshot().coverage_complete == doc["coverage"]["complete"]
    report["months"][month] = {"rows":len(doc["cashflow"]),"income_cents":expected_income,"net_spend_cents":expected_spend,
                              "coverage_complete":doc["coverage"]["complete"],"review_required_count":parsed.review_required_count}
report["unmatched_refunds"] = sum("退款缺少唯一原单" in r.get("review_reason","") for d in documents.values() for r in d["cashflow"])
(args.output / "report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(report,ensure_ascii=False))
