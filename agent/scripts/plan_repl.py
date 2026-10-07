"""方案生成的多轮 CLI（默认假模型，`--real` 真连 DeepSeek）。

用法::

    python scripts/plan_repl.py --mock                 # 假模型走全流程（不联网、不烧 token）
    python scripts/plan_repl.py --mock --show-reasoning
    python scripts/plan_repl.py --real                 # 真连 DeepSeek（需密钥）
    python scripts/plan_repl.py --real --period 2026-10

会话内命令：``/confirm`` 通过 · ``/edit <意见>`` 提修改意见 · ``/more <补充>`` 补充信息 ·
``/why`` 看思维链 · ``/state`` 看方案 · ``/trace`` 看各步轨迹 · ``/quit``。

输入（画像 / 金融简报 / 账单）本次用内置样例演示；真实接入由服务端在进程内注入。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langchain_core.messages import AIMessage  # noqa: E402

from moneyrouter_agent.config import Settings  # noqa: E402
from moneyrouter_agent.domain.money import format_yuan  # noqa: E402
from moneyrouter_agent.domain.month import CategorySpend, IncomeFact, MonthSnapshot  # noqa: E402
from moneyrouter_agent.domain.plan import PlanInputs  # noqa: E402
from moneyrouter_agent.domain.plan_turn import PlanAdjustment, PlanTurnDecision  # noqa: E402
from moneyrouter_agent.domain.profile import Profile  # noqa: E402
from moneyrouter_agent.plan_agent import PlanAgent  # noqa: E402


# --------------------------------------------------------------------------- #
# 演示数据
# --------------------------------------------------------------------------- #
def demo_inputs(period: str) -> PlanInputs:
    """一份可直接跑的样例输入（真实使用时由各上游 agent 注入）。"""
    profile = Profile(
        occupation="上班族",
        income_cents=1_200_000,
        income_basis="税后月薪",
        income_stable=True,
        family_load=False,
        debt_cents=0,
        reserve_cents=2_000_000,
        horizon_months=36,
        max_loss_pct=10,
        experience="some",
        goal="三年内攒下一笔购房首付",
    )
    snapshot = MonthSnapshot(
        period=period,
        income=IncomeFact(amount_cents=1_200_000, basis="税后月薪", source="file"),
        categories=[
            CategorySpend(category="居住", amount_cents=450_000, source="file"),
            CategorySpend(category="餐饮", amount_cents=180_000, source="file"),
            CategorySpend(category="交通", amount_cents=60_000, source="file"),
            CategorySpend(category="购物", amount_cents=150_000, source="file"),
            CategorySpend(category="娱乐社交", amount_cents=80_000, source="file"),
        ],
    )
    return PlanInputs(profile=profile, snapshot=snapshot, period=period)


# --------------------------------------------------------------------------- #
# 假模型（--mock）
# --------------------------------------------------------------------------- #
class MockAgentModel:
    """先调一个工具、再收尾；之后每轮都返回一句收尾语。"""

    def __init__(self) -> None:
        self._turn = 0

    def bind_tools(self, tools: list[Any]) -> "MockAgentModel":
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:
        self._turn += 1
        if self._turn == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "summarize_cashflow",
                        "args": {},
                        "id": "mock-1",
                        "type": "tool_call",
                    }
                ],
            )
        if self._turn == 2:
            return AIMessage(content="资料够了，我来安排。")
        return AIMessage(content="好，我按您的意见重新调整了。")


class MockDecideRunner:
    def __call__(self, messages: list[Any]) -> PlanTurnDecision:
        return PlanTurnDecision(
            status="finalize",
            reply="这是根据您的收支和风险偏好做的月度安排，您先看看。",
            headline="本月资金安排（演示）",
            sections=[
                "先说现金：您的月均收入与必要开支都差不多稳定，留出可选开支后每月能结余一部分。",
                "再说预备金：应急金还差一点，建议这个月先补上，补完再谈增值。",
                "最后是配置：按您能接受的波动，给出保守储蓄与稳健配置的大致比例。",
            ],
            caveats=["收益是按假设情景算的，不是预测，实际可能亏损。"],
        )


class MockAdjustRunner:
    def __call__(self, messages: list[Any]) -> PlanAdjustment:
        return PlanAdjustment(wants_ratio_pct=10.0, notes="把可选开支压一压")


# --------------------------------------------------------------------------- #
# 装配与展示
# --------------------------------------------------------------------------- #
def build_agent(args: argparse.Namespace) -> PlanAgent:
    inputs = demo_inputs(args.period)
    if args.mock:
        return PlanAgent(
            settings=Settings(api_key="mock"),
            inputs=inputs,
            agent_model=MockAgentModel(),
            decide_runner=MockDecideRunner(),
            adjust_runner=MockAdjustRunner(),
        )
    settings = Settings.from_env()
    if settings.degraded:
        print("未找到 DeepSeek 密钥：请设置 DEEPSEEK_API_KEY 或 ../.env/deepseek_api.key，或改用 --mock。")
        raise SystemExit(2)
    return PlanAgent(settings=settings, inputs=inputs)


def print_plan(result: Any) -> None:
    print(
        f"[轮次 {result.turn_count}] 降级={result.degraded} 待确认={result.awaiting_confirmation} "
        f"已确认={result.confirmed} 可收尾={result.ready_to_finalize}"
    )
    if result.error:
        print(f"  错误：{result.error}")
    plan = result.plan
    if plan is None:
        return
    print(f"  方案：{plan.headline or '（无标题）'}")
    print(
        f"    月均收入 {format_yuan(plan.cashflow.income_cents)} 元 · "
        f"必要 {format_yuan(plan.cashflow.necessary_cents)} 元 · "
        f"可选预算 {format_yuan(plan.budget.wants_cents)} 元 · "
        f"可储蓄 {format_yuan(plan.budget.savings_cents)} 元"
    )
    print(
        f"    预备金 目标 {format_yuan(plan.reserve.target_cents)}（{plan.reserve.months} 个月）· "
        f"缺口 {format_yuan(plan.reserve.gap_cents)} · 本月补足 {format_yuan(plan.reserve.monthly_cents)} · "
        f"可投资 {format_yuan(plan.reserve.investable_cents)}"
    )
    if plan.allocation.recommended:
        segments = "、".join(f"{s.category} {format_yuan(s.amount_cents)}" for s in plan.allocation.recommended)
        print(f"    配置（风险档 {plan.risk.level}，上限 {plan.risk.growth_cap_pct}%）：{segments}")
    if plan.scenario is not None:
        print(
            f"    情景年化 {plan.scenario.net_annual_pct:.2f}%（"
            f"{'可达' if plan.scenario.goal_reachable else '未达'} >3%，情景非预测）"
        )
    if plan.narrative:
        print(f"    说明：{plan.narrative}")
    if plan.warnings:
        print("    留意：" + "；".join(plan.warnings))
    if result.validation is not None:
        print(f"    复核：{'通过' if result.validation.ok else '未通过'}")
    if result.trace:
        print("    轨迹：")
        for line in result.trace:
            print(f"      - {line}")


def print_reply(result: Any) -> None:
    if result.reply:
        print(f"\n助理：{result.reply}")


def main() -> None:
    parser = argparse.ArgumentParser(description="方案生成 Agent 多轮演练")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mock", action="store_true", help="用假模型走全流程（默认，不联网）")
    group.add_argument("--real", action="store_true", help="真连 DeepSeek（需密钥）")
    parser.add_argument("--thread", default="plan-demo", help="会话标识")
    parser.add_argument("--period", default="2026-10", help="方案所属期间 YYYY-MM")
    parser.add_argument("--show-reasoning", action="store_true", help="展示模型思维链")
    args = parser.parse_args()
    if not args.mock and not args.real:
        args.mock = True

    agent = build_agent(args)
    print("方案生成演练。命令：/confirm /edit <意见> /more <补充> /why /state /trace /quit\n")

    result = agent.plan(args.thread)
    print_reply(result)
    print_plan(result)
    if args.show_reasoning and result.reasoning:
        print(f"  [思维链] {result.reasoning}")

    while True:
        try:
            raw = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        if raw.startswith("/"):
            command, _, argument = raw.partition(" ")
            command = command.lower()
            argument = argument.strip()
            if command == "/quit":
                break
            if command == "/state":
                print_plan(agent.snapshot(args.thread))
                continue
            if command == "/trace":
                snapshot = agent.snapshot(args.thread)
                for line in snapshot.trace:
                    print(f"  - {line}")
                continue
            if command == "/why":
                snapshot = agent.snapshot(args.thread)
                print(f"  [思维链] {snapshot.reasoning or '（无）'}")
                continue
            if command == "/confirm":
                result = agent.plan(args.thread, resume={"action": "confirm"})
                print_reply(result)
                print_plan(result)
                continue
            if command in ("/edit", "/more"):
                action = "edit" if command == "/edit" else "more"
                result = agent.plan(args.thread, resume={"action": action, "message": argument})
                print_reply(result)
                print_plan(result)
                if args.show_reasoning and result.reasoning:
                    print(f"  [思维链] {result.reasoning}")
                continue
            print("未知命令。")
            continue

        result = agent.plan(args.thread, user_message=raw)
        print_reply(result)
        print_plan(result)
        if args.show_reasoning and result.reasoning:
            print(f"  [思维链] {result.reasoning}")


if __name__ == "__main__":
    main()
