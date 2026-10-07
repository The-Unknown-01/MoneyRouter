"""总结图：离线降级、假模型全流程、confirm/edit 分支、模型无写数字通路、三产物落盘。"""

from __future__ import annotations

import json
from typing import Any

from moneyrouter_agent.config import Settings, SummarySettings
from moneyrouter_agent.domain.experience import ExperiencePack, Lesson
from moneyrouter_agent.domain.month import (
    CategorySpend,
    FundAllocation,
    IncomeFact,
    MonthSnapshot,
    recompute,
)
from moneyrouter_agent.domain.plan import (
    AllocationProposal,
    AllocationSlice,
    BudgetBaseline,
    Plan,
    ReservePlan,
)
from moneyrouter_agent.domain.probe import Probe
from moneyrouter_agent.domain.profile_delta import EventEffect, ProfileFieldUpdate
from moneyrouter_agent.domain.summary import (
    EventDraft,
    LessonDraft,
    SummaryDraft,
    build_diff,
)
from moneyrouter_agent.history import InMemoryMonthHistoryStore, MonthRecord
from moneyrouter_agent.model.deepseek import StructuredCall
from moneyrouter_agent.summary_agent import SummaryAgent
from moneyrouter_agent.summary_store import (
    InMemoryExperiencePackStore,
    InMemoryMonthlySummaryStore,
    InMemoryProfileEventStore,
)

PERIOD = "2026-09"
NEXT = "2026-10"


def _record(period: str = PERIOD, *, note: str = "朋友结婚随了礼") -> MonthRecord:
    snapshot = recompute(
        MonthSnapshot(
            period=period,
            income=IncomeFact(amount_cents=1_000_000),
            categories=[
                CategorySpend(category="餐饮", amount_cents=300_000),
                CategorySpend(category="娱乐社交", amount_cents=500_000),
            ],
            allocation=FundAllocation(non_invested_cents=100_000, invested_cents=100_000),
        )
    )
    return MonthRecord(
        period=period,
        snapshot=snapshot,
        articulation=f"{period}：收入一万，可选支出偏高。",
        probes=[
            Probe(
                id="over_last_month:娱乐社交",
                kind="over_last_month",
                topic="娱乐社交比上月多",
                detail="本月明显偏高",
                status="answered",
                answer=note,
            )
        ],
    )


def _plan(period: str = PERIOD) -> Plan:
    return Plan(
        period=period,
        budget=BudgetBaseline(
            income_cents=1_000_000,
            necessary_cents=300_000,
            wants_cents=400_000,
            savings_cents=300_000,
        ),
        reserve=ReservePlan(
            target_cents=1_800_000, existing_cents=900_000, gap_cents=900_000, investable_cents=100_000
        ),
        allocation=AllocationProposal(
            investable_cents=100_000,
            recommended=[AllocationSlice(category="增长配置", amount_cents=40_000)],
        ),
    )


class ScriptedReflectRunner:
    def __init__(self, drafts: list[Any]) -> None:
        self._drafts = list(drafts)
        self.calls: list[list[Any]] = []

    def __call__(self, messages: list[Any]) -> Any:
        self.calls.append(messages)
        if not self._drafts:
            raise AssertionError("脚本化 runner 已无更多草稿，但图又调用了一次")
        value = self._drafts.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _stores():
    return (
        InMemoryExperiencePackStore(),
        InMemoryProfileEventStore(),
        InMemoryMonthlySummaryStore(),
    )


def _agent(
    drafts: list[Any],
    *,
    history: InMemoryMonthHistoryStore | None = None,
    stores=None,
    settings: Settings | None = None,
    **kwargs,
) -> tuple[SummaryAgent, Any, Any, Any]:
    experience, events, summaries = stores or _stores()
    store = history if history is not None else InMemoryMonthHistoryStore()
    if store.list_periods() == []:
        store.save(_record())
    agent = SummaryAgent(
        settings=settings or Settings(api_key="fake-key"),
        summary_settings=SummarySettings(),
        reflect_runner=ScriptedReflectRunner(drafts),
        history_store=store,
        experience_store=experience,
        event_store=events,
        summary_store=summaries,
        plan=_plan(),
        **kwargs,
    )
    return agent, experience, events, summaries


# --------------------------------------------------------------------------- #
# 离线降级
# --------------------------------------------------------------------------- #
def test_offline_degrades_to_rule_pack_and_still_confirms():
    agent, experience, events, summaries = _agent(
        [], settings=Settings(api_key=None, key_file=None)
    )

    result = agent.summarize("s-off", period=PERIOD)

    assert result.degraded is True
    assert result.error is not None
    assert result.awaiting_confirmation is True
    assert result.summary.headline  # 规则文案
    assert result.summary.sections  # 由差异表生成
    assert "生成详细复盘" in result.summary.notes
    assert any("计划可选支出" in item for item in result.pack.lessons[0].evidence)

    done = agent.turn("s-off", resume={"action": "confirm"})
    assert done.confirmed is True and done.written is True
    assert summaries.list_periods() == [PERIOD]


