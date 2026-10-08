"""本月实况图：离线降级、假模型全流程、关注点不重复问、代码字段不可篡改。"""

from __future__ import annotations

from typing import Any

import pytest

from moneyrouter_agent.config import MonthSettings, Settings
from moneyrouter_agent.domain.month import (
    CategorySpend,
    FundAllocation,
    GoalAlignment,
    HoldingReturn,
    IncomeFact,
    InvestmentSnapshot,
    MonthResult,
    MonthSnapshot,
)
from moneyrouter_agent.domain.month_turn import MonthTurnDecision, ProbeAnswer
from moneyrouter_agent.month_agent import MonthAgent
from moneyrouter_agent.model.deepseek import StructuredCall
from moneyrouter_agent.tools.dossier import sample_dossier

PERIOD = "2026-09"
BILL = "日期,分类,金额,摘要\n2026-09-01,餐饮,-2500.50,聚餐\n2026-09-05,工资,8000.00,9月工资\n"
DOSSIER = sample_dossier(PERIOD)


def test_bill_details_and_selected_period_reach_model_on_every_turn():
    runner = ScriptedTurnRunner([MonthTurnDecision(reply="预计收入多少？"),
                                 MonthTurnDecision(reply="还有待支付费用吗？")])
    agent = MonthAgent(settings=Settings(api_key="fake-key"),
                       month_settings=MonthSettings(), turn_runner=runner,
                       finalize_runner=ScriptedFinalizeRunner(MonthResult()),
                       history_store=None)
    agent.turn("bill-context", bill=BILL, period=PERIOD)
    agent.turn("bill-context", "预计收入八千")
    for messages in runner.calls:
        context = str(messages[1].content)
        assert PERIOD in context
        assert "2026-09-01" in context
        assert "聚餐" in context
        assert "250050" in context



