"""LangGraph wallet planning: model proposal -> validation feedback -> confirmation."""
import json
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, interrupt
from langgraph.prebuilt import ToolNode

from .plan_state import PlanState
from .plan_build import default_checkpointer
from ..domain.plan import ValidationReport, ValidationIssue
from ..domain.plan_turn import PlanTurnDecision
from ..domain.wallet import planning_facts, validate_wallets, compose_wallet_plan
from ..model.deepseek import StructuredCall

from ..prompts.wallet import INSTRUCTION


def public_reply(content):
    """Only these messages may cross the planning conversation API boundary."""
    return AIMessage(content=content, additional_kwargs={"public_reply": True})


def build_wallet_graph(*, context, decide_runner, agent_runner=None, tools=None, max_tool_rounds=4, **_):
    graph = StateGraph(PlanState)

    def prepare(state):
        return {"clarification_target": "", "tool_rounds": 0, "proposal_attempts": 0, "validation_feedback": [], "degraded": False,
                "error": None, "confirmed": False, "awaiting_confirmation": False,
                "ready_to_finalize": False, "plan": None, "validation": None}

    def inspect(state):
        if agent_runner is None:
            return {}
        try:
            result = agent_runner([SystemMessage(content=INSTRUCTION),
                HumanMessage(content="按需读取事实与来源，可以测试自主候选。随后通过结构化 proposal 提交完整方案。"),
                *state.get("messages", [])[-20:]])
            return {"messages": [result], "tool_rounds": int(state.get("tool_rounds", 0))+1}
        except Exception:
            # Structured decision can still succeed; no rule allocation is synthesized.
            return {"tool_rounds": max_tool_rounds}

    def route_inspect(state):
        last = (state.get("messages") or [None])[-1]
        return "tools" if getattr(last, "tool_calls", None) else "propose"

    def route_tools(state):
        return "inspect" if state.get("tool_rounds", 0) < max_tool_rounds else "propose"

    def propose(state):
        messages = [SystemMessage(content=INSTRUCTION),
                    HumanMessage(content=json.dumps(planning_facts(context.inputs), ensure_ascii=False)),
                    *state.get("messages", [])[-20:]]
        if state.get("validation_feedback"):
            messages.append(HumanMessage(content="请修订完整方案：" + json.dumps(state["validation_feedback"], ensure_ascii=False)))
        try:
            result = decide_runner(messages)
            if isinstance(result, StructuredCall):
                result = result.parsed
            decision = result if isinstance(result, PlanTurnDecision) else PlanTurnDecision.model_validate(result)
        except Exception:
            return {"degraded": True, "error": "方案模型暂不可用，请稍后重试；未生成默认分配。",
                    "ready_to_finalize": False, "awaiting_confirmation": False,
                    "messages": [public_reply("本次方案未生成，已有正式计划保持可查看，请稍后重试。")],
                    "proposal_attempts": int(state.get("proposal_attempts", 0))+1}
        return {"decision": decision, "messages": [public_reply(decision.reply)],
                "turn_count": int(state.get("turn_count", 0))+1,
                "proposal_attempts": int(state.get("proposal_attempts", 0))+1,
                "clarification_target": "month" if decision.status == "ask" else "",
                "notes": decision.notes, "ready_to_finalize": decision.status == "finalize"}

    def route_proposal(state):
        if state.get("degraded") or not state.get("ready_to_finalize"):
            return END
        return "validate"

    def validate(state):
        decision = state["decision"]
        if decision.proposal is None:
            report = {"ok": False, "errors": ["必须提交完整钱包 proposal，不能只给文字或策略旋钮"]}
        else:
            report = validate_wallets(decision.proposal, context.inputs)
        update = {"validation": ValidationReport(ok=report["ok"], issues=[ValidationIssue(code="wallet_validation", message=e) for e in report["errors"]]),
                  "validation_feedback": report["errors"], "trace": ["钱包方案独立复核：" + ("通过" if report["ok"] else "未通过")]}
        if report["ok"]:
            plan = compose_wallet_plan(decision.proposal, context.inputs)
            update.update(plan=plan, awaiting_confirmation=True, messages=[public_reply(plan.narrative)])
        elif state.get("proposal_attempts", 0) >= 3:
            update.update(error="方案未通过校验，请核对本月信息后重试。", ready_to_finalize=False,
                          messages=[public_reply("方案未通过校验：" + "；".join(report["errors"]))])
        return update

    def route_validation(state):
        if state["validation"].ok:
            return "confirm"
        return "propose" if state.get("proposal_attempts", 0) < 3 else END

    def confirm(state):
        response = interrupt({"type": "plan_confirmation", "plan": state["plan"].model_dump(mode="json"),
                              "options": ["confirm", "edit", "more"]})
        if isinstance(response, str):
            response = {"action": response}
        if response.get("action") == "confirm":
            return Command(goto=END, update={"confirmed": True, "awaiting_confirmation": False})
        return Command(goto="prepare", update={"messages": [HumanMessage(content=response.get("message") or "请重新评估完整安排")],
            "confirmed": False, "awaiting_confirmation": False})

    graph.add_node("prepare", prepare)
    graph.add_node("inspect", inspect)
    graph.add_node("tools", ToolNode(tools or [], handle_tool_errors=True))
    graph.add_node("propose", propose)
    graph.add_node("validate", validate)
    graph.add_node("confirm", confirm)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "inspect")
    graph.add_conditional_edges("inspect", route_inspect)
    graph.add_conditional_edges("tools", route_tools)
    graph.add_conditional_edges("propose", route_proposal)
    graph.add_conditional_edges("validate", route_validation)
    return graph.compile(checkpointer=_.get("checkpointer") or default_checkpointer())
