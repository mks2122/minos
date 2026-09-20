"""Configuration: .env, the environment, and a local model by default.

The precedence rule is the whole point, and it is the thing that breaks
silently: a flag someone typed must beat a file they forgot they wrote.
"""

from __future__ import annotations

import os

import pytest

from minos.config import DEFAULTS, Settings, load_dotenv, settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No MINOS_* leaking in from the developer's own shell."""
    for name in list(os.environ):
        if name.startswith("MINOS_"):
            monkeypatch.delenv(name, raising=False)


# -- defaults are local ----------------------------------------------------


def test_with_no_configuration_at_all_it_is_local(tmp_path):
    """A fresh checkout runs against Ollama on this machine. That is the claim."""
    cfg = settings(dotenv=tmp_path / "nonexistent")

    assert cfg.base_url == "http://localhost:11434/v1"
    assert cfg.model == "qwen3:8b"
    assert cfg.planner == "auto"
    assert cfg.is_local


def test_every_knob_has_a_default():
    """ "What will this do on a fresh checkout" should have one answer."""
    cfg = settings(dotenv=None)

    for name in DEFAULTS:
        assert cfg.source[name] in {"default", "environment"}


def test_the_remote_model_is_separate_from_the_local_one(tmp_path):
    """Switching planner must not silently send a local model name to the API."""
    cfg = settings(dotenv=tmp_path / "none")

    assert cfg.model_for("local") == cfg.model
    assert cfg.model_for("claude") == cfg.remote_model
    assert cfg.model != cfg.remote_model


# -- .env parsing ----------------------------------------------------------


