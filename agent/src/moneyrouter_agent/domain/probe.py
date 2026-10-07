"""关注点（Probe）探测机制——一套**通用、可扩展**的"值得向用户追问的点"生成器。

设计：

- 关注点由**代码确定性生成**（多基线、多规则），再交给对话去问用户为什么；
- 规则通过 ``@probe_rule`` 装饰器注册——**新增一类追问 = 加一个纯函数**，不改主流程；
- 规则只吃 :class:`ProbeContext`（纯数据），便于单测；
- :func:`merge_probes` 保证**已解答的不再重复问**、随事发条件消失的置为 ``dismissed``；
- 框架**不绑定任何一种基线**——内置规则只是示例，阈值集中在 :class:`ProbeThresholds`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from .money import format_yuan
from .month import (
    Baseline,
    CategorySpend,
    FundAllocation,
    GoalAlignment,
    IncomeFact,
    InvestmentSnapshot,
    OneOffItem,
    category_map,
)

ProbeStatus = Literal["open", "answered", "dismissed"]


class Probe(BaseModel):
    """一条"值得向用户追问"的关注点。**由代码生成，不是模型编的。**"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="稳定去重键，形如 'over_budget:餐饮'。")
    kind: str = Field(description="命中哪条规则。")
    topic: str = Field(description="面向用户的中文主题短语，代码渲染。")
    detail: str = Field(description="事实依据，代码渲染（含金额已换算成元）。")
    severity: int = Field(default=50, description="排序权重，越小越先问。")
    status: ProbeStatus = Field(default="open", description="open 待问 / answered 已归因 / dismissed 已失效。")
    answer: str = Field(default="", description="用户给出的归因/解释，由对话填入。")
    related: dict[str, str] = Field(
        default_factory=dict, description="结构化上下文，如 {'category': '餐饮'}，供可视化定位。"
    )


@dataclass(frozen=True)
class ProbeThresholds:
    """触发阈值（集中在 config.MonthSettings，可 env 覆盖）。"""

    over_baseline_factor: float = 1.3
    one_off_min_cents: int = 100_000
    one_off_income_ratio: float = 0.10
    big_delta_pct: float = 20.0
    goal_off_track_factor: float = 0.8
    investment_loss_min_cents: int = 100_000


@dataclass(frozen=True)
class ProbeContext:
    """探测规则的只读输入（纯数据，便于单测）。"""

    period: str
    categories: list[CategorySpend]
    income: IncomeFact | None
    baselines: list[Baseline]
    one_offs: list[OneOffItem]
    goal_alignment: GoalAlignment
    allocation: FundAllocation
    investments: InvestmentSnapshot
    prior: dict[str, Probe]
    thresholds: ProbeThresholds

    @property
    def current(self) -> dict[str, int]:
        return category_map(self.categories)

    @property
    def balance_cents(self) -> int | None:
        if self.income is None:
            return None
        return self.income.amount_cents - sum(self.current.values())


@dataclass(frozen=True)
class ProbeRule:
    kind: str
    priority: int
    detect: Callable[[ProbeContext], list[Probe]]


PROBE_RULES: list[ProbeRule] = []


def probe_rule(kind: str, priority: int = 50) -> Callable[[Callable[[ProbeContext], list[Probe]]], Callable[[ProbeContext], list[Probe]]]:
    """装饰器：注册一条探测规则。新增规则无需改动 :func:`generate_probes`。"""

    def decorator(fn: Callable[[ProbeContext], list[Probe]]) -> Callable[[ProbeContext], list[Probe]]:
        PROBE_RULES.append(ProbeRule(kind=kind, priority=priority, detect=fn))
        return fn

    return decorator


def _probe(kind: str, scope: str, topic: str, detail: str, severity: int, **related: str) -> Probe:
    return Probe(
        id=f"{kind}:{scope}",
        kind=kind,
        topic=topic,
        detail=detail,
        severity=severity,
        related=dict(related),
    )


# --------------------------------------------------------------------------- #
# 内置规则（示例；框架不绑定任何一种）
# --------------------------------------------------------------------------- #
@probe_rule("over_budget", priority=20)
def _over_budget(ctx: ProbeContext) -> list[Probe]:
    out: list[Probe] = []
    factor = ctx.thresholds.over_baseline_factor
    for base in ctx.baselines:
        if base.metric != "budget" or base.amount_cents <= 0:
            continue
        cur = ctx.current.get(base.category, 0)
        if cur >= base.amount_cents * factor:
            scope = base.category or "total"
            out.append(
                _probe(
                    "over_budget",
                    scope,
                    f"{base.category or '总支出'}高于预算",
                    f"本月{base.category or '总支出'} {format_yuan(cur)} 元，预算 {format_yuan(base.amount_cents)} 元，"
                    f"超出 {format_yuan(cur - base.amount_cents)} 元。",
                    20,
                    category=base.category,
                )
            )
    return out


