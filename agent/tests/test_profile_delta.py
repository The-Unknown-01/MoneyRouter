"""画像增量：append-only 不变式、稳定 id、关闭事件、读时派生、生效画像叠加。"""

from __future__ import annotations

import json

from moneyrouter_agent.domain.profile import Profile
from moneyrouter_agent.domain.profile_delta import (
    CLOSE_EVENT_TYPE,
    ProfileDelta,
    ProfileEvent,
    ProfileFieldUpdate,
    active_events,
    append_events,
    build_delta,
    close_event_id,
    effective_profile,
    event_ended_period,
    event_status,
    is_close_event,
    make_event_id,
    validate_events,
)

INJURY = "life_event:2026-09"
RECOVERY = close_event_id(INJURY)


def _injury() -> ProfileEvent:
    return ProfileEvent(
        id=INJURY,
        event_type="life_event",
        statement="用户本月受伤，出行不便，出行相关支出需相应放宽。",
        from_period="2026-09",
        effects=[
            {
                "kind": "category_cap",
                "target": "交通",
                "direction": "up",
                "detail": "出行方式受限，需更多打车支出。",
            }
        ],
        evidence=["本月交通支出高于上月"],
    )


def _recovery() -> ProfileEvent:
    return ProfileEvent(
        id=RECOVERY,
        event_type=CLOSE_EVENT_TYPE,
        statement="用户本月康复，出行恢复常态。",
        from_period="2026-11",
        closes=INJURY,
    )


# --------------------------------------------------------------------------- #
# 稳定 id
# --------------------------------------------------------------------------- #
def test_make_event_id_is_deterministic_and_disambiguates_same_period():
    assert make_event_id("income_change", "2026-09") == "income_change:2026-09"
    taken = {"income_change:2026-09"}
    assert make_event_id("income_change", "2026-09", taken) == "income_change:2026-09#2"
    taken.add("income_change:2026-09#2")
    assert make_event_id("income_change", "2026-09", taken) == "income_change:2026-09#3"


def test_close_event_id_is_stable_for_the_same_target():
    assert close_event_id(INJURY) == close_event_id(INJURY) == f"close:{INJURY}"


# --------------------------------------------------------------------------- #
# append-only
# --------------------------------------------------------------------------- #
def test_duplicate_append_is_noop_and_returns_original_entry():
    original = _injury()
    tampered = original.model_copy(update={"statement": "被改写过的说法"})

    log = append_events([original], [tampered])

    assert len(log) == 1
    assert log[0].statement == original.statement  # 既有一字未改


def test_append_only_log_keeps_every_field_untouched():
    original = _injury()
    before = original.model_dump(mode="json")

    log = append_events([original], [_recovery(), original])

    found = next(item for item in log if item.id == INJURY)
    assert found.model_dump(mode="json") == before
    assert [item.id for item in log] == [INJURY, RECOVERY]


def test_append_events_does_not_mutate_the_input_list():
    original = [_injury()]
    append_events(original, [_recovery()])
    assert len(original) == 1


# --------------------------------------------------------------------------- #
# 关闭事件与读时派生
# --------------------------------------------------------------------------- #
def test_close_appends_new_event_and_status_is_derived_not_stored():
    log = append_events([_injury()], [_recovery()])

    # 派生结果
    target = next(item for item in log if item.id == INJURY)
    assert event_ended_period(target, log) == "2026-11"
    assert event_status(target, log) == "ended"

    # 磁盘上根本不存 status / ended_period 这两个字段
    payload = json.loads(target.model_dump_json())
    assert "status" not in payload
    assert "ended_period" not in payload


def test_event_is_active_before_its_close_month():
    log = append_events([_injury()], [_recovery()])
    target = next(item for item in log if item.id == INJURY)

    assert event_status(target, log, as_of_period="2026-10") == "active"
    assert event_status(target, log, as_of_period="2026-11") == "ended"
    assert event_status(target, log, as_of_period="2026-12") == "ended"


def test_active_events_excludes_closed_and_close_events():
    log = append_events([_injury()], [_recovery()])

    assert [item.id for item in active_events(log)] == []
    assert [item.id for item in active_events(log, as_of_period="2026-09")] == [INJURY]


def test_effects_survive_the_close_without_touching_the_original():
    log = append_events([_injury()], [_recovery()])
    target = next(item for item in log if item.id == INJURY)

    assert target.effects[0].kind == "category_cap"
    assert target.effects[0].target == "交通"
    assert target.effects[0].direction == "up"


# --------------------------------------------------------------------------- #
# 生效画像叠加
# --------------------------------------------------------------------------- #
def test_effective_profile_overlays_only_active_events_in_period_order():
    gain = ProfileEvent(
        id="income_change:2026-09",
        event_type="income_change",
        statement="用户本月换了工作，收入口径随之变化。",
        from_period="2026-09",
        field_updates=ProfileFieldUpdate(income_cents=1_200_000, income_basis="新公司月薪"),
    )
    later = ProfileEvent(
        id="income_change:2026-10",
        event_type="income_change",
        statement="用户本月收入再次调整。",
        from_period="2026-10",
        field_updates=ProfileFieldUpdate(income_cents=1_500_000),
    )
    log = append_events([_injury(), gain], [later, _recovery()])

    result = effective_profile(Profile(occupation="设计师"), log)

    assert result.updates == {"income_cents": 1_500_000, "income_basis": "新公司月薪"}
    assert result.applied_event_ids == ["income_change:2026-09", "income_change:2026-10"]

    # 受伤那条只影响出行预算、不带字段值，所以不进 applied_ids
    assert INJURY not in result.applied_event_ids


