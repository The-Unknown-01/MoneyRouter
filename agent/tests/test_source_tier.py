"""来源可信度分级：判定规则、入库剔除与排序。

背景：地缘政治、突发事件这类题材里，论坛帖与自媒体洗稿会被大量召回
（实测搜"全球军事冲突"直接召回虎扑小说帖）。它们可以当线索，不能当事实。
这组测试锁住三件事：

1. 判定只认明确特征，认不出来的走 ``general``（宁可漏判，不可误杀）；
2. 低级别材料默认不进清单，但**全被判低时保留**（不能把整轮打空）；
3. 兜底检索意图里始终有一条地缘/事件类——规划降级时最不该丢的就是它。
"""

from __future__ import annotations

from moneyrouter_agent.domain.finance import (
    DEFAULT_QUERIES,
    TIER_GENERAL,
    TIER_LOW,
    TIER_MAINSTREAM,
    TIER_OFFICIAL,
    PlannedQuery,
    classify_source,
    host_of,
)
from moneyrouter_agent.graph.finance_nodes import make_search
from moneyrouter_agent.prompts.finance import render_evidence
from moneyrouter_agent.tools.bocha import SearchHit, SearchOutcome, _to_hit

# --------------------------------------------------------------------------- #
# 判定规则
# --------------------------------------------------------------------------- #
OFFICIAL_CASES = [
    ("中国人民银行", "http://www.pbc.gov.cn/goutongjiaoliu/144859/index.html"),
    ("国家统计局", "https://www.stats.gov.cn/sj/zxfb/202609/t20260915.html"),
    ("中国证监会", "http://www.csrc.gov.cn/csrc/c100028/index.shtml"),
    ("上海证券交易所", "https://www.sse.com.cn/market/bonddata/"),
    ("中证指数有限公司", "https://www.csindex.com.cn/#/indices/family/detail?indexCode=000300"),
    ("新华社", "http://www.news.cn/fortune/20260915/abc/c.html"),
]

MAINSTREAM_CASES = [
    ("财新网", "https://www.caixin.com/2026-10-05/102030405.html"),
    ("第一财经", "https://www.yicai.com/news/102345678.html"),
    ("证券时报", "https://www.stcn.com/article/detail/1234567.html"),
    ("新浪财经", "https://finance.sina.com.cn/headline/2026-10-03/doc-initwzsm0842326.shtml"),
    ("腾讯新闻", "https://news.qq.com/rain/a/20261005A02B0A00"),
    ("财联社", "https://www.cls.cn/detail/1234567"),
]

LOW_CASES = [
    ("虎扑", "https://bbs.hupu.com/642696803.html"),
    ("百度贴吧", "https://tieba.baidu.com/p/1234567890"),
    ("知乎", "https://www.zhihu.com/question/123456/answer/789"),
    ("今日头条", "https://www.toutiao.com/article/7691259320243077666/"),
    ("微信公众号", "https://mp.weixin.qq.com/s/AbCdEfGhIjKlMn"),
    ("百家号", "https://baijiahao.baidu.com/s?id=1234567890"),
    ("雪球", "https://xueqiu.com/1234567890/234567890"),
    # 正规站点的自媒体路径：同域名，但内容主体无法核实
    ("搜狐", "https://www.sohu.com/a/1084087573_99992453"),
    ("凤凰网", "https://feng.ifeng.com/c/8wy5gvYtY22"),
    ("网易", "https://m.163.com/dy/article/L8F7RSL505198RSU.html"),
]


def test_official_sources():
    for site, url in OFFICIAL_CASES:
        assert classify_source(site, url) == TIER_OFFICIAL, url


def test_mainstream_sources():
    for site, url in MAINSTREAM_CASES:
        assert classify_source(site, url) == TIER_MAINSTREAM, url


def test_low_sources():
    for site, url in LOW_CASES:
        assert classify_source(site, url) == TIER_LOW, url


def test_unknown_source_is_general():
    """认不出来的一律 general——误判成 low 会丢掉真实材料。"""
    assert classify_source("某地方财经", "https://www.example-finance.com/a/1.html") == TIER_GENERAL
    assert classify_source("", "") == TIER_GENERAL
    assert classify_source("华图教育", "https://ah.huatu.com/2026/1005/3288185.html") == TIER_GENERAL


def test_gov_suffix_is_trusted():
    """gov.cn 是受控命名空间，按后缀信任，不必逐个列。"""
    assert classify_source("某省财政厅", "https://czt.someprovince.gov.cn/xwzx/1.html") == TIER_OFFICIAL
    assert classify_source("", "http://whitehouse.gov/briefing/") == TIER_OFFICIAL


