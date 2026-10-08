"""金融情况子图的节点：

    fetch_metrics（代码取数）→ plan（拟检索意图）→ search（联网）
    → reflect（够不够，不够补一轮）→ analyze（模型写当月分析）→ compose（代码合成）

六个节点里，``fetch_metrics`` / ``search`` / ``compose`` 不调用模型；
``plan`` / ``reflect`` / ``analyze`` 各做一次结构化 LLM 调用。

两条独立的降级线：

- **指标线**：某个指标取不到 → 进缺口清单，其余照常；
- **检索线**：搜不到材料 → 分析里就没有"政策与事件"那一段，指标仍然完整。

任何一步失败都**不抛出**，而是就地降级并把原因写进轨迹——这条链路的价值是
"给方案提供带依据的背景"，不该因为一次网络抖动就整体不可用。
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from ..domain.finance import (
    DEFAULT_QUERIES,
    MAX_FOLLOW_UP_QUERIES,
    MAX_QUERIES_PER_ROUND,
    TIER_GENERAL,
    TIER_LABELS,
    TIER_LOW,
    TIER_ORDER,
    AnalysisDraft,
    Evidence,
    MetricGap,
    MetricPoint,
    PlannedQuery,
    Reflection,
    SearchPlan,
    classify_source,
    compose_briefing,
)
from ..model.deepseek import StructuredCall
from ..prompts.finance import (
    ANALYZE_SYSTEM,
    PLAN_SYSTEM,
    REFLECT_SYSTEM,
    build_analyze_human,
    build_plan_human,
    build_reflect_human,
)
from ..tools.bocha import Searcher
from ..tools.market_data import FetchOutcome

PlanRunner = Callable[[list[BaseMessage]], Any]
ReflectRunner = Callable[[list[BaseMessage]], Any]
AnalyzeRunner = Callable[[list[BaseMessage]], Any]
# 取数动作：返回 (指标, 缺口)
MetricsFetcher = Callable[[], FetchOutcome]


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _split(result: Any) -> tuple[Any, str | None]:
    """兼容真实模型的 ``StructuredCall`` 与脚本化 runner 的领域对象。"""
    if isinstance(result, StructuredCall):
        return result.parsed, result.reasoning
    return result, None


def _clean_queries(queries: Any, limit: int) -> list[PlannedQuery]:
    out: list[PlannedQuery] = []
    seen: set[str] = set()
    for raw in queries or []:
        item = raw if isinstance(raw, PlannedQuery) else PlannedQuery.model_validate(raw)
        if not item.query or item.query in seen:
            continue
        seen.add(item.query)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _reasoning_line(reasoning: str | None) -> list[str]:
    return [reasoning] if reasoning else []


def _hit_tier(hit: Any) -> str:
    """取一条材料的来源级别；调用方没判定过就现场判一次。

    这样无论材料来自真实检索器（``bocha`` 已判好）还是外部注入的脚本化
    searcher（可能只填了 url/site），入库时都有一致的级别。
    """
    tier = str(getattr(hit, "tier", "") or "")
    if tier in TIER_ORDER:
        return tier
    return classify_source(
        str(getattr(hit, "site", "") or ""), str(getattr(hit, "url", "") or "")
    )


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #
def make_fetch_metrics(fetcher: MetricsFetcher):
    """取数节点：把结构化指标取回来。失败只是记缺口，不阻断整条链路。"""

    def fetch_metrics(state: dict) -> dict:
        try:
            outcome = fetcher()
            metrics, gaps = outcome
            stale = list(getattr(outcome, "stale", None) or [])
        except Exception as exc:  # noqa: BLE001 - 数据层整体不可用时也要能继续
            return {
                "metrics": [],
                "gaps": [],
                "stale_metrics": [],
                "degraded": True,
                "error": f"取数降级：{type(exc).__name__}: {exc}",
                "trace": [f"metrics：数据层不可用（{type(exc).__name__}），跳过结构化取数"],
            }

        trace = [f"metrics：取到 {len(metrics)} 项指标"]
        if stale:
            trace.append("  · 源站不可用，以下指标使用了落盘缓存：" + "、".join(stale))
        if gaps:
            trace.append("  · 未取到：" + "、".join(gap.label for gap in gaps))
        return {
            "metrics": list(metrics),
            "gaps": list(gaps),
            "stale_metrics": stale,
            "trace": trace,
        }

    return fetch_metrics


def make_plan(
    plan_runner: PlanRunner, *, fallback_queries: tuple[PlannedQuery, ...] = DEFAULT_QUERIES
):
    """规划节点：拟检索意图；模型不可用时退回内置意图。"""

    def plan(state: dict) -> dict:
        messages = [
            SystemMessage(content=PLAN_SYSTEM),
            HumanMessage(
                content=build_plan_human(
                    state.get("request") or "",
                    list(state.get("focus") or []),
                    state.get("as_of") or "",
                )
            ),
        ]
        try:
            planned, reasoning = _split(plan_runner(messages))
            if not isinstance(planned, SearchPlan):
                planned = SearchPlan.model_validate(planned)
            queries = _clean_queries(planned.queries, MAX_QUERIES_PER_ROUND)
            if not queries:
                raise ValueError("没有产出任何检索意图")
        except Exception as exc:  # noqa: BLE001 - 降级：用内置意图继续
            fallback = list(fallback_queries)
            return {
                "planned_queries": fallback,
                "pending_queries": fallback,
                "degraded": True,
                "error": f"规划降级：{type(exc).__name__}: {exc}",
                "trace": [f"plan：模型不可用，改用内置的 {len(fallback)} 条检索意图"],
            }

        topics = "、".join(sorted({query.topic for query in queries}))
        return {
            "planned_queries": queries,
            "pending_queries": queries,
            "trace": [f"plan：{len(queries)} 条检索意图（覆盖 {topics}）"],
            "reasoning": _reasoning_line(reasoning),
        }

    return plan


def make_search(searcher: Searcher, *, drop_low_tier: bool = True):
    """取数节点（联网）：并发执行待检索意图，按 URL 去重后编号入库。

    入库前做一次**来源分级**：官方发布与主流媒体排在前面，论坛与自媒体类材料
    默认不进清单——地缘、突发这类题材里它们占比很高，一旦被引用进简报，
    整份材料的可信度就没了。判级别用的是 URL 与站点名，判定规则见
    :func:`domain.finance.classify_source`。

    剔除有一条兜底：若本轮**全部**材料都被判成低级别，就原样保留（只排在最后）。
    宁可让模型看到带瑕疵的线索，也好过整轮空手而归。
    """

    def search(state: dict) -> dict:
        existing = list(state.get("evidence") or [])
        pending = list(state.get("pending_queries") or [])
        round_no = int(state.get("round") or 0) + 1

        if not pending:
            return {
                "round": round_no,
                "pending_queries": [],
                "trace": [f"search#{round_no}：没有待执行的检索意图"],
            }

        try:
            outcome = searcher(pending)
            hits, errors = outcome
            unavailable = getattr(outcome, "unavailable", None)
        except Exception as exc:  # noqa: BLE001 - 整批失败也不抛
            hits, errors, unavailable = [], [f"{type(exc).__name__}: {exc}"], None

        graded = [(_hit_tier(hit), hit) for hit in hits]
        kept = graded
        dropped_low = 0
        if drop_low_tier and graded:
            without_low = [(tier, hit) for tier, hit in graded if tier != TIER_LOW]
            if without_low:
                dropped_low = len(graded) - len(without_low)
                kept = without_low
        # 稳定排序：同级别内保持"意图顺序"，结果可复现
        kept.sort(key=lambda pair: TIER_ORDER.get(pair[0], TIER_ORDER[TIER_GENERAL]))

        seen = {item.url for item in existing}
        new_items: list[Evidence] = []
        counts: dict[str, int] = {}
        next_id = max((item.id for item in existing), default=0) + 1
        for tier, hit in kept:
            if not hit.url or hit.url in seen:
                continue
            seen.add(hit.url)
            counts[tier] = counts.get(tier, 0) + 1
            new_items.append(
                Evidence(
                    id=next_id,
                    topic=hit.topic,
                    query=hit.query,
                    title=hit.title,
                    url=hit.url,
                    site=hit.site,
                    published_at=hit.published_at,
                    snippet=hit.snippet,
                    tier=tier,
                )
            )
            next_id += 1

        trace = [
            f"search#{round_no}：发出 {len(pending)} 条检索 → 新增材料 {len(new_items)} 条"
            f"（累计 {len(existing) + len(new_items)}）"
        ]
        if counts:
            breakdown = "、".join(
                f"{TIER_LABELS.get(tier) or tier} {counts[tier]}"
                for tier in sorted(counts, key=lambda k: TIER_ORDER.get(k, len(TIER_ORDER)))
            )
            trace.append(f"  · 来源级别：{breakdown}")
        if dropped_low:
            trace.append(f"  · 已剔除 {dropped_low} 条论坛/自媒体材料")
        if unavailable:
            trace.append(f"  · 检索服务不可用：{unavailable}")
        trace += [f"  · 失败：{err}" for err in errors]

        patch: dict[str, Any] = {
            "evidence": existing + new_items,
            "pending_queries": [],
            "round": round_no,
            "trace": trace,
        }
        if unavailable:
            patch["search_unavailable"] = unavailable
        return patch

    return search


def make_reflect(reflect_runner: ReflectRunner):
    """反思节点：判断材料够不够；不够就给出补搜意图。"""

    def reflect(state: dict) -> dict:
        messages = [
            SystemMessage(content=REFLECT_SYSTEM),
            HumanMessage(
                content=build_reflect_human(
                    state.get("request") or "",
                    list(state.get("evidence") or []),
                    int(state.get("round") or 1),
                    int(state.get("round_limit") or 1),
                )
            ),
        ]
        try:
            reflection, reasoning = _split(reflect_runner(messages))
            if not isinstance(reflection, Reflection):
                reflection = Reflection.model_validate(reflection)
        except Exception as exc:  # noqa: BLE001 - 判断不了就直接写分析
            return {
                "pending_queries": [],
                "sufficient": True,
                "trace": [f"reflect：判断失败（{type(exc).__name__}），直接进入分析"],
            }

        follow = (
            []
            if reflection.sufficient
            else _clean_queries(reflection.follow_up_queries, MAX_FOLLOW_UP_QUERIES)
        )
        head = "reflect：材料已够" if reflection.sufficient else "reflect：仍需补充"
        if reflection.missing:
            head += "（" + "；".join(reflection.missing) + "）"
        trace = [head]
        if follow:
            trace.append(f"  · 补搜 {len(follow)} 条：" + "；".join(q.query for q in follow))
        return {
            "pending_queries": follow,
            "sufficient": bool(reflection.sufficient),
            "trace": trace,
            "reasoning": _reasoning_line(reasoning),
        }

    return reflect


def make_analyze(analyze_runner: AnalyzeRunner):
    """分析节点：把指标表与材料合成一份当月分析草稿。"""

    def analyze(state: dict) -> dict:
        metrics: list[MetricPoint] = list(state.get("metrics") or [])
        evidence: list[Evidence] = list(state.get("evidence") or [])
        gaps: list[MetricGap] = list(state.get("gaps") or [])
        degraded = bool(state.get("degraded"))
        error = state.get("error")

        messages = [
            SystemMessage(content=ANALYZE_SYSTEM),
            HumanMessage(
                content=build_analyze_human(
                    state.get("request") or "",
                    state.get("as_of") or "",
                    state.get("period") or "",
                    metrics,
                    gaps,
                    evidence,
                    state.get("search_unavailable"),
                )
            ),
        ]
        try:
            raw, reasoning = _split(analyze_runner(messages))
            draft = raw if isinstance(raw, AnalysisDraft) else AnalysisDraft.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - 只保留指标与来源
            return {
                "analysis_draft": None,
                "degraded": True,
                "error": f"分析降级：{type(exc).__name__}: {exc}",
                "trace": ["analyze：生成失败，简报只保留指标与来源"],
            }

        return {
            "analysis_draft": draft,
            "degraded": degraded,
            "error": error,
            "trace": [f"analyze：{len(draft.sections)} 个方面、{len(draft.implications)} 条含义"],
            "reasoning": _reasoning_line(reasoning),
        }

    return analyze


def make_compose():
    """合成节点：装指标表 + 分析 + 来源，并校验两类引用是否真实存在。"""

    def compose(state: dict) -> dict:
        briefing = compose_briefing(
            as_of=state.get("as_of") or "",
            period=state.get("period") or "",
            metrics=list(state.get("metrics") or []),
            gaps=list(state.get("gaps") or []),
            evidence=list(state.get("evidence") or []),
            draft=state.get("analysis_draft"),
            degraded=bool(state.get("degraded")),
            error=state.get("error"),
            search_unavailable=state.get("search_unavailable"),
            stale_metrics=list(state.get("stale_metrics") or []),
        )
        trace = [
            f"compose：指标 {len(briefing.metrics)} 项、缺口 {len(briefing.missing)} 项、"
            f"分析 {len(briefing.analysis.sections)} 段、引用来源 {len(briefing.sources)} 条"
        ]
        return {"briefing": briefing, "trace": trace}

    return compose


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
def route_after_search(state: dict) -> str:
    """一条材料都没取到就直接进分析（对空材料做反思是白烧一次调用）。"""
    return "reflect" if state.get("evidence") else "analyze"


def route_after_reflect(state: dict) -> str:
    if state.get("sufficient"):
        return "analyze"
    if not state.get("pending_queries"):
        return "analyze"
    if int(state.get("round") or 0) >= int(state.get("round_limit") or 1):
        return "analyze"
    return "search"