# --------------------------------------------------------------------------- #
# 全流程
# --------------------------------------------------------------------------- #
def test_happy_path_produces_three_artifacts():
    draft = SummaryDraft(
        headline="本月可选支出高于计划。",
        sections=["可选支出高出计划 1000 元。"],
        lessons=[LessonDraft(id=f"wants_down:{PERIOD}", statement="出现计划外的礼金支出，下月宜留出余量。")],
        events=[
            EventDraft(
                event_type="life_event",
                statement="用户本月因朋友结婚产生了计划外礼金支出。",
                effects=[
                    EventEffect(
                        kind="category_cap", target="人情往来", direction="up", detail="社交场合增多"
                    )
                ],
            )
        ],
    )
    agent, experience, events, summaries = _agent([draft])

    result = agent.summarize("s-happy", period=PERIOD)

    assert result.awaiting_confirmation is True and result.confirmed is False
    assert result.summary.headline == "本月可选支出高于计划。"
    assert result.pack.version == 1
    assert [item.kind for item in result.pack.lessons] == ["wants_down", "reserve_priority"]
    assert [event.id for event in result.profile_delta.events] == [f"life_event:{PERIOD}"]
    # 没确认之前一份都不该落盘
    assert experience.load() is None and events.load() == [] and summaries.list_periods() == []

    done = agent.turn("s-happy", resume={"action": "confirm"})

    assert done.confirmed is True and done.written is True
    assert done.write_location == "画像事件、经验包、月度复盘"
    assert experience.load().version == 1
    assert [item.id for item in events.load()] == [f"life_event:{PERIOD}"]
    assert summaries.list_periods() == [PERIOD]


def test_lesson_statement_falls_back_to_template_when_model_skips_it():
    draft = SummaryDraft(headline="x", lessons=[])
    agent, _, _, _ = _agent([draft])

    result = agent.summarize("s-template", period=PERIOD)

    by_kind = {item.kind: item.statement for item in result.pack.lessons}
    assert by_kind["wants_down"]  # 模板兜底，不是空串


def test_prior_pack_is_merged_not_replaced():
    experience = InMemoryExperiencePackStore()
    experience.save(
        ExperiencePack(
            version=3,
            from_period="2026-08",
            lessons=[
                Lesson(
                    id="debt_priority:2026-08",
                    kind="debt_priority",
                    statement="上月结论：债务优先。",
                    from_period="2026-08",
                )
            ],
        )
    )
    agent, _, _, _ = _agent(
        [SummaryDraft(headline="x")], stores=(experience, InMemoryProfileEventStore(), InMemoryMonthlySummaryStore())
    )

    result = agent.summarize("s-merge", period=PERIOD)

    kinds = {item.kind for item in result.pack.lessons}
    assert "debt_priority" in kinds  # 上月的教训仍在
    assert "wants_down" in kinds
    assert result.pack.version == 4


# --------------------------------------------------------------------------- #
# edit 分支 / 降级分支
# --------------------------------------------------------------------------- #
def test_reflect_failure_routes_to_fallback_then_compose():
    agent, _, _, _ = _agent([RuntimeError("模型炸了")])

    result = agent.summarize("s-boom", period=PERIOD)

    assert result.degraded is True
    assert result.error and "模型炸了" in result.error
    assert result.awaiting_confirmation is True
    assert result.summary.sections
    assert any("规则" in step for step in result.trace) or result.pack.lessons


def test_edit_resumes_back_to_reflect_with_feedback():
    runner_drafts = [
        SummaryDraft(headline="第一版结论。"),
        SummaryDraft(headline="按你的意见改过了。"),
    ]
    agent, _, _, _ = _agent(runner_drafts)

    agent.summarize("s-edit", period=PERIOD)
    result = agent.turn("s-edit", resume={"action": "edit", "message": "可选支出没那么高"})

    assert result.confirmed is False
    assert result.awaiting_confirmation is True
    assert result.summary.headline == "按你的意见改过了。"
    # 修改意见确实进了模型输入
    runner = agent.reflect_runner
    assert any("可选支出没那么高" in str(message.content) for message in runner.calls[-1])


# --------------------------------------------------------------------------- #
# 模型没有写数字的通路
# --------------------------------------------------------------------------- #
def test_model_cannot_change_the_diff():
    draft = SummaryDraft(
        headline="x",
        sections=["可选支出 999999 元。"],
        lessons=[LessonDraft(id=f"wants_down:{PERIOD}", statement="随便写一句。")],
    )
    agent, _, _, _ = _agent([draft])

    result = agent.summarize("s-tamper", period=PERIOD)
    expected = build_diff(_record().snapshot, plan=_plan(), period=PERIOD)

    assert [item.model_dump() for item in result.summary.diff.layers] == [
        item.model_dump() for item in expected.layers
    ]
    wants = next(item for item in result.summary.diff.layers if item.layer == "wants")
    assert wants.actual_cents == 500_000  # 代码算的值，不是模型写的 999999


