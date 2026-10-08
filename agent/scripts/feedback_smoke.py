"""Resume real summary/next-plan checks from confirmed full_flow_smoke artifacts.

Upstream model results and confirmed month records are read as-is, not mocked.
The destination must be new; new feedback products are isolated there.
"""
from __future__ import annotations
import argparse
import os
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from moneyrouter_agent.api.contract import TurnResult, PlanResult, MonthTurnResult
from moneyrouter_agent.config import Settings, SummarySettings
from moneyrouter_agent.domain.finance import FinanceBriefingResult
from moneyrouter_agent.domain.plan import PlanInputs, validate_plan
from moneyrouter_agent.history import JsonMonthHistoryStore
from moneyrouter_agent.plan_agent import PlanAgent
from moneyrouter_agent.prompts.plan_rules import render_plan_narrative
from moneyrouter_agent.summary_agent import SummaryAgent
from moneyrouter_agent.summary_store import JsonExperiencePackStore, JsonProfileEventStore, JsonMonthlySummaryStore


def run(source: Path, output: Path):
    source, output = source.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ["MONEYROUTER_CHECKPOINT_DIR"] = str(output / "checkpoints")
    settings = Settings.from_env()
    settings.require_api_key()
    started = time.monotonic()
    report = {"mode": "real-feedback-resume", "upstream": str(source), "stages": [], "ok": False}

    def write_report():
        report["elapsed_s"] = round(time.monotonic() - started, 2)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def read(name, model):
        return model.model_validate_json((source / name).read_text(encoding="utf-8"))

    def stage(name, value):
        (output / f"{name}.json").write_text(value.model_dump_json(indent=2, exclude={"reasoning"}), encoding="utf-8")
        entry = {"name": name, "error": value.error, "degraded": value.degraded}
        report["stages"].append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)
        write_report()
        assert not value.error and not value.degraded, value.error

    try:
        profile_result = read("02-profile-confirmed.json", TurnResult)
        previous_result = read("04-september-plan-confirmed.json", PlanResult)
        current_result = read("month-2026-09-confirmed.json", MonthTurnResult)
        finance = read("03-finance.json", FinanceBriefingResult)
        assert profile_result.confirmed and previous_result.confirmed and current_result.confirmed
        assert previous_result.validation.ok and current_result.recorded
        assert not finance.briefing.degraded and not finance.briefing.error
        history = JsonMonthHistoryStore(str(source / "months"))
        assert history.list_periods() == ["2026-08", "2026-09"]
        profile = profile_result.final_profile
        stores = dict(history_store=history,
                      experience_store=JsonExperiencePackStore(str(output / "experience"), user="smoke"),
                      event_store=JsonProfileEventStore(str(output / "events"), user="smoke"),
                      summary_store=JsonMonthlySummaryStore(str(output / "summaries"), user="smoke"))
        summary = SummaryAgent(settings=settings, summary_settings=SummarySettings(user="smoke"), **stores)
        result = summary.summarize("feedback-summary", period="2026-09", plan=previous_result.plan)
        draft = summary.graph.get_state(summary._config("feedback-summary")).values.get("draft")
        if draft is not None:
            (output / "summary-model-draft.json").write_text(draft.model_dump_json(indent=2), encoding="utf-8")
        stage("01-summary-draft", result)
        assert result.awaiting_confirmation and not result.event_warnings, result.event_warnings
        # Discard the facade; the next instance must resume the saved interrupt/context.
        summary = SummaryAgent(settings=settings, summary_settings=SummarySettings(user="smoke"), **stores)
        result = summary.turn("feedback-summary", resume={"action": "confirm"})
        stage("02-summary-confirmed", result)
        assert result.confirmed and result.written
        reader = SummaryAgent(settings=Settings(api_key=None), **stores)
        effective = reader.effective_profile(profile, as_of_period="2026-10").merged()
        pack = reader.load_pack()
        assert profile.income_stable is True and effective.income_stable is False, "Profile event did not feed back"
        assert pack and any(item.kind == "wants_down" for item in pack.lessons)
        inputs = PlanInputs(profile=effective, briefing=finance.briefing, snapshot=history.load("2026-09").snapshot,
                            history=[history.load("2026-08").snapshot], experience=pack, period="2026-10", debt_payment_cents=0)
        planner = PlanAgent(settings=settings, inputs=inputs)
        result = planner.plan("feedback-october")
        for _ in range(3):
            assert not result.error and not result.degraded, result.error
            if result.awaiting_confirmation:
                break
            result = planner.plan("feedback-october", user_message="没有债务或月供，资料已完整提供，请生成下月方案。")
        stage("03-october-plan-draft", result)
        assert result.awaiting_confirmation and result.validation.ok, result.validation
        planner = PlanAgent(settings=settings)
        result = planner.plan("feedback-october", resume={"action": "confirm"})
        stage("04-october-plan-confirmed", result)
        assert result.confirmed and validate_plan(result.plan, inputs).ok
        assert result.plan.narrative == render_plan_narrative(result.plan), "Explanation differs from final amounts"
        assert result.plan.lessons_applied and result.plan.budget.wants_ratio_pct < 30
        assert result.plan.reserve.months == 6
        report["comparison"] = {
            "september_wants_cents": previous_result.plan.budget.wants_cents,
            "october_wants_cents": result.plan.budget.wants_cents,
            "reserve_months_before": previous_result.plan.reserve.months,
            "reserve_months_after": result.plan.reserve.months,
            "lessons_applied": result.plan.lessons_applied,
            "profile_updates": reader.effective_profile(profile).updates,
            "history_periods": history.list_periods(),
        }
        assert report["comparison"]["october_wants_cents"] < report["comparison"]["september_wants_cents"]
        report["ok"] = True
    except Exception as exc:
        report["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        write_report()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.output), ensure_ascii=False, indent=2))
