"""金融情况子图：六个节点、补搜回路、两条独立降级线。全部离线。"""

from __future__ import annotations

from typing import Any

from moneyrouter_agent.config import MarketDataSettings, SearchSettings, Settings
from moneyrouter_agent.domain.finance import (
    METRIC_SPECS,
    AnalysisDraft,
    AnalysisSection,
    MetricGap,
    MetricPoint,
    Percentile,
    PlannedQuery,
    Reflection,
    SearchPlan,
)
from moneyrouter_agent.finance_agent import FinanceAgent
from moneyrouter_agent.tools.bocha import SearchHit, SearchOutcome
from moneyrouter_agent.tools.market_data import FetchOutcome


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
        percentiles=[Percentile(window="5y", value=42.0, start="2021-09-30", observations=60)],
    )


def _hit(query: str, url: str, *, topic: str = "宏观与市场环境") -> SearchHit:
    return SearchHit(
        query=query, topic=topic, title="标题", url=url,
        site="示例站点", published_at="2026-10-01", snippet="片段",
    )


def _q(text: str, topic: str = "宏观与市场环境") -> PlannedQuery:
    return PlannedQuery(query=text, topic=topic)


class ScriptedRunner:
    def __init__(self, values: list[Any]) -> None:
        self._values = list(values)
        self.calls: list[list[Any]] = []

    def __call__(self, messages: list[Any]) -> Any:
        self.calls.append(messages)
        if not self._values:
            raise AssertionError("脚本化 runner 已耗尽，但图又调用了一次")
        value = self._values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class RecordingSearcher:
    def __init__(
        self,
        batches: list[tuple[list[SearchHit], list[str]]],
        *,
        unavailable: str | None = None,
    ) -> None:
        self._batches = list(batches)
        self._unavailable = unavailable
        self.seen: list[list[PlannedQuery]] = []

    def __call__(self, queries: list[PlannedQuery]) -> SearchOutcome:
        self.seen.append(list(queries))
        hits, errors = self._batches.pop(0) if self._batches else ([], [])
        return SearchOutcome(hits=hits, errors=errors, unavailable=self._unavailable)


def _agent(
    *,
    plans: list[Any],
    reflections: list[Any],
    drafts: list[Any],
    batches: list[tuple[list[SearchHit], list[str]]],
    metrics: list[MetricPoint] | None = None,
    gaps: list[MetricGap] | None = None,
    fetch_error: Exception | None = None,
    max_rounds: int = 2,
    search_unavailable: str | None = None,
) -> tuple[FinanceAgent, RecordingSearcher]:
    searcher = RecordingSearcher(batches, unavailable=search_unavailable)
    payload = list(metrics) if metrics is not None else [_metric("cn_10y_yield")]
    gap_payload = list(gaps or [])

    def fetcher() -> tuple[list[MetricPoint], list[MetricGap]]:
        if fetch_error is not None:
            raise fetch_error
        return payload, gap_payload

    agent = FinanceAgent(
        settings=Settings(api_key="fake-key"),
        search_settings=SearchSettings(api_key="fake-key", max_rounds=max_rounds),
        market_settings=MarketDataSettings(enabled=False),
        plan_runner=ScriptedRunner(plans),
        reflect_runner=ScriptedRunner(reflections),
        analyze_runner=ScriptedRunner(drafts),
        searcher=searcher,
        metrics_fetcher=fetcher,
    )
    return agent, searcher


def _draft(*sections: AnalysisSection, headline: str = "总述") -> AnalysisDraft:
    return AnalysisDraft(headline=headline, sections=list(sections), implications=["含义一"])


# --------------------------------------------------------------------------- #
# 正常链路
# --------------------------------------------------------------------------- #
def test_happy_path_produces_metrics_and_analysis():
    agent, searcher = _agent(
        plans=[SearchPlan(queries=[_q("2026年10月 央行 流动性")])],
        reflections=[Reflection(sufficient=True)],
        drafts=[
            _draft(
                AnalysisSection(
                    category="无风险基准",
                    narrative="国债收益率处于低位。",
                    metric_keys=["cn_10y_yield"],
                    source_ids=[1],
                )
            )
        ],
        batches=[([_hit("2026年10月 央行 流动性", "https://a.example/1")], [])],
        metrics=[_metric("cn_10y_yield", 1.68)],
    )
    result = agent.briefing("给学生做规划", as_of="2026-10-07")
    briefing = result.briefing

    assert briefing.degraded is False
    assert briefing.as_of == "2026-10-07"
    assert briefing.period == "2026-10"
    assert [m.value for m in briefing.metrics] == [1.68]
    assert briefing.analysis.headline == "总述"
    assert briefing.analysis.sections[0].metric_keys == ["cn_10y_yield"]
    assert [s.url for s in briefing.sources] == ["https://a.example/1"]
    assert len(searcher.seen) == 1