def test_low_wins_over_domain():
    """降级先于升级：网易号走的是 163 域名，但仍是自媒体。"""
    assert classify_source("网易", "https://m.163.com/dy/article/L8F7.html") == TIER_LOW
    # 同一域名下的正规新闻频道不受影响
    assert classify_source("网易新闻", "https://news.163.com/26/1005/07/ABC.html") == TIER_MAINSTREAM


def test_host_of_handles_messy_urls():
    assert host_of("https://user:pw@www.PBC.gov.cn:8443/a/b?c=1") == "www.pbc.gov.cn"
    assert host_of("http://example.com") == "example.com"
    assert host_of("") == ""
    assert host_of("not a url") == "not a url"


def test_to_hit_carries_tier():
    """检索器解析响应时就把级别算好，入库节点不必再查一遍域名表。"""
    hit = _to_hit(
        {"url": "https://bbs.hupu.com/642696803.html", "name": "帖子", "siteName": "虎扑"},
        "q",
        "财经新闻与政策",
    )
    assert hit is not None and hit.tier == TIER_LOW


# --------------------------------------------------------------------------- #
# 入库节点
# --------------------------------------------------------------------------- #
def _hit(url: str, *, site: str = "示例站点", title: str = "标题") -> SearchHit:
    return SearchHit(
        query="查询", topic="财经新闻与政策", title=title, url=url,
        site=site, published_at="2026-10-05", snippet="片段",
    )


def _searcher(hits: list[SearchHit]):
    def search(_queries: list[PlannedQuery]) -> SearchOutcome:
        return SearchOutcome(hits=list(hits), errors=[])

    return search


def _run(hits: list[SearchHit], *, drop_low_tier: bool = True) -> dict:
    node = make_search(_searcher(hits), drop_low_tier=drop_low_tier)
    return node({"pending_queries": [PlannedQuery(query="查询", topic="财经新闻与政策")]})


MIXED = [
    _hit("https://bbs.hupu.com/1.html", site="虎扑"),
    _hit("https://www.caixin.com/2.html", site="财新网"),
    _hit("http://www.pbc.gov.cn/3.html", site="中国人民银行"),
    _hit("https://www.toutiao.com/article/4/", site="今日头条"),
    _hit("https://www.example.com/5.html", site="某站点"),
]


def test_low_tier_materials_are_dropped():
    patch = _run(MIXED)
    urls = [item.url for item in patch["evidence"]]
    assert not any("hupu" in url or "toutiao" in url for url in urls)
    assert len(patch["evidence"]) == 3
    assert any("已剔除 2 条" in line for line in patch["trace"])


def test_kept_materials_are_ordered_by_tier():
    """编号顺序 = 分析时的呈现顺序，官方发布必须排在最前。"""
    patch = _run(MIXED)
    assert [item.tier for item in patch["evidence"]] == [
        TIER_OFFICIAL,
        TIER_MAINSTREAM,
        TIER_GENERAL,
    ]
    assert [item.id for item in patch["evidence"]] == [1, 2, 3]


def test_all_low_batch_is_kept():
    """全被判成低级别时保留——否则这轮就真的空手而归，比拿到带瑕疵的线索更糟。"""
    patch = _run([
        _hit("https://bbs.hupu.com/1.html", site="虎扑"),
        _hit("https://www.toutiao.com/article/2/", site="今日头条"),
    ])
    assert len(patch["evidence"]) == 2
    assert not any("已剔除" in line for line in patch["trace"])


def test_drop_can_be_disabled():
    """关掉开关后只排序不剔除（保留现场，便于排查召回质量）。"""
    patch = _run(MIXED, drop_low_tier=False)
    assert len(patch["evidence"]) == 5
    assert patch["evidence"][-1].tier == TIER_LOW
    assert not any("已剔除" in line for line in patch["trace"])


def test_tier_breakdown_in_trace():
    patch = _run(MIXED)
    joined = " ".join(patch["trace"])
    assert "来源级别" in joined
    assert "官方发布 1" in joined and "主流财经媒体 1" in joined


# --------------------------------------------------------------------------- #
# 提示词侧
# --------------------------------------------------------------------------- #
def test_render_evidence_shows_level():
    patch = _run(MIXED)
    text = render_evidence(patch["evidence"])
    assert "级别：官方发布" in text
    assert "级别：主流财经媒体" in text


def test_default_queries_cover_geopolitics():
    """兜底意图的地缘线是回归锁：规划降级时，事件类信息最不该消失。"""
    joined = " ".join(query.query for query in DEFAULT_QUERIES)
    assert any(word in joined for word in ("地缘", "冲突", "国际事件"))


def test_default_query_topics_are_valid():
    from moneyrouter_agent.domain.finance import TOPICS

    for query in DEFAULT_QUERIES:
        assert query.topic in TOPICS
