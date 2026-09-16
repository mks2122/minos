"""The interactive menu.

A menu is the first thing a person touches, so the things worth pinning are:
it starts read-only, it shows what it will do before doing it, and one bad
option does not kill the session.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_menu():
    """Import main.py as a real module so it can be monkeypatched."""
    spec = importlib.util.spec_from_file_location("writ_menu", ROOT / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before executing: @dataclass looks the module up in sys.modules
    # while processing the class, and gets None if it is not there yet.
    sys.modules["writ_menu"] = module
    spec.loader.exec_module(module)
    return module


main_module = _load_menu()
Settings = main_module.Settings
menu_main = main_module.main


def test_starts_read_only():
    """Nothing destructive should be possible without visiting Settings."""
    settings = Settings()
    assert not settings.allow_write
    assert not settings.allow_delete
    assert settings.scopes() == [f"fs.read:{settings.workspace}/**"]


def test_permissions_appear_in_scopes(tmp_path):
    settings = Settings(workspace=tmp_path, allow_write=True, allow_delete=True)
    scopes = settings.scopes()
    assert any(s.startswith("fs.read:") for s in scopes)
    assert any(s.startswith("fs.write:") for s in scopes)
    assert any(s.startswith("fs.delete:") for s in scopes)


def test_cli_args_carry_the_settings(tmp_path):
    settings = Settings(workspace=tmp_path, allow_write=True, dry_run=True, max_steps=7)
    args = settings.cli_args("do a thing")
    assert args[0] == "run"
    assert args[1] == "do a thing"
    assert "--allow-write" in args
    assert "--dry-run" in args
    assert "--allow-delete" not in args
    assert "--max-steps" in args and "7" in args


def test_blank_model_is_not_passed_through(tmp_path):
    """The CLI picks the right default per planner; don't override it with ''."""
    assert "--model" not in Settings(workspace=tmp_path).cli_args("g")
    assert "--model" in Settings(workspace=tmp_path, model="qwen3:8b").cli_args("g")


def test_model_default_follows_the_planner():
    assert Settings(planner="local").effective_model == "qwen3:8b"
    assert Settings(planner="claude").effective_model == "claude-opus-5"


def test_auto_prefers_local_when_a_server_is_up(monkeypatch):
    monkeypatch.setattr(main_module, "server_available", lambda _url=None: True)
    assert Settings(planner="auto").effective_planner == "local"


def test_auto_falls_back_to_claude_when_no_server(monkeypatch):
    monkeypatch.setattr(main_module, "server_available", lambda _url=None: False)
    assert Settings(planner="auto").effective_planner == "claude"


def test_an_explicit_planner_is_not_overridden(monkeypatch):
    """auto is a convenience; naming a planner must win."""
    monkeypatch.setattr(main_module, "server_available", lambda _url=None: True)
    assert Settings(planner="claude").effective_planner == "claude"


def test_quitting_immediately(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt="": "0")
    assert menu_main() == 0
    assert "bye" in capsys.readouterr().out


def test_a_bad_choice_does_not_end_the_session(monkeypatch, capsys):
    answers = iter(["99", "", "0"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert menu_main() == 0
    out = capsys.readouterr().out
    assert "is not on the menu" in out
    assert "bye" in out


def test_the_demo_runs_from_the_menu(monkeypatch, capsys):
    answers = iter(["2", "", "0"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    menu_main()
    out = capsys.readouterr().out
    assert "byte-identical: True" in out


def test_an_empty_goal_does_nothing(monkeypatch, capsys):
    answers = iter(["1", "", "", "0"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    menu_main()
    assert "nothing to do" in capsys.readouterr().out


def test_the_menu_is_cp1252_safe():
    """A plain Windows console raises on box drawing. A menu that crashes is useless."""
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    text.encode("cp1252")


@pytest.mark.parametrize("option", ["1", "2", "3", "4", "5", "6", "7", "8", "0"])
def test_every_advertised_option_is_listed(option):
    assert f"  {option}." in main_module.MENU