class ScriptedTurnRunner:
    def __init__(self, decisions: list[MonthTurnDecision]) -> None:
        self._decisions = list(decisions)
        self.calls: list[list[Any]] = []

    def __call__(self, messages: list[Any]) -> MonthTurnDecision:
        self.calls.append(messages)
        if not self._decisions:
            raise AssertionError("脚本化 runner 已无更多决策，但图又调用了一次")
        value = self._decisions.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class ScriptedFinalizeRunner:
    def __init__(self, result: MonthResult | Exception) -> None:
        self._result = result

    def __call__(self, messages: list[Any]) -> MonthResult:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _agent(decisions: list[MonthTurnDecision], finalize: Any = None, **kwargs) -> MonthAgent:
    return MonthAgent(
        settings=Settings(api_key="fake-key"),
        month_settings=MonthSettings(),
        turn_runner=ScriptedTurnRunner(decisions),
        finalize_runner=finalize if isinstance(finalize, ScriptedFinalizeRunner) else ScriptedFinalizeRunner(finalize or MonthResult()),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 离线降级
# --------------------------------------------------------------------------- #
def test_offline_degrades_to_rule_guide_and_never_finalizes():
    agent = MonthAgent(settings=Settings(api_key=None, key_file=None), month_settings=MonthSettings())
    result = agent.turn("off-income", "你好")

    assert result.degraded is True
    assert result.error is not None
    assert result.ready_to_finalize is False
    assert result.awaiting_confirmation is False
    assert result.reply  # 规则引导文案

    for index, text in enumerate(["收入大概八千", "主要花在吃饭上"], start=2):
        result = agent.turn("off-income", text)
        assert result.degraded is True
        assert result.turn_count == index
        assert result.ready_to_finalize is False


# --------------------------------------------------------------------------- #
# 假模型全流程
# --------------------------------------------------------------------------- #
def test_happy_path_ingest_to_confirm():
    runner_decisions = [
        MonthTurnDecision(reply="说说这个月收入？", snapshot=MonthSnapshot(period=PERIOD)),
        MonthTurnDecision(
            reply="这个月的情况我了解得差不多了。",
            snapshot=MonthSnapshot(period=PERIOD, income=IncomeFact(amount_cents=800_000)),
            ready_to_finalize=True,
        ),
    ]
    agent = _agent(
        runner_decisions,
        MonthResult(snapshot=MonthSnapshot(), articulation="用户本月收入约 8000 元。"),
    )
    agent.turn("happy", "开始", bill=BILL, period=PERIOD)
    result = agent.turn("happy", "大概八千")

    assert result.awaiting_confirmation is True
    assert result.confirmation_payload is not None
    assert result.confirmation_payload["options"] == ["confirm", "edit", "more"]
    assert result.final_result is not None
    assert result.snapshot.spend_total_cents == 250_050

    done = agent.turn("happy", resume={"action": "confirm"})
    assert done.confirmed is True
    assert done.awaiting_confirmation is False


def test_edit_branch_returns_to_converse():
    agent = _agent(
        [
            MonthTurnDecision(reply="整理一下", ready_to_finalize=True),
            MonthTurnDecision(reply="好的，我改一下。", ready_to_finalize=False),
        ],
        MonthResult(articulation="x"),
    )
    agent.turn("edit", "开始")
    result = agent.turn("edit", resume={"action": "edit", "message": "餐饮其实没那么多"})
    assert result.confirmed is False
    assert result.ready_to_finalize is False


# --------------------------------------------------------------------------- #
# 关注点：answered 之后不再注入
# --------------------------------------------------------------------------- #
def test_answered_probe_is_not_injected_again():
    runner = ScriptedTurnRunner(
        [
            MonthTurnDecision(reply="a", snapshot=MonthSnapshot(period=PERIOD)),
            MonthTurnDecision(
                reply="b",
                snapshot=MonthSnapshot(period=PERIOD),
                probe_answers=[ProbeAnswer(probe_id="over_budget:餐饮", answer="几次聚餐")],
            ),
            MonthTurnDecision(reply="c", snapshot=MonthSnapshot(period=PERIOD)),
        ]
    )
    agent = MonthAgent(
        settings=Settings(api_key="fake-key"),
        month_settings=MonthSettings(),
        turn_runner=runner,
        finalize_runner=ScriptedFinalizeRunner(MonthResult()),
        budget={"餐饮": 100_000},
    )
    agent.turn("probe", "开始", bill=BILL, period=PERIOD)  # 餐饮 2500 > 预算 1000
    first_context = " ".join(str(m.content) for m in runner.calls[0])
    assert "over_budget:餐饮" in first_context

    agent.turn("probe", "餐饮偶尔聚餐")  # 归因
    agent.turn("probe", "还有别的")  # 第三轮
    third_context = " ".join(str(m.content) for m in runner.calls[2])
    assert "over_budget:餐饮" not in third_context


# --------------------------------------------------------------------------- #
# 代码字段不可被模型篡改
# --------------------------------------------------------------------------- #
def test_file_category_cannot_be_rewritten_by_model():
    agent = _agent(
        [
            MonthTurnDecision(reply="a", snapshot=MonthSnapshot(period=PERIOD)),
            MonthTurnDecision(
                reply="b",
                snapshot=MonthSnapshot(
                    period=PERIOD,
                    categories=[CategorySpend(category="餐饮", amount_cents=1, source="stated")],
                ),
            ),
        ]
    )
    agent.turn("tamper", "开始", bill=BILL, period=PERIOD)
    result = agent.turn("tamper", "随便说点什么")

    by_cat = {c.category: c.amount_cents for c in result.snapshot.categories}
    assert by_cat["餐饮"] == 250_050  # 账单值，未被模型改成 1 分


# --------------------------------------------------------------------------- #
# 无账单纯对话：缺项转关注点
# --------------------------------------------------------------------------- #
def test_pure_dialogue_surfaces_missing_income_probe():
    agent = _agent([MonthTurnDecision(reply="这个月收入大概多少？", snapshot=MonthSnapshot(period=PERIOD))])
    result = agent.turn("nobill", "你好", period=PERIOD)
    assert any(p.id == "missing_field:income" for p in result.probes)


# --------------------------------------------------------------------------- #
# 结余去向：非投资部分也要采集
# --------------------------------------------------------------------------- #
def test_dialogue_collects_fund_allocation_and_flags_mismatch():
    agent = _agent(
        [
            MonthTurnDecision(
                reply="剩下的钱是怎么安排的？",
                snapshot=MonthSnapshot(
                    period=PERIOD,
                    income=IncomeFact(amount_cents=500_000),
                    categories=[CategorySpend(category="餐饮", amount_cents=100_000)],
                    allocation=FundAllocation(non_invested_cents=200_000, invested_cents=100_000),
                ),
            )
        ]
    )
    result = agent.turn("alloc", "一部分留着备用，一部分买了基金")

    assert result.snapshot.allocation.non_invested_cents == 200_000
    assert result.snapshot.allocation.invested_cents == 100_000
    assert result.snapshot.allocation.unallocated_cents == 100_000  # 400000-200000-100000
    assert any(p.id == "inconsistent:allocation" for p in result.probes)


# --------------------------------------------------------------------------- #
# 已有投资的收益：采集 + 派生合计 + 亏损转为关注点
# --------------------------------------------------------------------------- #
def test_dialogue_collects_investment_returns():
    agent = _agent(
        [
            MonthTurnDecision(
                reply="以前投的那些这个月收益怎么样？",
                snapshot=MonthSnapshot(
                    period=PERIOD,
                    income=IncomeFact(amount_cents=800_000),
                    investments=InvestmentSnapshot(
                        has_investments=True,
                        holdings=[
                            HoldingReturn(
                                name="沪深300指数基金",
                                kind="基金",
                                cost_cents=100_000,
                                market_value_cents=110_000,
                                month_return_cents=5_000,
                            )
                        ],
                    ),
                ),
            )
        ]
    )
    result = agent.turn("invest", "以前买的基金，这个月赚了一点")

    inv = result.snapshot.investments
    assert inv.has_investments is True
    assert inv.month_return_cents == 5_000
    assert inv.total_return_cents == 10_000  # 由市值−成本推出
    assert result.snapshot.net_worth_change_cents == result.snapshot.balance_cents + 5_000
    assert not any(p.id == "missing_field:investments" for p in result.probes)


def test_investment_loss_surfaces_probe_then_gets_answered():
    loss = MonthSnapshot(
        period=PERIOD,
        income=IncomeFact(amount_cents=800_000),
        investments=InvestmentSnapshot(
            has_investments=True,
            holdings=[HoldingReturn(name="某股票基金", month_return_cents=-200_000)],
        ),
    )
    agent = _agent(
        [
            MonthTurnDecision(reply="这只基金这个月亏了不少，是什么情况？", snapshot=loss),
            MonthTurnDecision(
                reply="了解了。",
                snapshot=loss,
                probe_answers=[ProbeAnswer(probe_id="investment_loss:某股票基金", answer="市场回调")],
            ),
        ]
    )
    first = agent.turn("loss", "以前买过一点基金")
    probe = next(p for p in first.probes if p.id == "investment_loss:某股票基金")
    assert probe.status == "open"

    second = agent.turn("loss", "最近行情不好，跌了一些")
    answered = next(p for p in second.probes if p.id == "investment_loss:某股票基金")
    assert answered.status == "answered"
    assert answered.answer == "市场回调"


def test_explicit_no_investment_is_accepted_without_nagging():
    agent = _agent(
        [
            MonthTurnDecision(
                reply="好的。",
                snapshot=MonthSnapshot(
                    period=PERIOD,
                    income=IncomeFact(amount_cents=800_000),
                    investments=InvestmentSnapshot(has_investments=False),
                ),
            )
        ]
    )
    result = agent.turn("none", "我没做过投资")
    assert result.snapshot.investments.has_investments is False
    assert "investments" not in result.snapshot.unsettled
    assert not any(p.kind == "missing_field" and p.related.get("field") == "investments" for p in result.probes)


# --------------------------------------------------------------------------- #
# 规范文档驱动：账单已经给了的，不再重复问
# --------------------------------------------------------------------------- #
def test_dossier_supplies_allocation_and_investments_without_asking():
    agent = _agent(
        [
            MonthTurnDecision(
                reply="这个月有一笔较大的数码支出，方便说说是什么情况吗？",
                snapshot=MonthSnapshot(period=PERIOD),
            )
        ]
    )
    result = agent.turn("dossier", "开始", bill=DOSSIER, period=PERIOD)
    snap = result.snapshot

    # 文件来源的结余去向与投资收益直接落进快照
    assert snap.allocation.source == "file"
    assert snap.allocation.non_invested_cents == 324_950
    assert snap.investments.source == "file"
    assert snap.investments.month_return_cents == 8_000
    assert snap.balance_cents == 324_950
    assert snap.net_worth_change_cents == 332_950  # 结余 + 本月投资盈亏

    # 只留下"账单解释不了"的那一条；缺项类关注点不该再出现
    assert [p.id for p in result.probes] == ["one_off_large:换手机"]
    # 「不计收支」是正常记账，不该当成告警
    assert not any("不计收支" in w for w in result.parse_warnings)


def test_stated_cannot_override_file_allocation_or_investments():
    agent = _agent(
        [
            MonthTurnDecision(reply="a", snapshot=MonthSnapshot(period=PERIOD)),
            MonthTurnDecision(
                reply="b",
                snapshot=MonthSnapshot(
                    period=PERIOD,
                    allocation=FundAllocation(non_invested_cents=1, invested_cents=1),
                    investments=InvestmentSnapshot(has_investments=False),
                ),
            ),
        ]
    )
    agent.turn("lock", "开始", bill=DOSSIER, period=PERIOD)
    result = agent.turn("lock", "这个月没投资，钱都花掉了")

    assert result.snapshot.allocation.non_invested_cents == 324_950  # 文件值未被口述改掉
    assert result.snapshot.investments.has_investments is True


# --------------------------------------------------------------------------- #
# 目标达成：注入后由代码算
# --------------------------------------------------------------------------- #
def test_goal_alignment_is_computed_from_injection():
    goal = GoalAlignment(
        goal="攒首付", target_cents=1_000_000, saved_to_date_cents=200_000, planned_this_month_cents=100_000
    )
    agent = _agent(
        [MonthTurnDecision(reply="a", snapshot=MonthSnapshot(period=PERIOD))],
        goal=goal,
    )
    result = agent.turn("goal", "开始", bill=BILL, period=PERIOD)
    ga = result.snapshot.goal_alignment

    assert ga.saved_this_month_cents == 549_950  # 800000 - 250050
    assert ga.progress_pct == 20.0
    assert ga.on_track is None  # This CSV has no confirmed full-month coverage.
    assert ga.projected_months == 2  # (100万-20万)/54.995万 -> 向上取整 2


# --------------------------------------------------------------------------- #
# 降级与参数
# --------------------------------------------------------------------------- #
def test_runner_failure_degrades_to_fallback():
    agent = _agent([RuntimeError("模型炸了")])
    result = agent.turn("boom", "你好")
    assert result.degraded is True
    assert result.error is not None
    assert result.reply


def test_reasoning_is_captured_and_not_written_back():
    class ReasoningRunner:
        def __init__(self) -> None:
            self.calls: list[list[Any]] = []

        def __call__(self, messages: list[Any]) -> StructuredCall:
            self.calls.append(messages)
            return StructuredCall(
                parsed=MonthTurnDecision(reply="a", snapshot=MonthSnapshot(period=PERIOD)),
                reasoning="仅供观测的思维链",
            )

    runner = ReasoningRunner()
    agent = MonthAgent(
        settings=Settings(api_key="fake-key"),
        month_settings=MonthSettings(),
        turn_runner=runner,
        finalize_runner=ScriptedFinalizeRunner(MonthResult()),
    )
    first = agent.turn("reason", "第一句")
    assert first.reasoning == "仅供观测的思维链"
    agent.turn("reason", "第二句")
    for payload in runner.calls:
        assert all("仅供观测的思维链" not in str(m.content) for m in payload)


def test_empty_thread_id_rejected():
    agent = _agent([MonthTurnDecision(reply="hi")])
    with pytest.raises(ValueError):
        agent.turn("", "你好")
