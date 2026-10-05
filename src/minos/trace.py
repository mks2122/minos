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
import re
import sys
import threading
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
    "SessionStep",
    "SessionSummary",
    "fan_out",
    "find_session",
    "list_sessions",
    "load_session",
    "printable",
    "resume_context",
]

_MAX_THINKING = 1200
_MAX_VALUE = 400

# C0 controls except tab, plus DEL and C1. ESC is ASCII, so encoding to ASCII
# does not remove it, and a model that can print ESC can repaint the terminal:
# hide a line, fake a prompt, rewrite what an approval appears to say.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")


def printable(text: str) -> str:
    """One line of untrusted text, safe to put on a terminal: ASCII, no controls."""
    return _CONTROL.sub("?", text).encode("ascii", "replace").decode("ascii")


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened during a run.

    ``kind`` is a small fixed vocabulary rather than free text, so a listener can
    switch on it without string-matching prose:

    ``waiting``    the planner has been asked for the next step
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
    spinner: bool | None = None
    """Animate the wait for the model. None means only on a real terminal,
    where a carriage return redraws the line instead of littering a log."""

    _spin_stop: threading.Event | None = field(default=None, init=False, repr=False)
    _spin_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __call__(self, event: Event) -> None:
        self.stop_spinner()
        handler = getattr(self, f"_on_{event.kind}", None)
        if handler is not None:
            handler(event)
        self.stream.flush()

    def stop_spinner(self) -> None:
        """Stop and erase the waiting line. Safe to call when none is running.

        The run's owner calls this on the way out as well: a planner that raises
        never sends the event that would otherwise stop it.
        """
        stop, thread = self._spin_stop, self._spin_thread
        if stop is None or thread is None:
            return
        stop.set()
        thread.join(timeout=1.0)
        self._spin_stop = self._spin_thread = None
        with self._lock:
            self.stream.write("\r" + " " * 60 + "\r")
            self.stream.flush()

    def _animate(self) -> bool:
        if self.spinner is not None:
            return self.spinner
        try:
            return bool(self.stream.isatty())
        except Exception:
            return False

    # -- per kind ----------------------------------------------------------

    def _on_waiting(self, event: Event) -> None:
        # A local model can be silent for half a minute. Without this, a slow
        # step and a hung one look exactly the same.
        if not self._animate():
            return
        stop = threading.Event()
        started = time.monotonic()
        step = event.step

        def spin() -> None:
            frames = "|/-\\"
            tick = 0
            while not stop.is_set():
                elapsed = int(time.monotonic() - started)
                with self._lock:
                    if stop.is_set():
                        return
                    frame = frames[tick % len(frames)]
                    self.stream.write(f"\r  [{step}] waiting for the model {frame} {elapsed}s ")
                    self.stream.flush()
                tick += 1
                stop.wait(0.25)

        self._spin_stop = stop
        self._spin_thread = threading.Thread(target=spin, name="minos-spinner", daemon=True)
        self._spin_thread.start()

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
        text = "\n".join(printable(part) for part in line.split("\n"))
        with self._lock:
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
    resumed_from: str = ""
    """The session this run continues, when it is a ``--resume``."""

    _path: Path | None = field(default=None, init=False, repr=False)

    @property
    def path(self) -> Path:
        if self._path is None:
            directory = Path(self.state).expanduser() / "sessions"
            directory.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self._path = directory / f"{stamp}-{self.session_id}.jsonl"
            header: dict[str, Any] = {"kind": "session", "goal": self.goal, "id": self.session_id}
            if self.resumed_from:
                header["resumed_from"] = self.resumed_from
            self._append(header)
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


# -- reading a session back ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionStep:
    operation: str
    params: dict[str, Any]
    status: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """What a recorded run did, read back from its transcript."""

    path: Path
    session_id: str
    goal: str
    steps: tuple[SessionStep, ...] = ()
    finished: bool = False
    """A ``finish`` event was recorded: the run ended rather than died."""

    succeeded: bool = False
    summary: str = ""

    @property
    def interrupted(self) -> bool:
        return not self.finished


def load_session(path: Path) -> SessionSummary:
    """Parse one transcript. Tolerant: a run that died mid-write leaves a torn line."""
    session_id, goal = path.stem.rsplit("-", 1)[-1], ""
    steps: list[SessionStep] = []
    pending: dict[str, Any] | None = None
    finished = succeeded = False
    summary = ""
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("kind")
            data = event.get("data") or {}
            if kind == "session":
                goal = str(event.get("goal", ""))
                session_id = str(event.get("id", session_id))
            elif kind == "plan":
                pending = {
                    "operation": str(event.get("text", "")),
                    "params": data.get("params") or {},
                }
            elif kind == "result" and pending is not None:
                steps.append(
                    SessionStep(
                        operation=pending["operation"],
                        params=dict(pending["params"]),
                        status=str(data.get("status", "")),
                        detail=str(event.get("text", "")),
                    )
                )
                pending = None
            elif kind == "finish":
                finished = True
                succeeded = bool(data.get("succeeded", False))
                summary = str(event.get("text", ""))
    return SessionSummary(
        path=path,
        session_id=session_id,
        goal=goal,
        steps=tuple(steps),
        finished=finished,
        succeeded=succeeded,
        summary=summary,
    )


def list_sessions(state: Path) -> list[SessionSummary]:
    """Every transcript under ``state``, newest first."""
    directory = Path(state).expanduser() / "sessions"
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("*.jsonl"), key=lambda p: p.name, reverse=True)
    return [load_session(path) for path in files]


def find_session(state: Path, which: str = "") -> SessionSummary:
    """The newest session, or the one whose id starts with ``which``.

    Raises ``LookupError`` naming the candidates when the prefix is ambiguous,
    for the same reason a GUI control with two matches is refused: guessing
    which run to continue is how the wrong one gets continued.
    """
    sessions = list_sessions(state)
    if not sessions:
        raise LookupError(f"no recorded sessions under {Path(state) / 'sessions'}")
    if not which:
        return sessions[0]
    matches = [s for s in sessions if s.session_id.startswith(which)]
    if not matches:
        raise LookupError(f"no session id starts with {which!r}; see `minos sessions`")
    if len(matches) > 1:
        ids = ", ".join(s.session_id for s in matches[:5])
        raise LookupError(f"{which!r} matches {len(matches)} sessions ({ids}); give more of the id")
    return matches[0]


def resume_context(session: SessionSummary, *, max_steps: int = 40) -> str:
    """Prime a fresh run with what an earlier one did.

    A new conversation, not the old one replayed: a thinking block is valid
    only in the conversation that produced it, and a summary costs a fraction
    of the tokens. The steps come from the runtime's own record of the run, but
    their details include tool output, which is untrusted; they are offered as
    background, and the model is told the world may have moved since.
    """
    shown = session.steps[-max_steps:]
    skipped = len(session.steps) - len(shown)
    lines = [
        f"This continues an earlier run (session {session.session_id}) that "
        + ("was interrupted" if session.interrupted else f"ended: {session.summary[:200]}")
        + ". What it did is below. Files may have changed since: read before you "
        "rely on anything, and do not redo a step that already succeeded.",
    ]
    if skipped:
        lines.append(f"  ({skipped} earlier steps not shown)")
    for number, step in enumerate(shown, start=skipped + 1):
        params = json.dumps(step.params, default=str, sort_keys=True)
        if len(params) > 160:
            params = params[:160] + "..."
        detail = f" -- {step.detail[:160]}" if step.detail and step.status != "ok" else ""
        lines.append(f"  {number}. {step.operation} {params} -> {step.status}{detail}")
    return "\n".join(lines)
