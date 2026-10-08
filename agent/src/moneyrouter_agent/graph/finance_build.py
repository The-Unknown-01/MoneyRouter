"""金融情况子图装配。

    START → fetch_metrics → plan → search ──(有材料)──→ reflect ──够用/达上限──→ analyze → compose → END
                                        └──(零材料)──────────────────────────────→ analyze
                                                 reflect ──不够且有补搜意图──→ search（回环）

两条输入线互不阻塞：

- ``fetch_metrics`` 走结构化数据源，某个指标失败只进缺口清单；
- ``plan → search`` 走联网检索，搜不到材料只会让分析少掉"政策与事件"那一段。

``compose`` 用代码装配最终简报，并校验分析里引用的指标标识与材料编号是否真实存在。

这个子图是一次性任务，默认不挂检查点。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from ..tools.bocha import Searcher
from .finance_nodes import (
    AnalyzeRunner,
    MetricsFetcher,
    PlanRunner,
    ReflectRunner,
    make_analyze,
    make_compose,
    make_fetch_metrics,
    make_plan,
    make_reflect,
    make_search,
    route_after_reflect,
    route_after_search,
)
from .finance_state import FinanceState

FETCH_METRICS = "fetch_metrics"
PLAN = "plan"
SEARCH = "search"
REFLECT = "reflect"
ANALYZE = "analyze"
COMPOSE = "compose"


def build_finance_graph(
    *,
    metrics_fetcher: MetricsFetcher,
    plan_runner: PlanRunner,
    searcher: Searcher,
    reflect_runner: ReflectRunner,
    analyze_runner: AnalyzeRunner,
    drop_low_tier: bool = True,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译金融情况子图。"""
    builder = StateGraph(FinanceState)
    builder.add_node(FETCH_METRICS, make_fetch_metrics(metrics_fetcher))
    builder.add_node(PLAN, make_plan(plan_runner))
    builder.add_node(SEARCH, make_search(searcher, drop_low_tier=drop_low_tier))
    builder.add_node(REFLECT, make_reflect(reflect_runner))
    builder.add_node(ANALYZE, make_analyze(analyze_runner))
    builder.add_node(COMPOSE, make_compose())

    builder.add_edge(START, FETCH_METRICS)
    builder.add_edge(FETCH_METRICS, PLAN)
    builder.add_edge(PLAN, SEARCH)
    builder.add_conditional_edges(
        SEARCH, route_after_search, {REFLECT: REFLECT, ANALYZE: ANALYZE}
    )
    builder.add_conditional_edges(
        REFLECT, route_after_reflect, {SEARCH: SEARCH, ANALYZE: ANALYZE}
    )
    builder.add_edge(ANALYZE, COMPOSE)
    builder.add_edge(COMPOSE, END)

    return builder.compile(checkpointer=checkpointer)
