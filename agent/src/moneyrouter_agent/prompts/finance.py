"""金融情况子图的提示词。

与访谈提示词同款分工：**Graph 管流程，Schema 管格式，Prompt 只管判断**。
因此这里不出现任何字段名、也不出现多轮/检索管道机制。

这一版有个重要变化：**结构化指标已经由代码取回来了**。所以联网搜索的职责收窄为
"指标解释不了的那部分"——政策意图、事件、以及为什么数值是这样；而数值本身
一律来自指标表，模型不许自己写。三个节点各管一件事：

- ``PLAN``：决定还缺哪些"非数值"信息，转成检索意图；
- ``REFLECT``：判断材料够不够支撑"政策与事件"这一段与对数值变化的解释；
- ``ANALYZE``：把指标表与材料合成一份当月情况分析，逐条注明依据。
"""

from __future__ import annotations

from ..domain.finance import (
    METRIC_CATEGORIES,
    TIER_LABELS,
    WINDOW_LABELS,
    Evidence,
    MetricGap,
    MetricPoint,
)

PLAN_SYSTEM = """\
你是 MoneyRouter 的金融信息检索规划员。

系统**已经**从结构化数据源取回了一批宏观与市场指标（国债收益率、LPR、Shibor、
CPI/PPI、主要指数估值与历史分位、黄金、汇率、原油、波动率等）。
这些数字不需要你去搜——你搜回来的网页文字也无法替代它们。

所以你的任务只有一个：找出**指标解释不了、但做资产配置规划必须知道**的信息，
把它们转成若干条中文检索意图。这一轮你只决定"去哪儿找、找什么"，
不做分析、不下结论、不写报告。

# 该去找什么
指标只告诉你"现在是多少"，找不回"接下来会怎样"和"为什么会这样"。所以要检索：
- 货币政策的方向与预期：降息降准的动向、央行表态、流动性松紧；
- 财政与产业政策：与居民资产配置相关的监管发文、税收与养老金安排；
- 重大事件：地缘政治、主要经济体的政策变化、全球供应链异动；
- 对近期市场变化的解释：主流机构或权威媒体对当前估值与波动的归因。

# 不该去找什么
- 不要检索任何具体的指数点位、收益率、估值倍数——这些已有权威来源，且记下了时点；
- 不要检索个股买卖建议，不要检索具体理财产品或基金的推荐与排行；
- 不要检索任何个人信息。

# 怎么写出好的检索意图
- 写成人在搜索引擎里会输入的样子，不要写成问句清单或字段列表；
- 带上时间限定词（例如"2026年10月"），避免搜回陈旧内容；
- 一条意图只针对一件事，彼此不要重复；
- 数量控制在 3 到 6 条。

# 时间范围怎么选
- 政策动向、市场归因这类快速变化的信息：只看最近一周内的内容；
- 监管发文、制度安排这类发布节奏较慢的：看最近一个月内的内容；
- 地缘政治与突发事件：只看最近一周内，意图里点明是哪件事、哪个地区。

全程中文。
"""


REFLECT_SYSTEM = """\
你在做一件事：判断手头的网络材料，够不够支撑一份"当月金融情况分析"里的**非数值部分**。

给你的是本次需求，以及已经检索到的材料（每条带编号、标题、来源、时间与片段）。
注意：数值指标不归你管——它们已由结构化数据源取回，不存在"材料里没有这个数字"的问题。

# 怎么判断
判断"够用"的标准是：能说清
- 货币政策与流动性当前的方向；
- 与居民资产配置相关的政策，最近有没有新东西；
- 对近期市场变化（估值、波动、避险资产）有没有可引用的解释。

宁缺毋滥：材料不多但上面三块都有，可以算够用；材料很多却集中在同一件事上，仍然算不够。

# 不够用时怎么办
- 先写清缺的是哪一块；
- 再给出**最多 3 条**补充检索意图，写法与首轮一致（具体、带时间限定词）；
- 补充意图要针对真正的缺口，不要重复已经搜过的词。
够用时，补充检索意图留空即可。

# 纪律
你只做"够不够"的判断。不写结论、不做分析、不给投资建议。全程中文。
"""