@probe_rule("one_off_large", priority=25)
def _one_off_large(ctx: ProbeContext) -> list[Probe]:
    out: list[Probe] = []
    income_cents = ctx.income.amount_cents if ctx.income else None
    for item in ctx.one_offs:
        big_by_abs = item.amount_cents >= ctx.thresholds.one_off_min_cents
        big_by_ratio = (
            income_cents is not None
            and income_cents > 0
            and item.amount_cents >= income_cents * ctx.thresholds.one_off_income_ratio
        )
        if big_by_abs or big_by_ratio:
            out.append(
                _probe(
                    "one_off_large",
                    item.label,
                    f"本月有一笔较大的支出：{item.label}",
                    f"{item.label} 这一笔 {format_yuan(item.amount_cents)} 元，想确认下是什么情况。",
                    25,
                    label=item.label,
                )
            )
    return out


@probe_rule("over_last_month", priority=30)
def _over_last_month(ctx: ProbeContext) -> list[Probe]:
    return _over_metric(ctx, "last_month", "over_last_month", "比上月多不少", 30)


@probe_rule("over_trailing", priority=35)
def _over_trailing(ctx: ProbeContext) -> list[Probe]:
    return _over_metric(ctx, "trailing_3m_avg", "over_trailing", "比近三个月明显偏多", 35)


def _over_metric(ctx: ProbeContext, metric: str, kind: str, verb: str, severity: int) -> list[Probe]:
    out: list[Probe] = []
    factor = ctx.thresholds.over_baseline_factor
    for base in ctx.baselines:
        if base.metric != metric or base.amount_cents <= 0:
            continue
        cur = ctx.current.get(base.category, 0)
        if cur >= base.amount_cents * factor:
            out.append(
                _probe(
                    kind,
                    base.category or "total",
                    f"{base.category} {verb}",
                    f"本月{base.category} {format_yuan(cur)} 元，{_metric_label(metric)} "
                    f"{format_yuan(base.amount_cents)} 元，多了 {format_yuan(cur - base.amount_cents)} 元。",
                    severity,
                    category=base.category,
                )
            )
    return out


def _metric_label(metric: str) -> str:
    return {"last_month": "上月", "trailing_3m_avg": "近三月平均", "budget": "预算"}.get(metric, metric)


@probe_rule("spend_over_income", priority=40)
def _spend_over_income(ctx: ProbeContext) -> list[Probe]:
    if ctx.income is None:
        return []
    total = sum(ctx.current.values())
    if total > ctx.income.amount_cents:
        return [
            _probe(
                "spend_over_income",
                "total",
                "本月支出超过了收入",
                f"本月支出合计 {format_yuan(total)} 元，收入 {format_yuan(ctx.income.amount_cents)} 元，"
                f"支出比收入多 {format_yuan(total - ctx.income.amount_cents)} 元。",
                40,
                category="",
            )
        ]
    return []


@probe_rule("goal_off_track", priority=45)
def _goal_off_track(ctx: ProbeContext) -> list[Probe]:
    ga = ctx.goal_alignment
    saved = ga.saved_this_month_cents
    planned = ga.planned_this_month_cents
    if saved is None or planned is None or planned <= 0:
        return []
    if saved < planned * ctx.thresholds.goal_off_track_factor:
        return [
            _probe(
                "goal_off_track",
                "goal",
                "这个月的攒钱进度落后于计划",
                f"本月净攒 {format_yuan(saved)} 元，计划攒 {format_yuan(planned)} 元，"
                f"比计划少 {format_yuan(planned - saved)} 元。",
                45,
                field="goal_alignment",
            )
        ]
    return []


@probe_rule("investment_loss", priority=42)
def _investment_loss(ctx: ProbeContext) -> list[Probe]:
    """某笔已有投资本月明显亏损——按单笔金额触发，值得问一句原因。"""
    out: list[Probe] = []
    threshold = ctx.thresholds.investment_loss_min_cents
    for holding in ctx.investments.holdings:
        month = holding.month_return_cents
        if month is None or month > -threshold:
            continue
        pct = (
            f"（约 {abs(holding.month_return_pct):.1f}%）"
            if holding.month_return_pct is not None
            else ""
        )
        out.append(
            _probe(
                "investment_loss",
                holding.name,
                f"「{holding.name}」这个月在亏损",
                f"{holding.name} 本月亏了 {format_yuan(abs(month))} 元{pct}，想了解下是什么情况。",
                42,
                label=holding.name,
            )
        )
    return out


