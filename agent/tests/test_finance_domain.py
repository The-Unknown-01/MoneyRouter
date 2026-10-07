"""金融简报领域模型：指标注册表、引用校验、降级语义。"""

from __future__ import annotations

from moneyrouter_agent.domain.finance import (
    CATEGORIES,
    DEFAULT_QUERIES,
    METRIC_CATEGORIES,
    METRIC_SPECS,
    TOPICS,
    AnalysisDraft,
    AnalysisSection,
    Evidence,
    MetricGap,
    MetricPoint,
    Percentile,
    category_rank,
    compose_briefing,
)


def _metric(key: str, value: float = 1.0) -> MetricPoint:
    spec = METRIC_SPECS[key]
    return MetricPoint(
        key=spec.key,
        label=spec.label,
        category=spec.category,
        value=value,
        unit=spec.unit,
        as_of="2026-09-30",
        source="测试来源",
        method=spec.method,
        percentiles=[Percentile(window="5y", value=50.0, start="2021-09-30", observations=60)],
    )


def _evidence(index: int) -> Evidence:
    return Evidence(
        id=index,
        topic="宏观与市场环境",
        title=f"标题{index}",
        url=f"https://a.example/{index}",
        site="示例站点",
        published_at="2026-10-01",
    )


def _draft(*sections: AnalysisSection, headline: str = "总述", caveats=None) -> AnalysisDraft:
    return AnalysisDraft(
        headline=headline, sections=list(sections), implications=[], caveats=caveats or []
    )


def _compose(draft, *, metrics=None, evidence=None, gaps=None):
    return compose_briefing(
        as_of="2026-10-07",
        period="2026-10",
        metrics=list(metrics if metrics is not None else [_metric("cn_10y_yield")]),
        gaps=list(gaps or []),
        evidence=list(evidence if evidence is not None else [_evidence(1)]),
        draft=draft,
    )


# --------------------------------------------------------------------------- #
# 指标注册表
# --------------------------------------------------------------------------- #
def test_registry_is_internally_consistent():
    for key, spec in METRIC_SPECS.items():
        assert key == spec.key
        assert spec.label and spec.unit
        assert spec.category in METRIC_CATEGORIES, f"{key} 的分组非法：{spec.category}"


def test_every_registered_metric_has_a_builder():
    """注册了就一定要实现取数——否则它只会静默地一直进缺口。"""
    from moneyrouter_agent.tools.market_data import _METRIC_BUILDERS

    assert set(METRIC_SPECS) == set(_METRIC_BUILDERS)


def test_categories_put_metric_groups_before_policy():
    assert category_rank("无风险基准") < category_rank("权益估值") < category_rank("政策与事件")
    assert set(METRIC_CATEGORIES).issubset(set(CATEGORIES))
    assert "政策与事件" not in METRIC_CATEGORIES  # 它只能靠搜索


def test_default_queries_cover_every_topic():
    assert {item.topic for item in DEFAULT_QUERIES} == set(TOPICS)


def test_metric_point_requires_a_value():
    """指标值不允许为空——取不到就不该生成指标，而是进缺口清单。"""
    with_value = _metric("hs300_pe_ttm", 12.48)
    assert isinstance(with_value.value, float)
    assert with_value.unit == "倍"


# --------------------------------------------------------------------------- #
# 引用校验：引用不上就剔除
# --------------------------------------------------------------------------- #
def test_compose_strips_metric_keys_that_do_not_exist():
    draft = _draft(
        AnalysisSection(
            category="权益估值",
            narrative="估值处于中间分位。",
            metric_keys=["hs300_pe_ttm", "不存在的指标"],
            source_ids=[1],
        )
    )
    briefing = _compose(draft, metrics=[_metric("hs300_pe_ttm")], evidence=[_evidence(1)])

    assert briefing.analysis.sections[0].metric_keys == ["hs300_pe_ttm"]
    assert any("指标引用不存在" in c for c in briefing.analysis.caveats)


def test_compose_strips_source_ids_that_do_not_exist():
    draft = _draft(
        AnalysisSection(category="政策与事件", narrative="有政策发布。", source_ids=[1, 99])
    )
    briefing = _compose(draft, evidence=[_evidence(1)])

    assert briefing.analysis.sections[0].source_ids == [1]
    assert any("材料引用不存在" in c for c in briefing.analysis.caveats)


