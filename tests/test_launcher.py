"""scripts/bot.py launcher -- logging setup (the only unit-testable part; main() needs creds)."""
import importlib.util
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LAUNCHER = Path(__file__).resolve().parent.parent / "scripts" / "bot.py"


def _load_launcher():
    # scripts/ isn't a package; load bot.py by path under a synthetic name (avoids shadowing matador.bot).
    spec = importlib.util.spec_from_file_location("matador_launcher", _LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_configure_logging_attaches_rotating_file_handler(tmp_path, monkeypatch):
    launcher = _load_launcher()
    monkeypatch.chdir(tmp_path)  # LOG_DIR is relative to CWD -- keep logs/ out of the repo
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        launcher._configure_logging()
        assert any(isinstance(h, RotatingFileHandler) for h in root.handlers)
        assert (tmp_path / "logs" / "matador.log").exists()          # file opened on the mounted path
        assert root.level == logging.INFO
        assert logging.getLogger("httpx").level == logging.WARNING   # chatty libs pinned down
        assert logging.getLogger("apscheduler").level == logging.WARNING
    finally:
        for h in root.handlers:
            if h not in saved_handlers:
                h.close()  # release the open file handle before tmp_path is torn down
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_configure_logging_degrades_to_console_when_logs_unwritable(tmp_path, monkeypatch):
    """A root-owned/unwritable logs mount must NOT crash-loop the bot -- fall back to console-only."""
    launcher = _load_launcher()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").write_text("not a dir")  # 'logs' exists as a FILE -> mkdir(exist_ok=True) raises
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        launcher._configure_logging()  # must NOT raise
        assert not any(isinstance(h, RotatingFileHandler) for h in root.handlers)   # file handler skipped
        assert any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
                   for h in root.handlers)                                          # console still present
    finally:
        for h in root.handlers:
            if h not in saved_handlers:
                h.close()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
