"""总结提示词契约：结构骨架、自由度基调、语气纪律、不掺管道机制、降级文案。"""

from __future__ import annotations

from moneyrouter_agent.domain.profile_delta import EFFECT_KINDS, EVENT_TYPES
from moneyrouter_agent.domain.summary import EventDraft, LessonDraft, SummaryDraft
from moneyrouter_agent.model.deepseek import format_instruction
from moneyrouter_agent.prompts import summary_rules
from moneyrouter_agent.prompts.summary import SUMMARY_SYSTEM


def test_summary_prompt_has_full_skeleton():
    for section in (
        "# 你的目标",
        "# 系统会给你的材料",
        "# 怎么下结论",
        "# 记录用户情况的变化",
        "# 怎么说话：亲切，但有分寸",
        "# 合规与纪律（必须遵守）",
    ):
        assert section in SUMMARY_SYSTEM, f"缺少章节：{section}"


def test_summary_prompt_keeps_large_freedom():
    assert "方向而不是话术模板" in SUMMARY_SYSTEM
    assert "由你根据对方的情况自然发挥" in SUMMARY_SYSTEM


def test_summary_prompt_keeps_tone_warm_but_not_chatty():
    assert "亲切，但有分寸" in SUMMARY_SYSTEM
    assert "闲聊的朋友" in SUMMARY_SYSTEM
    assert "不要 emoji" in SUMMARY_SYSTEM
    assert "过度口语化" in SUMMARY_SYSTEM
    for banned in ("好呀", "咱们", "挺不错的"):
        assert banned in SUMMARY_SYSTEM, f"应显式点名禁用该口语化表达：{banned}"


def test_summary_prompt_has_compliance_boundary():
    assert "不构成投资建议" in SUMMARY_SYSTEM
    assert "不代替用户决策" in SUMMARY_SYSTEM
    assert "不评判" in SUMMARY_SYSTEM
    for banned in ("稳赚", "保本高息"):
        assert banned in SUMMARY_SYSTEM


def test_summary_prompt_forbids_conclusions_without_process():
    assert "只写结论" in SUMMARY_SYSTEM
    assert "不写过程" in SUMMARY_SYSTEM
    assert "不引用用户的口语原话" in SUMMARY_SYSTEM


def test_summary_prompt_does_not_leak_pipeline_mechanics():
    """多轮机制、字段名与管道内容不得写进 prompt。"""
    for banned in (
        "对话历史",
        "messages",
        "thread_id",
        "JSON",
        "snapshot",
        "reply",
        "probe",
        "ready_to_finalize",
        "goal_alignment",
        "apply_lessons",
    ):
        assert banned not in SUMMARY_SYSTEM, f"prompt 里混入了管道内容：{banned}"


def test_field_list_lives_in_model_layer_not_prompt():
    instruction = format_instruction(SummaryDraft).content
    for field in ("headline", "sections", "lessons", "events", "notes"):
        assert field in instruction
    # 嵌套字段也要展开，模型才知道数组元素的形状
    assert "lessons.statement" in instruction
    assert "events.event_type" in instruction
    assert "events.field_updates.income_cents" in instruction
    assert "events.effects.kind" in instruction


def test_prompt_states_the_whitelists_not_invents():
    """模型只能从白名单里选类型，且不得新增候选项。"""
    assert "不要新增候选项" in SUMMARY_SYSTEM
    assert "原样回填" in LessonDraft.model_fields["id"].description
    assert "代码白名单" in EventDraft.model_fields["event_type"].description


def test_whitelists_are_not_empty():
    assert "life_event" in EVENT_TYPES and "learning" in EVENT_TYPES
    assert "category_cap" in EFFECT_KINDS


def test_degraded_copy_covers_every_lesson_kind_and_keeps_tone():
    for kind in ("wants_down", "risk_conservative", "reserve_priority", "debt_priority", "goal_pace"):
        assert summary_rules.LESSON_STATEMENT_TEMPLATES.get(kind), f"缺少降级文案：{kind}"
    assert summary_rules.SUMMARY_DEGRADED_NOTICE
    assert summary_rules.lesson_statement("不认识的类型")  # 兜底不炸

    texts = [summary_rules.SUMMARY_DEGRADED_NOTICE, *summary_rules.LESSON_STATEMENT_TEMPLATES.values()]
    for text in texts:
        for banned in ("咱们", "好呀", "～"):
            assert banned not in text, f"降级文案混入了过度口语化表达：{text}"