def test_compose_keeps_only_referenced_sources():
    draft = _draft(AnalysisSection(category="政策与事件", narrative="x", source_ids=[2]))
    briefing = _compose(draft, evidence=[_evidence(1), _evidence(2)])

    assert [s.id for s in briefing.sources] == [2]
    assert briefing.sources[0].title == "标题2"
    assert briefing.sources[0].url.startswith("http")


def test_every_listed_source_has_title_and_url():
    """对齐 PRD：来源必须有 URL + Title。"""
    draft = _draft(AnalysisSection(category="政策与事件", narrative="x", source_ids=[1]))
    for source in _compose(draft).sources:
        assert source.title and source.url.startswith("http")


# --------------------------------------------------------------------------- #
# 结构
# --------------------------------------------------------------------------- #
def test_sections_are_sorted_by_category_order():
    draft = _draft(
        AnalysisSection(category="政策与事件", narrative="c"),
        AnalysisSection(category="无风险基准", narrative="a"),
        AnalysisSection(category="权益估值", narrative="b"),
    )
    briefing = _compose(draft)

    assert [s.category for s in briefing.analysis.sections] == [
        "无风险基准",
        "权益估值",
        "政策与事件",
    ]


def test_metrics_are_sorted_by_category_then_key():
    metrics = [_metric("hs300_pe_ttm"), _metric("cn_10y_yield"), _metric("cn_2y_yield")]
    briefing = _compose(_draft(), metrics=metrics)

    assert [m.key for m in briefing.metrics] == ["cn_10y_yield", "cn_2y_yield", "hs300_pe_ttm"]


def test_period_and_as_of_are_set_from_code_not_model():
    briefing = _compose(AnalysisDraft(headline="h"))
    assert briefing.as_of == "2026-10-07"
    assert briefing.analysis.period == "2026-10"
    assert briefing.analysis.headline == "h"


# --------------------------------------------------------------------------- #
# 降级
# --------------------------------------------------------------------------- #
def test_no_draft_still_keeps_the_whole_metric_table():
    """分析失败也不能丢掉指标表——那正是要交给方案复算的东西。"""
    briefing = _compose(None, metrics=[_metric("cn_10y_yield"), _metric("hs300_pe_ttm")])

    assert [m.key for m in briefing.metrics] == ["cn_10y_yield", "hs300_pe_ttm"]
    assert briefing.analysis.sections == []
    assert any("未能生成分析" in c for c in briefing.analysis.caveats)


def test_gaps_are_surfaced_in_caveats_without_technical_jargon():
    gaps = [MetricGap(key="vix", label="VIX 恐慌指数", reason="ProxyError: push2.eastmoney.com")]
    briefing = _compose(_draft(), gaps=gaps)

    assert len(briefing.missing) == 1
    assert briefing.missing[0].reason.startswith("ProxyError")  # 技术原因保留给排查
    caveat = next(c for c in briefing.analysis.caveats if "未取到" in c)
    assert "VIX 恐慌指数" in caveat
    assert "ProxyError" not in caveat


def test_degraded_caveat_is_generic_not_the_raw_error():
    """详细原因可能带上游 API 原文（历史上出现过回显密钥），只留在 error 里。"""
    briefing = compose_briefing(
        as_of="2026-10-07",
        period="2026-10",
        metrics=[_metric("cn_10y_yield")],
        gaps=[],
        evidence=[],
        draft=_draft(),
        degraded=True,
        error="规划降级：RuntimeError: secret-abc 无效",
    )

    assert briefing.error and "secret-abc" in briefing.error
    assert all("secret-abc" not in caveat for caveat in briefing.analysis.caveats)
    assert any("降级路径" in c for c in briefing.analysis.caveats)


def test_empty_briefing_flags_missing_information():
    briefing = compose_briefing(
        as_of="2026-10-07", period="2026-10", metrics=[], gaps=[], evidence=[], draft=_draft()
    )

    assert any("未取到指标" in c and "未获取到网络材料" in c for c in briefing.analysis.caveats)


def test_caveats_are_deduplicated():
    briefing = _compose(_draft(caveats=["同一句提醒", "同一句提醒"]))
    assert briefing.analysis.caveats.count("同一句提醒") == 1
