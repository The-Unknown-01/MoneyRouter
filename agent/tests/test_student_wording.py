"""学生场景：收入口径必须能表达为「每月生活费」，而不是被当成月薪。"""

from moneyrouter_agent.domain.profile import ProfileDraft
from moneyrouter_agent.prompts.interview import INTERVIEW_SYSTEM


def test_prompt_mentions_student_living_allowance():
    assert "生活费" in INTERVIEW_SYSTEM


def test_income_basis_is_free_form():
    student = ProfileDraft(income_cents=150_000, income_basis="每月生活费")
    worker = ProfileDraft(income_cents=2_000_000, income_basis="税后月薪")
    freelancer = ProfileDraft(income_cents=800_000, income_basis="接单收入")

    assert student.income_basis == "每月生活费"
    assert student.income_cents == 150_000  # 1500 元
    assert worker.income_basis == "税后月薪"
    assert freelancer.income_basis == "接单收入"


def test_blank_basis_is_dropped():
    draft = ProfileDraft(income_cents=150_000, income_basis="   ")
    assert draft.income_basis is None
