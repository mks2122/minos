"""Watching a run, and keeping what you watched.

Two problems, one mechanism.

**You cannot see anything.** A local model takes tens of seconds per step, and
until now the agent printed nothing until the whole run finished. A watcher had
no idea whether it was thinking, stuck, or about to do something alarming. For a
runtime whose pitch is "you decide what it may do", being unable to see what it
is doing is not a cosmetic gap.

**Nothing is kept.** The audit log records *admitted actions* — deliberately, it
is a tamper-evident record of what touched the world. It does not record the
model's reasoning, the steps that were denied before they became actions, or the
code a script ran. That is the material you need to answer "why did it do that",
and it was being discarded.

So: the agent emits :class:`Event` objects as it goes, and anything can listen.
:class:`ConsolePrinter` shows them live; :class:`SessionRecorder` writes them to
``.minos/sessions/`` as JSONL for afterwards.

**A session transcript is not an audit log and must not be mistaken for one.**
The audit chain is hash-linked and tamper-evident because it is the record of
what was done. A transcript is a debugging convenience: plain JSONL, unchained,
rewritable. Keeping them in separate files with different guarantees is the
honest arrangement.

Everything here treats planner output as **untrusted**: reasoning text is
displayed and stored, never parsed for decisions, and it is truncated before it
reaches a terminal so a hostile model cannot flood the screen.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ConsolePrinter",
    "Event",
    "Observer",
    "SessionRecorder",
    "fan_out",
]

_MAX_THINKING = 1200
_MAX_VALUE = 400


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened during a run.

    ``kind`` is a small fixed vocabulary rather than free text, so a listener can
    switch on it without string-matching prose:

    ``thinking``   the planner's commentary before it chose
    ``plan``       the action it chose, with arguments
    ``route``      which tier will serve it, and why not a better one
    ``verdict``    the broker's admission decision
    ``result``     what happened, including any reversal
    ``finish``     the run ended
    ``note``       anything else worth showing
    """

    kind: str
    step: int
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": round(self.ts, 3),
            "step": self.step,
            "kind": self.kind,
            "text": self.text,
            "data": self.data,
        }


Observer = Callable[[Event], None]


def fan_out(*observers: Observer | None) -> Observer:
    """One observer that feeds several. Missing ones are skipped.

    A failing observer must never take the run down with it -- watching is not
    supposed to change the outcome.
    """
    live = [o for o in observers if o is not None]

    def emit(event: Event) -> None:
        for observer in live:
            # Deliberately suppressed. A broken printer must not abort a task
            # that is otherwise going fine.
            with contextlib.suppress(Exception):
                observer(event)

    return emit


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f" ... [+{len(text) - limit} chars]"


def _short(value: Any, limit: int = _MAX_VALUE) -> str:
    """A parameter, rendered for a terminal.

    Code is the case that matters: a model's script is the single most useful
    thing to see and the single easiest thing to drown in.
    """
    if isinstance(value, str):
        return _truncate(value, limit)
    if isinstance(value, list | tuple):
        return (
            "["
            + ", ".join(_short(v, 80) for v in value[:4])
            + ("]" if len(value) <= 4 else ", ...]")
        )
    return _truncate(repr(value), limit)


@dataclass
class ConsolePrinter:
    """Live, plain-text trace of a run.

    ASCII only. A plain Windows console is cp1252 and raises on box drawing --
    a trace that crashes the run it is describing would be a poor trade.
    """

    stream: Any = field(default_factory=lambda: sys.stdout)
    show_thinking: bool = True
    show_code: bool = True

    def __call__(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.kind}", None)
        if handler is not None:
            handler(event)
        self.stream.flush()

    # -- per kind ----------------------------------------------------------

    def _on_thinking(self, event: Event) -> None:
        if not self.show_thinking or not event.text:
            return
        self._write(f"\n  [{event.step}] thinking")
        for line in _truncate(event.text, _MAX_THINKING).splitlines():
            if line.strip():
                self._write(f"        {line.strip()}")

    def _on_plan(self, event: Event) -> None:
        self._write(f"\n  [{event.step}] {event.text}")
        params = event.data.get("params") or {}
        for name, value in params.items():
            if name == "code" and self.show_code:
                self._write("        code:")
                for line in str(value).strip().splitlines():
                    self._write(f"          | {line}")
            else:
                self._write(f"        {name}: {_short(value)}")

    def _on_route(self, event: Event) -> None:
        self._write(f"        via {event.text}")

    def _on_verdict(self, event: Event) -> None:
        verdict = event.data.get("verdict", "?")
        mark = {"allow": "allowed", "deny": "DENIED", "prompt": "needs approval"}.get(
            verdict, verdict
        )
        self._write(f"        {mark}: {event.text}")

    def _on_result(self, event: Event) -> None:
        status = event.data.get("status", "?")
        mark = {"ok": "ok", "denied": "DENIED", "failed": "FAILED"}.get(status, status.upper())
        self._write(f"        -> {mark}" + (f": {event.text}" if event.text else ""))

    def _on_finish(self, event: Event) -> None:
        self._write(f"\n  {event.text}")

    def _on_note(self, event: Event) -> None:
        self._write(f"        {event.text}")

    def _write(self, line: str) -> None:
        # errors='replace': a model can emit anything, and an encoding error in
        # the trace must not end the run.
        text = line.encode("ascii", "replace").decode("ascii")
        print(text, file=self.stream)


@dataclass
class SessionRecorder:
    """Writes a run's events to ``.minos/sessions/<id>.jsonl``.

    Plain JSONL and explicitly *not* hash-chained: this is a debugging record,
    not evidence. Conflating it with the audit log would weaken the thing that
    actually needs to be trustworthy.
    """

    state: Path
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    goal: str = ""
    _path: Path | None = field(default=None, init=False, repr=False)

    @property
    def path(self) -> Path:
        if self._path is None:
            directory = Path(self.state).expanduser() / "sessions"
            directory.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self._path = directory / f"{stamp}-{self.session_id}.jsonl"
            self._append({"kind": "session", "goal": self.goal, "id": self.session_id})
        return self._path

    def __call__(self, event: Event) -> None:
        self._append(event.to_json())

    def _append(self, payload: dict[str, Any]) -> None:
        path = self._path if self._path is not None else self.path
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")

    def prune(self, keep: int = 20) -> int:
        """Keep the newest N transcripts. Returns how many were removed.

        Unbounded debugging output on a laptop is a slow leak, and these are
        worth less the older they get.
        """
        directory = Path(self.state).expanduser() / "sessions"
        if not directory.is_dir():
            return 0
        files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        removed = 0
        for stale in files[keep:]:
            with contextlib.suppress(OSError):
                os.remove(stale)
                removed += 1
        return removed
