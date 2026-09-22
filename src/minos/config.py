"""Configuration from the environment, with a local model as the default.

Three layers, narrowest wins: **a command-line flag beats `.env` beats the
process environment beats the built-in default.** A flag someone typed is the
most specific statement of intent available, so nothing may override it.

The defaults are deliberately local. This runtime's claim is that it works
offline on one laptop, so the out-of-the-box configuration points at Ollama on
localhost and a model that fits in 8 GB of VRAM. Reaching a remote API is
something you configure, not something that happens because you forgot to.

`.env` is read without a dependency. python-dotenv is a fine library and this is
twenty lines; the runtime has zero required dependencies and that is worth more
than the twenty lines.

**Secrets belong in the environment, not in the repository.** `.env` is
gitignored and `.env.example` is the documented, committed one. Nothing here
ever logs a value — `describe()` exists so that a misconfiguration can be
diagnosed without an API key ending up in a terminal someone screenshots.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "DEFAULTS",
    "Settings",
    "load_dotenv",
    "settings",
]

DEFAULTS: dict[str, str] = {
    # Local first. Ollama's OpenAI-compatible endpoint, on this machine.
    "MINOS_PLANNER": "auto",
    "MINOS_BASE_URL": "http://localhost:11434/v1",
    "MINOS_MODEL": "qwen3:8b",
    "MINOS_REMOTE_MODEL": "claude-opus-5",
    "MINOS_OFFLINE": "0",
    "MINOS_MAX_STEPS": "20",
    "MINOS_STATE": ".minos",
    "MINOS_SANDBOX_TIMEOUT": "60",
    "MINOS_CHECKPOINT_DAYS": "7",
    "MINOS_CHECKPOINT_GB": "2",
    "MINOS_CONTEXT_TOKENS": "16384",
}
"""Every knob, with the value you get if you set nothing.

One table rather than defaults scattered through argparse, because "what will
this do on a fresh checkout" should be answerable by reading one thing.
"""

_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def load_dotenv(path: Path | str = ".env", *, override: bool = False) -> dict[str, str]:
    """Read a ``.env`` file into the process environment.

    ``override`` is False by default: a variable already exported in the shell
    wins over the file, which is what anyone who has just run
    ``MINOS_MODEL=... minos run`` expects.

    Tolerant by design. A malformed line is skipped rather than raising, because
    failing to start over a stray line in a config file is a worse outcome than
    ignoring it.
    """
    file = Path(path).expanduser()
    loaded: dict[str, str] = {}
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return loaded

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export ") :].strip()
        if not name:
            continue
        value = value.strip()
        # Strip one layer of matching quotes, so both KEY=v and KEY="v" work.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or name not in os.environ:
            os.environ[name] = value
        loaded[name] = value
    return loaded


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved configuration. Values, never secrets in ``repr``."""

    planner: str
    base_url: str
    model: str
    remote_model: str
    offline: bool
    max_steps: int
    state: str
    sandbox_timeout: float
    checkpoint_days: float
    checkpoint_gb: float
    context_tokens: int
    source: dict[str, str] = field(default_factory=dict, repr=False)
    """Where each value came from, for `minos doctor`. Names only, no values."""

    @property
    def is_local(self) -> bool:
        return self.planner in {"local", "auto"}

    def model_for(self, planner: str) -> str:
        """The model name to use for a given planner choice."""
        return self.remote_model if planner == "claude" else self.model

    def describe(self) -> list[str]:
        """Human-readable configuration, with secrets redacted.

        Never prints a value that looks like a credential, because the most
        likely reason anyone runs this is to paste the output somewhere.
        """
        lines = [
            f"  planner    : {self.planner}",
            f"  base url   : {self.base_url}",
            f"  local model: {self.model}",
            f"  remote     : {self.remote_model}",
            f"  offline    : {'yes' if self.offline else 'no'}",
        ]
        for name in sorted(os.environ):
            if name.startswith("MINOS_") and any(m in name for m in _SECRET_MARKERS):
                lines.append(f"  {name.lower():<11}: set (hidden)")
        if any(k in os.environ for k in ("ANTHROPIC_API_KEY",)):
            lines.append("  anthropic  : API key set (hidden)")
        return lines


def _get(name: str) -> tuple[str, str]:
    """A value and where it came from."""
    if name in os.environ and os.environ[name] != "":
        return os.environ[name], "environment"
    return DEFAULTS[name], "default"


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_float(value: str, fallback: float) -> float:
    try:
        return float(value)
    except ValueError:
        # A typo in a config file should not stop the runtime starting.
        return fallback


def settings(dotenv: Path | str | None = ".env") -> Settings:
    """Resolve configuration: ``.env``, then the environment, then defaults.

    Flags are applied by the caller *on top* of this, because argparse is where
    a typed flag lives and a typed flag is the most specific intent there is.
    """
    if dotenv is not None:
        load_dotenv(dotenv)

    source: dict[str, str] = {}
    values: dict[str, str] = {}
    for name in DEFAULTS:
        value, origin = _get(name)
        values[name] = value
        source[name] = origin

    return Settings(
        planner=values["MINOS_PLANNER"],
        base_url=values["MINOS_BASE_URL"],
        model=values["MINOS_MODEL"],
        remote_model=values["MINOS_REMOTE_MODEL"],
        offline=_as_bool(values["MINOS_OFFLINE"]),
        max_steps=int(_as_float(values["MINOS_MAX_STEPS"], 20)),
        state=values["MINOS_STATE"],
        sandbox_timeout=_as_float(values["MINOS_SANDBOX_TIMEOUT"], 60.0),
        checkpoint_days=_as_float(values["MINOS_CHECKPOINT_DAYS"], 7.0),
        checkpoint_gb=_as_float(values["MINOS_CHECKPOINT_GB"], 2.0),
        context_tokens=int(_as_float(values["MINOS_CONTEXT_TOKENS"], 16384)),
        source=source,
    )
