"""总结（月度复盘）的 CLI 演练脚本。

用法（在 agent/ 目录下）：

    ./.venv/Scripts/python.exe scripts/summary_repl.py --mock                        # 无网络，假模型走全流程
    ./.venv/Scripts/python.exe scripts/summary_repl.py --mock --period 2026-10       # 指定月份
    ./.venv/Scripts/python.exe scripts/summary_repl.py --real --plan 方案.json       # 真连 DeepSeek，并注入上一版方案
    ./.venv/Scripts/python.exe scripts/summary_repl.py --real --show-reasoning
    ./.venv/Scripts/python.exe scripts/summary_repl.py --mock --no-write             # 只演流程，不落盘

``--period`` 留空时取「本月实况」留档里最近的月份（先用 month_repl.py 落定一个月即可）。

会话内命令：
    /confirm          确认这份复盘（并写入经验包 / 画像增量 / 月度复盘）
    /edit <一句话>     带着修改意见重跑叙述
    /show             打印「计划 vs 实际」差异表
    /pack             打印经验包（喂方案生成的那份）
    /events           打印画像事件日志与读时状态
    /summary          打印月度复盘
    /why              看最近一轮模型的思维链（reasoning_content）
    /quit             退出
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langchain_core.messages import BaseMessage  # noqa: E402

from moneyrouter_agent.config import MonthSettings, Settings, SummarySettings  # noqa: E402
from moneyrouter_agent.domain.experience import Lesson, apply_lessons  # noqa: E402
from moneyrouter_agent.domain.money import format_yuan  # noqa: E402
from moneyrouter_agent.domain.profile_delta import (  # noqa: E402
    EventEffect,
    active_events,
    event_ended_period,
    is_close_event,
)
from moneyrouter_agent.domain.summary import (  # noqa: E402
    EventDraft,
    LessonDraft,
    SummaryDraft,
)
from moneyrouter_agent.summary_agent import SummaryAgent  # noqa: E402

BANNER = "=" * 68
ENERGY = "\u2500" * 68
CANDIDATE_RE = re.compile(r"^\[([^\]]+)\]\s*类型=([^；;\s]+)", re.MULTILINE)
DIGEST_RE = re.compile(r"^-\s*(.+?)：(.+)$", re.MULTILINE)
LAYER_LABELS = {
    "income": "收入",
    "necessary": "必要支出",
    "wants": "可选支出",
    "savings": "可储蓄",
    "growth": "增长类配置",
}


class MockReflectRunner:
    """把复盘流程演一遍的假模型（不联网）。

    它从系统给的材料里**只取标识与类型**（这些本来就由代码给出），据此写出叙述；
    另外把"本月已归因的第一条疑问"记成一条画像变化，好让画像增量这条链路也能被看见。
    真实模型做的是同一件事，只是叙述由它自己想。
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, messages: list[BaseMessage]) -> SummaryDraft:
        self.calls += 1
        # 只看系统给的**材料**那条，别去解析原则性的说明文字
        payload = "\n".join(
            str(message.content)
            for message in messages
            if str(message.content).startswith("【系统算出来的本月情况")
        )

        lessons = [
            LessonDraft(id=match.group(1), statement=f"本月的{match.group(2)}结论已记录，下个月据此微调。")
            for match in CANDIDATE_RE.finditer(payload)
        ]

        digest = DIGEST_RE.findall(payload)
        events = []
        if digest:
            topic, answer = digest[0]
            events.append(
                EventDraft(
                    event_type="other",
                    statement=f"本月出现与「{topic}」相关的情况变化，用户说明为：{answer}。",
                    effects=[
                        EventEffect(
                            kind="category_cap",
                            target=topic.split("比")[0].strip() or "其他",
                            direction="up",
                            detail="本月该方面支出高于平常。",
                        )
                    ],
                )
            )

        return SummaryDraft(
            headline="本月计划与实际已对照完成，主要差异集中在一处。",
            sections=["复盘要点见差异表（本行为假模型生成，用于演练）。"],
            lessons=lessons,
            events=events,
            notes="",
        )