@probe_rule("inconsistent", priority=50)
def _inconsistent(ctx: ProbeContext) -> list[Probe]:
    """结余与"非投资 + 投资"对不上——账目不自洽，值得问一句。"""
    alloc = ctx.allocation
    if alloc.unallocated_cents is None or alloc.unallocated_cents == 0:
        return []
    gap = abs(alloc.unallocated_cents)
    return [
        _probe(
            "inconsistent",
            "allocation",
            "这个月的资金去向对不上",
            f"结余与「不投资 + 投资」相差 {format_yuan(gap)} 元，想确认下这部分去哪了。",
            50,
            field="allocation",
        )
    ]


@probe_rule("missing_field", priority=60)
def _missing_field(ctx: ProbeContext) -> list[Probe]:
    out: list[Probe] = []
    if ctx.income is None:
        out.append(
            _probe(
                "missing_field",
                "income",
                "这个月的收入还没了解到",
                "这个月大概有多少收入、是怎么来的，还没确认。",
                60,
                field="income",
            )
        )
    if not ctx.categories:
        out.append(
            _probe(
                "missing_field",
                "categories",
                "这个月的支出还没了解到",
                "这个月主要花在哪些方面，还没确认。",
                61,
                field="categories",
            )
        )
    balance = ctx.balance_cents
    if (
        balance is not None
        and balance > 0
        and ctx.allocation.non_invested_cents is None
        and ctx.allocation.invested_cents is None
    ):
        out.append(
            _probe(
                "missing_field",
                "allocation",
                "剩下的钱怎么安排的还没了解到",
                f"这个月结余约 {format_yuan(balance)} 元，其中多少留着备用（不投资）、"
                f"多少投了出去，还没确认。",
                55,
                field="allocation",
            )
        )

    investments = ctx.investments
    if investments.has_investments is None:
        out.append(
            _probe(
                "missing_field",
                "investments",
                "以前的投资收益还没了解到",
                "以前有没有在做投资 / 理财、现在赚了还是亏了、大概多少，还没确认"
                "（如果本来就没有投资，如实说明即可）。",
                56,
                field="investments",
            )
        )
    elif investments.has_investments and not investments.holdings:
        out.append(
            _probe(
                "missing_field",
                "holding_returns",
                "投资的具体收益还没了解到",
                "已经知道有在投资，但具体买了什么、这个月赚赔多少、累计收益如何，还没确认。",
                57,
                field="investments",
            )
        )
    return out


# --------------------------------------------------------------------------- #
# 生成与合并
# --------------------------------------------------------------------------- #
def generate_probes(ctx: ProbeContext) -> list[Probe]:
    """跑全部规则 → 去重（同 id 只留 severity 最小的一条）→ 排序 ``(severity, id)``。"""
    by_id: dict[str, Probe] = {}
    for rule in PROBE_RULES:
        try:
            found = rule.detect(ctx)
        except Exception:  # noqa: BLE001 - 单条规则出错不该拖垮整批
            continue
        for probe in found:
            existing = by_id.get(probe.id)
            if existing is None or probe.severity < existing.severity:
                by_id[probe.id] = probe
    return sorted(by_id.values(), key=lambda p: (p.severity, p.id))


def merge_probes(candidates: list[Probe], prior: dict[str, Probe]) -> list[Probe]:
    """与上一轮的关注点合并，保证"已解答的不再重复问"。

    - 候选里已在 ``prior`` 且 ``answered`` → 继承 ``answer``，**保持 answered**（不再进待问集）；
    - 候选里已在 ``prior`` 且 ``dismissed`` → 继承（保持 dismissed）；
    - 候选里的新 id → ``open``；
    - ``prior`` 里已被解答、但这轮不再触发的 → 保留为 ``dismissed``（记录归因，不再追问）。
    """
    merged: dict[str, Probe] = {}
    for probe in candidates:
        old = prior.get(probe.id)
        if old is not None and old.status in ("answered", "dismissed"):
            merged[probe.id] = probe.model_copy(update={"status": old.status, "answer": old.answer})
        else:
            merged[probe.id] = probe
    for pid, old in prior.items():
        if pid not in merged and old.status == "answered":
            merged[pid] = old.model_copy(update={"status": "dismissed"})
    return sorted(merged.values(), key=lambda p: (p.severity, p.id))


def open_probes(probes: list[Probe]) -> list[Probe]:
    """待问的关注点（status=open），按 severity 排序。"""
    return [p for p in probes if p.status == "open"]
