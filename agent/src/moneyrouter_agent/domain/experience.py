"""经验契约（`ExperiencePack`）——**由月底后馈产生、作为方案生成的可选输入**。

本模块本次只定义**数据结构 + 语义**：把「上个月的复盘结论」提炼成若干条 *教训*（:class:`Lesson`），
再由 :func:`apply_lessons` 折算成一组**软调整**（:class:`ExperienceEffects`），交给方案生成的
:func:`moneyrouter_agent.domain.plan.build_plan` 在硬约束之内应用。

产生路径（本次**不实现**，只描述）：月底月结取该月的 :class:`~moneyrouter_agent.domain.month.MonthResult`
与上一版方案做「计划 vs 实际」差异 → 代码 + 模型例程把差异提炼成 `Lesson` → 汇入版本化
`ExperiencePack` 持久化 → 下次经 :attr:`PlanInputs.experience` 注入。注入方式对齐
``MonthAgent`` 的 ``CodeLayer``：闭包捕获，不进检查点。

设计纪律（与全仓一致）：

- **软调整不得突破硬约束**：经验只影响「怎么分配更贴合这个用户」，绝不绕过 50/30/20 对照、
  预备金、风险上限与总额校验（由 :func:`moneyrouter_agent.domain.plan.validate_plan` 兜底）。
- 未了解到就给空，不臆造；``strength`` 越大影响越强，但一律有界。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LessonKind = Literal[
    "wants_down",
    "risk_conservative",
    "reserve_priority",
    "debt_priority",
    "goal_pace",
    "custom",
]

# 需要下调「可选支出」基线的教训，下降幅度 = 强度 × 该系数（封顶 60%）
WANTS_DOWN_MAX_SCALE = 0.60


class Lesson(BaseModel):
    """一条经验教训——对某个用户「该怎么调」的结论性描述。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="稳定标识，如 `wants_down:2026-09`；同一 id 重复出现时以最后一次为准。")
    kind: LessonKind = Field(description="经验类型，决定它对方案产生什么软调整。")
    statement: str = Field(
        description="自然语言结论（第三人称）：如「连续两月可选支出超预算，建议下调可选上限」。"
        "只写结论，不引用户原话、不复述当时怎么问的。"
    )
    strength: float = Field(default=1.0, description="软调整强度 0-1；越大越保守，代码会钳到该区间。")
    from_period: str = Field(default="", description="产生这条教训的对账月份，YYYY-MM。")
    evidence: list[str] = Field(
        default_factory=list, description="依据要点（上月计划 vs 实际的差异），不写过程叙述。"
    )


class ExperiencePack(BaseModel):
    """一个用户的经验包——所有历史教训的版本化集合。"""

    model_config = ConfigDict(extra="ignore")

    version: int = Field(default=1, description="经验包版本号，代码递增。")
    generated_at: str = Field(default="", description="生成时间，YYYY-MM-DD。")
    from_period: str = Field(default="", description="最近一次对账的月份，YYYY-MM。")
    lessons: list[Lesson] = Field(default_factory=list, description="教训清单；没有就留空。")
    notes: str = Field(default="", description="整体补充说明（要点，非过程叙述）。")


class ExperienceEffects(BaseModel):
    """把经验折算成的**软调整**——供方案算法在硬约束内应用。"""

    model_config = ConfigDict(extra="ignore")

    wants_ratio_scale: float = Field(
        default=1.0, description="可选支出上限比例的缩放系数；（0, 1] 表示下调，1 表示不变。"
    )
    risk_downgrade_notches: int = Field(
        default=0, description="风险档保守化的档位数（0/1/2），最终不得高于评估出的档位。"
    )
    force_reserve_first: bool = Field(
        default=False, description="本月结余是否**全部**优先留作预备金/现金（不新增投资）。"
    )
    debt_priority: bool = Field(default=False, description="是否提示高息债务优先、暂缓投资。")
    goal_conservative: bool = Field(default=False, description="目标措辞是否更保守（不乐观估计进度）。")
    applied_ids: list[str] = Field(default_factory=list, description="实际生效的教训 id，供追溯。")


def _clamp_strength(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(lo, min(hi, number))


def apply_lessons(pack: ExperiencePack | None) -> ExperienceEffects:
    """把经验包折算成一组软调整。``None`` 或空包返回「无调整」（全默认值）。

    纯函数、幂等：同一条教训重复出现时按 id 去重（后者覆盖前者）。
    """
    effects = ExperienceEffects()
    if pack is None or not pack.lessons:
        return effects

    latest: dict[str, Lesson] = {}
    for lesson in pack.lessons:
        latest[lesson.id or f"{lesson.kind}:{lesson.from_period}"] = lesson

    for lesson in latest.values():
        strength = _clamp_strength(lesson.strength)
        if lesson.kind == "wants_down":
            effects.wants_ratio_scale *= 1.0 - WANTS_DOWN_MAX_SCALE * strength
        elif lesson.kind == "risk_conservative":
            effects.risk_downgrade_notches = min(2, effects.risk_downgrade_notches + 1)
        elif lesson.kind == "reserve_priority":
            effects.force_reserve_first = True
        elif lesson.kind == "debt_priority":
            effects.debt_priority = True
        elif lesson.kind == "goal_pace":
            effects.goal_conservative = True
        # custom：只作为叙述参考，不改变算法
        effects.applied_ids.append(lesson.id or f"{lesson.kind}:{lesson.from_period}")

    # 缩放系数留 3 位小数，避免浮点噪声
    effects.wants_ratio_scale = round(max(0.0, min(1.0, effects.wants_ratio_scale)), 3)
    return effects
