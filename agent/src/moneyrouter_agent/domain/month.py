"""本月实况领域模型：分类体系、月度实况快照、基线与目标达成。

设计要点：

- **混合来源**：每个金额事实都带 ``source``（``file`` 账单 / ``stated`` 口述）；
- **计算字段只由代码写**（合计、结余、基线偏差、目标进度）——模型没有写入通路；
- 未了解到一律留 ``None`` / 空，不给默认值、不推测；``0`` 是合法值；
- 字段语义写在 ``Field(description=...)`` 里，交给结构化输出；
- **一份快照装下这个月的全景**：收入、分类支出、结余、结余去向（投资/非投资）、
  **已有投资的收益**、目标达成与近月趋势——下游既能直接出图，也能直接接着分析；
- 结构**可视化就绪**：分类占比、预算执行率、环比/近月趋势、目标达成进度、收益走势、异常标注
  都能从这份快照里直接取到（见 README「可视化就绪」）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# 分类体系：固定枚举保证可比可复算；原始类目→枚举用关键词映射表（改数据即可扩展）
# --------------------------------------------------------------------------- #
SpendCategory = Literal[
    "餐饮",
    "居住",
    "交通",
    "购物",
    "娱乐社交",
    "医疗健康",
    "学习成长",
    "人情往来",
    "订阅服务",
    "其他",
]

SPEND_CATEGORIES: tuple[str, ...] = (
    "餐饮",
    "居住",
    "交通",
    "购物",
    "娱乐社交",
    "医疗健康",
    "学习成长",
    "人情往来",
    "订阅服务",
    "其他",
)

CATEGORY_RANK: dict[str, int] = {name: index for index, name in enumerate(SPEND_CATEGORIES)}


def category_rank(category: str) -> int:
    """按 :data:`SPEND_CATEGORIES` 顺序排序，让输出/图表顺序稳定。"""
    return CATEGORY_RANK.get(category, len(SPEND_CATEGORIES))


# 原始类目 → 固定枚举。子串命中即归类；匹配时**按关键词长度倒序**，避免「餐」抢先命中「外卖」这类歧义。
RAW_CATEGORY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("外卖", "餐饮"),
    ("food", "餐饮"),
    ("餐饮", "餐饮"),
    ("餐", "餐饮"),
    ("房租", "居住"),
    ("水电", "居住"),
    ("物业", "居住"),
    ("燃气", "居住"),
    ("地铁", "交通"),
    ("公交", "交通"),
    ("打车", "交通"),
    ("加油", "交通"),
    ("超市", "购物"),
    ("淘宝", "购物"),
    ("京东", "购物"),
    ("拼多多", "购物"),
    ("数码", "购物"),
    ("电子", "购物"),
    ("手机", "购物"),
    ("电脑", "购物"),
    ("服饰", "购物"),
    ("电影", "娱乐社交"),
    ("游戏", "娱乐社交"),
    ("聚餐", "娱乐社交"),
    ("娱乐", "娱乐社交"),
    ("医院", "医疗健康"),
    ("买药", "医疗健康"),
    ("药", "医疗健康"),
    ("图书", "学习成长"),
    ("书", "学习成长"),
    ("课程", "学习成长"),
    ("培训", "学习成长"),
    ("学费", "学习成长"),
    ("红包", "人情往来"),
    ("转账", "人情往来"),
    ("会员", "订阅服务"),
    ("订阅", "订阅服务"),
)

# 预排序：长关键词优先
_SORTED_KEYWORDS: tuple[tuple[str, str], ...] = tuple(
    sorted(RAW_CATEGORY_KEYWORDS, key=lambda item: len(item[0]), reverse=True)
)

# 收入类目的关键词（账单里识别收入行）
INCOME_KEYWORDS: tuple[str, ...] = ("工资", "薪资", "收入", "生活费", "奖金", "报销", "salary", "income")

SOURCE_FILE: str = "file"
SOURCE_STATED: str = "stated"


def normalize_category(raw: str | None) -> str:
    """把账单原始类目归一成固定枚举；认不出就落 ``其他``。

    纯函数、幂等：``normalize_category(normalize_category(x)) == normalize_category(x)``。
    """
    text = (raw or "").strip().lower()
    if not text:
        return "其他"
    if text in CATEGORY_RANK:  # 已经是枚举值
        return text
    for keyword, category in _SORTED_KEYWORDS:
        if keyword in text:
            return category
    return "其他"


def is_income_text(raw: str | None) -> bool:
    """账单类目/摘要是否像一笔收入。"""
    text = (raw or "").strip().lower()
    return any(keyword in text for keyword in INCOME_KEYWORDS)


# --------------------------------------------------------------------------- #
# 事实模型（混合来源）
# --------------------------------------------------------------------------- #
Source = Literal["file", "stated"]


class CategorySpend(BaseModel):
    """一个分类本月的支出。"""

    model_config = ConfigDict(extra="ignore")

    category: str = Field(description="消费分类（固定枚举之一）。")
    amount_cents: int = Field(description="该分类本月支出，单位「分」；0 是合法值。")
    source: Source = Field(
        default="stated", description="这个数字来自账单文件（file）还是用户口述（stated）。"
    )
    evidence: str = Field(default="", description="出处：账单行摘要，或用户原话要点。")


class IncomeFact(BaseModel):
    """本月收入事实。"""

    model_config = ConfigDict(extra="ignore")

    amount_cents: int = Field(description="本月收入，单位「分」。")
    basis: str = Field(default="", description="口径，如「税后月薪」「每月生活费」「接单收入」。")
    source: Source = Field(default="stated", description="文件（file）还是口述（stated）。")
    evidence: str = Field(default="", description="出处说明。")


class OneOffItem(BaseModel):
    """一次性大额项（换手机、看病、旅行……）。"""

    model_config = ConfigDict(extra="ignore")

    label: str = Field(description="这笔一次性支出的名字，如「换手机」「住院」。")
    amount_cents: int = Field(description="金额，单位「分」。")
    source: Source = Field(default="stated", description="文件（file）还是口述（stated）。")
    evidence: str = Field(default="", description="出处说明。")


class Baseline(BaseModel):
    """某分类（或总体）与一个对比基线的偏差。**由代码算好，模型不可写。**"""

    model_config = ConfigDict(extra="ignore")

    metric: Literal["budget", "last_month", "trailing_3m_avg"] = Field(
        description="对比哪种基线：预算 / 上月 / 近三月均值。"
    )
    category: str = Field(default="", description="分类名；空串表示总体（不分品类）。")
    amount_cents: int = Field(description="基线值，单位「分」。")
    delta_cents: int = Field(default=0, description="本月 − 基线，正数表示超出基线。")
    delta_pct: float | None = Field(default=None, description="偏差百分比；基线为 0 时留空。")


class MonthlyPoint(BaseModel):
    """一个月的总体收支点，供趋势图使用。"""

    model_config = ConfigDict(extra="ignore")

    period: str = Field(description="统计期间 YYYY-MM。")
    income_cents: int | None = Field(default=None, description="当月收入（分）。")
    spend_total_cents: int | None = Field(default=None, description="当月支出合计（分）。")
    balance_cents: int | None = Field(default=None, description="当月结余（分）。")
    investment_return_cents: int | None = Field(
        default=None, description="当月投资盈亏（分），亏损为负数；供收益趋势图。"
    )


class GoalAlignment(BaseModel):
    """目标达成情况——**为可视化准备**；派生字段一律由代码算。"""

    model_config = ConfigDict(extra="ignore")

    goal: str = Field(default="", description="目标描述（来自用户画像，可注入）。")
    target_cents: int | None = Field(default=None, description="目标金额（分）。")
    horizon_months: int | None = Field(default=None, description="期限（月）。")
    planned_this_month_cents: int | None = Field(
        default=None, description="本月计划攒下的钱（分，来自方案预算，可注入）。"
    )
    saved_this_month_cents: int | None = Field(
        default=None, description="本月实际净攒（分）＝结余，代码算。"
    )
    saved_to_date_cents: int | None = Field(
        default=None, description="累计已攒（分，外部注入或历史累计）。"
    )
    progress_pct: float | None = Field(default=None, description="累计进度百分比，代码算。")
    on_track: bool | None = Field(default=None, description="是否在计划轨道上，代码算。")
    projected_months: int | None = Field(
        default=None, description="按本月速度预计还需多少个月达成，代码算。"
    )


class FundAllocation(BaseModel):
    """本月结余（收入−支出）的去向：**非投资**（留着备用/储蓄）与**投资**各占多少。

    PRD 的资金分层里，只有一部分会拿去承担风险、做财富增值；应急金、活期、保守储蓄
    这类"不投资"的资金同样要如实掌握——否则"攒了多少"和"投了多少"会混成一笔账。
    """

    model_config = ConfigDict(extra="ignore")

    non_invested_cents: int | None = Field(
        default=None,
        description=(
            "本月**不投入市场**的资金（应急金/活期/保守储蓄等），单位「分」；0 是合法值。"
            "未了解到就留空。"
        ),
    )
    invested_cents: int | None = Field(
        default=None, description="本月实际**投入投资**的资金，单位「分」；0 是合法值。"
    )
    unallocated_cents: int | None = Field(
        default=None, description="结余 − 非投资 − 投资，代码算；非 0 表示还有部分去向没对上。"
    )
    note: str = Field(default="", description="资金去向的补充说明。")
    source: Source = Field(
        default="stated",
        description="这段去向来自账单文件（file）还是对话口述（stated）；文件来源的整段优先。",
    )
    evidence: str = Field(default="", description="出处说明。")


class HoldingReturn(BaseModel):
    """一笔**已有投资**的收益情况（本月与累计）。金额一律「分」，**亏损为负数**。

    与 :class:`FundAllocation` 互补：后者记"本月新投出去多少"，这里记"已经投出去的赚赔多少"。
    ``cost_cents`` / ``market_value_cents`` / ``total_return_cents`` 三者满足
    ``市值 = 成本 + 累计收益``，只要给到其中两个，第三个由代码推出来（见 :func:`compute_investments`）。
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(description="这笔投资的名称，如「沪深300指数基金」「某银行理财」。")
    kind: str = Field(
        default="",
        description="类别，如 基金 / 股票 / 银行理财 / 存款 / 债券 / 保险 / 其他；不确定留空。",
    )
    cost_cents: int | None = Field(default=None, description="本金 / 持仓成本（分），未了解到留空。")
    market_value_cents: int | None = Field(default=None, description="当前市值 / 持仓金额（分）。")
    month_return_cents: int | None = Field(
        default=None, description="本月收益（分），亏损为负数；0 是合法值。"
    )
    total_return_cents: int | None = Field(
        default=None, description="累计收益（分），亏损为负数；0 是合法值。"
    )
    month_return_pct: float | None = Field(default=None, description="本月收益率（%），代码算；算不出留空。")
    total_return_pct: float | None = Field(default=None, description="累计收益率（%），代码算；算不出留空。")
    source: Source = Field(default="stated", description="文件（file）还是口述（stated）。")
    evidence: str = Field(default="", description="出处说明。")


