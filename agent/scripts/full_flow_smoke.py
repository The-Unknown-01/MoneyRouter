"""Five-agent integration smoke run, with isolated artifacts and bounded turns.

Run from agent/: python scripts/full_flow_smoke.py --real --output .data/smoke/run-1
Without --real, scripted models/data run the same graphs without network calls.
The output directory must be new; no existing user history/cache is consumed.
"""
from __future__ import annotations

import argparse
import os
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langchain_core.messages import AIMessage
from moneyrouter_agent.agent import ProfileAgent
from moneyrouter_agent.config import Settings, MarketDataSettings, SummarySettings
from moneyrouter_agent.domain.finance import AnalysisDraft, Reflection, SearchPlan, PlannedQuery
from moneyrouter_agent.domain.month import IncomeFact, MonthSnapshot, MonthResult
from moneyrouter_agent.domain.month_turn import MonthTurnDecision
from moneyrouter_agent.domain.plan import PlanInputs, validate_plan
from moneyrouter_agent.domain.plan_turn import PlanTurnDecision, PlanAdjustment
from moneyrouter_agent.domain.wallet import Wallet, WalletProposal
from moneyrouter_agent.domain.profile import Profile, ProfileResult
from moneyrouter_agent.domain.profile_delta import ProfileFieldUpdate
from moneyrouter_agent.domain.summary import SummaryDraft, LessonDraft, EventDraft
from moneyrouter_agent.domain.turn import TurnDecision
from moneyrouter_agent.finance_agent import FinanceAgent
from moneyrouter_agent.history import JsonMonthHistoryStore
from moneyrouter_agent.month_agent import MonthAgent
from moneyrouter_agent.plan_agent import PlanAgent
from moneyrouter_agent.prompts.plan_rules import render_plan_narrative
from moneyrouter_agent.summary_agent import SummaryAgent
from moneyrouter_agent.summary_store import JsonExperiencePackStore, JsonProfileEventStore, JsonMonthlySummaryStore
from moneyrouter_agent.tools.bocha import SearchOutcome
from moneyrouter_agent.tools.market_data import FetchOutcome
from moneyrouter_agent.tools.bill_cleaner import clean as clean_bills, documents_to_text
from moneyrouter_agent.tools.bill_classify import make_deepseek_classifier
from moneyrouter_agent.tools.bills import parse_bill


PROFILE_TEXT = (
    "我是一名程序员，税后月薪10000元且稳定，不用养家，没有任何债务也没有月供。"
    "现有应急储蓄20000元，资金三年后用，最多接受本金亏损10%，有一点基金投资经验。"
    "目标是攒首付。生活支出约3000元，购物娱乐约3000元。"
)
MONTH_TEXT = (
    "这是九月完整账单。这个月购物娱乐花了5000元，是几次聚餐和买数码产品，"
    "并非必须支出，下月愿意少花。没有漏记、债务和已有投资，结余2000元全部留作应急储蓄。"
    "九月我从公司离职转自由职业，收入本月仍10000元，但之后不稳定。"
)


def dossier(period: str, wants: int) -> str:
    return json.dumps({
        "period": period,
        "cashflow": [
            {"date": f"{period}-05", "direction": "income", "amount": 10000, "category": "工资"},
            {"date": f"{period}-06", "direction": "expense", "amount": 2000, "category": "居住"},
            {"date": f"{period}-07", "direction": "expense", "amount": 1000, "category": "餐饮"},
            {"date": f"{period}-08", "direction": "expense", "amount": wants, "category": "娱乐社交"},
        ],
        "savings": {"non_invested": 7000 - wants, "invested": 0},
        "investments": {"has_investments": False, "holdings": []},
    }, ensure_ascii=False)


class Sequence:
    def __init__(self, *items):
        self.items = list(items)

    def __call__(self, messages):
        if not self.items:
            raise AssertionError("Scripted runner exhausted")
        return self.items.pop(0)


