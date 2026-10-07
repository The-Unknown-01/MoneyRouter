"""总结图的共享状态。

一次性任务：一轮跑完就是"差异表 → 叙述 → 三份产物 → 用户确认"。
外部依赖（该月留档、上一版方案、既有经验包与画像事件）**由门面闭包注入，不进状态**——
与 `month_state.py` / `plan_state.py` 同一约定，避免把领域对象塞进检查点。
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from ..domain.experience import ExperiencePack, Lesson
from ..domain.profile_delta import ProfileDelta
from ..domain.summary import MonthlySummary, PlanActualDiff, SummaryDraft


class SummaryState(TypedDict, total=False):
    """总结图的共享状态。"""

    # 输入
    period: str
    feedback: str
    # 代码算出来的东西
    diff: PlanActualDiff
    candidates: list[Lesson]
    # 模型输出
    draft: SummaryDraft | None
    # 代码合成的产物
    pack: ExperiencePack
    profile_delta: ProfileDelta
    summary: MonthlySummary | None
    # 核对环节
    awaiting_confirmation: bool
    confirmed: bool
    # 观测 / 降级
    degraded: bool
    error: str | None
    reasoning: str | None
    # 轨迹与告警是**累积**的（各节点各交一段，不互相覆盖）
    trace: Annotated[list[str], operator.add]
    event_warnings: Annotated[list[str], operator.add]
