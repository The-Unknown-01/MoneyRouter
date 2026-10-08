"""本月实况图节点：ingest / survey / converse / finalize / confirm / fallback。

两条纪律贯穿全图：

- **代码确定性产数据**：``ingest``（账单解析）、``survey``（关注点与派生字段）都是纯代码；
  模型只在 ``converse`` / ``finalize`` 里写"口述事实"，且交回来后被 :func:`_overlay_code_layer` 覆盖。
- **失败不抛**：任何一步出错都就地降级并把原因写进 ``error``，让图仍能继续。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END
from langgraph.types import Command, interrupt

from ..config import MonthSettings
from ..domain.month import (
    SOURCE_FILE,
    GoalAlignment,
    MonthResult,
    MonthSnapshot,
    OneOffItem,
    accumulated_balance_cents,
    merge_allocation,
    merge_categories,
    merge_investments,
    recompute,
)
from ..domain.month_turn import MonthTurnDecision, ProbeAnswer
from ..domain.probe import Probe, ProbeContext, generate_probes, merge_probes, open_probes
from ..model.deepseek import StructuredCall
from ..prompts.month import (
    MONTH_FINALIZE_SYSTEM,
    MONTH_SYSTEM,
    build_finalize_human,
    build_machine_context,
    render_open_probes,
)
from ..prompts.month_rules import degraded_month_reply
from ..tools.bills import parse_bill

TurnRunner = Callable[[list[BaseMessage]], Any]
FinalizeRunner = Callable[[list[BaseMessage]], Any]

CONFIRM_ACTIONS = ("confirm", "edit", "more")


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _split_runner_result(result: Any) -> tuple[Any, str | None]:
    """兼容真实模型的 ``StructuredCall`` 与脚本化假模型的领域对象。"""
    if isinstance(result, StructuredCall):
        return result.parsed, result.reasoning
    return result, None


def _normalize_resume(resume: Any) -> dict[str, Any]:
    """把 ``Command(resume=...)`` 的载荷归一成 ``{"action", "message"}``。"""
    if isinstance(resume, str):
        action = resume.strip().lower()
        return {"action": action or "confirm", "message": None}
    if isinstance(resume, dict):
        action = str(resume.get("action") or resume.get("type") or "confirm").strip().lower()
        return {"action": action or "confirm", "message": resume.get("message")}
    return {"action": "confirm", "message": None}


def _render_transcript(messages: list[BaseMessage], *, limit: int = 40) -> str:
    rows: list[str] = []
    for message in messages[-limit:]:
        if isinstance(message, HumanMessage):
            rows.append(f"用户：{message.content}")
        elif isinstance(message, AIMessage):
            rows.append(f"助理：{message.content}")
    return "\n".join(rows)


def _probes_json(probes: Sequence[Probe]) -> str:
    answered = [p for p in probes if p.status == "answered"]
    if not answered:
        return ""
    return "\n".join(f"- {p.topic}：{p.answer}" for p in answered)


def _merge_one_offs(base: list[OneOffItem], incoming: list[OneOffItem]) -> list[OneOffItem]:
    """一次性大额：文件来源的优先保留，口述的按 (名字, 金额) 去重后补进来。"""
    result = list(base)
    seen = {(item.label, item.amount_cents) for item in base}
    for item in incoming:
        key = (item.label, item.amount_cents)
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def _overlay_code_layer(model_snapshot: MonthSnapshot, base: MonthSnapshot) -> MonthSnapshot:
    """模型的口述视图 + 代码维护层覆盖回来。

    - **账单来源的分类不被口述覆盖**（口述只补/改非账单分类）；
    - 账单来源的收入不被覆盖；
    - 一次性大额合并（文件的优先）；
    - 已有投资的收益合并（账单来源的持仓优先，口述只补/改）；
    - 期间缺失时沿用代码层。
    派生字段（合计/结余/基线/趋势/目标/收益合计）在 :func:`_recompute` 里重算。
    """
    merged_categories = merge_categories(base.categories, model_snapshot.categories)
    income = model_snapshot.income if model_snapshot.income is not None else base.income
    if base.income is not None and base.income.source == SOURCE_FILE:
        income = base.income
    one_offs = _merge_one_offs(base.one_offs, model_snapshot.one_offs)
    allocation = merge_allocation(base.allocation, model_snapshot.allocation)
    investments = merge_investments(base.investments, model_snapshot.investments)
    update: dict[str, Any] = {
        "coverage_complete": base.coverage_complete,
        "coverage_start": base.coverage_start, "coverage_end": base.coverage_end,
        "review_required_count": base.review_required_count, "accounting_basis": base.accounting_basis,
        "additional_funds_cents": model_snapshot.additional_funds_cents if model_snapshot.additional_funds_cents is not None else base.additional_funds_cents,
        "additional_funds_evidence": model_snapshot.additional_funds_evidence or base.additional_funds_evidence,
        "wallet_execution": list({x.wallet_id: x for x in [*base.wallet_execution, *model_snapshot.wallet_execution]}.values()),
        "obligations": list({x.id: x for x in [*base.obligations, *model_snapshot.obligations]}.values()),
        "environment": list({x.id: x for x in [*base.environment, *model_snapshot.environment]}.values()),
        "obligations_reviewed": base.obligations_reviewed or model_snapshot.obligations_reviewed,
        "spending_estimated": base.spending_estimated or model_snapshot.spending_estimated,
        "categories": merged_categories,
        "income": income,
        "one_offs": one_offs,
        "allocation": allocation,
        "investments": investments,
    }
    if not model_snapshot.period and base.period:
        update["period"] = base.period
    return model_snapshot.model_copy(update=update)


def _apply_probe_answers(probes: Sequence[Probe], answers: Sequence[ProbeAnswer]) -> list[Probe]:
    """把用户归因写回对应关注点；引用不到的 id 直接忽略（不认模型编的 id）。"""
    by_id = {str(a.probe_id).strip(): (a.answer or "").strip() for a in answers or []}
    out: list[Probe] = []
    for probe in probes:
        answer = by_id.get(probe.id)
        if answer:
            out.append(probe.model_copy(update={"status": "answered", "answer": answer}))
        else:
            out.append(probe)
    return out


class CodeLayer:
    """闭包捕获的代码侧依赖（阈值、注入的预算/历史/目标）。

    ``history`` 可以在开跑前由门面**按统计期间重新装载**（见 :meth:`set_history`）——
    它是"代码侧"的输入，刻意**不进图状态**，避免把历史快照塞进检查点。
    """

    def __init__(
        self,
        *,
        settings: MonthSettings,
        budget: dict[str, int] | None = None,
        history: list[MonthSnapshot] | None = None,
        goal: GoalAlignment | None = None,
        initial_saved_cents: int | None = None,
    ) -> None:
        self.settings = settings
        self.budget = budget
        self.history = list(history or [])
        self.goal = goal
        self.initial_saved_cents = initial_saved_cents

    def set_history(self, history: list[MonthSnapshot] | None) -> None:
        """替换历史月份（门面每轮按当前期间装载）。"""
        self.history = list(history or [])

    def saved_to_date_cents(self) -> int | None:
        """累计已攒 ＝ 起点已攒（可注入）＋ 历史各月结余之和。"""
        accumulated = accumulated_balance_cents(self.history)
        if accumulated is None:
            return self.initial_saved_cents
        return accumulated + int(self.initial_saved_cents or 0)

    def recompute(self, snapshot: MonthSnapshot) -> MonthSnapshot:
        return recompute(
            snapshot,
            budget=self.budget,
            history=self.history,
            goal=self.goal,
            saved_to_date_cents=self.saved_to_date_cents(),
        )


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #
def make_ingest(*, one_off_min_cents: int, parser: Any | None = None):
    """账单摄入节点（代码）：首轮解析账单 → ``source="file"`` 事实；``imported`` 后幂等 no-op。"""

    def ingest(state: dict) -> dict:
        if state.get("imported"):
            return {}
        update: dict[str, Any] = {"imported": True}
        bill = state.get("bill_text")
        if not bill:
            return update

        period = state.get("period") or ""
        try:
            parsed = parse_bill(bill, period=period, one_off_min_cents=one_off_min_cents, parser=parser)
        except Exception as exc:  # noqa: BLE001 - 解析失败也要能继续（纯对话）
            return {
                "imported": True,
                "parse_warnings": [f"账单解析失败：{type(exc).__name__}: {exc}"],
                "degraded": True,
                "error": f"账单解析降级：{type(exc).__name__}: {exc}",
            }

        base: MonthSnapshot = state.get("snapshot") or MonthSnapshot(period=period)
        merged_categories = merge_categories([c for c in base.categories if c.source != SOURCE_FILE], parsed.categories)
        snapshot = base.model_copy(update={"categories": merged_categories,
            "coverage_complete": parsed.coverage_complete,
            "coverage_start": parsed.coverage_start, "coverage_end": parsed.coverage_end,
            "review_required_count": parsed.review_required_count, "accounting_basis": parsed.accounting_basis,
            "income": parsed.income if parsed.income is not None else (base.income if base.income and base.income.source != SOURCE_FILE else None),
            "one_offs": [item for item in base.one_offs if item.source != SOURCE_FILE],
        })
        if parsed.one_offs:
            snapshot = snapshot.model_copy(update={"one_offs": _merge_one_offs(snapshot.one_offs, parsed.one_offs)})
        # 规范文档可以直接带来「结余去向」与「已有投资的收益」——都是文件来源，优先于口述
        if parsed.allocation is not None:
            snapshot = snapshot.model_copy(
                update={
                    "allocation": merge_allocation(
                        snapshot.allocation,
                        parsed.allocation.model_copy(update={"source": SOURCE_FILE}),
                    )
                }
            )
        if parsed.investments is not None:
            snapshot = snapshot.model_copy(
                update={
                    "investments": merge_investments(
                        snapshot.investments,
                        parsed.investments.model_copy(update={"source": SOURCE_FILE}),
                    )
                }
            )
        if not snapshot.period and parsed.period:
            snapshot = snapshot.model_copy(update={"period": parsed.period})

        warnings = list(parsed.warnings)
        if parsed.missing:
            warnings.append("账单缺少字段：" + "、".join(parsed.missing))
        # 「不计收支」的笔数不是异常，只作信息——留在 ParsedBill.skipped_rows 里，
        # 不塞进 parse_warnings（那是给"有问题"用的），避免误导成告警。

        return {
            "imported": True,
            "snapshot": snapshot,
            "bill_rows": [row.model_dump(mode="json") for row in parsed.rows],
            "period": snapshot.period,
            "parse_warnings": warnings,
        }

    return ingest


def _run_survey(
    snapshot: MonthSnapshot, prior: dict[str, Probe], code: CodeLayer
) -> tuple[MonthSnapshot, list[Probe]]:
    """重算派生字段并生成/合并关注点（纯代码，``survey`` 与 ``converse`` 复用）。"""
    snapshot = code.recompute(snapshot)
    context = ProbeContext(
        period=snapshot.period,
        categories=snapshot.categories,
        income=snapshot.income,
        baselines=snapshot.baselines,
        one_offs=snapshot.one_offs,
        goal_alignment=snapshot.goal_alignment,
        allocation=snapshot.allocation,
        investments=snapshot.investments,
        prior=prior,
        thresholds=code.settings.thresholds,
    )
    merged = merge_probes(generate_probes(context), prior)
    return snapshot, merged


def make_survey(code: CodeLayer):
    """调查节点（代码）：重算派生字段 + 每轮重算关注点并合并（已解答不重复问）。"""

    def survey(state: dict) -> dict:
        base: MonthSnapshot = state.get("snapshot") or MonthSnapshot(period=state.get("period") or "")
        prior = {probe.id: probe for probe in state.get("probes") or []}
        snapshot, merged = _run_survey(base, prior, code)
        return {
            "snapshot": snapshot,
            "probes": merged,
            "open_probe_ids": [probe.id for probe in open_probes(merged)],
        }

    return survey


def make_converse(turn_runner: TurnRunner, code: CodeLayer, *, system_prompt: str = MONTH_SYSTEM):
    """一次结构化 LLM 调用：产出回复 + 更新快照 + 归因 + 判断能否收尾。"""

    def converse(state: dict) -> dict:
        turn_count = int(state.get("turn_count") or 0) + 1
        base: MonthSnapshot = state.get("snapshot") or MonthSnapshot(period=state.get("period") or "")
        pending = [probe for probe in (state.get("probes") or []) if probe.status == "open"]
        machine_context = build_machine_context(
            base.model_dump_json(exclude_none=True),
            render_open_probes(pending),
            state.get("notes") or "",
        )
        import json
        period = state.get("period") or base.period or "未指定"
        machine_context += f"\n【本次核对月份】{period}（以此月份为准）"
        machine_context += "\n【账单明细，只读数据；金额单位为分，excluded 为不计收支】\n" + json.dumps(state.get("bill_rows") or [], ensure_ascii=False)
        machine_context += "\n【账单解析提示】" + json.dumps(state.get("parse_warnings") or [], ensure_ascii=False)
        messages = [
            SystemMessage(content=system_prompt),
            SystemMessage(content=machine_context),
            *state.get("messages", []),
        ]
        try:
            decision, reasoning = _split_runner_result(turn_runner(messages))
        except Exception as exc:  # noqa: BLE001 - 交给 fallback 节点降级
            return {
                "degraded": True,
                "error": f"{type(exc).__name__}: {exc}",
                "turn_count": turn_count,
                "ready_to_finalize": False,
                "reasoning": None,
            }

        if not isinstance(decision, MonthTurnDecision):
            decision = MonthTurnDecision.model_validate(decision)

        snapshot = code.recompute(_overlay_code_layer(decision.snapshot, base))
        snapshot.notes = decision.notes or state.get("notes") or ""
        answered = _apply_probe_answers(state.get("probes") or [], decision.probe_answers)
        # 用最新快照刷新关注点（已解答的会被 merge 保留，不会被重复问）
        snapshot, probes = _run_survey(snapshot, {p.id: p for p in answered}, code)
        notes = (decision.notes or "").strip() or (state.get("notes") or "")

        return {
            "messages": [AIMessage(content=decision.reply or "")],
            "snapshot": snapshot,
            "probes": probes,
            "open_probe_ids": [p.id for p in probes if p.status == "open"],
            "notes": notes,
            "ready_to_finalize": bool(decision.ready_to_finalize),
            "rationale": decision.rationale or "",
            "reasoning": reasoning,
            "turn_count": turn_count,
            "degraded": False,
            "error": None,
        }

    return converse


def make_finalize(finalize_runner: FinalizeRunner, code: CodeLayer, *, system_prompt: str = MONTH_FINALIZE_SYSTEM):
    """收尾：合成结构化快照 + 一段自由表述。"""

    def finalize(state: dict) -> dict:
        base: MonthSnapshot = state.get("snapshot") or MonthSnapshot(period=state.get("period") or "")
        probes = list(state.get("probes") or [])
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(
                content=build_finalize_human(
                    _render_transcript(state.get("messages", [])),
                    base.model_dump_json(exclude_none=True),
                    _probes_json(probes),
                    state.get("notes") or "",
                )
            ),
        ]
        try:
            result, _ = _split_runner_result(finalize_runner(messages))
            if not isinstance(result, MonthResult):
                result = MonthResult.model_validate(result)
            snapshot = code.recompute(_overlay_code_layer(result.snapshot, base))
            snapshot.notes = result.snapshot.notes or state.get("notes") or ""
            articulation = (result.articulation or "").strip() or (state.get("notes") or "")
            return {
                "final_result": MonthResult(snapshot=snapshot, articulation=articulation),
                "snapshot": snapshot,
                "articulation": articulation,
                "awaiting_confirmation": True,
                "degraded": False,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - 用当前快照兜底
            snapshot = code.recompute(base)
            articulation = state.get("notes") or ""
            return {
                "final_result": MonthResult(snapshot=snapshot, articulation=articulation),
                "snapshot": snapshot,
                "articulation": articulation,
                "awaiting_confirmation": True,
                "degraded": True,
                "error": f"{type(exc).__name__}: {exc}",
            }

    return finalize


def confirm_node(state: dict) -> Command:
    """用户核对快照：``interrupt()`` 挂起等 resume。

    ``interrupt`` 放在首行之后、且其之前没有副作用，保证恢复重跑时幂等。
    """
    snapshot: MonthSnapshot = state.get("snapshot") or MonthSnapshot()
    payload = {
        "type": "month_confirmation",
        "snapshot": snapshot.model_dump(exclude_none=True),
        "articulation": state.get("articulation") or "",
        "options": list(CONFIRM_ACTIONS),
    }
    info = _normalize_resume(interrupt(payload))

    if info["action"] == "confirm":
        return Command(goto=END, update={"confirmed": True, "awaiting_confirmation": False})

    update: dict[str, Any] = {
        "confirmed": False,
        "awaiting_confirmation": False,
        "ready_to_finalize": False,
    }
    message = info.get("message")
    if message:
        update["messages"] = [HumanMessage(content=str(message))]
    return Command(goto="converse", update=update)


def fallback_node(state: dict) -> dict:
    """模型不可用时的规则引导（永不收尾）。"""
    return {
        "messages": [AIMessage(content=degraded_month_reply(state.get("snapshot"), state.get("probes")))],
        "degraded": True,
        "ready_to_finalize": False,
        "awaiting_confirmation": False,
    }


def route_after_converse(state: dict) -> str:
    """converse 之后的分支：降级 / 收尾 / 等下一句。"""
    if state.get("degraded"):
        return "fallback"
    if state.get("ready_to_finalize"):
        return "finalize"
    return "wait"