def test_dotenv_sets_values(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("MINOS_MODEL=qwen3:14b\nMINOS_OFFLINE=1\n")

    cfg = settings(dotenv=env)

    assert cfg.model == "qwen3:14b"
    assert cfg.offline is True


def test_the_shell_environment_beats_the_file(tmp_path, monkeypatch):
    """Anyone who just ran MINOS_MODEL=x minos run expects x to win."""
    monkeypatch.setenv("MINOS_MODEL", "from-the-shell")
    env = tmp_path / ".env"
    env.write_text("MINOS_MODEL=from-the-file\n")

    assert settings(dotenv=env).model == "from-the-shell"


def test_override_makes_the_file_win(tmp_path, monkeypatch):
    monkeypatch.setenv("MINOS_MODEL", "from-the-shell")
    env = tmp_path / ".env"
    env.write_text("MINOS_MODEL=from-the-file\n")

    load_dotenv(env, override=True)
    assert os.environ["MINOS_MODEL"] == "from-the-file"


def test_comments_and_blank_lines_are_ignored(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# a comment\n\n   \nMINOS_MODEL=real\n# MINOS_MODEL=commented-out\n")

    assert settings(dotenv=env).model == "real"


def test_quotes_are_stripped(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MINOS_MODEL=\"quoted\"\nMINOS_BASE_URL='single'\n")

    cfg = settings(dotenv=env)
    assert cfg.model == "quoted"
    assert cfg.base_url == "single"


def test_export_prefixes_are_tolerated(tmp_path):
    """People paste shell snippets into .env files. Let them."""
    env = tmp_path / ".env"
    env.write_text("export MINOS_MODEL=exported\n")

    assert settings(dotenv=env).model == "exported"


def test_a_malformed_line_is_skipped_not_fatal(tmp_path):
    """Failing to start over a stray line is worse than ignoring it."""
    env = tmp_path / ".env"
    env.write_text("this line has no equals sign\nMINOS_MODEL=still-works\n")

    assert settings(dotenv=env).model == "still-works"


def test_a_missing_file_is_not_an_error(tmp_path):
    assert load_dotenv(tmp_path / "nope") == {}


def test_a_binary_file_is_not_an_error(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"\xff\xfe\x00\x00binary")

    assert load_dotenv(env) == {}


def test_values_containing_equals_survive(tmp_path):
    """Base64 and URLs with query strings both contain '='."""
    env = tmp_path / ".env"
    env.write_text("MINOS_BASE_URL=http://host/v1?a=b&c=d\n")

    assert settings(dotenv=env).base_url == "http://host/v1?a=b&c=d"


# -- typed values ----------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_offline_values(tmp_path, value):
    (tmp_path / ".env").write_text(f"MINOS_OFFLINE={value}\n")
    assert settings(dotenv=tmp_path / ".env").offline is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_falsey_offline_values(tmp_path, value):
    (tmp_path / ".env").write_text(f"MINOS_OFFLINE={value}\n")
    assert settings(dotenv=tmp_path / ".env").offline is False


def test_a_typo_in_a_number_does_not_stop_the_runtime(tmp_path):
    (tmp_path / ".env").write_text("MINOS_MAX_STEPS=twenty\nMINOS_CHECKPOINT_GB=lots\n")

    cfg = settings(dotenv=tmp_path / ".env")
    assert cfg.max_steps == 20
    assert cfg.checkpoint_gb == 2.0


# -- secrets ---------------------------------------------------------------


def test_describe_never_prints_a_secret(monkeypatch, tmp_path):
    """doctor output gets pasted into issues. It must be safe to paste."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-do-not-leak")
    monkeypatch.setenv("MINOS_API_TOKEN", "also-secret")

    text = "\n".join(settings(dotenv=tmp_path / "none").describe())

    assert "do-not-leak" not in text
    assert "also-secret" not in text
    assert "hidden" in text


def test_settings_repr_does_not_dump_sources(tmp_path):
    cfg = settings(dotenv=tmp_path / "none")

    assert "source" not in repr(cfg)


def test_the_example_file_is_committed_and_env_is_not():
    """The one that documents; the one that holds keys."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    assert (root / ".env.example").exists()
    assert ".env" in (root / ".gitignore").read_text(encoding="utf-8")


def test_the_example_covers_every_setting():
    """A knob nobody documents is a knob nobody finds."""
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")

    for name in DEFAULTS:
        assert name in text, f"{name} is undocumented in .env.example"


def test_the_example_contains_no_real_key():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")

    for line in text.splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            pytest.fail("the example file must not set a real key")


# -- the CLI reads it ------------------------------------------------------


def test_cli_defaults_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("MINOS_MODEL", "qwen3:32b")
    monkeypatch.setenv("MINOS_MAX_STEPS", "5")
    from minos.__main__ import build_parser

    args = build_parser().parse_args(["run", "a goal"])

    assert args.max_steps == 5


def test_a_typed_flag_beats_the_environment(monkeypatch):
    """The rule that matters: what you typed wins."""
    monkeypatch.setenv("MINOS_MAX_STEPS", "5")
    monkeypatch.setenv("MINOS_BASE_URL", "http://from-env:1234/v1")
    from minos.__main__ import build_parser

    args = build_parser().parse_args(
        ["run", "a goal", "--max-steps", "99", "--base-url", "http://typed:1/v1"]
    )

    assert args.max_steps == 99
    assert args.base_url == "http://typed:1/v1"


def test_offline_can_be_set_by_environment(monkeypatch):
    monkeypatch.setenv("MINOS_OFFLINE", "1")
    from minos.__main__ import build_parser

    assert build_parser().parse_args(["run", "g"]).offline is True


def test_settings_is_immutable(tmp_path):
    cfg = settings(dotenv=tmp_path / "none")

    with pytest.raises((AttributeError, TypeError)):
        cfg.model = "something-else"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("planner", "expected"),
    [("auto", True), ("local", True), ("claude", False)],
)
def test_is_local_covers_auto_and_local(tmp_path, monkeypatch, planner, expected):
    # One case per parametrisation rather than a loop: load_dotenv deliberately
    # does not override a variable already in the environment, so looping would
    # test the first value three times.
    monkeypatch.delenv("MINOS_PLANNER", raising=False)
    (tmp_path / ".env").write_text(f"MINOS_PLANNER={planner}\n")

    assert settings(dotenv=tmp_path / ".env").is_local is expected


def test_doctor_reports_the_configuration():
    from minos.doctor import _config_section

    text = "\n".join(_config_section())

    assert "planner" in text
    assert "local model" in text


def test_settings_type():
    assert isinstance(settings(dotenv=None), Settings)