def test_period_defaults_to_as_of_month():
    agent, _ = _agent(
        plans=[SearchPlan(queries=[_q("甲")])],
        reflections=[Reflection(sufficient=True)],
        drafts=[_draft()],
        batches=[([], [])],
    )
    assert agent.briefing("x", as_of="2026-03-15").briefing.period == "2026-03"


# --------------------------------------------------------------------------- #
# 指标线降级
# --------------------------------------------------------------------------- #
def test_metric_gap_does_not_break_the_briefing():
    agent, _ = _agent(
        plans=[SearchPlan(queries=[_q("甲")])],
        reflections=[Reflection(sufficient=True)],
        drafts=[_draft(AnalysisSection(category="权益估值", narrative="x"))],
        batches=[([_hit("甲", "https://a.example/1")], [])],
        metrics=[_metric("cn_10y_yield")],
        gaps=[MetricGap(key="vix", label="VIX 恐慌指数", reason="数据源未覆盖")],
    )
    briefing = agent.briefing("x").briefing

    assert len(briefing.metrics) == 1
    assert [g.key for g in briefing.missing] == ["vix"]
    assert any("VIX" in c for c in briefing.analysis.caveats)


def test_fetcher_exception_degrades_but_keeps_going():
    agent, _ = _agent(
        plans=[SearchPlan(queries=[_q("甲")])],
        reflections=[Reflection(sufficient=True)],
        drafts=[_draft(AnalysisSection(category="政策与事件", narrative="有政策发布。", source_ids=[1]))],
        batches=[([_hit("甲", "https://a.example/1")], [])],
        fetch_error=RuntimeError("数据层炸了"),
    )
    result = agent.briefing("x")

    assert result.briefing.degraded is True
    assert result.briefing.metrics == []
    assert "数据层不可用" in " ".join(result.trace)
    # 详细原因留在 error 里（轨迹只写异常类型，避免把上游原文带出去）
    assert "数据层炸了" in (result.briefing.error or "")
    # 检索线不受影响
    assert [s.url for s in result.briefing.sources] == ["https://a.example/1"]


# --------------------------------------------------------------------------- #
# 检索线
# --------------------------------------------------------------------------- #
def test_reflection_triggers_one_extra_search_round():
    agent, searcher = _agent(
        plans=[SearchPlan(queries=[_q("甲")])],
        reflections=[Reflection(sufficient=False, follow_up_queries=[_q("乙")])],
        drafts=[_draft()],
        batches=[
            ([_hit("甲", "https://a.example/1")], []),
            ([_hit("乙", "https://b.example/2")], []),
        ],
    )
    result = agent.briefing("x")

    assert len(searcher.seen) == 2
    assert "search#2" in " ".join(result.trace)


def test_round_limit_stops_the_loop():
    agent, searcher = _agent(
        plans=[SearchPlan(queries=[_q("甲")])],
        reflections=[
            Reflection(sufficient=False, follow_up_queries=[_q("乙")]),
            Reflection(sufficient=False, follow_up_queries=[_q("丙")]),
        ],
        drafts=[_draft()],
        batches=[
            ([_hit("甲", "https://a.example/1")], []),
            ([_hit("乙", "https://a.example/2")], []),
        ],
        max_rounds=2,
    )
    result = agent.briefing("x")

    assert len(searcher.seen) == 2
    assert "search#3" not in " ".join(result.trace)


def test_no_evidence_skips_reflection():
    reflect_runner = ScriptedRunner([])  # 被调用就会抛错
    agent = FinanceAgent(
        settings=Settings(api_key="fake-key"),
        search_settings=SearchSettings(api_key="fake-key"),
        market_settings=MarketDataSettings(enabled=False),
        plan_runner=ScriptedRunner([SearchPlan(queries=[_q("甲")])]),
        reflect_runner=reflect_runner,
        analyze_runner=ScriptedRunner([_draft()]),
        searcher=RecordingSearcher([([], ["网络不可用"])]),
        metrics_fetcher=lambda: ([_metric("cn_10y_yield")], []),
    )
    result = agent.briefing("x")

    assert reflect_runner.calls == []
    # 指标表仍在
    assert len(result.briefing.metrics) == 1


