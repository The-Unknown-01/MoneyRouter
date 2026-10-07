"""提示词契约测试：结构、自由度基调、合规边界、不掺管道机制。"""

from moneyrouter_agent.domain.month_turn import MonthTurnDecision
from moneyrouter_agent.model.deepseek import format_instruction
from moneyrouter_agent.prompts import month_rules
from moneyrouter_agent.prompts.month import MONTH_FINALIZE_SYSTEM, MONTH_SYSTEM


def test_month_prompt_has_full_skeleton():
    for section in (
        "# 你的目标",
        "# 怎么推进",
        "# 系统会给你的线索",
        "# 怎么说话：亲切，但有分寸",
        "# 什么时候收尾",
        "# 合规与纪律（必须遵守）",
    ):
        assert section in MONTH_SYSTEM, f"缺少章节：{section}"


def test_month_prompt_keeps_large_freedom():
    assert "方向而不是话术模板" in MONTH_SYSTEM
    assert "请勿照搬字句" in MONTH_SYSTEM


def test_month_prompt_keeps_tone_warm_but_not_chatty():
    assert "亲切，但有分寸" in MONTH_SYSTEM
    assert "闲聊的朋友" in MONTH_SYSTEM
    assert "不要 emoji" in MONTH_SYSTEM
    for banned in ("好呀", "咱们", "挺不错的"):
        assert banned in MONTH_SYSTEM, f"应显式点名禁用该口语化表达：{banned}"
    assert "过度口语化" in MONTH_SYSTEM


def test_month_prompt_has_compliance_boundary():
    assert "不构成投资建议" in MONTH_SYSTEM
    assert "不代替用户决策" in MONTH_SYSTEM
    assert "不评判" in MONTH_SYSTEM
    for banned in ("稳赚", "保本高息"):
        assert banned in MONTH_SYSTEM


def test_month_prompt_does_not_leak_pipeline_mechanics():
    """多轮机制与字段名不得写进 prompt。"""
    for banned in (
        "对话历史",
        "messages",
        "thread_id",
        "JSON",
        "probe",
        "snapshot",
        "reply",
        "ready_to_finalize",
        "goal_alignment",
    ):
        assert banned not in MONTH_SYSTEM, f"prompt 里混入了管道内容：{banned}"


def test_field_list_lives_in_model_layer_not_prompt():
    for field in ("reply", "snapshot", "probe_answers", "notes", "ready_to_finalize"):
        assert field in format_instruction(MonthTurnDecision).content
    # 嵌套字段也要展开，模型才知道数组元素的形状
    instruction = format_instruction(MonthTurnDecision).content
    assert "snapshot.categories.category" in instruction
    assert "snapshot.goal_alignment.progress_pct" in instruction
    # 已有投资的收益也在同一份快照里（含数组元素字段）
    assert "snapshot.investments.has_investments" in instruction
    assert "snapshot.investments.holdings.month_return_cents" in instruction
    assert "snapshot.net_worth_change_cents" in instruction


def test_finalize_prompt_asks_for_conclusions_not_process():
    assert "只写结论" in MONTH_FINALIZE_SYSTEM
    assert "不写过程" in MONTH_FINALIZE_SYSTEM
    assert "不要引用用户的口语原话" in MONTH_FINALIZE_SYSTEM


def test_month_prompt_covers_existing_investment_returns():
    assert "以前投出去的那些" in MONTH_SYSTEM
    assert "已有的投资" in MONTH_FINALIZE_SYSTEM or "投资的收益" in MONTH_FINALIZE_SYSTEM


def test_degraded_copy_covers_every_aspect_and_keeps_tone():
    for key in ("income", "category", "allocation", "investments", "probe", "default"):
        assert month_rules.MONTH_RULES_GUIDE.get(key), f"缺少降级文案：{key}"
    assert month_rules.MONTH_DEGRADED_NOTICE
    for text in [month_rules.MONTH_DEGRADED_NOTICE, *month_rules.MONTH_RULES_GUIDE.values()]:
        for banned in ("咱们", "好呀", "～"):
            assert banned not in text, f"降级文案混入了过度口语化表达：{text}"