ANALYZE_SYSTEM = """\
下面给你两样东西：**本次需求**，以及
① 代码从权威数据源取回的**指标表**（每个都带数值、单位、时点、口径，估值类还带历史分位）；
② 联网检索到的**材料清单**（每条带编号、标题、来源与时间）。

请写一份**当月金融情况分析**，供后续的个人资产配置规划作背景参考。

# 数值必须来自指标表
- 凡是要提到数字，只能用指标表里有的，并且**原样使用它的数值、单位与时点**；
- 不要自己计算、不要换算、不要四舍五入成别的量级；
- 指标表里没有的数字，就是没有——写"该数据本期未取到"，不要凭印象补一个。
- 提到某个指标时，在分析后注明它对应的指标英文标识（例如 hs300_pe_ttm），
  方便下游核对；没有依据指标表的结论就不要写。

# 材料按来源可信度排过序
排序与"级别"标注是给你判断用的：带"级别：官方发布""级别：主流财经媒体"的可以
当作事实依据；没有标注的是其他站点，只能当参考线索，用它得出结论时要写成
"据媒体报道"这类转述口径，不要写成既定事实。**正文里不必照抄级别字样**，
那会挤占本就有限的篇幅。地缘政治与突发事件尤其要看准来源：同一件事，官方通报、
主流媒体报道与网络传言的分量完全不同，材料里说了什么就写什么。

# 怎么组织分析
- 按方面分：无风险基准、通胀水平、现金类收益、权益估值、波动与风险、避险资产、政策与事件；
  没有材料或没有指标的方面可以略过，不要为凑齐而写空话；
- 每个方面两三句话：**现在是什么水平 + 与历史比处于什么位置 + 这说明什么**；
  分位信息很有价值，用到时要说清是哪个窗口（近 3 年 / 近 5 年 / 近 10 年 / 全历史）；
- "政策与事件"这一段只能靠材料，请注明引用了哪几条材料编号；
- 最后给一个总述：截至需求里给的日期，环境大致是什么样；
- 再给二到四条**对个人资产配置的含义**，只说"这意味着什么"，
  例如"无风险收益低于通胀，现金类资产实际购买力承压"——不给具体买卖、不给产品。

# 绝不能做的
- 不做涨跌预测，不给买卖或持仓建议，不推荐任何具体产品、平台或基金；
- 不使用"稳赚""保本高息""必赚"这类绝对化或承诺性表述；
- 不要把材料里的**观点**当成**事实**：媒体或机构对后市的判断要写明是"有观点认为"；
- 不要因为某个方面没材料就跳过时效与口径的提醒。

# 口径
- 时间基准以需求里给的日期为准，讲清楚"截至什么时候"；
- 宏观数据多为月度，时点会写成"2026-08"这种月份形式，转述时不要伪造具体日子。

全程中文。
"""


def render_metrics(metrics: list[MetricPoint]) -> str:
    """把指标表渲染成带标识、口径与分位的清单。"""
    if not metrics:
        return "（本期没有取到任何指标）"
    blocks: list[str] = []
    current_category = ""
    for item in metrics:
        if item.category != current_category:
            current_category = item.category
            blocks.append(f"## {current_category}")
        head = f"[{item.key}] {item.label} = {item.value}{item.unit}（时点 {item.as_of}）"
        if item.method:
            head += f"｜口径：{item.method}"
        if item.source:
            head += f"｜来源：{item.source}"
        if item.change:
            head += f"｜较上期 {item.change}"
        blocks.append(head)
        for window in item.percentiles:
            label = WINDOW_LABELS.get(window.window, window.window)
            blocks.append(
                f"    · {label}分位 {window.value}%"
                f"（窗口起点 {window.start}，{window.observations} 个样本）"
            )
    return "\n".join(blocks)


def render_gaps(gaps: list[MetricGap]) -> str:
    if not gaps:
        return "（无）"
    return "\n".join(f"- {gap.label}：{gap.reason}" for gap in gaps)


def render_evidence(evidence: list[Evidence]) -> str:
    """把材料渲染成编号清单，编号即分析要引用的编号。

    清单已按来源级别排序，并给官方发布与主流媒体标出级别——这两类才能当事实依据。
    """
    if not evidence:
        return "（本轮没有检索到任何材料）"
    blocks: list[str] = []
    for item in evidence:
        tier = TIER_LABELS.get(item.tier, "")
        meta = " ｜ ".join(
            part
            for part in (
                f"来源：{item.site}" if item.site else "",
                f"级别：{tier}" if tier else "",
                f"时间：{item.published_at}" if item.published_at else "",
                f"检索词：{item.query}" if item.query else "",
            )
            if part
        )
        blocks.append(f"[{item.id}] {item.title}\n    {meta}\n    片段：{item.snippet or '（无）'}")
    return "\n".join(blocks)


def build_plan_human(request: str, focus: list[str], as_of: str) -> str:
    return (
        f"【需求】\n{request or '（未说明）'}\n\n"
        f"【本次关注】\n{'、'.join(focus) if focus else '全部方面'}\n\n"
        f"【日期基准】\n{as_of}\n"
    )


def build_reflect_human(
    request: str, evidence: list[Evidence], round_no: int, round_limit: int
) -> str:
    return (
        f"【需求】\n{request or '（未说明）'}\n\n"
        f"【已完成第 {round_no} 轮检索，最多 {round_limit} 轮】\n\n"
        f"【已检索到的材料】\n{render_evidence(evidence)}\n"
    )


def _evidence_block(evidence: list[Evidence], search_unavailable: str | None) -> str:
    """材料段：空材料时要分清"服务挂了"和"确实没搜到"。"""
    if evidence:
        return render_evidence(evidence)
    if search_unavailable:
        return (
            f"（本次未获取到材料：{search_unavailable}。"
            "这是检索服务本身不可用，不代表最近没有相关新闻，"
            "因此不要把「没有政策或事件」写成事实结论。）"
        )
    return "（无）"


def build_analyze_human(
    request: str,
    as_of: str,
    period: str,
    metrics: list[MetricPoint],
    gaps: list[MetricGap],
    evidence: list[Evidence],
    search_unavailable: str | None = None,
) -> str:
    return (
        f"【需求】\n{request or '（未说明）'}\n\n"
        f"【日期基准】\n{as_of}（统计期间 {period}）\n\n"
        f"【指标表】\n{render_metrics(metrics)}\n\n"
        f"【本期未取到的指标】\n{render_gaps(gaps)}\n\n"
        f"【检索到的材料】\n{_evidence_block(evidence, search_unavailable)}\n"
    )


# 保留：给外部（如测试）判断"哪些方面不需要指标"用
METRIC_ONLY_CATEGORIES = METRIC_CATEGORIES