class InvestmentSnapshot(BaseModel):
    """已有投资的整体收益情况——**为可视化和后续分析准备**；派生字段一律由代码算。"""

    model_config = ConfigDict(extra="ignore")

    has_investments: bool | None = Field(
        default=None,
        description="用户是否持有投资 / 理财：未了解到留空，明确说没有就填 false（false 是有效结论）。",
    )
    holdings: list[HoldingReturn] = Field(
        default_factory=list, description="逐个产品的收益；没报到的不要编。"
    )
    cost_total_cents: int | None = Field(default=None, description="持仓成本合计（分），代码算。")
    market_value_total_cents: int | None = Field(default=None, description="当前市值合计（分），代码算。")
    month_return_cents: int | None = Field(default=None, description="本月收益合计（分），代码算。")
    total_return_cents: int | None = Field(default=None, description="累计收益合计（分），代码算。")
    total_return_pct: float | None = Field(default=None, description="累计收益率（%），代码算。")
    note: str = Field(default="", description="补充说明（持仓风格、期限、顾虑等），非对话转录。")
    source: Source = Field(
        default="stated",
        description="这段收益来自账单文件（file）还是对话口述（stated）；文件来源的整段优先。",
    )


class WalletExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wallet_id: str = Field(min_length=1)
    amount_cents: int = Field(ge=0, strict=True, description="本月已执行金额，不是待安排金额")
    evidence: str = Field(min_length=1)


