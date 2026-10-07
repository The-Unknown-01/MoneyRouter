"""画像 schema 单测：校验语义、越界清理、0% 亏损合法。"""

from moneyrouter_agent.domain.profile import Profile, ProfileDraft, ProfileResult


def test_max_loss_zero_survives_sanitize():
    """0% 是合法值，绝不能被当作「未回答」清掉（修掉 Go 的零值缺陷）。"""
    draft = ProfileDraft(max_loss_pct=0)
    assert draft.max_loss_pct == 0


def test_out_of_range_values_are_dropped_not_defaulted():
    draft = ProfileDraft(horizon_months=0, max_loss_pct=200, income_cents=-5)
    assert draft.horizon_months is None
    assert draft.max_loss_pct is None
    assert draft.income_cents is None


def test_blank_strings_become_none():
    draft = ProfileDraft(occupation="   ", goal="", income_basis="\n")
    assert draft.occupation is None
    assert draft.goal is None
    assert draft.income_basis is None


def test_goal_is_capped_at_500_chars():
    draft = ProfileDraft(goal="目" * 800)
    assert draft.goal is not None
    assert len(draft.goal) == 500


def test_unknown_fields_are_ignored():
    draft = ProfileDraft.model_validate({"occupation": "学生", "未知字段": 1})
    assert draft.occupation == "学生"


def test_unanswered_fields_stay_none():
    """不做默认值填充：没了解到的一律 None。"""
    draft = ProfileDraft(occupation="程序员")
    assert draft.income_cents is None
    assert draft.horizon_months is None
    assert draft.experience is None


def test_profile_result_shape():
    result = ProfileResult.model_validate(
        {
            "profile": {"occupation": "大四学生", "income_cents": 150_000, "income_basis": "每月生活费"},
            "articulation": "用户是一名大四学生，靠家里给生活费。",
        }
    )
    assert isinstance(result.profile, Profile)
    assert result.profile.income_cents == 150_000
    assert result.profile.income_basis == "每月生活费"
    assert "大四学生" in result.articulation


def test_profile_result_defaults():
    result = ProfileResult()
    assert result.profile.occupation is None
    assert result.articulation == ""