def test_search_service_failure_is_told_apart_from_no_news():
    """检索服务不可用（配额 / 鉴权）必须与"这次确实没搜到"分开说：
    前者要人去充值或换密钥，后者只是本期没有相关报道。"""
    analyze_runner = ScriptedRunner([_draft()])
    agent = FinanceAgent(
        settings=Settings(api_key="fake-key"),
        search_settings=SearchSettings(api_key="fake-key"),
        market_settings=MarketDataSettings(enabled=False),
        plan_runner=ScriptedRunner([SearchPlan(queries=[_q("甲")])]),
        reflect_runner=ScriptedRunner([Reflection(sufficient=True)]),
        analyze_runner=analyze_runner,
        searcher=RecordingSearcher(
            [([], ["甲 → SearchError: 博查返回 HTTP 403：配额或套餐额度不足"])],
            unavailable="搜索服务配额或套餐额度不足",
        ),
        metrics_fetcher=lambda: ([_metric("cn_10y_yield")], []),
    )
    result = agent.briefing("x")

    caveats = " ".join(result.briefing.analysis.caveats)
    assert "检索服务不可用" in caveats
    assert "搜索服务配额或套餐额度不足" in caveats
    assert "未能获取" in caveats
    assert any("检索服务不可用" in line for line in result.trace)

    # 分析提示词也要说明"是服务不可用、不是没有新闻"，
    # 否则模型会把"没有政策或事件"写成事实结论
    analyze_human = analyze_runner.calls[0][1].content
    assert "检索服务本身不可用" in analyze_human


def test_plain_missing_evidence_does_not_claim_service_failure():
    """对照组：普通"没搜到材料"不该冒出"服务不可用"的字样。"""
    agent = FinanceAgent(
        settings=Settings(api_key="fake-key"),
        search_settings=SearchSettings(api_key="fake-key"),
        market_settings=MarketDataSettings(enabled=False),
        plan_runner=ScriptedRunner([SearchPlan(queries=[_q("甲")])]),
        reflect_runner=ScriptedRunner([Reflection(sufficient=True)]),
        analyze_runner=ScriptedRunner([_draft()]),
        searcher=RecordingSearcher([([], ["网络抖动"])]),
        metrics_fetcher=lambda: ([_metric("cn_10y_yield")], []),
    )
    result = agent.briefing("x")

    assert "检索服务不可用" not in " ".join(result.briefing.analysis.caveats)


def test_cached_metrics_are_disclosed_in_caveats():
    """源站故障时用了落盘缓存，必须在用户可见提示里说明——不能让人以为数据是刚取的。"""
    stale_label = METRIC_SPECS["cn_10y_yield"].label
    agent = FinanceAgent(
        settings=Settings(api_key="fake-key"),
        search_settings=SearchSettings(api_key="fake-key"),
        market_settings=MarketDataSettings(enabled=False),
        plan_runner=ScriptedRunner([SearchPlan(queries=[_q("甲")])]),
        reflect_runner=ScriptedRunner([Reflection(sufficient=True)]),
        analyze_runner=ScriptedRunner([_draft()]),
        searcher=RecordingSearcher([([_hit("甲", "https://a.example/1")], [])]),
        metrics_fetcher=lambda: FetchOutcome(
            metrics=[_metric("cn_10y_yield")], gaps=[], stale=[stale_label]
        ),
    )
    result = agent.briefing("x")

    assert any(stale_label in line for line in result.trace)
    caveats = " ".join(result.briefing.analysis.caveats)
    assert "缓存数据" in caveats
    assert stale_label in caveats


def test_reflection_failure_goes_straight_to_analysis():
    agent, searcher = _agent(
        plans=[SearchPlan(queries=[_q("甲")])],
        reflections=[RuntimeError("判断失败")],
        drafts=[_draft()],
        batches=[([_hit("甲", "https://a.example/1")], [])],
    )
    result = agent.briefing("x")

    assert len(searcher.seen) == 1
    assert "reflect：判断失败" in " ".join(result.trace)


