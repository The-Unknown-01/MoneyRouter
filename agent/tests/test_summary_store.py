"""总结落档层：三份产物的写语义、错误纪律、路径消毒、目录隔离守卫。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moneyrouter_agent import summary_store as store_mod
from moneyrouter_agent.config import SummarySettings
from moneyrouter_agent.domain.experience import ExperiencePack, Lesson
from moneyrouter_agent.domain.profile_delta import (
    CLOSE_EVENT_TYPE,
    ProfileEvent,
    close_event_id,
)
from moneyrouter_agent.domain.summary import MonthlySummary, PlanActualDiff
from moneyrouter_agent.summary_store import (
    InMemoryExperiencePackStore,
    InMemoryMonthlySummaryStore,
    InMemoryProfileEventStore,
    JsonExperiencePackStore,
    JsonMonthlySummaryStore,
    JsonProfileEventStore,
    SummaryStoreError,
    resolve_dir,
    sanitize_key,
)

PERIOD = "2026-09"
PREV = "2026-08"


def _lesson(period: str = PERIOD, strength: float = 1.0) -> Lesson:
    return Lesson(
        id=f"wants_down:{period}",
        kind="wants_down",
        statement="可选支出高于计划，下月宜收紧。",
        strength=strength,
        from_period=period,
        evidence=["测试依据"],
    )


def _pack(version: int = 1, period: str = PERIOD) -> ExperiencePack:
    return ExperiencePack(version=version, generated_at="2026-10-07", from_period=period, lessons=[_lesson(period)])


def _event(event_id: str, period: str) -> ProfileEvent:
    return ProfileEvent(
        id=event_id,
        event_type="life_event",
        statement="用户本月受伤，出行不便。",
        from_period=period,
    )


def _summary(period: str = PERIOD) -> MonthlySummary:
    return MonthlySummary(period=period, generated_at="2026-10-07", headline="本月按计划执行。", diff=PlanActualDiff(period=period))


# --------------------------------------------------------------------------- #
# 经验包：版本化覆盖
# --------------------------------------------------------------------------- #
def test_experience_store_roundtrip_and_overwrite(tmp_path):
    store = JsonExperiencePackStore(tmp_path / "exp", user="u1")
    assert store.load() is None

    store.save(_pack(version=1))
    assert store.load().version == 1

    store.save(_pack(version=2))
    assert store.load().version == 2  # 覆盖，不累积成多份文件
    assert sorted(p.name for p in (tmp_path / "exp").glob("*.json")) == ["u1.json"]


def test_experience_store_raises_on_corrupt_file(tmp_path):
    store = JsonExperiencePackStore(tmp_path, user="u1")
    store.path.write_text("{ 坏掉的", encoding="utf-8")
    with pytest.raises(SummaryStoreError):
        store.load()


def test_experience_store_rejects_wrong_shape(tmp_path):
    store = JsonExperiencePackStore(tmp_path, user="u1")
    store.path.write_text(json.dumps({"version": "不是数字"}), encoding="utf-8")
    with pytest.raises(SummaryStoreError):
        store.load()


def test_memory_experience_store_roundtrip():
    store = InMemoryExperiencePackStore()
    assert store.load() is None
    store.save(_pack())
    assert store.load().version == 1


# --------------------------------------------------------------------------- #
# 画像事件：append-only
# --------------------------------------------------------------------------- #
def test_event_store_append_is_idempotent(tmp_path):
    store = JsonProfileEventStore(tmp_path / "events", user="u1")
    assert store.load() == []

    first = _event("life_event:2026-09", PERIOD)
    assert [item.id for item in store.append([first])] == ["life_event:2026-09"]

    tampered = first.model_copy(update={"statement": "被改写过的说法"})
    log = store.append([tampered])

    assert len(log) == 1
    assert store.load()[0].statement == first.statement  # 原文一字未改


def test_event_store_appends_and_persists_close(tmp_path):
    store = JsonProfileEventStore(tmp_path, user="u1")
    target = _event("life_event:2026-09", PERIOD)
    store.append([target])
    store.append(
        [
            ProfileEvent(
                id=close_event_id(target.id),
                event_type=CLOSE_EVENT_TYPE,
                statement="用户本月康复。",
                from_period="2026-11",
                closes=target.id,
            )
        ]
    )

    log = store.load()
    assert [item.id for item in log] == [target.id, close_event_id(target.id)]


def test_event_store_refuses_inconsistent_log(tmp_path):
    store = JsonProfileEventStore(tmp_path, user="u1")
    orphan = ProfileEvent(
        id=close_event_id("life_event:2025-01"),
        event_type=CLOSE_EVENT_TYPE,
        statement="关闭一条并不存在的事件。",
        from_period=PERIOD,
        closes="life_event:2025-01",
    )
    with pytest.raises(SummaryStoreError):
        store.append([orphan])


def test_event_store_point_load_raises_but_missing_returns_empty(tmp_path):
    store = JsonProfileEventStore(tmp_path, user="u1")
    assert store.load() == []  # 还没记过 ≠ 读不出来

    store.path.write_text("[{ 坏掉的", encoding="utf-8")
    with pytest.raises(SummaryStoreError):
        store.load()


def test_memory_event_store_is_append_only():
    store = InMemoryProfileEventStore()
    first = _event("life_event:2026-09", PERIOD)
    store.append([first])
    store.append([first])
    assert len(store.load()) == 1


# --------------------------------------------------------------------------- #
# 月度复盘：按期间覆盖
# --------------------------------------------------------------------------- #
def test_summary_store_overwrites_same_period(tmp_path):
    store = JsonMonthlySummaryStore(tmp_path / "sum", user="u1")
    store.save(_summary(PERIOD))
    store.save(_summary(PERIOD).model_copy(update={"headline": "改过的结论"}))

    assert store.list_periods() == [PERIOD]
    assert store.load(PERIOD).headline == "改过的结论"


def test_summary_store_lists_periods_ascending(tmp_path):
    store = JsonMonthlySummaryStore(tmp_path, user="u1")
    store.save(_summary(PREV))
    store.save(_summary(PERIOD))
    assert store.list_periods() == [PREV, PERIOD]
    assert store.load("2026-01") is None


def test_summary_store_rejects_bad_period(tmp_path):
    store = JsonMonthlySummaryStore(tmp_path, user="u1")
    with pytest.raises(SummaryStoreError):
        store.path_for("2026/09")


def test_summary_store_batch_read_skips_corrupt_with_warning(tmp_path):
    store = JsonMonthlySummaryStore(tmp_path, user="u1")
    store.save(_summary(PREV))
    broken = store.path_for(PERIOD)
    broken.write_text("{ 坏掉的", encoding="utf-8")

    warnings: list[str] = []
    found = store.load_all(warnings=warnings)

    assert [item.period for item in found] == [PREV]
    assert len(warnings) == 1 and PERIOD in warnings[0]


def test_summary_store_load_all_respects_limit(tmp_path):
    store = JsonMonthlySummaryStore(tmp_path, user="u1")
    for period in ("2026-06", "2026-07", PREV, PERIOD):
        store.save(_summary(period))
    assert [item.period for item in store.load_all(limit=2)] == [PREV, PERIOD]
    assert store.load_all(limit=0) == []


def test_memory_summary_store_roundtrip():
    store = InMemoryMonthlySummaryStore()
    store.save(_summary())
    assert store.list_periods() == [PERIOD]
    assert store.load(PERIOD).period == PERIOD


# --------------------------------------------------------------------------- #
# 路径消毒与配置
# --------------------------------------------------------------------------- #
def test_sanitize_key_blocks_path_traversal():
    assert sanitize_key("../../etc/passwd") == "etc_passwd"
    assert sanitize_key("..") == "default"
    assert sanitize_key("") == "default"
    assert sanitize_key(None) == "default"
    assert sanitize_key("u 1") == "u_1"


def test_traversal_user_stays_inside_the_directory(tmp_path):
    store = JsonMonthlySummaryStore(tmp_path / "sum", user="../../evil")
    assert store.root.parent == tmp_path / "sum"


def test_blank_directory_raises_so_callers_must_disable_explicitly():
    """空目录不静默兜底——要关掉留档就显式把配置置空、别建 store。"""
    with pytest.raises(SummaryStoreError):
        resolve_dir("")
    with pytest.raises(SummaryStoreError):
        JsonExperiencePackStore("")


def test_summary_settings_from_env():
    settings = SummarySettings.from_env({}, load_dotenv=False)
    assert settings.experience_dir == ".data/experience"
    assert settings.user == "default"

    off = SummarySettings.from_env({"SUMMARY_EXPERIENCE_DIR": ""}, load_dotenv=False)
    assert off.experience_dir is None

    custom = SummarySettings.from_env(
        {"SUMMARY_SUMMARY_DIR": "out/sum", "SUMMARY_USER": "u9"}, load_dotenv=False
    )
    assert custom.summary_dir == "out/sum" and custom.user == "u9"


# --------------------------------------------------------------------------- #
# 守卫：测试绝不写真实落档目录
# --------------------------------------------------------------------------- #
def test_default_dirs_land_under_isolated_root():
    real_agent_root = Path(store_mod.__file__).resolve().parents[2]
    assert store_mod.AGENT_ROOT != real_agent_root

    store = JsonProfileEventStore(SummarySettings().event_dir)
    assert Path(store.directory) == Path(store_mod.AGENT_ROOT) / ".data" / "profile_events"
