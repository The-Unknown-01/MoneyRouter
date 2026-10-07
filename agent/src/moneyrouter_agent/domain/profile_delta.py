"""画像增量：**append-only 的事件流**。

PRD 的「更新用户画像」在这套实现里＝**只增不改**：画像那一版（用户确认过的 `Profile`）永远不动，
新的情况以**事件**追加进来。于是"这个月找到新工作""学习了金融知识""受伤了"都是事件；
而「伤好了」不是去改「受伤」那条，而是**再追加一条关闭事件**。

三条纪律：

- **写入后不可变**：事件的任何字段一旦落盘就不再改动；没有按 id 编辑/删除的入口。
  「已结束」是**读时派生**的（见 :func:`event_status` / :func:`event_ended_period`），**永不落盘**。
- **只设值、不清空**：:class:`ProfileFieldUpdate` 只表达"某个字段变成了什么"，
  **不允许用 ``None`` 表示"清空"**——要表达结束就用关闭事件。`0` 是合法值（如"负债已还清"）。
- **不碰画像的合并语义**：这里的叠加是"显式设值"，与画像访谈图的 ``_fill_missing``（缺就补旧）
  方向相反，两者**绝不能串**——串起来会让"已结束"的事件静默复活。

事件字段里的金额一律用「分」；未了解到留空、不补 0。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .profile import Profile

EventType = Literal[
    "income_change",
    "occupation_change",
    "debt_change",
    "family_change",
    "goal_change",
    "habit_change",
    "risk_pref_change",
    "learning",
    "life_event",
    "event_ended",
    "other",
]

EVENT_TYPES: tuple[str, ...] = (
    "income_change",
    "occupation_change",
    "debt_change",
    "family_change",
    "goal_change",
    "habit_change",
    "risk_pref_change",
    "learning",
    "life_event",
    "event_ended",
    "other",
)

# 关闭事件的类型：凡是 ``closes`` 非空的事件都必须是它
CLOSE_EVENT_TYPE = "event_ended"

EventDirection = Literal["up", "down", "set"]
EventStatus = Literal["active", "ended"]

# 影响的类别白名单——首版**只描述、不接算法**（不扩 experience.ExperienceEffects）
EFFECT_KINDS: tuple[str, ...] = (
    "category_cap",
    "reserve_target",
    "risk_notch",
    "income_delta",
    "none",
)

# 有明确的分配含义、可以折算成经验教训的影响类别 → 对应的 LessonKind
EFFECT_TO_LESSON_KIND: dict[str, str] = {
    "reserve_target": "reserve_priority",
    "risk_notch": "risk_conservative",
    "income_delta": "debt_priority",
}


class ProfileFieldUpdate(BaseModel):
    """事件携带的**画像字段候选值**——只"设值"，不改动原画像。

    与 :class:`~moneyrouter_agent.domain.profile.Profile` 的字段一一对应，
    取值规则也**复用同一套清理**（见 :meth:`as_updates`）：非法值清空、`0` 保留。
    """

    model_config = ConfigDict(extra="ignore")

    occupation: str | None = Field(default=None, description="职业的新值；未变留空。")
    income_cents: int | None = Field(default=None, description="收入的新值（分）；未变留空，0 合法。")
    income_basis: str | None = Field(default=None, description="收入口径的新值；未变留空。")
    income_stable: bool | None = Field(default=None, description="收入是否稳定的新值；未变留空。")
    family_load: bool | None = Field(default=None, description="家庭负担的新值；未变留空。")
    debt_cents: int | None = Field(default=None, description="负债总额的新值（分）；未变留空，0 表示已还清。")
    reserve_cents: int | None = Field(default=None, description="现有储备的新值（分）；未变留空，0 合法。")
    horizon_months: int | None = Field(default=None, description="资金期限的新值（月）；未变留空。")
    max_loss_pct: int | None = Field(default=None, description="可承受亏损的新值（%）；未变留空，0 合法。")
    experience: Literal["none", "some", "experienced"] | None = Field(
        default=None, description="投资经验的新值；未变留空。"
    )
    goal: str | None = Field(default=None, description="目标的新表述；未变留空。")

    def as_updates(self) -> dict[str, Any]:
        """只取"确实设了值"的字段，并**复用画像自身的清理规则**（非法值清空、`0` 保留）。"""
        provided = {key: value for key, value in self.model_dump().items() if value is not None}
        if not provided:
            return {}
        # 借 Profile 的 _sanitize 做一次归一：越界/空串会被清成 None，再被 exclude_none 丢掉
        return Profile(**provided).model_dump(exclude_none=True)


class EventEffect(BaseModel):
    """事件对某一层的**框定影响**。首版只描述、不接算法。"""

    model_config = ConfigDict(extra="ignore")

    kind: str = Field(description="影响类别（代码白名单）：category_cap / reserve_target / risk_notch / income_delta / none。")
    target: str = Field(default="", description="作用对象，如分类名；无则留空。")
    direction: EventDirection = Field(default="set", description="方向：up 提高 / down 收紧 / set 设定。")
    detail: str = Field(default="", description="定性说明。")
    amount_cents: int | None = Field(default=None, description="若确需金额（分），只由代码写；否则留空。")


class ProfileEvent(BaseModel):
    """画像增量：一条"发生的事"。**append-only，写入后不可改。**"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="稳定去重键，形如 `income_change:2026-09`（同月同型加 `#2`），由代码生成。")
    event_type: EventType = Field(description="事件类型，代码白名单。")
    statement: str = Field(description="第三人称的事实描述：发生了什么、影响是什么。不写过程、不引原话。")
    from_period: str = Field(default="", description="发生月份 YYYY-MM，由代码默认本期。")
    field_updates: ProfileFieldUpdate = Field(
        default_factory=ProfileFieldUpdate, description="画像字段的新值候选；不改动原画像。"
    )
    effects: list[EventEffect] = Field(
        default_factory=list, description="框定的影响（首版只描述、不接算法）。"
    )
    closes: str = Field(default="", description="关闭事件填「被关闭的事件 id」；普通事件留空。")
    lesson_ids: list[str] = Field(default_factory=list, description="同源产出的经验条目 id。")
    evidence: list[str] = Field(default_factory=list, description="依据要点（代码写，可含金额）。")