class PaymentObligation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, description="稳定标识，同一费用不可重复记录")
    category: str = Field(description="系统消费分类或债务还款")
    label: str
    amount_cents: int = Field(ge=0, strict=True)
    evidence: str = Field(min_length=1, description="用户明确说明的尚未支付义务")


class LifeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    label: str
    evidence: str = Field(min_length=1, description="用户确认的假期、实习、旅行等，不能凭日历推测")
    affected_categories: list[str] = Field(default_factory=list)


class MonthSnapshot(BaseModel):
    """本月实况。口述部分由对话更新；文件来源与计算字段由代码维护。"""

    model_config = ConfigDict(extra="ignore")

    period: str = Field(default="", description="统计期间 YYYY-MM。")
    additional_funds_cents: int | None = Field(default=None, ge=0, strict=True, description="用户明确允许本月动用的收入外余额，未知不计入；不是全部储备")
    additional_funds_evidence: str = Field(default="", description="用户明确授权动用该余额的事实依据")
    wallet_execution: list[WalletExecution] = Field(default_factory=list, description="已执行储蓄或投资，关联既有钱包 id")
    obligations: list[PaymentObligation] = Field(default_factory=list, description="本月尚未支付的义务，不计入已花 categories")
    environment: list[LifeEvent] = Field(default_factory=list)
    spending_estimated: bool = False
    obligations_reviewed: bool = Field(default=False, description="已与用户核对尚未支付费用；没有费用也需明确核对")
    coverage_complete: bool | None = None
    coverage_start: str = ""
    coverage_end: str = ""
    review_required_count: int = 0
    accounting_basis: str = "platform"
    income: IncomeFact | None = Field(default=None, description="本月收入；未了解到就留空。")
    categories: list[CategorySpend] = Field(
        default_factory=list, description="各分类支出；没报到的分类不要编。"
    )
    spend_total_cents: int = Field(default=0, description="分类支出合计（分），代码算。")
    balance_cents: int | None = Field(
        default=None, description="结余＝收入−支出（分），收入未知时留空，代码算。"
    )
    allocation: FundAllocation = Field(
        default_factory=FundAllocation,
        description="结余的去向：多少留着不投资（应急金/储蓄）、多少投了出去。",
    )
    investments: InvestmentSnapshot = Field(
        default_factory=InvestmentSnapshot,
        description="已有投资的收益情况（本月与累计）。与结余去向互补：一个记新投出多少，一个记已投的赚赔。",
    )
    net_worth_change_cents: int | None = Field(
        default=None,
        description=(
            "本月净财富变动（分）＝结余 + 本月投资盈亏，代码算；两项缺一即留空。"
            "它把「攒下多少」与「投资赚赔多少」合成一个可直接呈现的总额。"
        ),
    )
    one_offs: list[OneOffItem] = Field(
        default_factory=list, description="一次性大额项（换手机、看病等）。"
    )
    baselines: list[Baseline] = Field(
        default_factory=list, description="与预算/上月/近三月均值的偏差，代码算。"
    )
    trailing: list[MonthlyPoint] = Field(
        default_factory=list, description="近若干月的总体收支序列，供趋势图，代码填。"
    )
    goal_alignment: GoalAlignment = Field(
        default_factory=GoalAlignment, description="目标达成情况，代码算。"
    )
    notes: str = Field(default="", description="额外了解要点（消费习惯、顾虑等），非对话转录。")
    unsettled: list[str] = Field(default_factory=list, description="仍缺的信息字段名，代码填。")


