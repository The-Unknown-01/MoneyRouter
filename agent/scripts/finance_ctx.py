"""金融情况 Agent 的一次性演练脚本。

用法::

    ./.venv/Scripts/python.exe scripts/finance_ctx.py --mock            # 不联网、不取数
    ./.venv/Scripts/python.exe scripts/finance_ctx.py --real            # 真取数 + 真模型 + 真检索
    ./.venv/Scripts/python.exe scripts/finance_ctx.py --real --metrics-only   # 只看指标表
    ./.venv/Scripts/python.exe scripts/finance_ctx.py --real --show-trace
    ./.venv/Scripts/python.exe scripts/finance_ctx.py --real --json

依赖的密钥：``DEEPSEEK_API_KEY``（模型）与 ``BOCHA_API_KEY``（联网检索）。
结构化指标走 AKShare，**不需要密钥**。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from moneyrouter_agent.config import (  # noqa: E402
    MarketDataSettings,
    SearchSettings,
    Settings,
)
from moneyrouter_agent.domain.finance import (  # noqa: E402
    TOPICS,
    WINDOW_LABELS,
    AnalysisDraft,
    AnalysisSection,
    MetricGap,
    MetricPoint,
    PlannedQuery,
    Reflection,
    SearchPlan,
)
from moneyrouter_agent.finance_agent import FinanceAgent  # noqa: E402
from moneyrouter_agent.tools.bocha import SearchHit  # noqa: E402

DEFAULT_REQUEST = (
    "给一名在校大三学生做三年期的稳健型资产配置规划，"
    "他每月生活费 2500 元、只有 3000 多元可动用储蓄，几乎不能承受亏损。"
    "请给出当前的金融环境背景。"
)

MOCK_QUERIES = [
    PlannedQuery(query="2026年10月 央行 货币政策 流动性", topic="宏观与市场环境", freshness="oneWeek"),
    PlannedQuery(query="2026年 最新 金融监管政策 动向", topic="财经新闻与政策", freshness="oneMonth"),
    PlannedQuery(query="2026年10月 黄金 与 人民币汇率 走势", topic="跨资产行情", freshness="oneWeek"),
]

MOCK_METRICS = [
    MetricPoint(
        key="cn_10y_yield", label="中国 10 年期国债收益率", category="无风险基准",
        value=1.68, unit="%", as_of="2026-09-30", source="中债/东方财富", change="+0.02 个百分点",
    ),
    MetricPoint(
        key="cn_lpr_1y", label="中国 1 年期 LPR", category="无风险基准",
        value=3.0, unit="%", as_of="2026-09", source="中国人民银行",
    ),
    MetricPoint(
        key="cn_cpi_yoy", label="中国 CPI 当月同比", category="通胀水平",
        value=0.8, unit="%", as_of="2026-08", source="国家统计局", change="+0.30 个百分点",
    ),
    MetricPoint(
        key="hs300_pe_ttm", label="沪深300 滚动市盈率", category="权益估值",
        value=12.48, unit="倍", as_of="2026-09-30", source="乐咕乐股（月度）",
        method="TTM 滚动市盈率", change="-0.56倍",
    ),
    MetricPoint(
        key="hs300_erp", label="沪深300 股权风险溢价", category="波动与风险",
        value=6.33, unit="%", as_of="2026-09-30", source="推导值", change="+0.35 个百分点",
    ),
    MetricPoint(
        key="gold_cny_gram", label="上海金（元/克）", category="避险资产",
        value=907.32, unit="元/克", as_of="2026-09-30", source="上海黄金交易所", change="+9.79元/克",
    ),
]

MOCK_GAPS = [
    MetricGap(key="vix", label="VIX 恐慌指数", reason="演示数据：境外数据源未覆盖"),
    MetricGap(key="cn_money_fund_7d", label="货币基金 7 日年化", reason="演示数据：数据源不给可靠值"),
]

MOCK_HITS = [
    SearchHit(
        query=MOCK_QUERIES[0].query, topic="宏观与市场环境",
        title="央行例会：保持流动性合理充裕", url="https://example.gov.cn/pbc-meeting",
        site="中国人民银行", published_at="2026-09-28",
        snippet="例会强调保持流动性合理充裕，引导社会综合融资成本稳中有降。",
    ),
    SearchHit(
        query=MOCK_QUERIES[1].query, topic="财经新闻与政策",
        title="监管部门就个人养老金账户投资范围发布通知", url="https://example.gov.cn/pension-notice",
        site="金融监管总局", published_at="2026-09-28",
        snippet="通知明确个人养老金账户可投资的公募基金范围，并强调销售机构应做好适当性管理。",
    ),
    SearchHit(
        query=MOCK_QUERIES[2].query, topic="跨资产行情",
        title="国际金价维持高位，人民币汇率保持稳定", url="https://example.com/gold-fx",
        site="示例财经", published_at="2026-10-05",
        snippet="现货黄金维持高位震荡；人民币对美元中间价在 6.7 附近波动。",
    ),
]

MOCK_DRAFT = AnalysisDraft(
    headline=(
        "无风险收益维持低位、物价温和，权益估值处于中间水平但短端波动很低，"
        "避险资产价格处于历史高位——典型的“安全垫薄、性价比不突出”的环境。"
    ),
    sections=[
        AnalysisSection(
            category="无风险基准",
            narrative=(
                "10 年期国债收益率 1.68%，1 年期 LPR 3.0%，均处于低位；"
                "这意味着存款与固收类资产的收益下限很低，作为基准使用时不宜高估。"
            ),
            metric_keys=["cn_10y_yield", "cn_lpr_1y"],
        ),
        AnalysisSection(
            category="通胀水平",
            narrative="CPI 同比 0.8%，物价温和。保值门槛不高，但无风险利率同样接近这一水平，实际收益空间有限。",
            metric_keys=["cn_cpi_yoy"],
        ),
        AnalysisSection(
            category="权益估值",
            narrative="沪深300 滚动市盈率 12.48 倍，历史分位处于中间区间，既谈不上便宜也谈不上泡沫。",
            metric_keys=["hs300_pe_ttm"],
        ),
    ],
    implications=[
        "无风险收益低于或接近通胀水平时，现金类资产的长期购买力增长有限。",
        "权益估值处于中间分位，意味着环境本身不构成加仓或减仓的强理由。",
    ],
    caveats=["本结果为演示用假数据，不代表真实行情。", "宏观指标为月度口径，转述时不要伪造具体日子。"],
)


class FixedRunner:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls = 0

    def __call__(self, _messages: list[object]) -> object:
        self.calls += 1
        return self.value


def mock_searcher(queries: list[PlannedQuery]) -> tuple[list[SearchHit], list[str]]:
    wanted = {item.query for item in queries}
    hits = [hit for hit in MOCK_HITS if hit.query in wanted]
    return (hits or MOCK_HITS), []


def mock_metrics() -> tuple[list[MetricPoint], list[MetricGap]]:
    return list(MOCK_METRICS), list(MOCK_GAPS)


def build_agent(args: argparse.Namespace) -> FinanceAgent:
    if args.mock:
        return FinanceAgent(
            settings=Settings(api_key="mock-key"),
            search_settings=SearchSettings(api_key="mock-key", max_rounds=args.rounds),
            market_settings=MarketDataSettings(enabled=False),
            plan_runner=FixedRunner(SearchPlan(queries=MOCK_QUERIES)),
            reflect_runner=FixedRunner(Reflection(sufficient=True, note="演示：材料已够")),
            analyze_runner=FixedRunner(MOCK_DRAFT),
            searcher=mock_searcher,
            metrics_fetcher=mock_metrics,
        )
    return FinanceAgent(
        settings=Settings.from_env(),
        search_settings=SearchSettings.from_env().with_overrides(max_rounds=args.rounds),
        market_settings=MarketDataSettings.from_env(),
    )


def render_metrics(briefing) -> str:
    lines = [f"=== 指标表（{len(briefing.metrics)} 项，基准日期 {briefing.as_of}）==="]
    if not briefing.metrics:
        lines.append("  （未取到任何指标）")
    current = ""
    for metric in briefing.metrics:
        if metric.category != current:
            current = metric.category
            lines.append(f"  【{current}】")
        change = f"  {metric.change}" if metric.change else ""
        lines.append(f"    {metric.label} = {metric.value}{metric.unit}  @{metric.as_of}{change}")
        for window in metric.percentiles:
            label = WINDOW_LABELS.get(window.window, window.window)
            lines.append(
                f"        · {label}分位 {window.value}%（{window.start} 起，{window.observations} 个样本）"
            )
    if briefing.missing:
        lines.append("")
        lines.append(f"  【未取到 {len(briefing.missing)} 项】")
        for gap in briefing.missing:
            lines.append(f"    - {gap.label}：{gap.reason}")
    return "\n".join(lines)


def render_analysis(briefing) -> str:
    analysis = briefing.analysis
    lines = ["", f"=== 当月情况分析（{analysis.period}）==="]
    if briefing.degraded:
        lines.append(f"[降级] {briefing.error or '结果可能不完整'}")
    lines.append("")
    lines.append("总述：" + (analysis.headline or "（无）"))
    for section in analysis.sections:
        lines.append("")
        lines.append(f"  【{section.category}】{section.narrative}")
        if section.metric_keys:
            lines.append(f"      依据指标：{'、'.join(section.metric_keys)}")
        if section.source_ids:
            lines.append(f"      依据材料：{'、'.join(str(i) for i in section.source_ids)}")
    if analysis.implications:
        lines.append("")
        lines.append("对资产配置的含义：")
        for item in analysis.implications:
            lines.append(f"  · {item}")
    lines.append("")
    lines.append("提示：")
    for caveat in analysis.caveats or ["（无）"]:
        lines.append(f"  · {caveat}")
    if briefing.sources:
        lines.append("")
        lines.append(f"来源（{len(briefing.sources)} 条）：")
        for source in briefing.sources:
            meta = " ｜ ".join(part for part in (source.site, source.published_at) if part)
            lines.append(f"  [{source.id}] {source.title}" + (f" ｜ {meta}" if meta else ""))
            lines.append(f"       {source.url}")
    return "\n".join(lines)


REASONING_PREVIEW_CHARS = 400


def render_trace(result) -> str:
    lines = ["", "=== 执行轨迹 ==="]
    lines += [f"  {line}" for line in result.trace] or ["  （无）"]
    if result.reasoning:
        lines.append("")
        lines.append("=== 思维链（仅观测，已截断）===")
        for index, item in enumerate(result.reasoning, start=1):
            text = " ".join(str(item).split())
            if len(text) > REASONING_PREVIEW_CHARS:
                text = text[:REASONING_PREVIEW_CHARS] + f"…（共 {len(item)} 字）"
            lines.append(f"  [{index}] {text}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成一份金融简报")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", help="全部用假数据（不联网、不取数）")
    mode.add_argument("--real", action="store_true", help="真取数 + 真模型 + 真检索")
    parser.add_argument("--request", default=DEFAULT_REQUEST, help="需求描述")
    parser.add_argument("--as-of", dest="as_of", default=None, help="信息基准日期 YYYY-MM-DD")
    parser.add_argument("--period", default=None, help="统计期间 YYYY-MM")
    parser.add_argument("--rounds", type=int, default=2, help="检索轮数上限（1-3）")
    parser.add_argument("--metrics-only", action="store_true", help="只取指标，不调模型也不检索")
    parser.add_argument("--show-trace", action="store_true", help="打印执行轨迹与思维链")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = parser.parse_args()

    if not args.mock and not args.real:
        args.mock = True  # 缺省用 mock，避免误烧 token

    if args.metrics_only:
        from datetime import date

        from moneyrouter_agent.domain.finance import compose_briefing
        from moneyrouter_agent.tools.market_data import MarketDataClient

        baseline = args.as_of or date.today().strftime("%Y-%m-%d")
        month = args.period or baseline[:7]
        outcome = MarketDataClient(MarketDataSettings.from_env()).fetch()
        # 走与正式链路同一个合成函数：这样"缓存兜底"等提示不会在调试路径里丢失
        briefing = compose_briefing(
            as_of=baseline,
            period=month,
            metrics=outcome.metrics,
            gaps=outcome.gaps,
            evidence=[],
            draft=None,
            stale_metrics=outcome.stale,
        )
        print(render_metrics(briefing))
        print(render_analysis(briefing))
        return 0

    agent = build_agent(args)
    result = agent.briefing(args.request, as_of=args.as_of, period=args.period, focus=list(TOPICS))

    if args.json:
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
    else:
        print(render_metrics(result.briefing))
        print(render_analysis(result.briefing))
        if args.show_trace:
            print(render_trace(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