class ProfileDelta(BaseModel):
    """本期增量——即"这次要追加进事件日志的东西"。"""

    model_config = ConfigDict(extra="ignore")

    period: str = Field(default="", description="本期月份 YYYY-MM。")
    events: list[ProfileEvent] = Field(default_factory=list, description="本期新增事件（含关闭事件）。")
    notes: str = Field(default="", description="补充说明（要点，非过程叙述）。")


class EffectiveProfile(BaseModel):
    """读时派生的「当前生效画像」。``base`` 是原始画像，**一字未改**。"""

    model_config = ConfigDict(extra="ignore")

    base: Profile = Field(default_factory=Profile, description="用户确认过的原画像（未改动）。")
    updates: dict[str, Any] = Field(
        default_factory=dict, description="生效事件叠加出的字段值；下游自行 model_copy 使用。"
    )
    applied_event_ids: list[str] = Field(default_factory=list, description="参与叠加的事件 id（按应用顺序）。")

    def merged(self) -> Profile:
        """把叠加结果落成一个 `Profile` 副本——**只读投影，不回写原画像**。"""
        return self.base.model_copy(update=dict(self.updates))


# --------------------------------------------------------------------------- #
# 派生（纯函数，读时算；绝不写盘）
# --------------------------------------------------------------------------- #
def is_close_event(event: ProfileEvent) -> bool:
    """是否是一条"关闭事件"。"""
    return bool((event.closes or "").strip())


def close_event_id(target_id: str) -> str:
    """关闭事件的 id——对同一目标天然幂等。"""
    return f"close:{target_id}"


def make_event_id(event_type: str, period: str, taken: set[str] | None = None) -> str:
    """生成稳定的事件 id：``{event_type}:{period}``；同月同型冲突时加 ``#2`` / ``#3``。"""
    base = f"{event_type}:{period}"
    used = taken or set()
    if base not in used:
        return base
    index = 2
    while f"{base}#{index}" in used:
        index += 1
    return f"{base}#{index}"


def event_ended_period(event: ProfileEvent, events: list[ProfileEvent] | None = None) -> str | None:
    """这条事件何时结束——由「指向它的关闭事件」的月份派生；没人关它就留空。"""
    if is_close_event(event):
        return None
    closers = [
        item.from_period
        for item in (events or [])
        if item.closes.strip() == event.id and item.from_period
    ]
    return min(closers) if closers else None


