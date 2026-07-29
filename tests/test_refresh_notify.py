"""scripts/refresh_notify.py -- the weekly-refresh outcome DM.

The three outcomes must be distinguishable in the message, because they demand different responses:
FAILED (model not updated at all), REJECTED (fetch ran but upstream moved, so the model is FROZEN
behind kept archives), and OK. The REJECTED case is the one that went unnoticed for weeks before
2026-07-27, so it must never be reported as success.
"""
import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "refresh_notify.py"


def _load():
    spec = importlib.util.spec_from_file_location("refresh_notify_mod", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_DATES = {"atp": "2026-08-02", "wta": "2026-08-01"}
_OK_LOG = "[atp] fetched 59 year file(s)\n[atp] wrote data/tennis_atp/atp_matches_all.csv: latest 20260802\n"
_REJECTED_LOG = _OK_LOG + "[atp] WARNING: 59 year file(s) rejected as unusable (LFS pointer or changed format)\n"


def test_ok_reports_the_dates():
    msg = _load().build_message(0, _DATES, _OK_LOG)
    assert msg.startswith("🔄 Weekly refresh OK")
    assert "ATP 2026-08-02" in msg and "WTA 2026-08-01" in msg


def test_nonzero_exit_is_reported_as_failure_not_success():
    msg = _load().build_message(1, _DATES, _OK_LOG)
    assert "FAILED" in msg and "NOT updated" in msg
    assert "refresh OK" not in msg
    assert "2026-08-02" in msg          # still say what the data is stuck at


def test_rejected_warning_is_never_reported_as_success():
    """The killer case: the fetch exits 0 because it deliberately KEEPS good archives when upstream
    turns unusable. A naive exit-code-only check would call this success and the model would sit
    frozen for the rest of the paper test."""
    msg = _load().build_message(0, _DATES, _REJECTED_LOG)
    assert "REJECTED" in msg and "FROZEN" in msg
    assert not msg.startswith("🔄 Weekly refresh OK")


def test_unknown_dates_do_not_crash_the_message():
    msg = _load().build_message(0, {}, _OK_LOG)
    assert "unknown" in msg


def test_data_through_reads_the_artifact_and_tolerates_a_bad_one(tmp_path):
    import json
    mod = _load()
    good = tmp_path / "m.json"
    good.write_text(json.dumps({"tours": {"atp": {"data_through": 20260802}, "wta": {}}}))
    assert mod._data_through(str(good)) == {"atp": "2026-08-02"}     # wta has no stamp -> omitted
    assert mod._data_through(str(tmp_path / "missing.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert mod._data_through(str(bad)) == {}