class MonthResult(BaseModel):
    """最终输出：规整的结构化快照 + 一段第三人称的实况结论。"""

    model_config = ConfigDict(extra="ignore")

    snapshot: MonthSnapshot = Field(default_factory=MonthSnapshot, description="规整后的月度实况快照。")
    articulation: str = Field(
        default="",
        description=(
            "本月实况结论（第三人称客观口吻）：收支概览、主要支出与异动、用户的归因、"
            "仍不确定的地方。只写结论不写过程——不复述对话怎么进行、不引用用户原话、逐条清点细节。"
        ),
    )


# --------------------------------------------------------------------------- #
# 计算（全部由代码执行，模型无写入通路）
# --------------------------------------------------------------------------- #
def category_map(categories: list[CategorySpend]) -> dict[str, int]:
    """分类 → 金额（分）的映射（同类合并求和）。"""
    out: dict[str, int] = {}
    for item in categories:
        out[item.category] = out.get(item.category, 0) + int(item.amount_cents)
    return out


def merge_categories(base: list[CategorySpend], incoming: list[CategorySpend]) -> list[CategorySpend]:
    """代码层 ``base`` 与模型口述 ``incoming`` 合并。

    规则：**账单来源（file）优先，口述不覆盖**；口述只补/改非账单来源的分类。
    模型若漏报某分类，``base`` 里的旧值保留（等价于 ``_fill_missing`` 语义）。
    """
    result: dict[str, CategorySpend] = {}
    locked: set[str] = set()
    for item in base:
        result[item.category] = item
        if item.source == SOURCE_FILE:
            locked.add(item.category)
    for item in incoming:
        if item.category in locked:
            continue
        result[item.category] = item
    ordered = sorted(result.values(), key=lambda c: category_rank(c.category))
    return ordered


