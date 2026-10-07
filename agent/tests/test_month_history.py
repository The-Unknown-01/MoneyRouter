"""历史留档：文件库读写、落定自动落档、历史回喂成基线、隔离守卫。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from moneyrouter_agent import history as history_mod
from moneyrouter_agent.config import MonthSettings, Settings
from moneyrouter_agent.domain.month import (
    CategorySpend,
    GoalAlignment,
    IncomeFact,
    MonthResult,
    MonthSnapshot,
    recompute,
)
from moneyrouter_agent.domain.month_turn import MonthTurnDecision
from moneyrouter_agent.history import (
    HistoryError,
    InMemoryMonthHistoryStore,
    JsonMonthHistoryStore,
    MonthRecord,
    now_iso,
    resolve_history_dir,
    snapshots_from,
)
from moneyrouter_agent.month_agent import MonthAgent, record_from_turn
from moneyrouter_agent.tools.dossier import sample_dossier

PERIOD = "2026-09"
PREV = "2026-08"
DOSSIER = sample_dossier(PERIOD)

_UNSET = object()


def _snapshot(period: str, *, dining_cents: int = 10_000, income_cents: int = 100_000) -> MonthSnapshot:
    """构造一个**已重算过**的快照——留档里存的就该是这个形状（含派生字段）。"""
    return recompute(
        MonthSnapshot(
            period=period,
            income=IncomeFact(amount_cents=income_cents),
            categories=[CategorySpend(category="餐饮", amount_cents=dining_cents)],
        )
    )


def _record(period: str, **kwargs: Any) -> MonthRecord:
    articulation = kwargs.pop("articulation", f"{period} 的实况结论")
    return MonthRecord(
        period=period,
        recorded_at=now_iso(),
        snapshot=kwargs.pop("snapshot", _snapshot(period)),
        articulation=articulation,
        **kwargs,
    )


class ScriptedTurnRunner:
    def __init__(self, decisions: list[MonthTurnDecision]) -> None:
        self._decisions = list(decisions)
        self.calls: list[list[Any]] = []

    def __call__(self, messages: list[Any]) -> MonthTurnDecision:
        self.calls.append(messages)
        if not self._decisions:
            raise AssertionError("脚本化 runner 已无更多决策，但图又调用了一次")
        return self._decisions.pop(0)


class ScriptedFinalizeRunner:
    def __init__(self, result: MonthResult | None = None) -> None:
        self._result = result or MonthResult(articulation="这个月的实况结论")

    def __call__(self, messages: list[Any]) -> MonthResult:
        return self._result


def _agent(decisions: list[MonthTurnDecision], *, store: Any = _UNSET, **kwargs: Any) -> MonthAgent:
    """``store`` 不传＝用默认文件库（受 conftest 隔离）；传 ``None`` ＝显式关闭历史。"""
    extra = {} if store is _UNSET else {"history_store": store}
    return MonthAgent(
        settings=Settings(api_key="fake-key"),
        month_settings=MonthSettings(),
        turn_runner=ScriptedTurnRunner(decisions),
        finalize_runner=ScriptedFinalizeRunner(),
        **extra,
        **kwargs,
    )


def _closing_decision() -> MonthTurnDecision:
    return MonthTurnDecision(
        reply="这个月的情况我了解得差不多了，帮您整理一下。",
        snapshot=MonthSnapshot(period=PERIOD),
        ready_to_finalize=True,
    )


# --------------------------------------------------------------------------- #
# 文件库
# --------------------------------------------------------------------------- #
def test_json_store_roundtrip(tmp_path):
    store = JsonMonthHistoryStore(tmp_path / "months")
    location = store.save(_record(PREV))

    assert location.endswith(f"{PREV}.json")
    assert store.list_periods() == [PREV]
    loaded = store.load(PREV)
    assert loaded is not None
    assert loaded.period == PREV
    assert loaded.snapshot.categories[0].category == "餐饮"
    assert loaded.articulation == f"{PREV} 的实况结论"


def test_save_is_idempotent_per_period(tmp_path):
    store = JsonMonthHistoryStore(tmp_path)
    store.save(_record(PREV, articulation="第一版"))
    store.save(_record(PREV, articulation="改过的"))

    assert store.list_periods() == [PREV]  # 同期间覆盖，不堆多份
    assert store.load(PREV).articulation == "改过的"


def test_load_missing_returns_none_but_corrupt_raises(tmp_path):
    store = JsonMonthHistoryStore(tmp_path)
    assert store.load("2026-01") is None

    (tmp_path / "2026-07.json").write_text("{ 坏掉的", encoding="utf-8")
    # 点名读不把"读不出来"伪装成"没有记录"
    with pytest.raises(HistoryError):
        store.load("2026-07")


def test_load_records_skips_corrupt_and_reports(tmp_path):
    store = JsonMonthHistoryStore(tmp_path)
    store.save(_record("2026-07"))
    store.save(_record(PREV))
    (tmp_path / "2026-06.json").write_text("[]", encoding="utf-8")  # 不是对象

    warnings: list[str] = []
    records = store.load_records(warnings=warnings)

    assert [r.period for r in records] == ["2026-07", PREV]
    assert len(warnings) == 1 and "2026-06" in warnings[0]


def test_load_records_limit_and_before_are_ascending(tmp_path):
    store = JsonMonthHistoryStore(tmp_path)
    for period in ("2026-05", "2026-06", "2026-07", PREV, PERIOD):
        store.save(_record(period))

    assert [r.period for r in store.load_records(limit=2)] == [PREV, PERIOD]
    # 只取更早的月份——这正是"不要拿本月跟自己比"的实现
    assert [r.period for r in store.load_records(before=PERIOD)] == [
        "2026-05",
        "2026-06",
        "2026-07",
        PREV,
    ]
    assert [r.period for r in store.load_records(before=PERIOD, limit=3)] == [
        "2026-06",
        "2026-07",
        PREV,
    ]
    assert store.load_records(limit=0) == []


def test_list_periods_ignores_foreign_files_and_rejects_bad_period(tmp_path):
    store = JsonMonthHistoryStore(tmp_path)
    store.save(_record(PREV))
    (tmp_path / "readme.json").write_text("{}", encoding="utf-8")
    (tmp_path / "2026-08.json.tmp").write_text("{}", encoding="utf-8")
    assert store.list_periods() == [PREV]

    with pytest.raises(HistoryError):
        store.path_for("2026/08")


def test_blank_directory_means_disabled():
    assert resolve_history_dir(None) is None
    assert resolve_history_dir("") is None
    with pytest.raises(HistoryError):
        JsonMonthHistoryStore("")


def test_settings_from_env_for_history():
    assert MonthSettings.from_env({}, load_dotenv=False).history_dir == ".data/months"
    assert MonthSettings.from_env({"MONTH_HISTORY_DIR": ""}, load_dotenv=False).history_dir is None
    assert (
        MonthSettings.from_env({"MONTH_HISTORY_DIR": "out/months"}, load_dotenv=False).history_dir
        == "out/months"
    )
    assert MonthSettings.from_env({"MONTH_HISTORY_LIMIT": "2"}, load_dotenv=False).history_limit == 2


# --------------------------------------------------------------------------- #
# 落定 → 落档
# --------------------------------------------------------------------------- #
def test_confirmed_turn_is_recorded(tmp_path):
    store = JsonMonthHistoryStore(tmp_path / "months")
    agent = _agent([_closing_decision()], store=store)

    first = agent.turn("h1", "开始", bill=DOSSIER, period=PERIOD)
    assert first.awaiting_confirmation is True
    assert first.recorded is False  # 还没核对，不算数
    assert store.list_periods() == []

    result = agent.turn("h1", resume={"action": "confirm"})

    assert result.confirmed is True
    assert result.recorded is True
    assert result.history_periods == [PERIOD]
    assert result.history_location and result.history_location.endswith(f"{PERIOD}.json")

    record = store.load(PERIOD)
    assert record is not None
    assert record.snapshot.balance_cents == 324_950  # 落定那一版的快照
    assert record.articulation  # 实况结论一并留下
    assert [p.id for p in record.probes] == ["one_off_large:换手机"]  # 归因也留下


def test_unconfirmed_session_records_nothing():
    store = InMemoryMonthHistoryStore()
    agent = _agent([MonthTurnDecision(reply="这个月收入多少？", snapshot=MonthSnapshot(period=PERIOD))], store=store)

    result = agent.turn("h2", "你好", period=PERIOD)

    assert result.confirmed is False
    assert result.recorded is False
    assert store.list_periods() == []


def test_reconfirming_same_period_overwrites_without_duplicates():
    store = InMemoryMonthHistoryStore()
    agent = _agent([_closing_decision()], store=store)
    agent.turn("h3", "开始", bill=DOSSIER, period=PERIOD)
    agent.turn("h3", resume={"action": "confirm"})
    agent.turn("h3", resume={"action": "confirm"})  # 再确认一次

    assert store.list_periods() == [PERIOD]


def test_record_without_period_is_not_saved():
    store = InMemoryMonthHistoryStore()
    agent = _agent([MonthTurnDecision(reply="整理一下", ready_to_finalize=True)], store=store)
    # 全程没给期间，快照也没有 period
    agent.turn("h4", "开始")
    result = agent.turn("h4", resume={"action": "confirm"})

    assert result.confirmed is True
    assert result.recorded is False
    assert result.error and "统计期间" in result.error
    assert store.list_periods() == []


# --------------------------------------------------------------------------- #
# 历史回喂：基线 / 累计已攒
# --------------------------------------------------------------------------- #
def test_history_feeds_last_month_baseline_previously_injected_only(tmp_path):
    store = JsonMonthHistoryStore(tmp_path)
    store.save(_record(PREV, snapshot=_snapshot(PREV, dining_cents=10_000)))
    agent = _agent(
        [MonthTurnDecision(reply="这个月餐饮好像多了，是什么情况？", snapshot=MonthSnapshot(period=PERIOD))],
        store=store,
    )

    result = agent.turn("h5", "开始", bill=DOSSIER, period=PERIOD)

    metrics = {b.metric for b in result.snapshot.baselines}
    assert "last_month" in metrics  # 历史自动喂进来了，不再需要外部注入
    assert "over_last_month:餐饮" in [p.id for p in result.probes]
    assert [point.period for point in result.snapshot.trailing] == [PREV, PERIOD]


def test_current_period_in_store_is_not_compared_with_itself(tmp_path):
    store = JsonMonthHistoryStore(tmp_path)
    store.save(_record(PERIOD, snapshot=_snapshot(PERIOD, dining_cents=999_999)))
    agent = _agent([MonthTurnDecision(reply="a", snapshot=MonthSnapshot(period=PERIOD))], store=store)

    result = agent.turn("h6", "开始", bill=DOSSIER, period=PERIOD)

    assert all(b.metric != "last_month" for b in result.snapshot.baselines)
    assert not any(p.id.startswith("over_last_month") for p in result.probes)


def test_history_accumulates_saved_to_date(tmp_path):
    store = JsonMonthHistoryStore(tmp_path)
    store.save(_record(PREV, snapshot=_snapshot(PREV, dining_cents=0, income_cents=100_000)))
    agent = _agent(
        [MonthTurnDecision(reply="a", snapshot=MonthSnapshot(period=PERIOD))],
        store=store,
        goal=GoalAlignment(goal="攒首付", target_cents=1_000_000),
    )

    result = agent.turn("h7", "开始", bill=DOSSIER, period=PERIOD)

    assert result.snapshot.goal_alignment.saved_to_date_cents == 100_000  # 八月结余累加
    assert result.snapshot.goal_alignment.progress_pct == 10.0


def test_history_can_be_turned_off():
    for store in (None, InMemoryMonthHistoryStore()):
        agent = _agent(
            [MonthTurnDecision(reply="a", snapshot=MonthSnapshot(period=PERIOD))],
            store=store,
            autoload_history=False,
        )
        result = agent.turn("h8", "开始", bill=DOSSIER, period=PERIOD)
        assert all(b.metric != "last_month" for b in result.snapshot.baselines)

    disabled = MonthAgent(
        settings=Settings(api_key="fake-key"),
        month_settings=MonthSettings(history_dir=None),  # 配置层关闭
        turn_runner=ScriptedTurnRunner([MonthTurnDecision(reply="a")]),
        finalize_runner=ScriptedFinalizeRunner(),
    )
    assert disabled.history_store is None
    assert disabled.history_periods() == []


# --------------------------------------------------------------------------- #
# 守卫：测试绝不写真实历史目录
# --------------------------------------------------------------------------- #
def test_tests_never_write_the_real_history_dir():
    """真实历史会被下个月当作基线使用——测试造的假月份若写进去就是假基线。
    由 ``tests/conftest.py`` 重定向 ``history.AGENT_ROOT`` 保证。"""
    real_agent_root = Path(history_mod.__file__).resolve().parents[2]
    assert history_mod.AGENT_ROOT != real_agent_root


def test_default_store_lands_under_the_isolated_root():
    """默认配置（不显式给 store）也必须落在被重定向过的目录里。"""
    agent = _agent([_closing_decision()])
    assert agent.history_store is not None
    assert Path(agent.history_store.directory) == Path(history_mod.AGENT_ROOT) / ".data" / "months"


# --------------------------------------------------------------------------- #
# 门面读取口
# --------------------------------------------------------------------------- #
def test_record_from_turn_uses_final_result_and_keeps_probes():
    store = InMemoryMonthHistoryStore()
    agent = _agent([_closing_decision()], store=store)
    agent.turn("h9", "开始", bill=DOSSIER, period=PERIOD)
    result = agent.turn("h9", resume={"action": "confirm"})

    record = record_from_turn(result)
    assert record.period == PERIOD
    assert record.snapshot.balance_cents == result.final_result.snapshot.balance_cents
    assert record.articulation == result.final_result.articulation
    assert snapshots_from([record])[0].period == PERIOD


def test_history_accessors():
    store = InMemoryMonthHistoryStore()
    store.save(_record("2026-07"))
    store.save(_record(PREV))
    agent = _agent([MonthTurnDecision(reply="a")], store=store)

    assert agent.history_periods() == ["2026-07", PREV]
    assert [r.period for r in agent.history_records(limit=1)] == [PREV]
    assert agent.load_record("2026-07").period == "2026-07"
    assert agent.load_record("2025-01") is None