def test_searcher_exception_degrades_without_raising():
    class ExplodingSearcher:
        def __call__(self, _queries: list[PlannedQuery]) -> tuple[list[SearchHit], list[str]]:
            raise RuntimeError("检索层炸了")

    agent = FinanceAgent(
        settings=Settings(api_key="fake-key"),
        search_settings=SearchSettings(api_key="fake-key"),
        market_settings=MarketDataSettings(enabled=False),
        plan_runner=ScriptedRunner([SearchPlan(queries=[_q("甲")])]),
        reflect_runner=ScriptedRunner([Reflection(sufficient=True)]),
        analyze_runner=ScriptedRunner([_draft()]),
        searcher=ExplodingSearcher(),
        metrics_fetcher=lambda: ([_metric("cn_10y_yield")], []),
    )
    result = agent.briefing("x")

    assert "检索层炸了" in " ".join(result.trace)
    assert len(result.briefing.metrics) == 1  # 指标线不受影响


# --------------------------------------------------------------------------- #
# 分析线
# --------------------------------------------------------------------------- #
def test_analyze_failure_keeps_the_metric_table():
    agent, _ = _agent(
        plans=[SearchPlan(queries=[_q("甲")])],
        reflections=[Reflection(sufficient=True)],
        drafts=[RuntimeError("分析炸了")],
        batches=[([_hit("甲", "https://a.example/1")], [])],
        metrics=[_metric("cn_10y_yield"), _metric("hs300_pe_ttm")],
    )
    briefing = agent.briefing("x").briefing

    assert briefing.degraded is True
    assert [m.key for m in briefing.metrics] == ["cn_10y_yield", "hs300_pe_ttm"]
    assert briefing.analysis.sections == []
    assert "分析降级" in (briefing.error or "")


def test_hallucinated_metric_key_is_stripped_end_to_end():
    agent, _ = _agent(
        plans=[SearchPlan(queries=[_q("甲")])],
        reflections=[Reflection(sufficient=True)],
        drafts=[
            _draft(
                AnalysisSection(
                    category="权益估值",
                    narrative="有依据",
                    metric_keys=["hs300_pe_ttm"],
                    source_ids=[1],
                ),
                AnalysisSection(
                    category="避险资产",
                    narrative="凭空引用",
                    metric_keys=["不存在的指标"],
                    source_ids=[77],
                ),
            )
        ],
        batches=[([_hit("甲", "https://a.example/1")], [])],
        metrics=[_metric("hs300_pe_ttm", 12.48)],
    )
    briefing = agent.briefing("x").briefing

    assert briefing.analysis.sections[0].metric_keys == ["hs300_pe_ttm"]
    assert briefing.analysis.sections[1].metric_keys == []
    assert briefing.analysis.sections[1].source_ids == []
    assert all(s.id == 1 for s in briefing.sources)


# --------------------------------------------------------------------------- #
# 观测
# --------------------------------------------------------------------------- #
def test_trace_covers_every_stage():
    agent, _ = _agent(
        plans=[SearchPlan(queries=[_q("甲")])],
        reflections=[Reflection(sufficient=True)],
        drafts=[_draft()],
        batches=[([_hit("甲", "https://a.example/1")], [])],
    )
    trace = " ".join(agent.briefing("x").trace)

    for stage in ("metrics", "plan", "search#1", "reflect", "analyze", "compose"):
        assert stage in trace


def test_reasoning_is_captured_from_structured_call():
    from moneyrouter_agent.model.deepseek import StructuredCall

    class ReasoningAnalyzeRunner:
        def __call__(self, _messages: list[Any]) -> StructuredCall:
            return StructuredCall(parsed=_draft(), reasoning="先看无风险基准，再看估值。")

    agent = FinanceAgent(
        settings=Settings(api_key="fake-key"),
        search_settings=SearchSettings(api_key="fake-key"),
        market_settings=MarketDataSettings(enabled=False),
        plan_runner=ScriptedRunner([SearchPlan(queries=[_q("甲")])]),
        reflect_runner=ScriptedRunner([Reflection(sufficient=True)]),
        analyze_runner=ReasoningAnalyzeRunner(),
        searcher=RecordingSearcher([([_hit("甲", "https://a.example/1")], [])]),
        metrics_fetcher=lambda: ([_metric("cn_10y_yield")], []),
    )
    assert "先看无风险基准" in " ".join(agent.briefing("x").reasoning)