def event_status(
    event: ProfileEvent, events: list[ProfileEvent] | None = None, *, as_of_period: str | None = None
) -> EventStatus:
    """这条事件当前是 ``active`` 还是 ``ended``。**读时算，不落盘。**

    ``as_of_period`` 给出"回看某个月"的口径：结束月**不早于**该月时，事件在那时仍然生效。
    """
    if is_close_event(event):
        return "active"  # 关闭事件本身不承载状态
    ended = event_ended_period(event, events)
    if ended is None:
        return "active"
    if as_of_period and ended > as_of_period:
        return "active"
    return "ended"


def active_events(
    events: list[ProfileEvent] | None, *, as_of_period: str | None = None
) -> list[ProfileEvent]:
    """当前**生效中**的事件（按月份、id 升序）——关闭事件本身不入列。"""
    out: list[ProfileEvent] = []
    all_events = list(events or [])
    for event in all_events:
        if is_close_event(event):
            continue
        if as_of_period and event.from_period and event.from_period > as_of_period:
            continue  # 那时还没发生
        if event_status(event, all_events, as_of_period=as_of_period) == "ended":
            continue
        out.append(event)
    return sorted(out, key=lambda item: (item.from_period, item.id))


def effective_profile(
    base: Profile | None, events: list[ProfileEvent] | None, *, as_of_period: str | None = None
) -> EffectiveProfile:
    """把生效中的事件叠加到原画像上，得到「当前生效画像」。

    **只叠加、不改原画像**：返回值里 ``base`` 保持原样，叠加结果放在 ``updates``。
    只覆盖事件"确实设了值"的字段（``0`` 视为设值）；叠加顺序按 ``(from_period, id)``，后来者胜。
    """
    applied_ids: list[str] = []
    updates: dict[str, Any] = {}
    for event in active_events(events, as_of_period=as_of_period):
        values = event.field_updates.as_updates()
        if not values:
            continue
        updates.update(values)
        applied_ids.append(event.id)
    return EffectiveProfile(
        base=base if base is not None else Profile(),
        updates=updates,
        applied_event_ids=applied_ids,
    )


def append_events(
    existing: list[ProfileEvent] | None, incoming: list[ProfileEvent] | None
) -> list[ProfileEvent]:
    """把新事件追加进日志——**已存在的 id 直接 no-op**（返回原条目，不覆盖）。

    这是"重跑同一期间幂等"的实现，也是 append-only 的执行点：任何既有条目都不会被改写。
    """
    result = list(existing or [])
    known = {item.id for item in result}
    for event in incoming or []:
        if event.id in known:
            continue
        result.append(event)
        known.add(event.id)
    return result


def build_delta(period: str, events: list[ProfileEvent] | None = None, *, notes: str = "") -> ProfileDelta:
    """组装本期增量。"""
    return ProfileDelta(period=period, events=list(events or []), notes=notes)


def validate_events(events: list[ProfileEvent] | None) -> list[str]:
    """检查事件流的一致性，返回人可读的问题清单（空列表 = 没问题）。

    查四件事：id 唯一、``closes`` 与事件类型互相匹配、关闭目标存在且**不是**关闭事件。
    """
    all_events = list(events or [])
    problems: list[str] = []

    seen: set[str] = set()
    for event in all_events:
        if event.id in seen:
            problems.append(f"事件 id 重复：{event.id}")
        seen.add(event.id)

    by_id = {item.id: item for item in all_events}
    for event in all_events:
        target = (event.closes or "").strip()
        is_close = event.event_type == CLOSE_EVENT_TYPE
        if target and not is_close:
            problems.append(f"{event.id}：带了关闭目标却不是关闭事件类型。")
        if is_close and not target:
            problems.append(f"{event.id}：是关闭事件类型，却没有写关闭目标。")
        if not target:
            continue
        found = by_id.get(target)
        if found is None:
            problems.append(f"{event.id}：关闭目标 {target} 不存在。")
        elif is_close_event(found):
            problems.append(f"{event.id}：关闭目标 {target} 本身是关闭事件，不能再被关闭。")
    return problems
