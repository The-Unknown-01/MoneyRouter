"""提示词契约测试：结构、自由度基调、合规边界，以及"不掺管道机制"。"""

from moneyrouter_agent.domain.turn import TurnDecision
from moneyrouter_agent.model.deepseek import format_instruction
from moneyrouter_agent.prompts import rules
from moneyrouter_agent.prompts.interview import FINALIZE_SYSTEM, INTERVIEW_SYSTEM


def test_interview_prompt_has_full_skeleton():
    for section in (
        "# 你的目标",
        "# 怎么开场",
        "# 怎么推进",
        "# 关键：每进入一个新方面，先给指引",
        "# 什么时候收尾",
        "# 合规与纪律（必须遵守）",
    ):
        assert section in INTERVIEW_SYSTEM, f"缺少章节：{section}"


def test_interview_prompt_keeps_large_freedom():
    """大自由度：只给方向，不给死话术。"""
    assert "方向而不是话术模板" in INTERVIEW_SYSTEM
    assert "请勿照搬字句" in INTERVIEW_SYSTEM
    assert "供你参考" in INTERVIEW_SYSTEM


def test_interview_prompt_requires_guidance_first():
    assert "先给指引" in INTERVIEW_SYSTEM
    assert "风险承受" in INTERVIEW_SYSTEM


def test_interview_prompt_has_compliance_boundary():
    assert "不构成投资建议" in INTERVIEW_SYSTEM
    assert "不代替用户决策" in INTERVIEW_SYSTEM
    # 绝对化用语以"禁令"形式出现
    for banned in ("稳赚", "保本高息"):
        assert banned in INTERVIEW_SYSTEM


def test_interview_prompt_has_no_pipeline_mechanics():
    """多轮对话机制由 LangGraph 承担，不得写进 prompt。"""
    for banned in ("回顾对话", "已知信息", "对话历史", "thread_id", "messages", "JSON"):
        assert banned not in INTERVIEW_SYSTEM, f"prompt 里混入了管道内容：{banned}"


def test_field_list_lives_in_model_layer_not_prompt():
    """字段格式说明由 schema 在模型层注入——prompt 里不出现字段清单。

    （`rationale` 是例外：收尾章节用它指明"把收尾理由写在哪"，属于行为指令，
    字段本身的格式仍由 model 层注入的 schema 说明负责。）
    """
    for field in ("reply", "understanding", "ready_to_finalize", "notes"):
        assert field not in INTERVIEW_SYSTEM, f"字段清单不该出现在 prompt：{field}"
    for field in ("reply", "understanding", "ready_to_finalize", "rationale"):
        assert field in format_instruction(TurnDecision).content


def test_interview_prompt_keeps_tone_warm_but_not_chatty():
    """亲切可以有，活泼不必有——口语化过度是明确要避免的。"""
    assert "亲切，但有分寸" in INTERVIEW_SYSTEM
    assert "闲聊的朋友" in INTERVIEW_SYSTEM
    assert "不要 emoji" in INTERVIEW_SYSTEM
    for banned in ("好呀", "咱们", "挺不错的"):
        assert banned in INTERVIEW_SYSTEM, f"应显式点名禁用该口语化表达：{banned}"
    assert "过度口语化" in INTERVIEW_SYSTEM


def test_finalize_prompt_asks_for_conclusions_not_interview_process():
    """articulation 只写结论画像，不复述访谈过程、不引用用户措辞。"""
    assert "只写结论" in FINALIZE_SYSTEM
    assert "不要描述访谈是怎么进行的" in FINALIZE_SYSTEM
    assert "不要引用用户的口语原话" in FINALIZE_SYSTEM
    assert "不要逐条清点" in FINALIZE_SYSTEM
    assert "结论式写法示例" in FINALIZE_SYSTEM
    assert "家庭负担情况尚未了解" in FINALIZE_SYSTEM  # 未知项用一句话带过即可


def test_finalize_prompt_is_third_person_and_anti_hallucination():
    assert "第三人称" in FINALIZE_SYSTEM
    assert "第二人称" not in FINALIZE_SYSTEM
    assert "不要推测" in FINALIZE_SYSTEM
    assert "投资建议" in FINALIZE_SYSTEM


def test_degraded_copy_covers_every_aspect():
    for key in ("open", "identity", "income", "spend", "goal", "risk", "default"):
        assert rules.RULES_GUIDE.get(key), f"缺少降级文案：{key}"
    assert rules.DEGRADED_NOTICE


def test_degraded_copy_keeps_the_same_tone_rules():
    """降级文案是规则生成的，语气要与主路径一致：不用"咱们"这类过度口语化表达。"""
    texts = [rules.DEGRADED_NOTICE, *rules.RULES_GUIDE.values()]
    for text in texts:
        for banned in ("咱们", "好呀", "～"):
            assert banned not in text, f"降级文案混入了过度口语化表达：{text}"
