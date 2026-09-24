"""Running code in the workspace.

The default backend is a subprocess, and what confines it depends on the
platform underneath. On Linux it is Landlock and seccomp-bpf: the filesystem is
narrowed to the workspace and the socket syscalls are refused by the kernel, so
a script that reaches libc directly gets EPERM rather than a way out. On Windows
it is a low-integrity token and a Job Object: writes outside the workspace are
refused by mandatory integrity, and memory and process count are bounded. On
macOS it is a ``sandbox-exec`` profile. Where none of that is available -- an
old kernel, an unknown platform -- what remains is the in-process hardening,
which stops a script that wanders off and does not stop one that is trying.

**The runtime never assumes which of those it got.** :func:`SubprocessSandbox.run`
asks :mod:`minos.sandbox.confine` what the platform actually offers, records the
answer in :attr:`CodeResult.confinement`, and ``require_confinement=True``
refuses to run at all rather than run unconfined. ``minos doctor`` prints the
same line. A control that is absent and silent is the thing this project treats
as a bug.

For code whose origin is not the local planner, none of the above is the right
answer: see :mod:`minos.sandbox.container` for a backend that does not share the
namespace at all, and :mod:`minos.sandbox.origin` for the policy that chooses.

What this backend enforces, and where each piece lives:

- **Filesystem.** Landlock (Linux) / integrity label (Windows) / seatbelt
  (macOS), plus an ``open`` audit hook in the child as a readable error and a
  last resort.
- **Network.** seccomp (Linux) / seatbelt (macOS), plus a patched ``socket``
  module everywhere.
- **Escape by re-entry.** ``subprocess.Popen``, ``os.system`` and
  ``ctypes.dlopen`` are refused at the call by an audit hook, which cannot be
  uninstalled. Imports are left alone by default: refusing them breaks packages
  that import ``subprocess`` for a branch they never take.
- **Environment.** Anything that looks like a credential is removed before the
  process starts.
- **Memory, processes, wall clock, output size.** ``RLIMIT_*`` on POSIX, a Job
  Object on Windows, a timeout and a truncation cap in the parent.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .confine import Confinement, ConfinementNotApplied, detect
from .workspace import Artifact, Workspace

__all__ = [
    "CodeResult",
    "SandboxBackend",
    "SubprocessSandbox",
    "Unconfined",
    "scrubbed_environment",
]

_MAX_OUTPUT = 64_000
"""Characters of stdout/stderr kept. The rest is truncated with a marker."""

_CREDENTIAL_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "SESSION",
    "COOKIE",
)

_KEEP_ALWAYS = frozenset(
    {
        # Without these Python does not start on Windows, and tempfile does not
        # work anywhere. They carry no secrets.
        "PATH",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "LANG",
        "LC_ALL",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
    }
)


class Unconfined(RuntimeError):
    """Raised when confinement was required and the platform cannot provide it.

    Deliberately not a warning. ``require_confinement=True`` is a statement that
    unconfined execution is unacceptable for this code, and the only honest
    response to "I cannot do that here" is to stop.
    """


def scrubbed_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a sandboxed script gets: the minimum that still works.

    Allow-list first, then a deny-list pass over what survived. An allow-list
    alone would break on the next platform that needs a variable nobody thought
    of; a deny-list alone leaks anything unusual. Both is not paranoia, it is
    the difference between ``ANTHROPIC_API_KEY`` reaching a script or not.
    """
    source = dict(os.environ if base is None else base)
    kept: dict[str, str] = {}
    for name, value in source.items():
        upper = name.upper()
        if upper not in _KEEP_ALWAYS:
            continue
        if any(marker in upper for marker in _CREDENTIAL_MARKERS):
            continue
        kept[name] = value

    # Deterministic, and never inherits the parent's interpreter settings.
    kept["PYTHONDONTWRITEBYTECODE"] = "1"
    kept["PYTHONHASHSEED"] = "0"
    kept["PYTHONUNBUFFERED"] = "1"
    kept["MINOS_SANDBOX"] = "1"
    return kept


_RUNNER = '''\
"""Written by minos. Installs the sandbox's restrictions, then runs the script.
Not part of the user's code and never promoted.

Everything interesting is in _minos_confine, which is a copy of
minos/sandbox/_child_confine.py placed here by the parent. This file only
sequences it: confine, report what held, then run. The report is written to a
file rather than printed, because stderr belongs to the script.
"""

import runpy
import sys
from pathlib import Path

import _minos_confine

if __name__ == "__main__":
    target, workspace, memory, flags = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    applied = _minos_confine.apply(
        workspace,
        memory,
        kernel="k" in flags,
        network="n" in flags,
        modules="m" in flags,
        filesystem="f" in flags,
        ctypes_calls="c" in flags,
        blocked_imports=tuple(n for n in sys.argv[5].split(",") if n),
    )
    try:
        Path(__file__).with_name("_minos_confinement.txt").write_text(
            applied.render(), encoding="utf-8"
        )
    except OSError:
        pass

    sys.argv = [target]
    runpy.run_path(target, run_name="__main__")
'''


