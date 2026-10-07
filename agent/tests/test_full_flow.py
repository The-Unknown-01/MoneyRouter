"""Cross-agent integration: confirmed records -> summary files -> next plan."""
import importlib.util
from pathlib import Path


def test_five_agents_feed_next_month_from_saved_artifacts(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts" / "full_flow_smoke.py"
    spec = importlib.util.spec_from_file_location("full_flow_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.run(tmp_path / "isolated-run")
    assert report["ok"]
    assert report["comparison"]["history_periods"] == ["2026-08", "2026-09"]
    assert report["comparison"]["october_wants_cents"] < report["comparison"]["september_wants_cents"]