def merge_holdings(base: list[HoldingReturn], incoming: list[HoldingReturn]) -> list[HoldingReturn]:
    """代码层 ``base`` 与口述 ``incoming`` 的持仓合并，规则同 :func:`merge_categories`。

    账单/代码来源（file）的持仓优先、口述不覆盖；口述只补/改非账单来源的持仓。
    合并按 ``name`` 去重，保持 ``base`` 的顺序在前。
    """
    result: dict[str, HoldingReturn] = {}
    locked: set[str] = set()
    for item in base:
        result[item.name] = item
        if item.source == SOURCE_FILE:
            locked.add(item.name)
    for item in incoming:
        if item.name in locked:
            continue
        result[item.name] = item
    return list(result.values())


def merge_investments(base: InvestmentSnapshot, incoming: InvestmentSnapshot) -> InvestmentSnapshot:
    """代码层与口述的投资视图合并。

    - **文件来源的整段优先**：``base`` 来自账单文件时，``has_investments`` 与口径都以它为准
      （持仓里的文件项本来就被 :func:`merge_holdings` 锁住）；
    - 否则 ``has_investments`` 以口述为准（这是用户才能回答的事实），口述没给才沿用 ``base``；
    - ``holdings`` 按 :func:`merge_holdings` 合并；``note`` 口述为空时沿用 ``base``。
    派生合计由 :func:`compute_investments` 重算。
    """
    from_file = base.source == SOURCE_FILE
    if from_file:
        has = base.has_investments
    else:
        has = incoming.has_investments if incoming.has_investments is not None else base.has_investments
    return incoming.model_copy(
        update={
            "has_investments": has,
            "holdings": merge_holdings(base.holdings, incoming.holdings),
            "note": incoming.note or base.note,
            "source": SOURCE_FILE if from_file else incoming.source,
        }
    )


