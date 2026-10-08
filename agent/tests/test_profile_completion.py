from langchain_core.messages import HumanMessage

from moneyrouter_agent.domain.profile import ProfileDraft, ProfileResult
from moneyrouter_agent.domain.turn import TurnDecision
from moneyrouter_agent.graph.nodes import PROFILE_QUESTIONS, make_converse, make_finalize


def full_draft(**changes):
    values = dict(occupation="学生", income_cents=250000, income_basis="生活费",
                  outcome_cents=230000, feature="家庭支持", income_stable=True,
                  family_load=False, debt_cents=0, reserve_cents=500000,
                  horizon_months=36, max_loss_pct=0, experience="some", goal="换电脑")
    values.update(changes)
    return ProfileDraft(**values)


def test_missing_field_blocks_premature_finish_and_is_followed_up():
    node = make_converse(lambda _: TurnDecision(reply="整理完了", ready_to_finalize=True,
                                               understanding=full_draft(horizon_months=None)))
    state = node({"messages": [HumanMessage(content="5000元，不希望亏损，买过债基")]})
    assert not state["ready_to_finalize"]
    assert PROFILE_QUESTIONS["horizon_months"] in state["messages"][0].content


def test_explicit_refusal_allows_blank_and_survives_next_turn():
    decision = TurnDecision(reply="整理", ready_to_finalize=True,
                            understanding=full_draft(horizon_months=None),
                            refused_fields={"horizon_months": "期限不愿回答"})
    node = make_converse(lambda _: decision)
    state = node({"messages": [HumanMessage(content="期限不愿回答")]})
    assert state["ready_to_finalize"]
    assert state["understanding"].horizon_months is None
    decision.refused_fields = {}
    assert node(state)["ready_to_finalize"]


def test_invented_refusal_is_not_accepted():
    node = make_converse(lambda _: TurnDecision(reply="整理", ready_to_finalize=True,
        understanding=full_draft(horizon_months=None), refused_fields={"horizon_months": "不愿回答"}))
    assert not node({"messages": [HumanMessage(content="买过债基")]})["ready_to_finalize"]


def test_unanswered_extra_question_blocks_finish():
    node = make_converse(lambda _: TurnDecision(reply="结束", ready_to_finalize=True,
        understanding=full_draft(), pending_questions=["换电脑预计花多少钱？"]))
    state = node({})
    assert not state["ready_to_finalize"]
    assert "换电脑预计花多少钱" in state["messages"][0].content


def test_uncertainty_is_not_treated_as_refusal():
    node = make_converse(lambda _: TurnDecision(reply="整理", ready_to_finalize=True,
        understanding=full_draft(horizon_months=None), refused_fields={"horizon_months": "还不知道"}))
    assert not node({"messages": [HumanMessage(content="还不知道")]})["ready_to_finalize"]


def test_notes_accumulate_and_are_saved_in_final_profile():
    node = make_converse(lambda _: TurnDecision(reply="继续", understanding=full_draft(), notes="持有纯债基金"))
    state = node({"notes": "电脑预算待定"})
    final = make_finalize(lambda _: ProfileResult())(state)
    assert "电脑预算待定" in final["final_profile"].notes
    assert "持有纯债基金" in final["final_profile"].notes
