"""本月实况的多轮 CLI 演练脚本。

用法（在 agent/ 目录下）：

    ./.venv/Scripts/python.exe scripts/month_repl.py --mock          # 无网络，假模型走全流程（自带规范文档样例）
    ./.venv/Scripts/python.exe scripts/month_repl.py --real          # 真连 DeepSeek（需密钥）
    ./.venv/Scripts/python.exe scripts/month_repl.py --real --bill 本月实况.json --period 2026-09
    ./.venv/Scripts/python.exe scripts/month_repl.py --mock --period 2026-10   # 换个期间，会自动用上已有历史做环比
    ./.venv/Scripts/python.exe scripts/month_repl.py --real --show-reasoning
    ./.venv/Scripts/python.exe scripts/month_repl.py --mock --no-history      # 本次不读也不写历史

``--bill`` 指向的文件**自动识别**：``{`` 开头按规范文档（推荐，见 tools/dossier.py），
否则按 CSV 宽表（兼容入口）。``--mock`` 默认用规范文档样例。

落定（``/confirm``）之后会自动写入**历史留档**（默认 ``.data/months/<YYYY-MM>.json``），
下次跑更晚的月份时它会被自动载入，作为环比 / 近三月均值 / 累计已攒的基线。

会话内命令：
    /confirm          核对通过，落定本月实况（并写入历史留档）
    /edit <一句话>     带着修改意见回到对话
    /more <一句话>     再补充一点
    /why              看最近一轮模型的思维链（reasoning_content）
    /state            打印当前快照
    /probes           打印关注点清单及其状态与归因
    /history          打印历史留档的月份与结论
    /quit             退出
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langchain_core.messages import BaseMessage  # noqa: E402

from moneyrouter_agent.config import MonthSettings, Settings  # noqa: E402
from moneyrouter_agent.domain.money import format_yuan  # noqa: E402
from moneyrouter_agent.domain.month import MonthResult, MonthSnapshot  # noqa: E402
from moneyrouter_agent.domain.month_turn import MonthTurnDecision, ProbeAnswer  # noqa: E402
from moneyrouter_agent.month_agent import MonthAgent  # noqa: E402
from moneyrouter_agent.tools.dossier import sample_dossier  # noqa: E402

BANNER = "=" * 68
PERIOD = "2026-09"


class MockMonthTurnRunner:
    """把本月核对流程演一遍的假模型（不联网）。

    注意它**刻意不重复问**账单已经给出的事实（收入、支出、去向、收益），
    只问账单解释不了的地方——这正是规范格式下期望的行为。
    """

    def __init__(self, period: str = PERIOD) -> None:
        self.period = period
        self.calls = 0

    def __call__(self, messages: list[BaseMessage]) -> MonthTurnDecision:
        self.calls += 1
        step = self.calls
        if step == 1:
            return MonthTurnDecision(
                reply=(
                    "您好，这个月的账单我已经看过了。有一处想跟您确认一下："
                    "这个月有一笔 2000 元的数码支出，方便说说是什么情况吗？"
                ),
                snapshot=MonthSnapshot(period=self.period),
                rationale="账单已给出收入与支出，直接问那笔一次性大额的原因",
            )
        if step == 2:
            return MonthTurnDecision(
                reply="了解了。除了这一笔，这个月还有没有别的大额开销没记在账单里？",
                snapshot=MonthSnapshot(period=self.period),
                probe_answers=[ProbeAnswer(probe_id="one_off_large:换手机", answer="手机坏了，换了一部")],
                rationale="归因已拿到，补问有没有账外大额",
            )
        return MonthTurnDecision(
            reply="这个月的情况我了解得差不多了，帮您整理一下。",
            snapshot=MonthSnapshot(period=self.period),
            notes="换手机属一次性支出，手机损坏所致；结余留作应急金；已有基金定投本月小幅盈利。",
            ready_to_finalize=True,
            rationale="已够还原这个月，收尾（mock）",
        )


class MockMonthFinalizeRunner:
    def __call__(self, messages: list[BaseMessage]) -> MonthResult:
        return MonthResult(
            snapshot=MonthSnapshot(),
            articulation=(
                "用户本月收入约 8000 元，支出集中在居住、购物与餐饮；购物中有一笔换手机的一次性支出，"
                "因手机损坏所致。结余留作应急金，未新增投资；已有基金定投本月小幅盈利。"
            ),
        )


def print_state(result, *, show_reasoning: bool = False) -> None:
    print(BANNER)
    print(
        f"[轮次 {result.turn_count}] 降级={result.degraded} 可收尾={result.ready_to_finalize} "
        f"待核对={result.awaiting_confirmation} 已确认={result.confirmed}"
    )
    if result.rationale:
        print(f"[模型理由] {result.rationale}")
    if show_reasoning and result.reasoning:
        text = result.reasoning.replace("\n", " ").strip()
        print(f"[模型思维链] {text[:400]}{'…' if len(text) > 400 else ''}")
    if result.error:
        print(f"[错误] {result.error}")
    if result.parse_warnings:
        print(f"[账单告警] {'；'.join(result.parse_warnings)}")

    snap = result.snapshot
    print(
        f"[本月实况] 期间={snap.period or '—'} "
        f"收入={format_yuan(snap.income.amount_cents) if snap.income else '—'} "
        f"支出={format_yuan(snap.spend_total_cents)} "
        f"结余={format_yuan(snap.balance_cents) if snap.balance_cents is not None else '—'}"
    )
    for item in snap.categories:
        print(f"    - {item.category} {format_yuan(item.amount_cents)}（{item.source}）")

    alloc = snap.allocation
    if alloc.non_invested_cents is not None or alloc.invested_cents is not None:
        non_inv = format_yuan(alloc.non_invested_cents) if alloc.non_invested_cents is not None else "—"
        inv = format_yuan(alloc.invested_cents) if alloc.invested_cents is not None else "—"
        print(f"    · 结余去向：留着不投资 {non_inv} 元，投资 {inv} 元")

    inv_snap = snap.investments
    if inv_snap.has_investments is not None or inv_snap.holdings:
        held = {True: "有", False: "无"}.get(inv_snap.has_investments, "—")
        month_ret = (
            format_yuan(inv_snap.month_return_cents)
            if inv_snap.month_return_cents is not None
            else "—"
        )
        total_ret = (
            format_yuan(inv_snap.total_return_cents)
            if inv_snap.total_return_cents is not None
            else "—"
        )
        print(f"    · 已有的投资：持有={held} 本月盈亏={month_ret} 累计收益={total_ret}")
        for holding in inv_snap.holdings:
            mret = format_yuan(holding.month_return_cents) if holding.month_return_cents is not None else "—"
            tret = format_yuan(holding.total_return_cents) if holding.total_return_cents is not None else "—"
            kind = holding.kind or "未分类"
            print(f"        - {holding.name}（{kind}）本月 {mret} / 累计 {tret}")

    if snap.net_worth_change_cents is not None:
        print(f"    · 本月净财富变动：{format_yuan(snap.net_worth_change_cents)} 元")

    if snap.baselines:
        labels = {"budget": "预算", "last_month": "上月", "trailing_3m_avg": "近三月均值"}
        print("    · 与基线对比：")
        for baseline in snap.baselines:
            mark = "↑超" if baseline.delta_cents > 0 else ("↓低" if baseline.delta_cents < 0 else "＝")
            pct = f"（{baseline.delta_pct:+.1f}%）" if baseline.delta_pct is not None else ""
            print(
                f"        {labels.get(baseline.metric, baseline.metric)} · {baseline.category or '总体'}："
                f"{format_yuan(baseline.amount_cents)} → {mark} "
                f"{format_yuan(abs(baseline.delta_cents))}{pct}"
            )

    if snap.trailing:
        trend = "；".join(
            f"{point.period} 结余 "
            f"{format_yuan(point.balance_cents) if point.balance_cents is not None else '—'}"
            for point in snap.trailing
        )
        print(f"    · 近月趋势：{trend}")

    open_probes = [p for p in result.probes if p.status == "open"]
    answered = [p for p in result.probes if p.status == "answered"]
    if open_probes:
        print("[待追问]")
        for probe in open_probes:
            print(f"    ? {probe.topic}：{probe.detail}")
    if answered:
        print("[已归因]")
        for probe in answered:
            print(f"    ✓ {probe.topic} → {probe.answer}")

    if result.confirmation_payload is not None:
        print("[待核对] 输入 /confirm 通过，或 /edit <意见> / /more <补充>")
    print(BANNER)


def build_agent(args: argparse.Namespace) -> MonthAgent:
    month_settings = MonthSettings.from_env()
    if args.no_history:
        month_settings = month_settings.with_overrides(history_dir=None)
    elif args.history_dir:
        month_settings = month_settings.with_overrides(history_dir=args.history_dir)

    if args.real:
        settings = Settings.from_env()
        if settings.degraded:
            print("！未找到 DeepSeek API Key，无法走真实模型。请设置 DEEPSEEK_API_KEY 或 .env。")
            print("  改用 --mock 可以先演练流程。")
            raise SystemExit(2)
        print(f"使用真实模型：{settings.model} @ {settings.base_url}")
        return MonthAgent(settings=settings, month_settings=month_settings)

    return MonthAgent(
        settings=Settings(api_key="mock"),
        month_settings=month_settings,
        turn_runner=MockMonthTurnRunner(args.period),
        finalize_runner=MockMonthFinalizeRunner(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="MoneyRouter 本月实况 Agent —— 多轮 CLI 演练")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mock", action="store_true", help="用假模型演练（默认，不联网）")
    group.add_argument("--real", action="store_true", help="连真实 DeepSeek（需密钥）")
    parser.add_argument("--thread", default="month-demo", help="会话 id（thread_id）")
    parser.add_argument(
        "--bill",
        default=None,
        help="导入文件路径：规范文档 JSON（推荐）或 CSV 宽表；自动识别。--mock 未指定时用规范文档样例",
    )
    parser.add_argument("--period", default=PERIOD, help="统计期间 YYYY-MM")
    parser.add_argument("--show-reasoning", action="store_true", help="每轮额外打印模型思维链")
    parser.add_argument(
        "--history-dir",
        default=None,
        help="历史留档目录（默认取 MONTH_HISTORY_DIR，即 .data/months）",
    )
    parser.add_argument("--no-history", action="store_true", help="本次不读也不写历史留档")
    args = parser.parse_args()

    agent = build_agent(args)
    thread_id = args.thread
    period = args.period
    show_reasoning = args.show_reasoning

    history_periods = agent.history_periods()
    if agent.history_store is None:
        print("[历史留档] 已关闭")
    else:
        existing = "、".join(history_periods) if history_periods else "还没有"
        print(f"[历史留档] {agent.history_store.directory}（已有：{existing}）")

    bill_text: str | None = None
    if args.bill:
        bill_text = Path(args.bill).read_text(encoding="utf-8-sig")
    elif args.mock:
        bill_text = sample_dossier(period)

    if bill_text is not None:
        parsed = agent.parse_bill(bill_text, period=period)
        extras: list[str] = []
        if parsed.allocation is not None:
            extras.append("含结余去向")
        if parsed.investments is not None:
            extras.append(f"含投资收益 {len(parsed.investments.holdings)} 笔")
        if parsed.skipped_rows:
            extras.append(f"不计收支 {parsed.skipped_rows} 笔")
        print(
            f"[账单预览] 期间 {parsed.period}：{len(parsed.categories)} 个分类，"
            f"收入 {'有' if parsed.income else '无'}，一次性大额 {len(parsed.one_offs)} 笔"
            + ("，" + "，".join(extras) if extras else "")
            + (f"，告警 {len(parsed.warnings)} 条" if parsed.warnings else "")
        )

    print(f"会话 {thread_id} 已开始（/quit 退出）")
    result = agent.turn(thread_id, bill=bill_text, period=period)
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
        if raw == "/probes":
            snap = agent.snapshot(thread_id)
            for probe in snap.probes:
                print(f"  [{probe.status}] {probe.id} | {probe.topic} | {probe.answer}")
            continue
        if raw == "/history":
            records = agent.history_records()
            if not records:
                print("（还没有历史月份）")
            for record in records:
                snap = record.snapshot
                balance = format_yuan(snap.balance_cents) if snap.balance_cents is not None else "—"
                trend = "".join(
                    f"\n        {point.period} 结余 "
                    f"{format_yuan(point.balance_cents) if point.balance_cents is not None else '—'}"
                    for point in snap.trailing
                )
                print(
                    f"  {record.period} | 结余 {balance} | 落定 {record.recorded_at or '—'}"
                    f"{trend}\n        结论：{record.articulation or '（无）'}"
                )
            continue
        if raw == "/confirm":
            if not agent.snapshot(thread_id).awaiting_confirmation:
                print("（当前没有待核对的快照，/confirm 已忽略）")
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
            print("本月实况已确认落定。最终结果：")
            if result.final_result:
                print(f"  结论：{result.final_result.articulation}")
            if result.recorded:
                print(f"  历史留档已写入：{result.history_location}")
                print(f"  现有历史月份：{'、'.join(result.history_periods) or '—'}")
            elif result.error:
                print(f"  ！{result.error}")
            break

    print("已退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
