"""多轮 CLI 演练脚本。

用法（在 agent/ 目录下）：

    ./.venv/Scripts/python.exe scripts/repl.py --mock     # 无网络，假模型走全流程
    ./.venv/Scripts/python.exe scripts/repl.py --real     # 真连 DeepSeek（需密钥）
    ./.venv/Scripts/python.exe scripts/repl.py --real --show-reasoning   # 每轮附模型思维链

会话内命令：
    /confirm          核对通过，落定画像
    /edit <一句话>     带着修改意见回到访谈
    /more <一句话>     再补充一点
    /why              看最近一轮模型的思维链（reasoning_content）
    /state            打印当前理解快照
    /quit             退出
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langchain_core.messages import BaseMessage  # noqa: E402

from moneyrouter_agent.agent import ProfileAgent  # noqa: E402
from moneyrouter_agent.config import Settings  # noqa: E402
from moneyrouter_agent.domain.profile import Profile, ProfileDraft, ProfileResult  # noqa: E402
from moneyrouter_agent.domain.turn import TurnDecision  # noqa: E402

BANNER = "=" * 68


class MockTurnRunner:
    """把访谈流程演一遍的假模型（不联网）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.understanding = ProfileDraft()

    def __call__(self, messages: list[BaseMessage]) -> TurnDecision:
        self.calls += 1
        step = self.calls

        if step == 1:
            return TurnDecision(
                reply="你好！我是帮您梳理财务情况的助理。方便先说说您是做什么的、大概什么年龄段吗？",
                understanding=ProfileDraft(),
                rationale="开场先问身份",
            )
        if step == 2:
            self.understanding = ProfileDraft(occupation="大四学生", experience="none")
            return TurnDecision(
                reply="了解～那收入方面呢？不用很精确，说个大概范围就行。",
                understanding=self.understanding,
                rationale="已拿到身份，转到收支",
            )
        if step == 3:
            self.understanding = ProfileDraft(
                occupation="大四学生",
                income_cents=150_000,
                income_basis="每月生活费",
                income_stable=True,
                family_load=False,
                experience="none",
            )
            return TurnDecision(
                reply="好的。这笔钱主要是为了什么在攒，大概什么时候会用得上？",
                understanding=self.understanding,
                notes="用户是大四学生，靠家里每月给生活费，没有负债。",
                rationale="已拿到收入，转到目标",
            )
        self.understanding = ProfileDraft(
            occupation="大四学生",
            income_cents=150_000,
            income_basis="每月生活费",
            income_stable=True,
            family_load=False,
            debt_cents=0,
            reserve_cents=300_000,
            horizon_months=36,
            max_loss_pct=0,
            experience="none",
            goal="毕业后租房押金和第一笔安家费",
        )
        return TurnDecision(
            reply="我了解得差不多了，帮您整理一下。",
            understanding=self.understanding,
            notes="用户是大四学生，靠家里每月给生活费 1500 元，没有负债，攒钱是为了毕业后租房押金和安家费，完全不能接受亏损。",
            ready_to_finalize=True,
            rationale="身份/收支/目标/风险都已了解，可以收尾（mock）",
        )


class MockFinalizeRunner:
    def __init__(self, turn_runner: MockTurnRunner) -> None:
        self._turn_runner = turn_runner

    def __call__(self, messages: list[BaseMessage]) -> ProfileResult:
        draft = self._turn_runner.understanding
        return ProfileResult(
            profile=Profile(**draft.model_dump()),
            articulation=(
                "用户是一名大四学生，收入为家里每月给的生活费约 1500 元，无负债，"
                "有一定积蓄，计划 3 年后毕业后用于租房押金与安家，明确表示不能接受亏损。"
            ),
        )


def print_state(result, *, show_reasoning: bool = False) -> None:
    print(BANNER)
    print(f"[轮次 {result.turn_count}] 降级={result.degraded} 可收尾={result.ready_to_finalize} "
          f"待确认={result.awaiting_confirmation} 已确认={result.confirmed}")
    if result.rationale:
        print(f"[模型理由] {result.rationale}")
    if show_reasoning and result.reasoning:
        text = result.reasoning.replace("\n", " ").strip()
        print(f"[模型思维链] {text[:400]}{'…' if len(text) > 400 else ''}")
    if result.error:
        print(f"[错误] {result.error}")
    snapshot = result.understanding.model_dump(exclude_none=True)
    print(f"[当前理解] {snapshot if snapshot else '（还没有了解到什么）'}")
    if result.confirmation_payload is not None:
        print("[待核对画像]")
        print(f"  {result.confirmation_payload.get('profile')}")
        print(f"  表述：{result.confirmation_payload.get('articulation')}")
        print("  → 输入 /confirm 通过，或 /edit <意见> / /more <补充>")
    print(BANNER)


def build_agent(args: argparse.Namespace) -> ProfileAgent:
    if args.real:
        settings = Settings.from_env()
        if settings.degraded:
            print("！未找到 DeepSeek API Key，无法走真实模型。请设置 DEEPSEEK_API_KEY 或 .env。")
            print("  改用 --mock 可以先演练流程。")
            raise SystemExit(2)
        print(f"使用真实模型：{settings.model} @ {settings.base_url}")
        return ProfileAgent(settings=settings)

    turn_runner = MockTurnRunner()
    return ProfileAgent(
        settings=Settings(api_key="mock"),
        turn_runner=turn_runner,
        finalize_runner=MockFinalizeRunner(turn_runner),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="MoneyRouter 画像访谈 Agent —— 多轮 CLI 演练")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mock", action="store_true", help="用假模型演练（默认，不联网）")
    group.add_argument("--real", action="store_true", help="连真实 DeepSeek（需密钥）")
    parser.add_argument("--thread", default="cli-demo", help="会话 id（thread_id）")
    parser.add_argument(
        "--show-reasoning", action="store_true", help="每轮额外打印模型思维链（reasoning_content）"
    )
    args = parser.parse_args()

    agent = build_agent(args)
    thread_id = args.thread
    show_reasoning = args.show_reasoning

    print(f"会话 {thread_id} 已开始（/quit 退出）")
    result = agent.turn(thread_id)
    print(f"\n助理：{result.reply}")
    print_state(result, show_reasoning=show_reasoning)

    while True:
        try:
            raw = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue

        if raw in ("/quit", "/exit"):
            break
        if raw == "/state":
            print_state(agent.snapshot(thread_id), show_reasoning=show_reasoning)
            continue
        if raw == "/why":
            print_state(agent.snapshot(thread_id), show_reasoning=True)
            continue
        if raw == "/confirm":
            if not agent.snapshot(thread_id).awaiting_confirmation:
                print("（当前没有待核对的画像，/confirm 已忽略）")
                continue
            result = agent.turn(thread_id, resume={"action": "confirm"})
        elif raw.startswith("/edit"):
            result = agent.turn(
                thread_id, resume={"action": "edit", "message": raw[len("/edit"):].strip()}
            )
        elif raw.startswith("/more"):
            result = agent.turn(
                thread_id, resume={"action": "more", "message": raw[len("/more"):].strip()}
            )
        else:
            result = agent.turn(thread_id, raw)

        print(f"\n助理：{result.reply}")
        print_state(result, show_reasoning=show_reasoning)

        if result.confirmed:
            print("画像已确认落定。最终画像：")
            print(f"  {result.final_profile.model_dump(exclude_none=True) if result.final_profile else {}}")
            print(f"  表述：{result.articulation}")
            break

    print("已退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