def print_diff(result) -> None:
    diff = result.summary.diff
    print(BANNER)
    print(f"[复盘] 期间={diff.period or '—'} 有计划可对照={diff.plan_available}")
    for note in diff.notes:
        print(f"    说明：{note}")
    for item in diff.layers:
        label = LAYER_LABELS.get(item.layer, item.layer)
        planned = format_yuan(item.planned_cents) if item.planned_cents is not None else "—"
        actual = format_yuan(item.actual_cents) if item.actual_cents is not None else "—"
        delta = (
            f"{'+' if (item.delta_cents or 0) > 0 else ''}{format_yuan(item.delta_cents)}"
            if item.delta_cents is not None
            else "—"
        )
        pct = f"（{item.delta_pct:+.1f}%）" if item.delta_pct is not None else ""
        print(f"    · {label}：计划 {planned} → 实际 {actual}　偏差 {delta}{pct}")
    if diff.reserve_gap_cents:
        print(f"    · 应急金缺口：{format_yuan(diff.reserve_gap_cents)} 元")
    if diff.investment_return_cents:
        print(f"    · 本月投资盈亏：{format_yuan(diff.investment_return_cents)} 元")

    free = [item for item in diff.categories if item.planned_cents is not None]
    if free:
        print("    · 分类对比：")
        for item in free:
            print(
                f"        {item.category}：预算 {format_yuan(item.planned_cents)} → "
                f"实际 {format_yuan(item.actual_cents or 0)}"
            )


def print_products(result) -> None:
    summary = result.summary
    print(BANNER)
    print(f"[月度复盘 {summary.period}] {summary.headline}")
    for line in summary.sections:
        print(f"    · {line}")
    if summary.notes:
        print(f"    （{summary.notes}）")
    print(f"    来源：{'、'.join(summary.sources) or '—'}　降级={summary.degraded}")

    pack = result.pack
    print(f"[经验包 v{pack.version}]（{len(pack.lessons)} 条，喂给方案生成）")
    for lesson in pack.lessons:
        print(f"    · [{lesson.kind}] {lesson.statement}（强度 {lesson.strength}）")
        for evidence in lesson.evidence:
            print(f"        依据：{evidence}")
    effects = apply_lessons(pack)
    print(
        f"    → 折算成软调整：可选上限×{effects.wants_ratio_scale}，"
        f"风险降 {effects.risk_downgrade_notches} 档，预备金优先={effects.force_reserve_first}，"
        f"债务优先={effects.debt_priority}"
    )

    delta = result.profile_delta
    print(f"[画像增量 {delta.period}]（{len(delta.events)} 条，只追加不改写）")
    for event in delta.events:
        mark = "结束" if is_close_event(event) else event.event_type
        print(f"    · [{mark}] {event.statement}")
        for effect in event.effects:
            target = effect.target or "—"
            print(f"        影响：{effect.kind} · {target} · {effect.direction}（{effect.detail}）")
        updates = event.field_updates.as_updates()
        if updates:
            print(f"        字段：{updates}")

    print(
        f"[核对] 待核对={result.awaiting_confirmation} 已确认={result.confirmed} "
        f"已写入={result.written}"
        + (f"（{result.write_location}）" if result.write_location else "")
    )
    if result.error:
        print(f"[错误] {result.error}")
    if result.trace:
        print("[轨迹] " + " → ".join(result.trace))
    print(BANNER)


def print_events(agent: SummaryAgent) -> None:
    events = agent.profile_events()
    if not events:
        print("（还没有记录任何画像变化）")
        return
    print(BANNER)
    print("[画像事件日志 · 只追加，已有条目永不改写]")
    for event in events:
        ended = event_ended_period(event, events)
        state = f"已结束于 {ended}" if ended else "生效中"
        if is_close_event(event):
            state = f"关闭事件 → {event.closes}"
        print(f"    · {event.id}｜{event.from_period}｜{state}")
        print(f"        {event.statement}")
    active = active_events(events)
    print(f"    → 当前生效中：{len(active)} 条")
    print(BANNER)