def test_event_ids_and_periods_are_assigned_by_code():
    draft = SummaryDraft(
        headline="x",
        events=[
            EventDraft(event_type="learning", statement="用户本月学习了金融知识。", field_updates=ProfileFieldUpdate(experience="some")),
            EventDraft(event_type="不认识的类型", statement="不该被接受。"),
        ],
    )
    agent, _, _, _ = _agent([draft])

    result = agent.summarize("s-ids", period=PERIOD)

    assert [event.id for event in result.profile_delta.events] == [f"learning:{PERIOD}"]
    assert result.profile_delta.events[0].from_period == PERIOD
    assert any("不在允许范围内" in item for item in result.write_warnings or []) or result.profile_delta.events


def test_reasoning_is_captured_and_not_written_back():
    class ReasoningRunner:
        def __init__(self) -> None:
            self.calls: list[list[Any]] = []

        def __call__(self, messages: list[Any]) -> StructuredCall:
            self.calls.append(messages)
            return StructuredCall(parsed=SummaryDraft(headline="x"), reasoning="仅供观测的思维链")

    runner = ReasoningRunner()
    agent = SummaryAgent(
        settings=Settings(api_key="fake-key"),
        summary_settings=SummarySettings(),
        reflect_runner=runner,
        history_store=_seeded_history(),
        experience_store=InMemoryExperiencePackStore(),
        event_store=InMemoryProfileEventStore(),
        summary_store=InMemoryMonthlySummaryStore(),
        plan=_plan(),
    )
    first = agent.summarize("s-reason", period=PERIOD)
    assert first.reasoning == "仅供观测的思维链"

    agent.turn("s-reason", resume={"action": "edit", "message": "再改一版"})
    for payload in runner.calls:
        assert all("仅供观测的思维链" not in str(message.content) for message in payload)


def test_interrupt_payload_is_json_serializable():
    agent, _, _, _ = _agent([SummaryDraft(headline="x")])

    result = agent.summarize("s-payload", period=PERIOD)
    payload = result.confirmation_payload

    assert payload is not None
    assert payload["type"] == "summary_confirmation"
    assert payload["options"] == ["confirm", "edit"]
    json.dumps(payload, ensure_ascii=False)  # 不抛即通过


# --------------------------------------------------------------------------- #
# 与「本月实况」的衔接
# --------------------------------------------------------------------------- #
def test_record_is_read_from_month_history_store():
    agent, _, _, _ = _agent([SummaryDraft(headline="x")])
    result = agent.summarize("s-read", period=PERIOD)

    assert result.summary.diff.plan_available is True
    assert result.summary.diff.period == PERIOD
    assert "本月实况" in result.summary.sources
    assert "上一版方案" in result.summary.sources


def test_missing_record_is_reported_not_faked():
    """没有该月的实况留档时，如实说明、不编造实际值，也不硬凑经验。"""
    agent, _, _, _ = _agent([SummaryDraft(headline="x")], history=InMemoryMonthHistoryStore())

    result = agent.summarize("s-none", period="2025-01")

    assert result.summary.diff.plan_available is False
    assert result.summary.diff.layers == []
    assert any("没有找到该月已落定的实况" in note for note in result.summary.diff.notes)
    assert result.pack.lessons == []


def test_close_event_is_appended_for_a_settled_matter():
    """本月结束的事：追加一条关闭事件，原条目不动。"""
    from moneyrouter_agent.domain.profile_delta import (
        CLOSE_EVENT_TYPE,
        ProfileEvent,
        close_event_id,
    )

    events = InMemoryProfileEventStore()
    events.append(
        [
            ProfileEvent(
                id="life_event:2026-09",
                event_type="life_event",
                statement="用户九月受伤，出行不便。",
                from_period=PERIOD,
            )
        ]
    )
    draft = SummaryDraft(
        headline="已经恢复。",
        events=[
            EventDraft(
                event_type=CLOSE_EVENT_TYPE,
                statement="用户本月康复，出行恢复常态。",
                closes="life_event:2026-09",
            )
        ],
    )
    agent, _, _, _ = _agent(
        [draft], stores=(InMemoryExperiencePackStore(), events, InMemoryMonthlySummaryStore())
    )
    # 让本月实况取自 10 月
    agent.history_store.save(_record(NEXT))

    agent.summarize("s-close", period=NEXT)
    done = agent.turn("s-close", resume={"action": "confirm"})

    assert done.confirmed is True
    log = events.load()
    assert [item.id for item in log] == ["life_event:2026-09", close_event_id("life_event:2026-09")]
    assert log[0].statement == "用户九月受伤，出行不便。"  # 原文一字未改
    assert agent.effective_profile().applied_event_ids == []


def test_empty_thread_id_rejected():
    import pytest

    agent, _, _, _ = _agent([SummaryDraft(headline="x")])
    with pytest.raises(ValueError):
        agent.summarize("", period=PERIOD)


def _seeded_history() -> InMemoryMonthHistoryStore:
    store = InMemoryMonthHistoryStore()
    store.save(_record())
    return store
