"""金融情况提示词契约：职责边界、合规红线、"不掺管道机制"、以及渲染函数。"""

from __future__ import annotations

from moneyrouter_agent.domain.finance import (
    AnalysisDraft,
    Evidence,
    MetricGap,
    MetricPoint,
    Percentile,
)
from moneyrouter_agent.model.deepseek import format_instruction
from moneyrouter_agent.prompts.finance import (
    ANALYZE_SYSTEM,
    PLAN_SYSTEM,
    REFLECT_SYSTEM,
    render_evidence,
    render_gaps,
    render_metrics,
)

ALL_PROMPTS = {
    "PLAN_SYSTEM": PLAN_SYSTEM,
    "REFLECT_SYSTEM": REFLECT_SYSTEM,
    "ANALYZE_SYSTEM": ANALYZE_SYSTEM,
}


def _metric() -> MetricPoint:
    return MetricPoint(
        key="hs300_pe_ttm",
        label="沪深300 滚动市盈率",
        category="权益估值",
        value=12.48,
        unit="倍",
        as_of="2026-09-30",
        source="乐咕乐股（月度）",
        method="TTM 滚动市盈率",
        change="-0.56倍",
        percentiles=[
            Percentile(window="5y", value=70.5, start="2021-09-30", observations=61),
            Percentile(window="all", value=49.6, start="2005-04-29", observations=258),
        ],
    )


# --------------------------------------------------------------------------- #
# 职责边界
# --------------------------------------------------------------------------- #
def test_each_prompt_owns_exactly_one_job():
    assert "只决定" in PLAN_SYSTEM and "不做分析" in PLAN_SYSTEM
    assert "只做" in REFLECT_SYSTEM and "不写结论" in REFLECT_SYSTEM
    assert "数值必须来自指标表" in ANALYZE_SYSTEM


def test_plan_prompt_narrows_search_to_what_metrics_cannot_cover():
    """指标已经由代码取回，搜索的职责必须收窄，否则会重复劳动。"""
    assert "已经" in PLAN_SYSTEM and "指标" in PLAN_SYSTEM
    assert "货币政策" in PLAN_SYSTEM and "地缘政治" in PLAN_SYSTEM
    assert "不要检索任何具体的指数点位" in PLAN_SYSTEM


def test_reflect_prompt_judges_only_non_numeric_part():
    assert "非数值" in REFLECT_SYSTEM
    assert "最多 3 条" in REFLECT_SYSTEM
    assert "留空" in REFLECT_SYSTEM


# --------------------------------------------------------------------------- #
# 合规红线
# --------------------------------------------------------------------------- #
def test_analyze_prompt_has_compliance_boundary():
    for banned in ("稳赚", "保本高息", "必赚"):
        assert banned in ANALYZE_SYSTEM, f"应显式点名禁用：{banned}"
    assert "涨跌预测" in ANALYZE_SYSTEM
    assert "不推荐任何具体产品" in ANALYZE_SYSTEM


def test_analyze_prompt_forbids_inventing_numbers():
    assert "原样使用它的数值、单位与时点" in ANALYZE_SYSTEM
    assert "不要自己计算" in ANALYZE_SYSTEM
    assert "不要凭印象补一个" in ANALYZE_SYSTEM


def test_analyze_prompt_separates_opinion_from_fact():
    assert "观点" in ANALYZE_SYSTEM and "事实" in ANALYZE_SYSTEM


def test_plan_prompt_excludes_personal_advice_and_privacy():
    assert "个股买卖建议" in PLAN_SYSTEM
    assert "个人信息" in PLAN_SYSTEM


def test_all_prompts_are_chinese_only():
    for name, text in ALL_PROMPTS.items():
        assert "全程中文" in text, f"{name} 缺少中文约束"


# --------------------------------------------------------------------------- #
# 不掺管道机制
# --------------------------------------------------------------------------- #
def test_no_field_names_in_finance_prompts():
    """字段格式由 schema 在模型层注入，prompt 里不出现字段清单。"""
    banned = (
        "json",
        "JSON",
        "freshness",
        "sufficient",
        "follow_up_queries",
        "headline",
        "sections",
        "narrative",
        "metric_keys",
        "source_ids",
        "caveats",
        "implications",
        "round_limit",
        "thread_id",
        "messages",
    )
    for name, text in ALL_PROMPTS.items():
        for token in banned:
            assert token not in text, f"{name} 里混入了字段名或管道内容：{token}"


def test_field_contract_lives_in_model_layer_and_expands_object_arrays():
    text = format_instruction(AnalysisDraft).content

    assert "json" in text.lower()
    for nested in (
        "sections.category",
        "sections.narrative",
        "sections.metric_keys",
        "sections.source_ids",
        "implications",
        "caveats",
    ):
        assert nested in text, f"格式说明缺少嵌套字段：{nested}"


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def test_render_metrics_carries_key_unit_method_and_percentiles():
    text = render_metrics([_metric()])

    assert "[hs300_pe_ttm]" in text
    assert "12.48倍" in text
    assert "时点 2026-09-30" in text
    assert "口径：TTM 滚动市盈率" in text
    assert "来源：乐咕乐股（月度）" in text
    assert "较上期 -0.56倍" in text
    # 分位必须带窗口名称、起点与样本数——否则不可复现
    assert "近 5 年分位 70.5%" in text and "2021-09-30" in text and "61 个样本" in text
    assert "全历史分位 49.6%" in text


def test_render_metrics_groups_by_category():
    text = render_metrics([_metric()])
    assert "## 权益估值" in text


def test_render_metrics_handles_empty():
    assert "没有取到任何指标" in render_metrics([])


def test_render_gaps_lists_reasons():
    text = render_gaps([MetricGap(key="vix", label="VIX 恐慌指数", reason="数据源未覆盖")])
    assert "VIX 恐慌指数" in text and "数据源未覆盖" in text
    assert render_gaps([]) == "（无）"


def test_render_evidence_numbers_every_item():
    evidence = [
        Evidence(id=1, topic="宏观与市场环境", query="检索甲", title="标题甲", url="https://a.example/1"),
        Evidence(id=2, topic="跨资产行情", query="检索乙", title="标题乙", url="https://a.example/2"),
    ]
    text = render_evidence(evidence)

    assert "[1] 标题甲" in text and "[2] 标题乙" in text
    assert "检索乙" in text
    assert "没有检索到任何材料" in render_evidence([])