def build_agent(args: argparse.Namespace) -> SummaryAgent:
    summary_settings = SummarySettings.from_env()
    month_settings = MonthSettings.from_env()
    if args.user:
        summary_settings = summary_settings.with_overrides(user=args.user)

    plan = None
    if args.plan:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8-sig"))

    if args.real:
        settings = Settings.from_env()
        if settings.degraded:
            print("！未找到 DeepSeek API Key，无法走真实模型。请设置 DEEPSEEK_API_KEY 或 .env。")
            print("  改用 --mock 可以先演练流程。")
            raise SystemExit(2)
        print(f"使用真实模型：{settings.model} @ {settings.base_url}")
        agent = SummaryAgent(
            settings=settings,
            summary_settings=summary_settings,
            turn_settings=month_settings,
            plan=plan,
            autosave=not args.no_write,
        )
    else:
        agent = SummaryAgent(
            settings=Settings(api_key="mock"),
            summary_settings=summary_settings,
            turn_settings=month_settings,
            reflect_runner=MockReflectRunner(),
            plan=plan,
            autosave=not args.no_write,
        )
    return agent


def main() -> int:
    parser = argparse.ArgumentParser(description="薪安理得总结 Agent —— 月度复盘 CLI 演练")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mock", action="store_true", help="用假模型演练（默认，不联网）")
    group.add_argument("--real", action="store_true", help="连真实 DeepSeek（需密钥）")
    parser.add_argument("--thread", default="summary-demo", help="会话 id（thread_id）")
    parser.add_argument("--period", default="", help="复盘哪个月 YYYY-MM；留空取最近已落定的月份")
    parser.add_argument("--user", default=None, help="落档用的用户标识（决定文件名）")
    parser.add_argument("--plan", default=None, help="上一版方案 JSON 文件（用于「计划 vs 实际」）")
    parser.add_argument("--no-write", action="store_true", help="确认后不落盘，只演流程")
    parser.add_argument("--show-reasoning", action="store_true", help="额外打印模型思维链")
    args = parser.parse_args()

    agent = build_agent(args)
    thread_id = args.thread
    period = args.period or (agent.history_periods()[-1] if agent.history_periods() else "")

    print(f"[本月实况留档] 已有：{'、'.join(agent.history_periods()) or '还没有'}")
    print(f"[画像事件日志] 已有：{len(agent.profile_events())} 条")
    print(f"[经验包] 已有：{agent.load_pack().version if agent.load_pack() else '还没有'}")
    if not period:
        print("！还没有可复盘的月份。请先用 scripts/month_repl.py 落定一个月，或用 --period 指定。")
        return 2
    print(f"会话 {thread_id} 开始复盘 {period}（/quit 退出）")

    result = agent.summarize(thread_id, period=period)
    print_diff(result)
    print_products(result)

    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        if raw in ("/quit", "/exit"):
            break
        if raw == "/show":
            print_diff(agent.snapshot(thread_id))
            continue
        if raw == "/pack":
            pack = agent.load_pack()
            print(
                f"（磁盘上的经验包：v{pack.version}，{len(pack.lessons)} 条）"
                if pack
                else "（磁盘上还没有经验包，确认后才会写入）"
            )
            continue
        if raw == "/events":
            print_events(agent)
            continue
        if raw == "/summary":
            saved = agent.load_summary(period)
            print(f"（磁盘上的复盘：{saved.headline}）" if saved else "（还没写入）")
            continue
        if raw == "/why":
            if args.show_reasoning and result.reasoning:
                print(f"[思维链] {result.reasoning.replace(chr(10), ' ')[:400]}")
            else:
                print("（没有思维链；可加 --show-reasoning 再跑一次）")
            continue

        if raw == "/confirm":
            if not agent.snapshot(thread_id).awaiting_confirmation:
                print("（当前没有待确认的复盘，/confirm 已忽略）")
                continue
            result = agent.turn(thread_id, resume={"action": "confirm"})
        elif raw.startswith("/edit"):
            result = agent.turn(
                thread_id, resume={"action": "edit", "message": raw[len("/edit"):].strip()}
            )
        else:
            print("未知命令。可用：/confirm /edit <一句话> /show /pack /events /summary /why /quit")
            continue

        print_products(result)
        if result.confirmed:
            print(f"复盘已确认。{'产物已写入。' if result.written else '（未落盘）'}")
            break

    print("已退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
