"""scripts/prepare_matches.py -- the fetch overwrite guard.

Upstream (LuckyLoser91/TennisCourtLog) moved its CSVs to Git LFS on 2026-07-27; raw.githubusercontent
then served 131-byte pointer stubs, and the old "any 200 with content" write destroyed 59 good year
archives. _is_usable_csv is what keeps a changed feed from overwriting good data again.
"""
import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "prepare_matches.py"

_LFS_POINTER = (
    b"version https://git-lfs.github.com/spec/v1\n"
    b"oid sha256:99852ba35fbfa59b0000000000000000000000000000000000000000000000\n"
    b"size 164611\n"
)
_GOOD_HEADER = (
    b"tourney_name,tourney_level,tourney_date,surface,round,best_of,winner_name,loser_name,score\n"
    b"Washington,A,2026-07-27,Hard,R32,3,Taylor Fritz,Zizou Bergs,6-4 6-3\n"
)


def _load():
    spec = importlib.util.spec_from_file_location("prepare_matches_mod", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rejects_git_lfs_pointer_stub():
    assert _load()._is_usable_csv(_LFS_POINTER) is False


def test_accepts_a_real_match_csv():
    assert _load()._is_usable_csv(_GOOD_HEADER) is True


def test_rejects_a_csv_missing_the_required_columns():
    mod = _load()
    # e.g. upstream renames winner_name -> winner: a 200 with plausible CSV content that
    # prepare() would still crash on, so it must not overwrite the existing archive.
    assert mod._is_usable_csv(b"tourney_date,surface,winner,loser,score\n2026-07-27,Hard,A,B,6-0\n") is False


def test_rejects_an_html_error_page():
    assert _load()._is_usable_csv(b"<!DOCTYPE html>\n<html><body>404</body></html>") is False


def test_rejects_a_header_only_or_truncated_body():
    """Review finding 2026-07-29: a header with no rows passed the original check and would overwrite
    a good archive with an empty one -- which reads downstream as 'that year had no tennis' rather
    than as an error, so nothing would alert."""
    mod = _load()
    header = b"tourney_name,tourney_date,winner_name,loser_name,score\n"
    assert mod._is_usable_csv(header) is False                    # header only
    assert mod._is_usable_csv(header + b"\n\n") is False           # header + blank lines
    # one COMPLETE row is enough (5 header fields -> the row needs 5 too)
    assert mod._is_usable_csv(header + b"Wim,2026-07-01,A,B,6-4 6-4") is True
    # a row truncated mid-way has fewer fields than the header -> misaligned, reject
    assert mod._is_usable_csv(header + b"Wim,2026-07-01,A") is False


def test_rejects_a_json_error_body():
    assert _load()._is_usable_csv(b'{"message":"Not Found"}') is False