def merge_allocation(base: FundAllocation, incoming: FundAllocation) -> FundAllocation:
    """代码层与口述的结余去向合并：**文件来源的整段优先**，其余以最新口述为准。

    口述这一轮整块都没提（两个金额都为空、也没有说明）时，沿用 ``base``，避免把已有信息抹掉。
    ``unallocated_cents`` 一律留待 :func:`compute_allocation` 重算。
    """
    if base.source == SOURCE_FILE:
        return base.model_copy(update={"unallocated_cents": None})
    empty_incoming = (
        incoming.non_invested_cents is None
        and incoming.invested_cents is None
        and not (incoming.note or "").strip()
    )
    if empty_incoming:
        return base.model_copy(update={"unallocated_cents": None})
    merged = incoming.model_copy(deep=True)
    if not merged.note:
        merged.note = base.note
    merged.unallocated_cents = None
    return merged


def past_snapshots(history: list[MonthSnapshot] | None, period: str) -> list[MonthSnapshot]:
    """只留下**严格早于本期**的历史，供基线/趋势使用。

    历史现在可以从留档里自动载入，所以必须有这道闸：否则同一个月会被拿来跟自己比
    （上月 = 本月、近三月均值含本月），基线全成了 0 偏差，反而看不出异动。
    期间为空的快照（外部手工注入、没标月份）保留——它们本来就是按位置表达"之前的月份"。
    """
    items = [s for s in (history or []) if s.coverage_complete is not False]
    if not period:
        return items
    return [snap for snap in items if not snap.period or snap.period < period]


def build_baselines(
    categories: list[CategorySpend],
    *,
    budget: dict[str, int] | None = None,
    history: list[MonthSnapshot] | None = None,
) -> list[Baseline]:
    """按注入的预算与历史，算出各分类/总体的偏差基线。

    - ``budget``：分类 → 预算（分）；键 ``""`` 表示总体预算。
    - ``history``：过去月份的实况快照（最近的在**末尾**），据此算上月与近三月均值。

    只对**有基线数据**的分类输出；缺就缺，不臆造。
    """
    current = category_map(categories)
    out: list[Baseline] = []

    def _pct(delta: int, base: int) -> float | None:
        return round(delta / base * 100.0, 2) if base else None

    def _add(metric: str, base_map: dict[str, int]) -> None:
        for category, base_value in base_map.items():
            cur = current.get(category, 0)
            out.append(
                Baseline(
                    metric=metric,  # type: ignore[arg-type]
                    category=category,
                    amount_cents=int(base_value),
                    delta_cents=cur - int(base_value),
                    delta_pct=_pct(cur - int(base_value), int(base_value)),
                )
            )

    if budget:
        _add("budget", {k: v for k, v in budget.items() if v is not None})

    hist = list(history or [])
    if hist:
        _add("last_month", category_map(hist[-1].categories))
    if hist:
        window = hist[-3:]
        totals: dict[str, int] = {}
        counts: dict[str, int] = {}
        for snap in window:
            for category, amount in category_map(snap.categories).items():
                totals[category] = totals.get(category, 0) + amount
                counts[category] = counts.get(category, 0) + 1
        avg = {category: round(totals[category] / counts[category]) for category in totals}
        _add("trailing_3m_avg", avg)

    out.sort(key=lambda b: (b.metric, category_rank(b.category)))
    return out


