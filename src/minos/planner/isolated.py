"""The planner in its own process, confined so that it cannot act.

Invariant I1 says the planner never executes. Until now that was true by
construction -- the planner's interface has no way to act -- but the planner
shared a process with the broker, so a bug anywhere in it (a parser, the model
SDK, a dependency of either) ran with everything the broker can do: write any
file the user can, start any program, read the audit log and the checkpoints.

Here the planner runs as a child process whose only channel to the world is
the network it needs to reach its model, and whose only channel to the
runtime is a pipe of JSON lines:

* **Windows**: a low-integrity token (it cannot write anything the user owns)
  and a Job Object allowing exactly one process (it cannot start a program).
* **Linux**: Landlock, applied by the child to itself before it reads its first
  message, granting read access to the filesystem and write access to nothing.
* **macOS**: a seatbelt profile denying file writes and process creation.
* Anywhere the kernel offers none of these, the child is still a separate
  process -- it cannot reach the broker's memory -- and the report says the
  confinement is absent rather than implying it.

The trusted side never unpickles, evaluates or imports anything the child
sends. Each reply is one JSON line, size-capped, decoded into an
:class:`~minos.types.ActionRequest` or :class:`~minos.planner.base.Done` field by
field, and rejected whole if any field is the wrong shape. A compromised
planner can therefore say anything it likes -- which it always could -- and do
nothing else.

Run the child directly with ``python -m minos.planner.isolated``; it speaks the
protocol on stdin and stdout and is useless without a parent.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from ..scopes import ScopeSet
from ..types import ActionRequest, Tier
from .base import Done, Observation, Step

__all__ = ["IsolatedPlanner", "PlannerProcessError", "isolation_available"]

MAX_REPLY_BYTES = 4 * 1024 * 1024
"""One reply line, at most. A step is a few hundred bytes; thinking a few KB."""

_PLANNERS = {
    "minos.planner.local:LocalPlanner",
    "minos.planner.claude:ClaudePlanner",
    "minos.planner.scripted:ScriptedPlanner",
}
"""What the child may construct. The parent chooses, but the list is short on
purpose: the child should never be a general-purpose object factory."""

_CALLS = {"context_warning", "overhead_tokens"}
_SETTABLE = {"context_window"}

_OPERATION = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class PlannerProcessError(RuntimeError):
    """The planner process failed, died, or sent something that is not a step."""


def isolation_available() -> str:
    """What confinement the planner process would get here, in a few words."""
    if os.name == "nt":
        return "low-integrity token + one-process job"
    if sys.platform.startswith("linux"):
        from ..sandbox._child_confine import landlock_abi

        return "Landlock, read-only filesystem" if landlock_abi() else "separate process only"
    if sys.platform == "darwin":
        return "seatbelt: no writes, no new processes"
    return "separate process only"


# -- encoding ------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    from .claude import _jsonable as convert

    return convert(value)


def _encode_observation(observation: Observation) -> dict[str, Any]:
    request = observation.request
    payload: dict[str, Any] = {
        "request": {
            "goal_id": request.goal_id,
            "intent": request.intent,
            "operation": request.operation,
            "params": _jsonable(request.params),
            "tier_hint": str(request.tier_hint) if request.tier_hint else None,
        },
        "status": observation.status,
        "result": _jsonable(observation.result),
        "error": observation.error,
        "detail": observation.detail,
    }
    # Fields a newer Observation carries (the loop guard's warning) travel too.
    for name in ("warning",):
        if hasattr(observation, name):
            payload[name] = getattr(observation, name)
    return payload


def _decode_observation(data: dict[str, Any]) -> Observation:
    request = data["request"]
    hint = request.get("tier_hint")
    extra = {
        name: data[name]
        for name in ("warning",)
        if name in data and name in {f.name for f in dataclasses.fields(Observation)}
    }
    return Observation(
        request=ActionRequest(
            goal_id=str(request.get("goal_id", "")),
            intent=str(request.get("intent", "")),
            operation=str(request.get("operation", "")),
            params=dict(request.get("params") or {}),
            tier_hint=Tier(hint) if hint else None,
        ),
        status=str(data.get("status", "")),
        result=data.get("result"),
        error=str(data.get("error", "")),
        detail=str(data.get("detail", "")),
        **extra,
    )


def _encode_step(step: Step) -> dict[str, Any]:
    if isinstance(step, Done):
        return {"kind": "done", "summary": step.summary, "succeeded": step.succeeded}
    return {
        "kind": "action",
        "goal_id": step.goal_id,
        "intent": step.intent,
        "operation": step.operation,
        "params": _jsonable(step.params),
    }


def _decode_step(data: Any) -> Step:
    """Untrusted JSON to a step, or an error. Nothing is coerced into shape."""
    if not isinstance(data, dict):
        raise PlannerProcessError("the planner process sent a step that is not an object")
    kind = data.get("kind")
    if kind == "done":
        summary, succeeded = data.get("summary"), data.get("succeeded")
        if not isinstance(summary, str) or not isinstance(succeeded, bool):
            raise PlannerProcessError("the planner process sent a malformed finish")
        return Done(summary=summary[:10_000], succeeded=succeeded)
    if kind == "action":
        operation, params = data.get("operation"), data.get("params")
        intent, goal_id = data.get("intent", ""), data.get("goal_id", "task")
        if not isinstance(operation, str) or len(operation) > 64 or not _OPERATION.match(operation):
            raise PlannerProcessError(
                f"the planner process named no valid operation: {operation!r}"[:200]
            )
        if not isinstance(params, dict) or not all(isinstance(k, str) for k in params):
            raise PlannerProcessError("the planner process sent parameters that are not an object")
        if not isinstance(intent, str) or not isinstance(goal_id, str):
            raise PlannerProcessError("the planner process sent a malformed action")
        return ActionRequest(
            goal_id=goal_id[:200], intent=intent[:2000], operation=operation, params=params
        )
    raise PlannerProcessError(f"the planner process sent an unknown kind of step: {kind!r}"[:200])


def _spec_of(planner: Any) -> dict[str, Any]:
    """The constructor arguments that rebuild ``planner`` in the child.

    Only init fields, and only the ones that survive JSON: a live SDK client
    does not travel, and the child builds its own.
    """
    cls = type(planner)
    target = f"{cls.__module__}:{cls.__qualname__}"
    if target not in _PLANNERS:
        raise TypeError(f"{target} cannot run in an isolated process")
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(planner):
        if not f.init or f.name == "client":
            continue
        value = getattr(planner, f.name)
        if f.name == "script":
            value = [_encode_step(step) for step in value]
        kwargs[f.name] = _jsonable(value)
    return {"target": target, "kwargs": kwargs}


def _build(spec: dict[str, Any]) -> Any:
    target = spec["target"]
    if target not in _PLANNERS:
        raise ValueError(f"{target} is not a planner this process may build")
    module, _, name = target.partition(":")
    cls = getattr(importlib.import_module(module), name)
    kwargs = dict(spec.get("kwargs") or {})
    hints = {f.name: f for f in dataclasses.fields(cls)}
    for key, value in list(kwargs.items()):
        if key == "script":
            kwargs[key] = [_decode_step(step) for step in value]
        elif isinstance(value, list) and key in hints and "tuple" in str(hints[key].type):
            kwargs[key] = tuple(tuple(v) if isinstance(v, list) else v for v in value)
    return cls(**kwargs)


# -- the trusted side ----------------------------------------------------------


class IsolatedPlanner:
    """A planner proxy whose planner runs in a confined child process.

    Implements the planner protocol and the handful of attributes the CLI
    reads. Everything crosses the boundary as JSON; see the module docstring
    for what the child can and cannot do.
    """

    def __init__(
        self,
        spec: dict[str, Any],
        *,
        timeout: float = 900.0,
        confine: bool = True,
        require: bool = False,
    ) -> None:
        self.timeout = timeout
        self.last_thinking = ""
        self.model = str(spec.get("kwargs", {}).get("model", ""))
        self.local_server = bool(spec.get("kwargs", {}).get("local_server", True))
        self._sent = 0
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._stderr: list[bytes] = []
        self._process = self._start(confine=confine, require=require)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        reply = self._ask({"op": "init", "spec": spec})
        self.pid = int(reply.get("pid", 0))
        self.confinement = str(reply.get("confinement", "unknown"))
        if self._parent_note:
            self.confinement = f"{self._parent_note}; {self.confinement}"

    @classmethod
    def of(cls, planner: Any, **options: Any) -> IsolatedPlanner:
        """Move an already-configured planner into a confined process."""
        return cls(_spec_of(planner), **options)

    # -- the planner protocol --------------------------------------------------

    def next_action(self, goal: str, observations: list[Observation], scopes: ScopeSet) -> Step:
        new = observations[self._sent :] if len(observations) >= self._sent else observations
        reply = self._ask(
            {
                "op": "next",
                "goal": goal,
                "scopes": [str(scope) for scope in scopes],
                "observations": [_encode_observation(o) for o in new],
                "reset_history": len(observations) < self._sent,
            }
        )
        self._sent = len(observations)
        thinking = reply.get("thinking", "")
        self.last_thinking = thinking[:20_000] if isinstance(thinking, str) else ""
        return _decode_step(reply.get("step"))

    def reset(self) -> None:
        self._ask({"op": "call", "name": "reset", "args": []}, missing_ok=True)
        self._sent = 0

    # -- what the CLI reads --------------------------------------------------

    def context_warning(self, served: int) -> str:
        reply = self._ask({"op": "call", "name": "context_warning", "args": [int(served)]})
        value = reply.get("value", "")
        return value if isinstance(value, str) else ""

    @property
    def context_window(self) -> int:
        value = self._ask({"op": "get", "name": "context_window"}).get("value", 0)
        return value if isinstance(value, int) else 0

    @context_window.setter
    def context_window(self, value: int) -> None:
        self._ask({"op": "set", "name": "context_window", "value": int(value)})

    def describe(self) -> str:
        return f"isolated in pid {self.pid} ({self.confinement})"

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._process.stdin.write(b'{"op": "exit"}\n')
            self._process.stdin.flush()
        closer = getattr(self._process, "close", None)
        if closer is not None:
            closer()
            return
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            with contextlib.suppress(Exception):
                stream.close()

    def __enter__(self) -> IsolatedPlanner:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the channel -----------------------------------------------------------

    def _start(self, *, confine: bool, require: bool) -> Any:
        env = dict(os.environ)
        # The child imports exactly what this process can, and nothing it would
        # find by looking around: no cwd entry, no user site.
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONNOUSERSITE"] = "1"
        env["MINOS_PLANNER_CONFINE"] = "1" if confine else "0"
        self._parent_note = ""

        if os.name == "nt":
            from ..sandbox.winspawn import ConfinedProcess

            # The base interpreter, not a venv's launcher: the launcher starts
            # the real one as a second process, which the job forbids.
            python = getattr(sys, "_base_executable", "") or sys.executable
            argv = [python, "-X", "utf8", "-m", "minos.planner.isolated"]
            if confine:
                process = ConfinedProcess(argv, cwd=Path.cwd(), env=env, require=require)
                if process.integrity_lowered and process.job_object:
                    self._parent_note = "low-integrity token: cannot write files"
                    self._parent_note += "; one-process job: cannot start programs"
                else:
                    self._parent_note = f"Windows confinement incomplete ({process.detail})"
                return process
            return subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
            )

        argv = [sys.executable, "-X", "utf8", "-m", "minos.planner.isolated"]
        if confine and sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists():
            argv = ["/usr/bin/sandbox-exec", "-p", _SEATBELT_PLANNER, *argv]
            self._parent_note = "seatbelt: no file writes, no new processes"
        return subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )

    def _read_stdout(self) -> None:
        stream = self._process.stdout
        try:
            while True:
                line = stream.readline(MAX_REPLY_BYTES + 1)
                if not line:
                    break
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        self._lines.put(None)

    def _read_stderr(self) -> None:
        # Drained so a chatty child can never fill the pipe and stall; only
        # the tail is kept, for the error message when it dies.
        stream = self._process.stderr
        with contextlib.suppress(OSError, ValueError):
            for chunk in iter(lambda: stream.read1(4096), b""):
                self._stderr.append(chunk)
                if len(self._stderr) > 64:
                    del self._stderr[:-32]

    def _stderr_tail(self) -> str:
        text = b"".join(self._stderr).decode("utf-8", "replace").strip()
        return text[-1500:]

    def _ask(self, message: dict[str, Any], *, missing_ok: bool = False) -> dict[str, Any]:
        data = (json.dumps(message, default=str) + "\n").encode("utf-8")
        try:
            self._process.stdin.write(data)
            self._process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise PlannerProcessError(
                f"the planner process is gone ({exc}). {self._stderr_tail()}".strip()
            ) from exc

        try:
            line = self._lines.get(timeout=self.timeout)
        except queue.Empty as exc:
            self._process.kill()
            raise PlannerProcessError(
                f"the planner process did not answer within {self.timeout:.0f}s and was stopped"
            ) from exc
        if line is None:
            raise PlannerProcessError(
                "the planner process exited. " + (self._stderr_tail() or "It said nothing.")
            )
        if len(line) > MAX_REPLY_BYTES:
            self._process.kill()
            raise PlannerProcessError("the planner process sent a reply over the size limit")
        try:
            reply = json.loads(line)
        except ValueError as exc:
            raise PlannerProcessError(
                "the planner process sent something that is not JSON"
            ) from exc
        if not isinstance(reply, dict):
            raise PlannerProcessError("the planner process sent a reply that is not an object")
        if not reply.get("ok"):
            error = reply.get("error", "unknown error")
            if missing_ok and reply.get("missing"):
                return reply
            raise PlannerProcessError(f"the planner failed: {str(error)[:2000]}")
        return reply


_SEATBELT_PLANNER = """(version 1)
(allow default)
(deny file-write*)
(allow file-write-data (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))
(deny process-fork)
"""
"""Everything the interpreter needs, except writing and starting processes.
``allow default`` is wider than the sandbox's profile on purpose: the planner
must reach the network and read its own installation; what it must not do is
change anything or launch anything."""


# -- the child -----------------------------------------------------------------


def _confine_self() -> str:
    """Apply what this platform lets a process apply to itself. Reported, never assumed."""
    if os.environ.get("MINOS_PLANNER_CONFINE") != "1":
        return "unconfined (asked not to be)"
    if sys.platform.startswith("linux"):
        from ..sandbox._child_confine import Applied, _landlock

        report = Applied()
        _landlock(["/dev/null"], ["/"], report, restrict_network=False)
        if report.enforced:
            return "Landlock: the filesystem is read-only to this process"
        return "Landlock unavailable: " + "; ".join(report.skipped)
    if os.name == "nt":
        return "confined by the parent"
    if sys.platform == "darwin":
        return "confined by the parent"
    return "no confinement on this platform"


def _serve(stdin: IO[bytes], out: Callable[[dict[str, Any]], None]) -> None:
    planner: Any = None
    observations: list[Observation] = []
    while True:
        line = stdin.readline()
        if not line:
            return
        try:
            message = json.loads(line)
            op = message.get("op")
            if op == "exit":
                return
            if op == "init":
                confinement = _confine_self()
                planner = _build(message["spec"])
                out({"ok": True, "pid": os.getpid(), "confinement": confinement})
            elif planner is None:
                out({"ok": False, "error": "not initialised"})
            elif op == "next":
                if message.get("reset_history"):
                    observations = []
                observations.extend(_decode_observation(o) for o in message["observations"])
                scopes = ScopeSet.parse(message["scopes"])
                step = planner.next_action(message["goal"], observations, scopes)
                out(
                    {
                        "ok": True,
                        "step": _encode_step(step),
                        "thinking": str(getattr(planner, "last_thinking", "") or ""),
                    }
                )
            elif op == "call":
                name = message["name"]
                method = getattr(planner, name, None)
                if name == "reset":
                    observations = []
                if (name in _CALLS or name == "reset") and callable(method):
                    out({"ok": True, "value": _jsonable(method(*message.get("args", [])))})
                else:
                    out({"ok": False, "missing": True, "error": f"{name} is not available"})
            elif op == "get" and message["name"] in _SETTABLE:
                out({"ok": True, "value": getattr(planner, message["name"], None)})
            elif op == "set" and message["name"] in _SETTABLE:
                setattr(planner, message["name"], message["value"])
                out({"ok": True})
            else:
                out({"ok": False, "error": f"unknown request {op!r}"})
        except Exception as exc:
            out({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def main() -> None:
    # The protocol owns the real stdout. Anything else that prints -- a
    # library's warning, a stray debug line -- goes to stderr, where it cannot
    # be mistaken for a reply.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr

    def out(reply: dict[str, Any]) -> None:
        protocol.write(json.dumps(reply, default=str) + "\n")
        protocol.flush()

    _serve(sys.stdin.buffer, out)


if __name__ == "__main__":
    main()