def test_effective_profile_keeps_base_untouched():
    base = Profile(occupation="设计师", income_cents=800_000)
    gain = ProfileEvent(
        id="income_change:2026-09",
        event_type="income_change",
        statement="收入变化。",
        from_period="2026-09",
        field_updates=ProfileFieldUpdate(income_cents=1_200_000),
    )

    result = effective_profile(base, [gain])

    assert base.income_cents == 800_000  # 原画像一字未改
    assert result.base.income_cents == 800_000
    assert result.merged().income_cents == 1_200_000  # 只有投影变了


def test_as_of_period_rolls_back_the_overlay():
    gain = ProfileEvent(
        id="income_change:2026-09",
        event_type="income_change",
        statement="收入变化。",
        from_period="2026-09",
        field_updates=ProfileFieldUpdate(income_cents=1_200_000),
    )
    assert effective_profile(Profile(), [gain], as_of_period="2026-08").updates == {}
    assert effective_profile(Profile(), [gain], as_of_period="2026-09").updates == {
        "income_cents": 1_200_000
    }


def test_learning_event_raises_investment_experience():
    learned = ProfileEvent(
        id="learning:2026-09",
        event_type="learning",
        statement="用户本月系统学习了金融知识，对投资的理解有明显提升。",
        from_period="2026-09",
        field_updates=ProfileFieldUpdate(experience="some"),
    )
    base = Profile(experience="none")

    assert effective_profile(base, [learned]).merged().experience == "some"
    assert base.experience == "none"


# --------------------------------------------------------------------------- #
# 只设值、不清空；0 是合法值
# --------------------------------------------------------------------------- #
def test_zero_override_is_treated_as_a_value_not_missing():
    paid_off = ProfileEvent(
        id="debt_change:2026-09",
        event_type="debt_change",
        statement="用户本月还清了全部负债。",
        from_period="2026-09",
        field_updates=ProfileFieldUpdate(debt_cents=0),
    )
    result = effective_profile(Profile(debt_cents=5_000_000), [paid_off])

    assert result.updates == {"debt_cents": 0}
    assert result.merged().debt_cents == 0


def test_invalid_field_values_are_dropped_by_shared_sanitize():
    """越界 / 空串沿用画像自身的清理规则：清空而不是硬塞。"""
    update = ProfileFieldUpdate(income_cents=-1, max_loss_pct=200, goal="   ", horizon_months=0)

    assert update.as_updates() == {}


def test_max_loss_pct_zero_survives():
    assert ProfileFieldUpdate(max_loss_pct=0).as_updates() == {"max_loss_pct": 0}


def test_as_updates_ignores_unset_fields():
    assert ProfileFieldUpdate(occupation="教师").as_updates() == {"occupation": "教师"}


# --------------------------------------------------------------------------- #
# 一致性校验
# --------------------------------------------------------------------------- #
def test_validate_events_accepts_a_healthy_log():
    log = append_events([_injury()], [_recovery()])
    assert validate_events(log) == []


def test_close_event_cannot_be_closed():
    log = append_events(
        [_injury()],
        [
            _recovery(),
            ProfileEvent(
                id=close_event_id(RECOVERY),
                event_type=CLOSE_EVENT_TYPE,
                statement="试图关闭一条关闭事件。",
                from_period="2026-12",
                closes=RECOVERY,
            ),
        ],
    )
    problems = validate_events(log)
    assert any("不能再被关闭" in item for item in problems)


def test_close_target_must_exist():
    orphan = ProfileEvent(
        id=close_event_id("life_event:2026-01"),
        event_type=CLOSE_EVENT_TYPE,
        statement="关闭一条并不存在的事件。",
        from_period="2026-09",
        closes="life_event:2026-01",
    )
    assert any("不存在" in item for item in validate_events([orphan]))


def test_close_flag_and_event_type_must_match():
    wrong_type = ProfileEvent(
        id="life_event:2026-09",
        event_type="life_event",
        statement="带关闭目标但类型不对。",
        from_period="2026-09",
        closes=INJURY,
    )
    empty_close = ProfileEvent(
        id="event_ended:2026-09",
        event_type=CLOSE_EVENT_TYPE,
        statement="类型对但没写目标。",
        from_period="2026-09",
    )
    problems = validate_events([_injury(), wrong_type, empty_close])

    assert any("却不是关闭事件类型" in item for item in problems)
    assert any("却没有写关闭目标" in item for item in problems)


def test_validate_events_flags_duplicate_ids():
    assert any("重复" in item for item in validate_events([_injury(), _injury()]))


def test_is_close_event_only_true_for_close_entries():
    assert is_close_event(_recovery()) is True
    assert is_close_event(_injury()) is False


# --------------------------------------------------------------------------- #
# 本期增量
# --------------------------------------------------------------------------- #
def test_build_delta_keeps_period_and_events():
    delta: ProfileDelta = build_delta("2026-09", [_injury()], notes="受伤影响出行预算。")
    assert delta.period == "2026-09"
    assert [item.id for item in delta.events] == [INJURY]
    assert delta.notes == "受伤影响出行预算。"


def test_delta_survives_json_roundtrip():
    delta = build_delta("2026-09", [_injury()])
    again = ProfileDelta.model_validate_json(delta.model_dump_json())
    assert again.events[0].effects[0].target == "交通"
