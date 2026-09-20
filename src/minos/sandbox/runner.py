"""Running code in the workspace.

⚠️ **This is a jail, not a security boundary.** Read `SECURITY.md`. A subprocess
with a scrubbed environment, a working-directory convention, resource limits and
a patched ``socket`` module stops *badly written* code from wandering off. It
does not stop code that is trying to escape, because nothing here is enforced by
the kernel: the script runs as the same user with the same filesystem
permissions as the runtime itself.

That is an acceptable trade for the threat that actually exists — code written
by a local model against the user's own task, which fails by being wrong rather
than by being hostile — and an unacceptable one for code from anywhere else. The
backend is pluggable (:class:`SandboxBackend`) precisely so a kernel-enforced
implementation can replace this when the threat changes.

What *is* enforced here:

- **cwd and materials.** The script starts in the workspace, and its inputs are
  copies. Corrupting an input corrupts a copy.
- **Environment.** Anything that looks like a credential is removed before the
  process starts, so a script cannot read a key it was never given.
- **Wall clock.** A run that does not finish is killed.
- **Output size.** Captured output is truncated, so a runaway print loop cannot
  exhaust memory in the *parent*.
- **Network.** ``socket.socket`` raises before the script's first line runs.
  This defeats every ordinary library (urllib, requests, httpx); it does not
  defeat ``ctypes``.
- **Memory and process count**, on POSIX only, via ``RLIMIT_AS`` and
  ``RLIMIT_NPROC``. Windows has no equivalent without Job Objects; see
  `SECURITY.md`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from typing import Protocol

from .workspace import Artifact, Workspace

__all__ = ["CodeResult", "SandboxBackend", "SubprocessSandbox", "scrubbed_environment"]

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
"""Written by minos. Installs the sandbox's in-process restrictions, then runs
the script. Not part of the user's code and never promoted."""

import runpy
import sys


def _block_network() -> None:
    """Make network access fail loudly, before the script's first line.

    Defeats every ordinary client library. Does not defeat ctypes, and is not
    claimed to -- see SECURITY.md.
    """
    import socket

    message = (
        "network access is disabled in the minos sandbox. "
        "Declare the data you need as a material instead."
    )

    class _Blocked(OSError):
        pass

    def _refuse(*args, **kwargs):
        raise _Blocked(message)

    # A subclass, not a function: ssl does `class SSLSocket(socket.socket)` at
    # import time, and replacing the name with a function turns a clear refusal
    # into a baffling TypeError from deep inside the stdlib.
    class _BlockedSocket(socket.socket):
        def __init__(self, *args, **kwargs):
            raise _Blocked(message)

    socket.socket = _BlockedSocket
    socket.create_connection = _refuse
    socket.create_server = _refuse
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        if hasattr(socket, name):
            setattr(socket, name, _refuse)


def _limit_resources(max_memory_bytes: int) -> None:
    """POSIX only. Windows needs Job Objects; see SECURITY.md."""
    try:
        import resource
    except ImportError:
        return
    if max_memory_bytes > 0:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (max_memory_bytes, max_memory_bytes))
        except (ValueError, OSError):
            pass
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    except (ValueError, OSError, AttributeError):
        pass


if __name__ == "__main__":
    _block_network()
    _limit_resources(int(sys.argv[2]))
    target = sys.argv[1]
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

    def summary(self) -> str:
        if self.timed_out:
            return f"timed out after {self.duration:.1f}s"
        if self.ok:
            return f"produced {len(self.artifacts)} artifact(s) in {self.duration:.1f}s"
        return f"exited {self.returncode}: {self.stderr.strip().splitlines()[-1:] or ['no output']}"


class SandboxBackend(Protocol):
    """Swap-in point for a stronger jail.

    The subprocess backend is a deliberate v1 choice, not a permanent one. A
    container or microVM backend implements this and nothing above it changes.
    """

    name: str

    def run(self, workspace: Workspace, code: str, *, timeout: float) -> CodeResult: ...


@dataclass
class SubprocessSandbox:
    """The default backend: a jailed subprocess. See the module docstring."""

    name: str = "subprocess"
    max_memory_bytes: int = 2 << 30
    default_timeout: float = 60.0
    python: str = field(default_factory=lambda: sys.executable)

    def run(self, workspace: Workspace, code: str, *, timeout: float | None = None) -> CodeResult:
        import time

        script = workspace.run / "script.py"
        script.write_text(textwrap.dedent(code), encoding="utf-8")
        runner = workspace.run / "_minos_runner.py"
        runner.write_text(_RUNNER, encoding="utf-8")

        limit = float(timeout if timeout is not None else self.default_timeout)
        started = time.monotonic()

        try:
            completed = subprocess.run(
                [
                    self.python,
                    "-E",  # ignore PYTHON* environment variables
                    "-s",  # no user site-packages
                    "-B",  # no bytecode, so the workspace stays clean
                    str(runner),
                    str(script),
                    str(self.max_memory_bytes),
                ],
                cwd=workspace.root,
                env=scrubbed_environment(),
                capture_output=True,
                text=True,
                timeout=limit,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CodeResult(
                ok=False,
                returncode=-1,
                stdout=_truncate(exc.stdout),
                stderr=_truncate(exc.stderr),
                artifacts=workspace.artifacts(),
                timed_out=True,
                duration=time.monotonic() - started,
                detail=f"killed after {limit}s",
            )
        except OSError as exc:
            return CodeResult(
                ok=False,
                returncode=-1,
                stdout="",
                stderr=str(exc),
                duration=time.monotonic() - started,
                detail="the sandbox interpreter could not be started",
            )

        duration = time.monotonic() - started
        return CodeResult(
            ok=completed.returncode == 0,
            returncode=completed.returncode,
            stdout=_truncate(completed.stdout),
            stderr=_truncate(completed.stderr),
            artifacts=workspace.artifacts(),
            duration=duration,
        )


def _truncate(text: str | bytes | None) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + f"\n... [truncated, {len(text) - _MAX_OUTPUT} more characters]"