def run(output: Path, *, real: bool = False, alipay: Path | None = None, wechat: Path | None = None) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ["MONEYROUTER_CHECKPOINT_DIR"] = str(output / "checkpoints")
    settings = Settings.from_env() if real else Settings(api_key="scripted")
    if real:
        settings.require_api_key()
    if not real:
        settings = replace(settings, timeout_s=90, max_retries=0, structured_retries=1)
    report = {"mode": "real" if real else "scripted", "stages": [], "ok": False}
    started = time.monotonic()
    last_stage_at = started

    def save_report():
        report["elapsed_s"] = round(time.monotonic() - started, 2)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def stage(name, value):
        nonlocal last_stage_at
        data = value if isinstance(value, dict) else value.model_dump(mode="json", exclude={"reasoning"})
        (output / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        entry = {"name": name, "degraded": getattr(value, "degraded", False), "error": getattr(value, "error", None)}
        now = time.monotonic()
        entry["elapsed_s"] = round(now - last_stage_at, 2)
        last_stage_at = now
        if hasattr(value, "briefing"):
            entry.update(degraded=value.briefing.degraded, error=value.briefing.error,
                         metrics=len(value.briefing.metrics), missing=len(value.briefing.missing), sources=len(value.briefing.sources))
        report["stages"].append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)
        save_report()

    def clean(value):
        assert not value.error and not value.degraded, f"Agent degraded: {value.error}"

    try:
        uploaded = None
        if alipay or wechat:
            assert real, "真实账单测试需启用 --real"
            classifier = make_deepseek_classifier(settings, thinking=False)
            uploaded = clean_bills(alipay=alipay.read_bytes() if alipay else None,
                                   wechat=wechat.read_bytes() if wechat else None,
                                   classifier=classifier, classifier_name="DeepSeek")
            assert uploaded.rows and not uploaded.warnings, uploaded.warnings
            assert not classifier.stats["reasoning_responses"]
            bills_dir = output / "uploaded-bills"
            bills_dir.mkdir()
            for period, text in documents_to_text(uploaded.documents).items():
                (bills_dir / f"{period}.json").write_text(text, encoding="utf-8")
                parsed = parse_bill(text, period=period)
                rows = [r for r in uploaded.rows if r.period == period]
                assert sum(c.amount_cents for c in parsed.categories) == sum(r.amount_cents for r in rows if r.direction == "expense")
                assert (parsed.income.amount_cents if parsed.income else 0) == sum(r.amount_cents for r in rows if r.direction == "income")
            review = [item for doc in uploaded.documents.values() for item in doc["cashflow"] if item.get("review_reason")]
            (bills_dir / "review.json").write_text(json.dumps({"items": review}, ensure_ascii=False, indent=2), encoding="utf-8")
            report["bills"] = {"rows": len(uploaded.rows), "periods": sorted(uploaded.documents),
                               "review_rows": len(review), "classifier": classifier.stats,
                               "platform_totals": uploaded.platform_totals,
                               "detail_totals": {source: uploaded.source_totals(source) for source in uploaded.rows_by_source}}
            stage("00-real-bill-cleaning", report["bills"])

        profile = Profile(occupation="程序员", income_cents=1_000_000, income_basis="税后月薪",
                          income_stable=True, family_load=False, debt_cents=0, reserve_cents=2_000_000,
                          horizon_months=36, max_loss_pct=10, experience="some", goal="攒首付")
        profile_agent = ProfileAgent(settings=settings, **({} if real else {
            "turn_runner": Sequence(TurnDecision(reply="请确认目标期限。", understanding=profile),
                                    TurnDecision(reply="已了解。", understanding=profile, ready_to_finalize=True)),
            "finalize_runner": Sequence(ProfileResult(profile=profile, articulation="稳定工资收入，三年攒首付。")),
        }))
        result = profile_agent.turn("smoke-profile", PROFILE_TEXT)
        for _ in range(5):
            clean(result)
            if result.awaiting_confirmation:
                break
            result = profile_agent.turn("smoke-profile", PROFILE_TEXT + "上述信息完整、准确，请整理画像供我核对。")
        stage("01-profile-draft", result)
        assert result.awaiting_confirmation, "Profile did not finish within six turns"
        result = profile_agent.turn("smoke-profile", resume={"action": "confirm"})
        stage("02-profile-confirmed", result)
        clean(result)
        assert result.confirmed and result.final_profile is not None
        profile = result.final_profile
        assert profile.income_cents == 1_000_000 and profile.max_loss_pct == 10

        finance_kwargs = {} if real else {
            "plan_runner": Sequence(SearchPlan(queries=[PlannedQuery(query="测试资料", topic="宏观与市场环境")])),
            "reflect_runner": Sequence(Reflection(sufficient=True)),
            "analyze_runner": Sequence(AnalysisDraft(headline="离线测试，无市场事实。")),
            "searcher": lambda queries: SearchOutcome(hits=[], errors=[]),
            "metrics_fetcher": lambda: FetchOutcome(metrics=[], gaps=[]),
        }
        finance = FinanceAgent(settings=settings, market_settings=replace(MarketDataSettings.from_env(), cache_dir=str(output / "market-cache")),
                               **finance_kwargs).briefing("为三年攒首付的个人整理金融环境，收入稳定、风险中等。", as_of="2026-10-07", period="2026-10")
        stage("03-finance", finance)
        assert not finance.briefing.error and not finance.briefing.degraded, finance.briefing.error

        history = JsonMonthHistoryStore(str(output / "months"))
        def month(period, wants, message):
            agent = MonthAgent(settings=settings, history_store=history, **({} if real else {
                "turn_runner": Sequence(MonthTurnDecision(reply="情况已了解。", snapshot=MonthSnapshot(period=period, obligations_reviewed=True),
                                                          notes=message, ready_to_finalize=True)),
                "finalize_runner": Sequence(MonthResult(articulation=message)),
            }))
            value = agent.turn(f"smoke-month-{period}", message, bill=dossier(period, wants), period=period)
            for _ in range(5):
                clean(value)
                if value.awaiting_confirmation:
                    break
                value = agent.turn(f"smoke-month-{period}", message + "全部已说明，请整理本月实况供核对。")
            stage(f"month-{period}-draft", value)
            assert value.awaiting_confirmation, "Month did not finish within six turns"
            value = agent.turn(f"smoke-month-{period}", resume={"action": "confirm"})
            stage(f"month-{period}-confirmed", value)
            clean(value)
            assert value.confirmed and value.recorded, "Month not saved"
            assert value.snapshot.balance_cents == (7000 - wants) * 100
            return history.load(period).snapshot

        previous = month("2026-08", 3000, "八月账单完整，结余4000元留作应急金，没有已有投资，没有漏记。")
        def plan(inputs, label, *, edit=False):
            def candidate(messages):
                # Explicit scripted decisions for the smoke scenario, not a production allocation rule.
                shopping = 220000 if edit else 200000
                wallets = [Wallet(id="home", name="居住", kind="expense", category="居住", amount_cents=300000,
                    reason="覆盖本月义务", execution="按时支付"),
                    Wallet(id="shopping", name="购物", kind="expense", category="购物", amount_cents=shopping,
                    reason="结合已确认经验自主安排", execution="按剩余周数核对"),
                    Wallet(id="goal", name="首付储蓄", kind="goal", amount_cents=1000000-300000-shopping,
                    reason="推进用户目标", execution="单独留存")]
                return PlanTurnDecision(status="finalize", reply="请核对方案", proposal=WalletProposal(headline="本月钱包安排", wallets=wallets))
            agent = PlanAgent(settings=settings, inputs=inputs, **({} if real else {
                "agent_runner": lambda messages: AIMessage(content="事实已核对"), "decide_runner": candidate,
            }))
            value = agent.plan(label)
            for _ in range(3):
                clean(value)
                if value.awaiting_confirmation:
                    break
                value = agent.plan(label, user_message="月收入10000元，没有月供，三年后用钱，最多亏10%，请按已提供完整账单生成方案。")
            stage(label + "-draft", value)
            clean(value)
            assert value.awaiting_confirmation and value.validation.ok, value.validation
            if edit:
                value = agent.plan(label, resume={"action": "edit", "message": "把可选支出上限改为收入的20%，其他不变。"})
                stage(label + "-edited", value)
                clean(value)
                assert value.validation.ok and value.plan.schema_version == 2
            value = agent.plan(label, resume={"action": "confirm"})
            stage(label + "-confirmed", value)
            clean(value)
            assert value.confirmed and value.validation.ok
            assert value.plan.wallets and value.plan.narrative
            return value.plan

        initial_inputs = PlanInputs(profile=profile, briefing=finance.briefing, snapshot=MonthSnapshot(period="2026-09", income=IncomeFact(amount_cents=1000000), obligations_reviewed=True), period="2026-09", debt_payment_cents=0)
        initial_plan = plan(initial_inputs, "04-september-plan", edit=True)
        current = month("2026-09", 5000, MONTH_TEXT)
        stores = dict(history_store=history, experience_store=JsonExperiencePackStore(str(output / "experience"), user="smoke"),
                      event_store=JsonProfileEventStore(str(output / "events"), user="smoke"),
                      summary_store=JsonMonthlySummaryStore(str(output / "summaries"), user="smoke"))
        summary = SummaryAgent(settings=settings, summary_settings=SummarySettings(user="smoke"), plan=initial_plan, **stores,
                               **({} if real else {"reflect_runner": Sequence(SummaryDraft(
                                   headline="可选支出超预算，下月收紧。", sections=["收入转为不稳定，先保障预备金。"],
                                   lessons=[LessonDraft(id="wants_down:2026-09", statement="减少非必要支出。")],
                                   events=[EventDraft(event_type="occupation_change", statement="本月离职转自由职业。",
                                                      field_updates=ProfileFieldUpdate(occupation="自由职业者", income_stable=False))],
                               ))}))
        value = summary.summarize("smoke-summary", period="2026-09")
        stage("05-summary-draft", value)
        clean(value)
        assert value.awaiting_confirmation
        value = summary.turn("smoke-summary", resume={"action": "confirm"})
        stage("06-summary-confirmed", value)
        clean(value)
        assert value.confirmed and value.written
        assert not value.event_warnings, value.event_warnings
        # Recreate facade to prove downstream reads saved files, not graph memory.
        reader = SummaryAgent(settings=Settings(api_key=None), **stores)
        effective = reader.effective_profile(profile, as_of_period="2026-10").merged()
        pack = reader.load_pack()
        assert pack and any(item.kind == "wants_down" for item in pack.lessons)
        assert profile.income_stable is True and effective.income_stable is False, "Profile event did not feed back"
        next_inputs = PlanInputs(profile=effective, briefing=finance.briefing, snapshot=MonthSnapshot(period="2026-10", income=IncomeFact(amount_cents=1000000), obligations_reviewed=True), history=[previous],
                                 period="2026-10", experience=pack, debt_payment_cents=0)
        next_plan = plan(next_inputs, "07-october-plan")
        assert validate_plan(next_plan, next_inputs).ok
        assert next_plan.input_facts["experience"]
        assert next_plan.budget.wants_cents < initial_plan.budget.wants_cents
        report["comparison"] = {"september_wants_cents": initial_plan.budget.wants_cents,
                                "october_wants_cents": next_plan.budget.wants_cents,
                                "october_wants_ratio_pct": next_plan.budget.wants_ratio_pct,
                                "reserve_months_before": initial_plan.reserve.months, "reserve_months_after": next_plan.reserve.months,
                                "lessons_applied": next_plan.lessons_applied, "profile_events": len(reader.profile_events()),
                                "history_periods": history.list_periods()}
        # Real uploaded cashflow uses a separate history/account from the fictional scenario.
        # Unknown transaction purposes and missing bank salary remain unknown.
        if uploaded:
            upload_history = JsonMonthHistoryStore(str(output / "uploaded-history"))
            report["uploaded_months"] = []
            for period in ("2026-08", "2026-09"):
                if period not in uploaded.documents:
                    continue
                thread = f"uploaded-month-{period}"
                upload_agent = MonthAgent(settings=settings, history_store=upload_history)
                message = ("这是本次后端验收的真实支付平台账单，文件金额和平台方向已核对。"
                           "未知分类尚待用户复核，不要猜用途；平台入账不等于工资，未提供银行工资记录。"
                           "结余去向和已有投资未知，不补造事实。仅整理本文件支持的月度实况，"
                           "保留缺失信息及待复核说明，供确认留档。")
                value = upload_agent.turn(thread, message,
                                          bill=json.dumps(uploaded.documents[period], ensure_ascii=False), period=period)
                for _ in range(5):
                    clean(value)
                    if value.awaiting_confirmation:
                        break
                    value = upload_agent.turn(thread, message + "当前无法补充其他信息，请保留未知并整理供核对。")
                stage(f"uploaded-{period}-draft", value)
                clean(value)
                assert value.awaiting_confirmation, "Uploaded month did not finish within six turns"
                value = upload_agent.turn(thread, resume={"action": "confirm"})
                stage(f"uploaded-{period}-confirmed", value)
                clean(value)
                assert value.confirmed and value.recorded
                saved = JsonMonthHistoryStore(str(output / "uploaded-history")).load(period).snapshot
                expected = parse_bill(json.dumps(uploaded.documents[period], ensure_ascii=False), period=period)
                assert saved.spend_total_cents == sum(c.amount_cents for c in expected.categories)
                assert (saved.income.amount_cents if saved.income else 0) == (expected.income.amount_cents if expected.income else 0)
                report["uploaded_months"].append({"period": period, "income_cents": saved.income.amount_cents if saved.income else 0,
                                                  "spend_cents": saved.spend_total_cents, "confirmed": True, "recorded": True})
        report["ok"] = True
    except Exception as exc:
        report["failure"] = f"{type(exc).__name__}: {exc}"
        save_report()
        raise
    finally:
        save_report()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--alipay", type=Path)
    parser.add_argument("--wechat", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.output, real=args.real, alipay=args.alipay, wechat=args.wechat), ensure_ascii=False, indent=2))
