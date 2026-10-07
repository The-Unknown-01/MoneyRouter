"""金融情况 Agent 门面。

用法::

    agent = FinanceAgent()
    result = agent.briefing("给一名大三学生做三年期稳健规划，关注当前环境")

    result.briefing.metrics          # 指标表（代码取回，带单位/时点/口径/分位）
    result.briefing.analysis.headline  # 当月情况分析
    result.briefing.sources          # 被引用到的来源（标题 + 链接）

它是**一次性任务**，没有多轮与确认环节——产出的是给方案 Agent 用的背景材料，
不是给用户直接看的东西。

两条降级线互相独立：

- 模型不可用 → 仍会取到完整指标表，只是没有分析文字；
- 数据源不可用 → 仍会有分析与来源，缺口如实列在 ``briefing.missing`` 里。
两条路径都**不会**让模型凭记忆补数值。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from .config import MarketDataSettings, SearchSettings, Settings
from .domain.finance import (
    TOPICS,
    AnalysisDraft,
    FinanceBriefing,
    FinanceBriefingResult,
    Reflection,
    SearchPlan,
)
from .graph.finance_build import build_finance_graph
from .model.deepseek import make_schema_runner
from .tools.bocha import Searcher, make_searcher
from .tools.market_data import FetchOutcome, MarketDataClient

DEFAULT_RECURSION_LIMIT = 25


def _unavailable_runner(messages: list[Any]) -> Any:
    """没有可用模型密钥时的 runner：抛错，让节点走降级。"""
    raise RuntimeError("DeepSeek 不可用：未配置 API Key")


class FinanceAgent:
    """「金融情况」子能力的门面。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        search_settings: SearchSettings | None = None,
        market_settings: MarketDataSettings | None = None,
        plan_runner: Callable[[list[Any]], Any] | None = None,
        reflect_runner: Callable[[list[Any]], Any] | None = None,
        analyze_runner: Callable[[list[Any]], Any] | None = None,
        searcher: Searcher | None = None,
        metrics_fetcher: Callable[[], FetchOutcome] | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.search_settings = search_settings or SearchSettings.from_env()
        self.market_settings = market_settings or MarketDataSettings.from_env()

        degraded_model = self.settings.degraded
        if plan_runner is None:
            plan_runner = (
                _unavailable_runner
                if degraded_model
                else make_schema_runner(self.settings, SearchPlan)
            )
        if reflect_runner is None:
            reflect_runner = (
                _unavailable_runner
                if degraded_model
                else make_schema_runner(self.settings, Reflection)
            )
        if analyze_runner is None:
            analyze_runner = (
                _unavailable_runner
                if degraded_model
                else make_schema_runner(self.settings, AnalysisDraft)
            )
        if searcher is None:
            searcher = make_searcher(self.search_settings)
        if metrics_fetcher is None:
            market_client = MarketDataClient(self.market_settings)
            metrics_fetcher = market_client.fetch

        self.graph = build_finance_graph(
            metrics_fetcher=metrics_fetcher,
            plan_runner=plan_runner,
            searcher=searcher,
            reflect_runner=reflect_runner,
            analyze_runner=analyze_runner,
            drop_low_tier=self.search_settings.drop_low_tier,
        )

    # ------------------------------------------------------------------ #
    def briefing(
        self,
        request: str,
        *,
        as_of: str | None = None,
        period: str | None = None,
        focus: list[str] | None = None,
    ) -> FinanceBriefingResult:
        """生成一份金融简报。

        :param request: 需求描述（"给谁、做什么规划"），模型据此决定检索方向与叙述重点。
        :param as_of: 信息基准日期，默认今天。
        :param period: 统计期间（YYYY-MM），默认取 ``as_of`` 所在月份。
        :param focus: 本次关注的检索方面，默认三个方面全要。
        """
        baseline = as_of or date.today().strftime("%Y-%m-%d")
        month = period or baseline[:7]
        config = {"recursion_limit": DEFAULT_RECURSION_LIMIT}

        state = {
            "request": request,
            "focus": list(focus) if focus else list(TOPICS),
            "as_of": baseline,
            "period": month,
            "round_limit": self.search_settings.max_rounds,
        }
        try:
            final = self.graph.invoke(state, config)
        except Exception as exc:  # noqa: BLE001 - 不抛给调用方
            return FinanceBriefingResult(
                request=request,
                briefing=FinanceBriefing(
                    as_of=baseline,
                    period=month,
                    degraded=True,
                    error=f"{type(exc).__name__}: {exc}",
                ),
                trace=[f"运行失败：{type(exc).__name__}: {exc}"],
            )

        briefing = final.get("briefing") or FinanceBriefing(as_of=baseline, period=month)
        return FinanceBriefingResult(
            request=request,
            briefing=briefing,
            trace=list(final.get("trace") or []),
            reasoning=list(final.get("reasoning") or []),
        )
