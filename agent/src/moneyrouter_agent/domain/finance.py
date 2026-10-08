"""金融简报领域模型。

一份简报分三块，职责严格分开：

1. **指标表**（:class:`MetricPoint`）——**全部由代码从结构化数据源取回，模型碰不到**。
   每个值都带 ``unit`` / ``as_of`` / ``source`` / ``method``；估值类还带多窗口历史分位。
   这是方案 Agent 唯一可以拿去复算的东西。
2. **当月分析**（:class:`MonthlyAnalysis`）——模型写的叙述。叙述里可以引用
   **指标 key** 与**材料编号**，代码会校验引用是否真实存在，引用不上的直接剔除。
3. **来源**（:class:`SourceRef`）——网页材料的标题与链接，对齐 PRD「来源必须有 URL + Title」。

三条纪律：

- **数值只能来自指标表**。模型可以在叙述里提数值，但那个数值必须先作为指标被取回来，
  否则方案 Agent 复算时会对不上账——所以代码层不信模型写的任何数字。
- **分位必须带窗口起点**。同一个 PE 用 3 年窗口和全历史窗口能算出完全不同的分位，
  不写清窗口等于不可复现。因此四个窗口都算、都带上起点日期，由下游按需取用。
- **宁可留空也不编**。取不到的指标进 :attr:`FinanceBriefing.missing` 并写明原因，
  而不是让模型凭记忆补一个"大概"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------- #
# 检索侧（联网搜索）
# --------------------------------------------------------------------------- #
Topic = Literal["宏观与市场环境", "财经新闻与政策", "跨资产行情"]
Freshness = Literal["noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"]

TOPICS: tuple[str, ...] = ("宏观与市场环境", "财经新闻与政策", "跨资产行情")
FRESHNESS_VALUES: tuple[str, ...] = ("noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear")

QUERY_MAX_CHARS = 120
MAX_QUERIES_PER_ROUND = 6
MAX_FOLLOW_UP_QUERIES = 3

# --------------------------------------------------------------------------- #
# 来源可信度（材料入库前的分级）
#
# 检索回来的东西不都能当依据。地缘政治、突发事件这类题材尤其严重：论坛帖子、
# 自媒体洗稿、"未来七年必有一战"这种标题党会被大量召回。它们可以当**线索**，
# 但不能当**事实**——一旦被引用进简报，整份材料的可信度就没了。
#
# 所以入库前先判一个级别：
#
# - ``official``   官方发布：政府、央行、交易所与指数公司、央媒；
# - ``mainstream`` 主流财经媒体；
# - ``general``    其余可辨认的站点（兜底档）；
# - ``low``        论坛、问答、自媒体平台、内容农场。
#
# 判定**只认明确命中的特征**，认不出来一律给 ``general``——不做有罪推定。
# 理由是不对称的：误判成 low 会丢掉真实材料，误判成 general 只是少了一次
# 排序优先，代价小得多。
# --------------------------------------------------------------------------- #
SourceTier = Literal["official", "mainstream", "general", "low"]

TIER_OFFICIAL: str = "official"
TIER_MAINSTREAM: str = "mainstream"
TIER_GENERAL: str = "general"
TIER_LOW: str = "low"

# 排序用：数字越小越靠前
TIER_ORDER: dict[str, int] = {
    TIER_OFFICIAL: 0,
    TIER_MAINSTREAM: 1,
    TIER_GENERAL: 2,
    TIER_LOW: 3,
}

# 只在需要向模型说明时展示；general 是兜底档，刻意留空不标注
TIER_LABELS: dict[str, str] = {
    TIER_OFFICIAL: "官方发布",
    TIER_MAINSTREAM: "主流财经媒体",
    TIER_GENERAL: "",
    TIER_LOW: "论坛或自媒体",
}

# 政府域名可按后缀信任（gov.cn 之类是受控命名空间）
OFFICIAL_SUFFIXES: tuple[str, ...] = ("gov.cn", "gov.hk", "gov.mo", "gov.uk", "gov")

# 官方机构与央媒（含其子域）
OFFICIAL_HOSTS: tuple[str, ...] = (
    "pbc.gov.cn",
    "csrc.gov.cn",
    "stats.gov.cn",
    "mof.gov.cn",
    "ndrc.gov.cn",
    "safe.gov.cn",
    "chinatax.gov.cn",
    "chinamoney.com.cn",
    "chinaclear.cn",
    "chinabond.com.cn",
    "sse.com.cn",
    "szse.cn",
    "bse.cn",
    "csindex.com.cn",
    "sge.com.cn",
    "shfe.com.cn",
    "cffex.com.cn",
    "dce.com.cn",
    "czce.com.cn",
    "xinhuanet.com",
    "news.cn",
    "people.com.cn",
    "cctv.com",
    "cnr.cn",
    "ce.cn",
    "china.com.cn",
    "chinadaily.com.cn",
)

# 主流财经媒体
MAINSTREAM_HOSTS: tuple[str, ...] = (
    "caixin.com",
    "yicai.com",
    "stcn.com",
    "cnstock.com",
    "cs.com.cn",
    "21jingji.com",
    "21cbh.com",
    "jiemian.com",
    "thepaper.cn",
    "thecover.cn",
    "ftchinese.com",
    "cls.cn",
    "wallstreetcn.com",
    "eeo.com.cn",
    "nbd.com.cn",
    "finance.sina.com.cn",
    "news.sina.com.cn",
    "news.qq.com",
    "news.163.com",
    "finance.ifeng.com",
    "news.ifeng.com",
)

# 命中即判为 low。两类特征：
# - 平台本身（论坛、问答、UGC 社区）；
# - 正规站点的**自媒体路径**——搜狐号 ``/a/``、大风号 ``/c/``、网易号 ``/dy/article/``，
#   这些走的是同一个域名，但内容主体无法核实，所以按自媒体处理。
LOW_MARKERS: tuple[str, ...] = (
    "bbs.",
    "tieba.baidu.com",
    "zhihu.com",
    "hupu.com",
    "tianya.cn",
    "douban.com",
    "jianshu.com",
    "blog.sina.com.cn",
    "baijiahao.baidu.com",
    "toutiao.com",
    "mp.weixin.qq.com",
    "weibo.com",
    "xueqiu.com",
    "guba.eastmoney.com",
    "bilibili.com",
    "360doc.com",
    "docin.com",
    "wenku.baidu.com",
    "sohu.com/a/",
    "ifeng.com/c/",
    "/dy/article/",
)


def host_of(url: str) -> str:
    """从 URL 里取小写 host；取不到返回空串。"""
    text = (url or "").strip()
    if not text:
        return ""
    authority = text.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]
    authority = authority.rsplit("@", 1)[-1]  # 去掉可能的 userinfo
    return authority.split(":", 1)[0].lower().strip(".")


def _in_domains(host: str, domains: Sequence[str]) -> bool:
    return bool(host) and any(host == item or host.endswith("." + item) for item in domains)


def classify_source(site: str, url: str) -> str:
    """判定一条材料的来源级别（见 :data:`SourceTier`）。

    顺序是**先降级、再升级**：论坛与自媒体平台的 URL 特征最明确，先拦掉；
    剩下的再看好域名是不是官方或主流媒体。认不出来给 ``general``。
    """
    host = host_of(url)
    haystack = f"{url} {site}".lower()
    if any(marker in haystack for marker in LOW_MARKERS):
        return TIER_LOW
    if _in_domains(host, OFFICIAL_HOSTS) or _in_domains(host, OFFICIAL_SUFFIXES):
        return TIER_OFFICIAL
    if _in_domains(host, MAINSTREAM_HOSTS):
        return TIER_MAINSTREAM
    return TIER_GENERAL

# --------------------------------------------------------------------------- #
# 指标侧（结构化数据）
# --------------------------------------------------------------------------- #
# 「政策与事件」没有对应指标，它只由联网搜索支撑——但分析里需要它作为一个方面。
Category = Literal[
    "无风险基准",
    "通胀水平",
    "现金类收益",
    "权益估值",
    "波动与风险",
    "避险资产",
    "政策与事件",
]
CATEGORIES: tuple[str, ...] = (
    "无风险基准",
    "通胀水平",
    "现金类收益",
    "权益估值",
    "波动与风险",
    "避险资产",
    "政策与事件",
)
# 只有这些方面有结构化指标；「政策与事件」只能靠搜索
METRIC_CATEGORIES: tuple[str, ...] = tuple(c for c in CATEGORIES if c != "政策与事件")

PercentileWindow = Literal["3y", "5y", "10y", "all"]
PERCENTILE_WINDOWS: tuple[str, ...] = ("3y", "5y", "10y", "all")
WINDOW_LABELS: dict[str, str] = {
    "3y": "近 3 年",
    "5y": "近 5 年",
    "10y": "近 10 年",
    "all": "全历史",
}

NARRATIVE_MAX_CHARS = 400
HEADLINE_MAX_CHARS = 160


def category_rank(category: str) -> int:
    """按 :data:`CATEGORIES` 的顺序给分组排序，让输出顺序稳定。"""
    try:
        return CATEGORIES.index(category)
    except ValueError:
        return len(CATEGORIES)


# --------------------------------------------------------------------------- #
# 指标注册表：key / 中文名 / 分组 / 单位 / 口径
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MetricSpec:
    """一个指标的静态定义（口径与单位在注册表里定死，避免各处口径漂移）。"""

    key: str
    label: str
    category: str
    unit: str
    method: str = ""
    has_percentile: bool = False


def _spec(
    key: str,
    label: str,
    category: str,
    unit: str,
    method: str = "",
    has_percentile: bool = False,
) -> MetricSpec:
    return MetricSpec(
        key=key, label=label, category=category, unit=unit, method=method, has_percentile=has_percentile
    )


METRIC_SPECS: dict[str, MetricSpec] = {
    spec.key: spec
    for spec in (
        # ── 无风险基准与利率环境 ──────────────────────────────────────────
        _spec("cn_10y_yield", "中国 10 年期国债收益率", "无风险基准", "%", "中债国债到期收益率"),
        _spec("cn_2y_yield", "中国 2 年期国债收益率", "无风险基准", "%", "中债国债到期收益率"),
        _spec("cn_term_spread", "中国 10Y-2Y 期限利差", "无风险基准", "%", "10 年期减 2 年期"),
        _spec("us_10y_yield", "美国 10 年期国债收益率", "无风险基准", "%", "美债到期收益率"),
        _spec("cn_lpr_1y", "中国 1 年期 LPR", "无风险基准", "%"),
        _spec("cn_lpr_5y", "中国 5 年期以上 LPR", "无风险基准", "%"),
        # ── 现金类收益（风险预备金的停泊基准）────────────────────────────
        _spec("cn_shibor_on", "隔夜 Shibor", "现金类收益", "%"),
        _spec("cn_shibor_3m", "3 个月 Shibor", "现金类收益", "%"),
        # ── 通胀水平（保值门槛）──────────────────────────────────────────
        _spec("cn_cpi_yoy", "中国 CPI 当月同比", "通胀水平", "%"),
        _spec("cn_ppi_yoy", "中国 PPI 当月同比", "通胀水平", "%"),
        _spec("us_cpi_yoy", "美国 CPI 当月同比", "通胀水平", "%"),
        # ── 权益估值（中证指数公司官方口径）──────────────────────────────
        # 只用官方接口：指数编制方自己发布，稳定且口径权威。代价是它**只给最近
        # 约 20 个交易日**，因此这一组**不提供历史分位**——用 20 天的样本算出
        # 的"近 5 年分位"是误导，宁可没有。
        _spec("hs300_pe_ttm", "沪深300 市盈率", "权益估值", "倍", "中证指数公司官方（市盈率2）"),
        _spec("sz50_pe_ttm", "上证50 市盈率", "权益估值", "倍", "中证指数公司官方（市盈率2）"),
        _spec("csi500_pe_ttm", "中证500 市盈率", "权益估值", "倍", "中证指数公司官方（市盈率2）"),
        _spec("csi1000_pe_ttm", "中证1000 市盈率", "权益估值", "倍", "中证指数公司官方（市盈率2）"),
        # ── 波动与风险 ───────────────────────────────────────────────────
        _spec("hs300_erp", "沪深300 股权风险溢价", "波动与风险", "%", "100/市盈率 − 10 年期国债收益率"),
        _spec("cn_qvix_50etf", "中国波指（50ETF期权）", "波动与风险", "点", "境内隐含波动率", True),
        # ── 避险与对冲资产 ───────────────────────────────────────────────
        _spec("gold_cny_gram", "上海金（元/克）", "避险资产", "元/克", "上海黄金交易所", True),
        _spec("usdcny_mid", "美元兑人民币中间价", "避险资产", "元", "中国银行/外汇局，原始报价每 100 美元", True),
        _spec("crude_oil", "国际原油（连续合约）", "避险资产", "美元/桶", "外盘连续合约，非特指布伦特", True),
    )
}


def metric_spec(key: str) -> MetricSpec | None:
    return METRIC_SPECS.get(key)


def metric_label(key: str) -> str:
    spec = METRIC_SPECS.get(key)
    return spec.label if spec else key


# --------------------------------------------------------------------------- #
# 指标
# --------------------------------------------------------------------------- #
class Percentile(BaseModel):
    """历史分位。窗口与起点必须一起给出，否则不可复现。"""

    model_config = ConfigDict(extra="ignore")

    window: PercentileWindow
    value: float = Field(description="0–100 的分位值。")
    start: str = Field(description="窗口起点日期 YYYY-MM-DD。")
    observations: int = Field(default=0, description="该窗口内的样本数。")


class MetricPoint(BaseModel):
    """一个取回来的指标值。**由代码写入，模型不参与。**"""

    model_config = ConfigDict(extra="ignore")

    key: str
    label: str
    category: str
    value: float
    unit: str = ""
    as_of: str = Field(
        description="数据时点。日频为 YYYY-MM-DD；月度统计口径（如 CPI）为 YYYY-MM。"
    )
    source: str = Field(default="", description="数据来源（接口或机构）。")
    method: str = Field(default="", description="口径说明。")
    change: str = Field(default="", description="较上期的变化，已算好。")
    percentiles: list[Percentile] = Field(default_factory=list)


class MetricGap(BaseModel):
    """尝试过但没取到的指标——如实记录下来，供分析说明缺口。"""

    model_config = ConfigDict(extra="ignore")

    key: str
    label: str
    reason: str = ""


# --------------------------------------------------------------------------- #
# 联网材料
# --------------------------------------------------------------------------- #
class Evidence(BaseModel):
    """一条检索回来的材料，带全局编号（供分析引用）与来源信息。"""

    model_config = ConfigDict(extra="ignore")

    id: int
    topic: str = ""
    query: str = ""
    title: str
    url: str
    site: str = ""
    published_at: str = ""
    snippet: str = ""
    # 来源级别（见 classify_source）；默认 general，是兜底档
    tier: str = TIER_GENERAL


class SourceRef(BaseModel):
    """对外呈现的来源条目。"""

    model_config = ConfigDict(extra="ignore")

    id: int
    title: str
    url: str
    site: str = ""
    published_at: str = ""


class PlannedQuery(BaseModel):
    """一条检索意图。"""

    model_config = ConfigDict(extra="ignore")

    query: str = Field(
        description="一条中文检索词，像人在搜索引擎里会输入的话，可带时间限定词（如「2026年10月」）。"
    )
    topic: Topic = Field(description="这条检索属于哪个方面。")
    freshness: Freshness = Field(
        default="noLimit",
        description="只看多久内的网页：noLimit 不限 / oneDay 一天 / oneWeek 一周 / "
        "oneMonth 一月 / oneYear 一年。",
    )

    @model_validator(mode="after")
    def _sanitize(self) -> "PlannedQuery":
        self.query = " ".join((self.query or "").split())[:QUERY_MAX_CHARS]
        return self


class SearchPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    queries: list[PlannedQuery] = Field(
        default_factory=list,
        description="3-6 条检索意图，覆盖「宏观与市场环境 / 财经新闻与政策 / 跨资产行情」。",
    )


class Reflection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sufficient: bool = Field(description="现有材料是否已足以支撑一份当月金融情况分析。")
    note: str = Field(default="", description="一句话说明判断依据。")
    missing: list[str] = Field(default_factory=list, description="还缺什么（没有就留空）。")
    follow_up_queries: list[PlannedQuery] = Field(
        default_factory=list,
        description="sufficient 为 false 时的补充检索意图，最多 3 条；sufficient 为 true 时必须留空。",
    )


# --------------------------------------------------------------------------- #
# 当月情况分析（模型写，必须引用真实存在的指标与材料）
# --------------------------------------------------------------------------- #
class AnalysisSection(BaseModel):
    """一个方面的分析。叙述之外必须标注它依据了什么。"""

    model_config = ConfigDict(extra="ignore")

    category: Category = Field(description="这个方面对应哪一组。")
    narrative: str = Field(
        description="本月该方面的情况，两三句话。只讲指标与材料支撑得起的事实，"
        "不给投资建议、不预测涨跌、不使用绝对化表述。"
    )
    metric_keys: list[str] = Field(
        default_factory=list, description="这条分析用到了哪些指标，填指标的英文 key，只能引用给定指标。"
    )
    source_ids: list[int] = Field(
        default_factory=list, description="这条分析引用了哪些材料，填材料编号。"
    )

    @model_validator(mode="after")
    def _sanitize(self) -> "AnalysisSection":
        self.narrative = " ".join((self.narrative or "").split())[:NARRATIVE_MAX_CHARS]
        self.metric_keys = list(dict.fromkeys(str(k).strip() for k in self.metric_keys if str(k).strip()))
        self.source_ids = list(dict.fromkeys(int(i) for i in self.source_ids))
        return self


class AnalysisDraft(BaseModel):
    """LLM 面向的当月分析——**不含期间字段**，期间由代码填。"""

    model_config = ConfigDict(extra="ignore")

    headline: str = Field(description="一句话概括本月金融环境。")
    sections: list[AnalysisSection] = Field(
        default_factory=list, description="分方面的分析，覆盖六个指标方面与「政策与事件」。"
    )
    implications: list[str] = Field(
        default_factory=list,
        description="对个人资产配置的含义，二到四条。只说「这意味着什么」，"
        "不给具体买卖或产品建议。",
    )
    caveats: list[str] = Field(default_factory=list, description="时效、口径差异、信息缺口等提醒。")


class MonthlyAnalysis(AnalysisDraft):
    """落定后的当月分析（多了期间）。"""

    period: str = Field(default="", description="统计期间，YYYY-MM。")


# --------------------------------------------------------------------------- #
# 简报
# --------------------------------------------------------------------------- #
class FinanceBriefing(BaseModel):
    """对外产物：一张带全部高价值数据的指标表 + 一份当月情况分析 + 来源。"""

    model_config = ConfigDict(extra="ignore")

    as_of: str = Field(description="信息基准日期，YYYY-MM-DD。")
    period: str = Field(default="", description="统计期间，YYYY-MM。")
    metrics: list[MetricPoint] = Field(default_factory=list, description="取到的指标（值非空）。")
    missing: list[MetricGap] = Field(default_factory=list, description="尝试过但没取到的指标。")
    analysis: MonthlyAnalysis = Field(default_factory=MonthlyAnalysis)
    sources: list[SourceRef] = Field(default_factory=list, description="被分析实际引用到的材料。")
    degraded: bool = Field(default=False, description="是否走了降级路径。")
    error: str | None = Field(default=None, description="降级原因，供观测。")

    def metric_map(self) -> dict[str, MetricPoint]:
        return {item.key: item for item in self.metrics}


class FinanceBriefingResult(BaseModel):
    """门面的返回：简报 + 观测轨迹。"""

    model_config = ConfigDict(extra="ignore")

    request: str = ""
    briefing: FinanceBriefing
    trace: list[str] = Field(default_factory=list)
    reasoning: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 降级用的内置检索意图
# --------------------------------------------------------------------------- #
DEFAULT_QUERIES: tuple[PlannedQuery, ...] = (
    PlannedQuery(
        query="最新 央行 货币政策 利率 流动性",
        topic="宏观与市场环境",
        freshness="oneMonth",
    ),
    PlannedQuery(
        query="最新 A股市场 走势 主要指数 估值",
        topic="宏观与市场环境",
        freshness="oneWeek",
    ),
    PlannedQuery(
        query="最新 金融政策 与 监管动向",
        topic="财经新闻与政策",
        freshness="oneMonth",
    ),
    # 事件类：指标只告诉你"现在是多少"，地缘冲突这类事件解释的是"为什么会这样、
    # 接下来可能怎样"。这条必须留在兜底里——规划降级时最不该丢的就是它。
    PlannedQuery(
        query="地缘政治 冲突 与 重大国际事件 最新进展",
        topic="财经新闻与政策",
        freshness="oneWeek",
    ),
    PlannedQuery(
        query="黄金价格 与 人民币汇率 最新走势",
        topic="跨资产行情",
        freshness="oneWeek",
    ),
)


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
def compose_briefing(
    *,
    as_of: str,
    period: str,
    metrics: list[MetricPoint],
    gaps: list[MetricGap],
    evidence: list[Evidence],
    draft: AnalysisDraft | None,
    degraded: bool = False,
    error: str | None = None,
    search_unavailable: str | None = None,
    stale_metrics: list[str] | None = None,
) -> FinanceBriefing:
    """把指标、材料与模型的分析合成为最终简报。

    校验两件事，都是"引用不上就剔除"：

    - 分析里引用的指标 key 必须真实存在于指标表；
    - 分析里引用的材料编号必须真实存在于材料清单。

    指标表本身由代码填充，**模型没有写入通路**，因此数值不可能被编造。
    """
    known_keys = {item.key for item in metrics}
    by_id = {item.id: item for item in evidence}

    sections: list[AnalysisSection] = []
    dropped_keys = 0
    dropped_sources = 0
    if draft is not None:
        for section in draft.sections:
            valid_keys = [k for k in section.metric_keys if k in known_keys]
            valid_ids = [i for i in section.source_ids if i in by_id]
            dropped_keys += len(section.metric_keys) - len(valid_keys)
            dropped_sources += len(section.source_ids) - len(valid_ids)
            sections.append(
                section.model_copy(update={"metric_keys": valid_keys, "source_ids": valid_ids})
            )

    caveats: list[str] = list(draft.caveats) if draft is not None else []
    if dropped_keys:
        caveats.append(f"{dropped_keys} 处指标引用不存在，已被剔除。")
    if dropped_sources:
        caveats.append(f"{dropped_sources} 处材料引用不存在，已被剔除。")
    if gaps:
        # 只列名称：技术性原因（异常原文）留在 missing[].reason 里供排查，
        # 不进面向用户的提示。
        caveats.append("以下指标本次未取到：" + "、".join(gap.label for gap in gaps) + "。")
    if stale_metrics:
        caveats.append(
            "以下指标因数据源不可用、使用的是最近一次成功取回的缓存数据"
            "（数据时点见各指标本身）：" + "、".join(stale_metrics) + "。"
        )
    if draft is None:
        caveats.append("本次未能生成分析，以下仅列出指标与来源。")
    # 材料为空时要分清"服务挂了"和"这次确实没搜到"——前者需要人去充值 / 换密钥，
    # 后者只是本期没有相关报道。混为一谈会让人误判成"最近没新闻"。
    if not metrics and not evidence:
        head = f"检索服务不可用：{search_unavailable}。" if search_unavailable else ""
        caveats.insert(0, f"{head}本次既未取到指标、也未获取到网络材料，简报不含实时信息。")
    elif not evidence:
        if search_unavailable:
            caveats.insert(
                0,
                f"检索服务不可用：{search_unavailable}；本期没有网络材料可依据，"
                "政策与事件方面属于「未能获取」而非「没有发生」。",
            )
        else:
            caveats.insert(0, "本次未获取到任何网络材料，以下内容不含实时信息。")
    if degraded:
        # 刻意只给概括：详细原因可能带上游 API 原文，只放进 error 供日志排查
        caveats.append("本次生成走了降级路径，结果可能不完整。")

    used_ids = {i for section in sections for i in section.source_ids}
    sources = [
        SourceRef(
            id=item.id,
            title=item.title,
            url=item.url,
            site=item.site,
            published_at=item.published_at,
        )
        for item in evidence
        if item.id in used_ids
    ]

    analysis = MonthlyAnalysis(
        period=period,
        headline=(draft.headline.strip() if draft is not None else ""),
        sections=sorted(sections, key=lambda s: category_rank(s.category)),
        implications=list(draft.implications) if draft is not None else [],
        caveats=list(dict.fromkeys(caveats)),
    )

    return FinanceBriefing(
        as_of=as_of,
        period=period,
        metrics=sorted(metrics, key=lambda m: (category_rank(m.category), m.key)),
        missing=list(gaps),
        analysis=analysis,
        sources=sources,
        degraded=degraded,
        error=error,
    )