@dataclass(frozen=True, slots=True)
class CodeResult:
    """What a sandbox run produced.

    ``stdout`` and ``stderr`` are **untrusted**: they are chosen by code the
    planner wrote and they flow back into the planner's context, so they are a
    prompt-injection carrier exactly like `ProcessResult`.
    """

    ok: bool
    returncode: int
    stdout: str
    stderr: str
    artifacts: tuple[Artifact, ...] = ()
    timed_out: bool = False
    duration: float = 0.0
    detail: str = ""
    confinement: str = ""
    """What actually confined this run, as reported by the platform and by the
    child itself. Recorded rather than assumed: the same code runs under a
    kernel boundary on one machine and under none on another, and a result that
    does not say which is a result nobody can reason about."""

    def summary(self) -> str:
        if self.timed_out:
            return f"timed out after {self.duration:.1f}s"
        if self.ok:
            return f"produced {len(self.artifacts)} artifact(s) in {self.duration:.1f}s"
        return f"exited {self.returncode}: {self.stderr.strip().splitlines()[-1:] or ['no output']}"


class SandboxBackend(Protocol):
    """Swap-in point for a stronger jail.

    The subprocess backend is a deliberate default, not a permanent one. The
    container backend implements this, and so would gVisor or a microVM, without
    anything above it changing.
    """

    name: str

    def run(self, workspace: Workspace, code: str, *, timeout: float) -> CodeResult: ...


