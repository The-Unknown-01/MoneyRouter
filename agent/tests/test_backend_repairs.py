from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from moneyrouter_agent.bill_imports import BillImportStore, import_bills
from moneyrouter_agent.tools.bill_cleaner import RawRow, CleanOutcome, build_documents, clean
from moneyrouter_agent.tools.bills import parse_bill
from moneyrouter_agent.domain.month import MonthSnapshot, IncomeFact, CategorySpend, past_snapshots
from moneyrouter_agent.domain.plan import PlanInputs, PlanStrategy, build_plan, summarize_cashflow


@pytest.mark.parametrize("kind", ["profile","month","plan","summary"])
def test_confirmation_and_context_survive_process_restart(kind, tmp_path):
    env = dict(os.environ, MONEYROUTER_CHECKPOINT_DIR=str(tmp_path / "checkpoints"),
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), PYTHONIOENCODING="utf-8")
    worker = Path(__file__).with_name("persistence_worker.py")
    for phase in ("start", "resume"):
        proc = subprocess.run([sys.executable, str(worker), kind, phase, str(tmp_path)], env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert proc.returncode == 0, proc.stdout + proc.stderr


def expense(**kwargs):
    return RawRow(source="wechat", date="2026-08-10", direction="expense", amount_cents=10000,
                  category="购物", transaction_id="purchase", merchant_order_id="order", **kwargs)


def refund(amount=3000, date="2026-09-10", **kwargs):
    return RawRow(source="wechat", date=date, direction="income", amount_cents=amount,
                  kind="退款", transaction_id="refund", merchant_order_id="order", **kwargs)


def test_partial_cross_month_refund_revises_purchase_month_and_preserves_raw():
    docs = build_documents([expense(),refund()], refund_policy="net")
    assert docs["2026-08"]["cashflow"][0]["amount"] == 70
    assert docs["2026-08"]["raw_cashflow"][0]["amount"] == 100
    assert docs["2026-09"]["cashflow"][0]["direction"] == "transfer"
    assert parse_bill(json.dumps(docs["2026-09"]),period="2026-09").income is None


@pytest.mark.parametrize("amount,expected", [(10000,0),(12000,100)])
def test_full_refund_and_over_refund(amount,expected):
    doc = build_documents([expense(),refund(amount=amount)],refund_policy="net")
    assert doc["2026-08"]["cashflow"][0]["amount"] == expected
    if amount > 10000:
        assert "超过" in doc["2026-09"]["cashflow"][0]["review_reason"]


def test_unmatched_or_ambiguous_refund_never_guesses_original():
    other = replace(expense(),transaction_id="other")
    docs = build_documents([expense(),other,refund()],refund_policy="net")
    assert sum(r["amount"] for r in docs["2026-08"]["cashflow"]) == 200
    assert "唯一" in docs["2026-09"]["cashflow"][0]["review_reason"]


def test_import_replay_overlap_status_update_and_user_isolation(tmp_path):
    store = BillImportStore(tmp_path / "ledger.sqlite",user="a")
    first = CleanOutcome(rows=[expense()],coverage_ranges={"wechat":("2026-08-01","2026-08-31")})
    assert store.import_outcome(first,digest="a")["added"] == 1
    assert store.import_outcome(first,digest="a")["duplicate"] == 1
    assert store.import_outcome(CleanOutcome(rows=[expense(),refund()]),digest="b")["added"] == 1
    assert store.documents()["2026-08"]["cashflow"][0]["amount"] == 70
    assert len(store.documents()["2026-09"]["cashflow"]) == 1
    assert BillImportStore(store.path,user="b").documents() == {}
    assert store.documents()["2026-08"]["coverage"]["complete"] is True


def test_conflicting_id_rolls_back_whole_upload(tmp_path):
    store = BillImportStore(tmp_path / "ledger.sqlite",user="a")
    store.import_outcome(CleanOutcome(rows=[expense()]),digest="a")
    with pytest.raises(ValueError,match="冲突"):
        store.import_outcome(CleanOutcome(rows=[refund(),replace(expense(),amount_cents=9000)]),digest="b")
    assert list(store.documents()) == ["2026-08"]


def test_gapped_export_ranges_do_not_claim_complete_month(tmp_path):
    store = BillImportStore(tmp_path / "ledger.sqlite",user="a")
    for n,span in enumerate([("2026-08-01","2026-08-10"),("2026-08-20","2026-08-31")]):
        store.import_outcome(CleanOutcome(rows=[expense()],coverage_ranges={"wechat":span}),digest=str(n))
    assert store.documents()["2026-08"]["coverage"]["complete"] is False


def test_partial_month_metadata_reaches_snapshot_and_excludes_baselines():
    doc = build_documents([expense()], coverage_ranges={"wechat":("2026-08-10","2026-08-31")})["2026-08"]
    snap = parse_bill(json.dumps(doc),period="2026-08").as_snapshot()
    assert snap.coverage_complete is False
    assert past_snapshots([snap],"2026-09") == []
    assert summarize_cashflow(PlanInputs(snapshot=snap)).months_covered == 0


def test_strategy_override_does_not_claim_user_request():
    plan = build_plan(PlanInputs(), PlanStrategy(reserve_months_override=6))
    assert "用户指定" not in plan.reserve.basis and "方案策略" in plan.reserve.basis


def test_authoritative_full_refund_status_zeroes_original_without_guessing_link():
    docs = build_documents([expense(status="已全额退款")],refund_policy="net")
    assert docs["2026-08"]["cashflow"][0]["amount"] == 0
    assert docs["2026-08"]["cashflow"][0]["original_amount"] == 100


def test_reimport_preserves_manual_category_and_allows_status_update(tmp_path):
    store = BillImportStore(tmp_path / "ledger.sqlite",user="a")
    store.import_outcome(CleanOutcome(rows=[expense(category_source="user")]),digest="a")
    store.import_outcome(CleanOutcome(rows=[replace(expense(),category="其他",category_source="unknown",status="已全额退款")]),digest="b")
    row = store.documents()["2026-08"]["cashflow"][0]
    assert row["category"] == "购物" and row["category_source"] == "user" and row["amount"] == 0


def test_replay_preserves_model_category_provenance(tmp_path):
    store = BillImportStore(tmp_path / "ledger.sqlite", user="a")
    store.import_outcome(CleanOutcome(rows=[expense(category_source="model")]), digest="a")
    store.import_outcome(CleanOutcome(rows=[replace(expense(), category="其他", category_source="unknown")]), digest="b")
    row = store.documents()["2026-08"]["cashflow"][0]
    assert row["category"] == "购物" and row["category_source"] == "model"


def test_no_id_repeat_file_and_alipay_encoding_identity(tmp_path):
    from test_bill_cleaner import ALIPAY_SAMPLE
    sample = ALIPAY_SAMPLE.replace("2026090123001", "").replace("2026090223001", "")
    store = BillImportStore(tmp_path / "ledger.sqlite",user="a")
    for _ in range(2):
        import_bills(store, alipay=sample.encode("gbk"),refund_policy="keep")
    assert len(store.documents(refund_policy="keep")["2026-09"]["cashflow"]) == 4


def test_replacing_bill_in_same_month_replaces_file_facts():
    from moneyrouter_agent.month_agent import MonthAgent
    from moneyrouter_agent.config import Settings, MonthSettings
    agent = MonthAgent(settings=Settings(api_key=None, key_file=None),
                       month_settings=MonthSettings(), history_store=None)
    first = agent.turn("replace", bill="日期,分类,金额,摘要\n2026-08-01,餐饮,-100,午饭\n2026-08-02,工资,1000,工资\n", period="2026-08")
    assert first.snapshot.income.amount_cents == 100000
    second = agent.turn("replace", bill="日期,分类,金额,摘要\n2026-08-01,购物,-200,衣服\n", period="2026-08")
    assert second.snapshot.income is None
    assert [c.category for c in second.snapshot.categories] == ["购物"]
    assert second.snapshot.categories[0].amount_cents == 20000
    rejected = agent.turn("replace", bill="", period="2026-09")
    assert "不同月份" in rejected.error
    assert rejected.snapshot.period == "2026-08"
