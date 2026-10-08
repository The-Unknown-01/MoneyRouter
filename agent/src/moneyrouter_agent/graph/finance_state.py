"""金融情况子图的共享状态。

与画像图不同，这个子图是**一次性任务**，所以状态里没有对话历史；
指标表（结构化数据）与材料清单（联网检索）是两条并行的输入，
最后合成一份简报。``trace`` / ``reasoning`` 用于观测，不参与业务。
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from ..domain.finance import (
    AnalysisDraft,
    Evidence,
    FinanceBriefing,
    MetricGap,
    MetricPoint,
    PlannedQuery,
)


class FinanceState(TypedDict, total=False):
    """金融情况子图的共享状态。"""

    # ---- 输入 ---- #
    request: str
    focus: list[str]
    as_of: str
    period: str
    round_limit: int

    # ---- 结构化数据（代码取回，模型只读） ---- #
    metrics: list[MetricPoint]
    gaps: list[MetricGap]
    # 走了落盘缓存兜底的指标名称（源站故障时）——必须如实告诉用户，
    # 不能让人以为这些是刚取回来的
    stale_metrics: list[str]

    # ---- 联网检索 ---- #
    planned_queries: list[PlannedQuery]
    pending_queries: list[PlannedQuery]
    evidence: list[Evidence]
    sufficient: bool
    round: int
    # 服务级失败（配额 / 鉴权 / 未配置密钥）的概括措辞，可直接展示；
    # None 表示没发生。与"这次确实没搜到材料"是两回事，必须区分。
    search_unavailable: str | None

    # ---- 产物 ---- #
    analysis_draft: AnalysisDraft | None
    briefing: FinanceBriefing | None

    # ---- 观测（每步追加） ---- #
    trace: Annotated[list[str], operator.add]
    reasoning: Annotated[list[str], operator.add]
    degraded: bool
    error: str | None
