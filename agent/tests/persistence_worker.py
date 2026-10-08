"""Executed by a subprocess regression, not by pytest collection."""
import json
from pathlib import Path
import sys
from langchain_core.messages import AIMessage
from moneyrouter_agent.agent import ProfileAgent
from moneyrouter_agent.month_agent import MonthAgent
from moneyrouter_agent.plan_agent import PlanAgent
from moneyrouter_agent.summary_agent import SummaryAgent
from moneyrouter_agent.config import Settings, SummarySettings
from moneyrouter_agent.domain.profile import Profile, ProfileResult
from moneyrouter_agent.domain.turn import TurnDecision
from moneyrouter_agent.domain.month import MonthSnapshot, MonthResult, IncomeFact, CategorySpend
from moneyrouter_agent.domain.month_turn import MonthTurnDecision
from moneyrouter_agent.domain.plan import PlanInputs, build_plan
from moneyrouter_agent.domain.plan_turn import PlanTurnDecision, PlanAdjustment
from moneyrouter_agent.domain.wallet import Wallet, WalletProposal
from moneyrouter_agent.domain.summary import SummaryDraft
from moneyrouter_agent.history import JsonMonthHistoryStore, MonthRecord

kind, phase, root = sys.argv[1:]
root = Path(root)
settings = Settings(api_key="scripted")
profile = Profile(income_cents=1000000, income_stable=True, debt_cents=0, reserve_cents=2000000,
                  max_loss_pct=10, horizon_months=36, experience="some", family_load=False,
                  occupation="程序员",income_basis="工资",outcome_cents=800000,feature="独立生活",goal="攒钱")
snap = MonthSnapshot(period="2026-09", obligations_reviewed=True, income=IncomeFact(amount_cents=1000000),
                     categories=[CategorySpend(category="居住",amount_cents=300000), CategorySpend(category="购物",amount_cents=500000)])
inputs = PlanInputs(profile=profile, snapshot=snap, period="2026-09", debt_payment_cents=0)
history = JsonMonthHistoryStore(str(root / "months"))
if kind == "profile":
    agent = ProfileAgent(settings=settings, turn_runner=lambda _: TurnDecision(reply="核对", understanding=profile, ready_to_finalize=True),
                         finalize_runner=lambda _: ProfileResult(profile=profile, articulation="测试画像"))
    result = agent.turn("persist", "资料完整") if phase == "start" else agent.turn("persist", resume={"action":"confirm"})
elif kind == "month":
    agent = MonthAgent(settings=settings, history_store=history, budget={"购物":100000} if phase == "start" else None,
                       turn_runner=lambda _: MonthTurnDecision(reply="核对", ready_to_finalize=True),
                       finalize_runner=lambda _: MonthResult(articulation="测试实况"))
    bill = json.dumps({"period":"2026-09","cashflow":[{"date":"2026-09-05","direction":"income","category":"收入","amount":10000},
        {"date":"2026-09-06","direction":"expense","category":"购物","amount":5000}], "coverage":{"complete":False,"start":"2026-09-05","end":"2026-09-06"}})
    result = agent.turn("persist", "资料完整", bill=bill, period="2026-09") if phase == "start" else agent.turn("persist", resume={"action":"confirm"})
    if phase != "start":
        assert agent.code.budget == {"购物":100000}
        assert result.recorded and history.load("2026-09").snapshot.coverage_complete is False
elif kind == "plan":
    agent = PlanAgent(settings=settings, inputs=inputs if phase == "start" else None,
        agent_runner=lambda _: AIMessage(content="足够"), decide_runner=lambda _: PlanTurnDecision(status="finalize",reply="核对", proposal=WalletProposal(headline="钱包安排",wallets=[
            Wallet(id="home",name="居住",kind="expense",category="居住",amount_cents=300000,reason="覆盖实际",execution="已支付"),
            Wallet(id="shop",name="购物",kind="expense",category="购物",amount_cents=500000,reason="覆盖实际",execution="已支付"),
            Wallet(id="goal",name="储蓄",kind="goal",amount_cents=200000,reason="目标需要",execution="留存")])),
        adjust_runner=lambda _: PlanAdjustment(wants_ratio_pct=20))
    if phase == "start":
        result = agent.plan("persist")
    else:
        result = agent.plan("persist", resume={"action":"edit","message":"可选支出改为20%"})
        assert result.awaiting_confirmation and result.validation.ok and result.plan.budget.wants_cents == 500000, result
        assert agent.context.inputs.profile.income_cents == 1000000
        result = agent.plan("persist", resume={"action":"confirm"})
else:
    if phase == "start":
        history.save(MonthRecord(period="2026-09", snapshot=snap))
    agent = SummaryAgent(settings=settings, summary_settings=SummarySettings(user="persist", experience_dir=str(root / "experience"),
        event_dir=str(root / "events"), summary_dir=str(root / "summaries")), history_store=history,
        reflect_runner=lambda _: SummaryDraft(headline="测试复盘",sections=["说明"]))
    if phase == "start":
        result = agent.summarize("persist",period="2026-09",plan=build_plan(inputs))
    else:
        result = agent.turn("persist",resume={"action":"edit","message":"修改说明"})
        assert result.awaiting_confirmation and not result.error, result
        assert agent.code.record.period == "2026-09" and agent.code.plan is not None
        result = agent.turn("persist",resume={"action":"confirm"})
        assert result.written, result
assert not result.error and not result.degraded, result
assert result.awaiting_confirmation if phase == "start" else result.confirmed
print(json.dumps({"kind":kind,"phase":phase,"ok":True}))