def build_trailing(history: list[MonthSnapshot] | None, current: MonthSnapshot) -> list[MonthlyPoint]:
    """把历史快照 + 当月拼成趋势序列（供折线/柱状图）。没有历史就只留当月。"""
    points: list[MonthlyPoint] = []
    for snap in history or []:
        points.append(
            MonthlyPoint(
                period=snap.period,
                income_cents=(snap.income.amount_cents if snap.income else None),
                spend_total_cents=snap.spend_total_cents,
                balance_cents=snap.balance_cents,
                investment_return_cents=snap.investments.month_return_cents,
            )
        )
    points.append(
        MonthlyPoint(
            period=current.period,
            income_cents=(current.income.amount_cents if current.income else None),
            spend_total_cents=current.spend_total_cents,
            balance_cents=current.balance_cents,
            investment_return_cents=current.investments.month_return_cents,
        )
    )
    return points


def accumulated_balance_cents(history: list[MonthSnapshot] | None) -> int | None:
    """历史各月结余之和——"累计已攒"的口径。没有任何可用结余时返回 ``None``（不补 0）。"""
    balances = [snap.balance_cents for snap in (history or []) if snap.balance_cents is not None]
    return sum(balances) if balances else None


def compute_goal_alignment(
    *,
    balance_cents: int | None,
    goal: GoalAlignment | None = None,
    saved_to_date_cents: int | None = None,
) -> GoalAlignment:
    """算出目标达成的派生字段（进度、是否在轨、预计还需月数）。"""
    base = goal.model_copy(deep=True) if goal is not None else GoalAlignment()

    base.saved_this_month_cents = balance_cents
    if saved_to_date_cents is not None:
        base.saved_to_date_cents = saved_to_date_cents
    elif base.saved_to_date_cents is None and base.target_cents is not None and balance_cents is not None:
        # 没有历史累计时，不臆造——留空由外部注入
        base.saved_to_date_cents = None

    if base.target_cents and base.saved_to_date_cents is not None:
        base.progress_pct = round(base.saved_to_date_cents / base.target_cents * 100.0, 2)

    if base.planned_this_month_cents is not None and balance_cents is not None:
        base.on_track = balance_cents >= base.planned_this_month_cents
    else:
        base.on_track = None

    if (
        base.target_cents is not None
        and base.saved_to_date_cents is not None
        and balance_cents is not None
        and balance_cents > 0
    ):
        remaining = base.target_cents - base.saved_to_date_cents
        base.projected_months = 0 if remaining <= 0 else -(-remaining // balance_cents)  # 向上取整
    else:
        base.projected_months = None

    return base


def compute_allocation(balance_cents: int | None, allocation: FundAllocation) -> FundAllocation:
    """算出结余去向里"还没对上的部分"（结余 − 非投资 − 投资）。"""
    alloc = allocation.model_copy(deep=True)
    if (
        balance_cents is not None
        and alloc.non_invested_cents is not None
        and alloc.invested_cents is not None
    ):
        alloc.unallocated_cents = balance_cents - alloc.non_invested_cents - alloc.invested_cents
    else:
        alloc.unallocated_cents = None
    return alloc


def _with_derived_holding(holding: HoldingReturn) -> HoldingReturn:
    """补齐单笔持仓里可由其余三项推出的那一项，并算出收益率。

    ``市值 = 成本 + 累计收益``：三者给到两个即可推出第三个（**缺就不推，不臆造**）。
    收益率算不出（分母缺失或为 0）时留空。
    """
    cost = holding.cost_cents
    value = holding.market_value_cents
    total = holding.total_return_cents
    month = holding.month_return_cents

    if total is None and cost is not None and value is not None:
        total = value - cost
    if value is None and cost is not None and total is not None:
        value = cost + total
    if cost is None and value is not None and total is not None:
        cost = value - total

    total_pct = round(total / cost * 100.0, 2) if (total is not None and cost) else None
    month_pct = None
    if month is not None and value is not None:
        start = value - month  # 月初市值
        if start:
            month_pct = round(month / start * 100.0, 2)

    return holding.model_copy(
        update={
            "cost_cents": cost,
            "market_value_cents": value,
            "total_return_cents": total,
            "total_return_pct": total_pct,
            "month_return_pct": month_pct,
        }
    )


def _sum_complete(holdings: list[HoldingReturn], attr: str) -> int | None:
    """按字段求和，但**要求每笔都有值**；有缺项就返回 ``None``（不部分求和、不补 0）。"""
    values = [getattr(item, attr) for item in holdings]
    if not values or any(value is None for value in values):
        return None
    return sum(int(value) for value in values)


def compute_investments(investment: InvestmentSnapshot) -> InvestmentSnapshot:
    """重算投资收益的派生字段：逐笔补齐 + 合计 + 累计收益率。**代码写入出口。**"""
    out = investment.model_copy(deep=True)
    out.holdings = [_with_derived_holding(holding) for holding in out.holdings]

    holdings = out.holdings
    out.cost_total_cents = _sum_complete(holdings, "cost_cents")
    out.market_value_total_cents = _sum_complete(holdings, "market_value_cents")
    out.month_return_cents = _sum_complete(holdings, "month_return_cents")
    out.total_return_cents = _sum_complete(holdings, "total_return_cents")
    out.total_return_pct = (
        round(out.total_return_cents / out.cost_total_cents * 100.0, 2)
        if (out.total_return_cents is not None and out.cost_total_cents)
        else None
    )
    return out


def recompute(
    snapshot: MonthSnapshot,
    *,
    budget: dict[str, int] | None = None,
    history: list[MonthSnapshot] | None = None,
    goal: GoalAlignment | None = None,
    saved_to_date_cents: int | None = None,
) -> MonthSnapshot:
    """把派生字段全部重算一遍（合计、结余、结余去向、基线、趋势、目标、收益合计）。

    **这是代码对快照的唯一写入出口**——模型交回来的派生字段一律被覆盖。

    ``saved_to_date_cents`` 是"累计已攒"的兜底来源（例如由历史月份结余累加而来）；
    目标里**显式给过** ``saved_to_date_cents`` 时以它为准，不被覆盖。
    """
    categories = sorted(snapshot.categories, key=lambda c: category_rank(c.category))
    spend_total = sum(int(c.amount_cents) for c in categories)
    balance = (snapshot.income.amount_cents - spend_total) if snapshot.income else None

    refreshed = snapshot.model_copy(
        update={"categories": categories, "spend_total_cents": spend_total, "balance_cents": balance}
    )
    refreshed.allocation = compute_allocation(balance, refreshed.allocation)
    refreshed.investments = compute_investments(refreshed.investments)
    past = past_snapshots(history, refreshed.period)
    refreshed.baselines = build_baselines(categories, budget=budget, history=past)
    refreshed.trailing = build_trailing(past, refreshed)

    effective_goal = goal or refreshed.goal_alignment
    explicit_saved = effective_goal.saved_to_date_cents
    refreshed.goal_alignment = compute_goal_alignment(
        balance_cents=balance,
        goal=effective_goal,
        saved_to_date_cents=explicit_saved if explicit_saved is not None else saved_to_date_cents,
    )
    refreshed.net_worth_change_cents = (
        balance + refreshed.investments.month_return_cents
        if (balance is not None and refreshed.investments.month_return_cents is not None)
        else None
    )

    unsettled: list[str] = []
    if refreshed.review_required_count:
        unsettled.append("bill_review")
    if refreshed.coverage_complete is False:
        unsettled.append("partial_period")
    if refreshed.income is None:
        unsettled.append("income")
    alloc = refreshed.allocation
    if (
        balance is not None
        and balance > 0
        and alloc.non_invested_cents is None
        and alloc.invested_cents is None
    ):
        unsettled.append("allocation")
    if refreshed.investments.has_investments is None:
        unsettled.append("investments")
    refreshed.unsettled = unsettled
    return refreshed