@dataclass
class SubprocessSandbox:
    """The default backend: a subprocess, confined as far as the platform allows.

    The four ``confine_*`` switches exist because each layer has a different
    cost. ``confine_modules`` refuses ``ctypes``, which a handful of scientific
    packages want; ``confine_filesystem`` refuses reads outside the workspace,
    which is exactly the point but will surprise a script that expected to load
    a font from the system. They default on. Turning one off is a decision that
    belongs to the person running the task, and the result says which were
    applied.
    """

    name: str = "subprocess"
    max_memory_bytes: int = 2 << 30
    default_timeout: float = 60.0
    python: str = field(default_factory=lambda: sys.executable)

    confine_kernel: bool = True
    """Landlock/seccomp, seatbelt, or a low-integrity token, per platform."""
    confine_network: bool = True
    confine_modules: bool = True
    """Refuse the calls that leave the interpreter: ``subprocess.Popen``,
    ``os.system``, ``ctypes.dlopen``. Call-level rather than import-level, for
    the reason ``_child_confine.STRICT_IMPORTS`` documents."""
    confine_filesystem: bool = True
    confine_ctypes: bool = False
    """Refuse ``ctypes.dlopen``. Off by default: it breaks numpy, pandas and
    openpyxl, and on Windows it breaks ``import ctypes`` outright. What contains
    ctypes is the kernel layer -- see ``_child_confine._CTYPES_EVENTS``."""
    blocked_imports: tuple[str, ...] = ()
    """Modules to refuse at ``import``, on top of the call-level refusals.

    Empty by default because a denylist breaks real packages -- ``pypdf``
    imports ``subprocess`` for a branch it will not take. Pass
    ``_child_confine.STRICT_IMPORTS`` when that trade is worth making."""
    require_confinement: bool = False
    """Refuse to run where the kernel will not enforce anything."""

    @property
    def confinement(self) -> Confinement:
        return detect() if self.confine_kernel else _NOTHING

    def describe(self) -> str:
        """One line naming what will actually contain the next run."""
        soft = self.in_process_layers()
        layers = [self.confinement.describe()]
        if soft:
            layers.append("in-process: " + ", ".join(soft))
        return "; ".join(layers)

    def in_process_layers(self) -> list[str]:
        """The layers applied inside the child, named for doctor and the trace."""
        return [
            name
            for name, on in (
                ("socket patched", self.confine_network),
                ("spawn refused", self.confine_modules),
                ("dlopen refused", self.confine_ctypes),
                ("filesystem allow-list", self.confine_filesystem),
                (f"{len(self.blocked_imports)} imports refused", bool(self.blocked_imports)),
            )
            if on
        ]

    def _flags(self) -> str:
        return "".join(
            letter
            for letter, on in (
                ("k", self.confine_kernel),
                ("n", self.confine_network),
                ("m", self.confine_modules),
                ("f", self.confine_filesystem),
                ("c", self.confine_ctypes),
            )
            if on
        )

    def run(self, workspace: Workspace, code: str, *, timeout: float | None = None) -> CodeResult:
        confinement = self.confinement
        if self.require_confinement and not confinement.kernel_enforced:
            raise Unconfined(
                "this run requires kernel-enforced confinement and this platform offers "
                f"none ({confinement.detail}). Use the container backend, or say "
                "explicitly that an unconfined run is acceptable."
            )

        script = workspace.run / "script.py"
        script.write_text(textwrap.dedent(code), encoding="utf-8")
        runner = workspace.run / "_minos_runner.py"
        runner.write_text(_RUNNER, encoding="utf-8")
        # A copy rather than an import: the child runs with -E -s in a directory
        # that is not a package and has no path to the installed runtime, which
        # is the property that keeps the sandbox from reaching back into minos.
        (workspace.run / "_minos_confine.py").write_text(
            Path(__file__).with_name("_child_confine.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        report = workspace.run / "_minos_confinement.txt"
        report.unlink(missing_ok=True)

        confinement.prepare(workspace.root)
        env = scrubbed_environment()
        # The confined child may not be able to write the user's temp directory
        # -- a low-integrity process certainly cannot -- and an interpreter that
        # cannot make a temp file fails in ways nobody enjoys diagnosing.
        scratch = workspace.run / "tmp"
        scratch.mkdir(exist_ok=True)
        for name in ("TEMP", "TMP", "TMPDIR"):
            env[name] = str(scratch)

        argv = confinement.wrap(
            [
                self.python,
                "-E",  # ignore PYTHON* environment variables
                "-s",  # no user site-packages
                "-B",  # no bytecode, so the workspace stays clean
                str(runner),
                str(script),
                str(workspace.root),
                str(self.max_memory_bytes),
                self._flags() or "-",
                ",".join(self.blocked_imports),
            ],
            workspace.root,
        )

        limit = float(timeout if timeout is not None else self.default_timeout)
        started = time.monotonic()

        def finish(
            ok: bool,
            returncode: int,
            stdout: str | bytes | None,
            stderr: str | bytes | None,
            note: str = "",
            **extra: object,
        ) -> CodeResult:
            enforced = ""
            with contextlib.suppress(OSError):
                enforced = report.read_text(encoding="utf-8").strip()
            return CodeResult(
                ok=ok,
                returncode=returncode,
                stdout=_truncate(stdout),
                stderr=_truncate(stderr),
                artifacts=workspace.artifacts(),
                duration=time.monotonic() - started,
                confinement="; ".join(p for p in (confinement.describe(), note, enforced) if p),
                **extra,  # type: ignore[arg-type]
            )

        try:
            outcome = confinement.spawn(
                argv,
                cwd=workspace.root,
                env=env,
                timeout=limit,
                max_memory_bytes=self.max_memory_bytes,
                output_dir=workspace.run,
                # The platform advertising a kernel boundary is not the same as
                # this run getting one: a token can fail to lower. Checked in
                # the parent while the child is still suspended.
                require=self.require_confinement,
            )
            if outcome is not None:
                return finish(
                    outcome.returncode == 0 and not outcome.timed_out,
                    outcome.returncode,
                    outcome.stdout,
                    outcome.stderr,
                    # What the parent's confinement *actually* achieved for this
                    # run, not what the platform advertises. A token that could
                    # not be lowered is the difference between a kernel boundary
                    # and an in-process one, and it has to reach the report.
                    note=_spawn_note(outcome),
                    timed_out=outcome.timed_out,
                    detail=(f"killed after {limit}s" if outcome.timed_out else outcome.detail),
                )

            completed = subprocess.run(
                argv,
                cwd=workspace.root,
                env=env,
                capture_output=True,
                text=True,
                timeout=limit,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return finish(
                False,
                -1,
                exc.stdout or "",
                exc.stderr or "",
                timed_out=True,
                detail=f"killed after {limit}s",
            )
        except ConfinementNotApplied as exc:
            # The same answer as the static check above, reached later: the
            # platform has the boundary, this run could not get it.
            raise Unconfined(str(exc)) from exc
        except OSError as exc:
            return finish(
                False, -1, "", str(exc), detail="the sandbox interpreter could not be started"
            )

        return finish(
            completed.returncode == 0, completed.returncode, completed.stdout, completed.stderr
        )


_NOTHING = Confinement(name="disabled", detail="kernel confinement switched off by the caller")


def _spawn_note(outcome: object) -> str:
    """Turn a parent-side spawn outcome into a line for the confinement report."""
    parts = []
    for attribute, applied, missing in (
        ("integrity_lowered", "integrity lowered", "INTEGRITY NOT LOWERED"),
        ("job_object", "job object applied", "NO JOB OBJECT"),
    ):
        value = getattr(outcome, attribute, None)
        if value is None:
            continue
        parts.append(applied if value else missing)
    detail = getattr(outcome, "detail", "")
    if detail:
        parts.append(detail)
    return ", ".join(parts)


def _truncate(text: str | bytes | None) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + f"\n... [truncated, {len(text) - _MAX_OUTPUT} more characters]"
