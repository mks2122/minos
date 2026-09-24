"""A container backend: the one that lets the jail warning come down.

Everything the subprocess backend does is a restriction a process places on
itself in a namespace it shares with the user. This backend does not share the
namespace. The script gets its own filesystem (the workspace, bind-mounted, and
a read-only root), its own network (none), its own PID space, no capabilities,
a non-root UID and a memory cap the kernel enforces. A script that escapes every
in-process layer still finds nothing of the user's on the other side.

**That is the difference that matters, and it is about where the code came
from.** A script a local model wrote against the user's own task fails by being
wrong. A skill downloaded from a stranger fails by being hostile if it wants to,
and no amount of monkey-patching ``socket`` addresses that. See
:mod:`minos.sandbox.origin` for the policy that picks between them.

The cost is honest: a container engine has to be installed, startup is roughly a
second rather than thirty milliseconds, and the image has to carry whatever the
script imports. So this is not the default -- it is what the default escalates
to when the code's origin is not trusted, and what a user can select outright.

gVisor or a microVM would go further still (the syscall surface, not just the
namespace), and both implement the same ``SandboxBackend`` protocol. The point
of the protocol is that this file is replaceable without anything above it
knowing.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field

from .confine import container_engine
from .runner import CodeResult, _truncate, scrubbed_environment
from .workspace import Workspace

__all__ = ["ContainerSandbox", "ContainerUnavailable"]

DEFAULT_IMAGE = "python:3.12-slim"


class ContainerUnavailable(RuntimeError):
    """No usable engine. Raised at construction, never mid-task."""


@dataclass
class ContainerSandbox:
    """Runs the script inside a container with no network and no capabilities.

    The workspace is the only thing mounted, and it is the only writable path.
    Everything else the script can see belongs to the image, is read-only, and
    is discarded when the run ends.
    """

    name: str = "container"
    image: str = DEFAULT_IMAGE
    engine: str = field(default_factory=container_engine)
    max_memory_bytes: int = 2 << 30
    max_processes: int = 128
    default_timeout: float = 60.0
    user: str = "65534:65534"
    """nobody. The workspace is bind-mounted, so a root-owned file left behind
    by the container would be one the user cannot delete without sudo."""

    def __post_init__(self) -> None:
        if not self.engine:
            raise ContainerUnavailable(
                "no container engine found. Install Docker or Podman, or run with "
                "the subprocess backend and read SECURITY.md for what it does not stop."
            )

    def available(self) -> bool:
        """Whether the engine answers. An installed binary is not a running daemon."""
        try:
            completed = subprocess.run(
                [self.engine, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    def describe(self) -> str:
        return (
            f"{self.engine} running {self.image}: no network, read-only root, "
            f"all capabilities dropped, {self.max_memory_bytes >> 20} MB, "
            f"uid {self.user}, workspace bind-mounted as the only writable path"
        )

    def _argv(self, workspace: Workspace, timeout: float) -> list[str]:
        mount = f"{workspace.root}:/workspace"
        return [
            self.engine,
            "run",
            "--rm",
            # The whole point. Not a patched socket module -- no interface.
            "--network",
            "none",
            # A script cannot gain privileges it was not started with, even via
            # a setuid binary that happens to be in the image.
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--read-only",
            f"--memory={self.max_memory_bytes}",
            f"--pids-limit={self.max_processes}",
            "--user",
            self.user,
            "--workdir",
            "/workspace",
            # tmpfs so the image's own /tmp is writable without making the root
            # filesystem writable.
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--volume",
            mount,
            "--env",
            "MINOS_SANDBOX=1",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONUNBUFFERED=1",
            self.image,
            "python",
            "-I",
            "-B",
            "/workspace/run/script.py",
        ]

    def run(self, workspace: Workspace, code: str, *, timeout: float | None = None) -> CodeResult:
        import textwrap

        script = workspace.run / "script.py"
        script.write_text(textwrap.dedent(code), encoding="utf-8")

        limit = float(timeout if timeout is not None else self.default_timeout)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                self._argv(workspace, limit),
                capture_output=True,
                text=True,
                # The engine gets a little longer than the script, so that a
                # container which ignores its own timeout is still bounded.
                timeout=limit + 15,
                check=False,
                env=scrubbed_environment(),
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
                detail=f"the {self.engine} engine could not be started",
            )

        detail = ""
        if completed.returncode != 0 and _engine_unreachable(completed.stderr):
            # An installed binary is not a running daemon, and "cannot connect to
            # the Docker daemon" reaching a planner as a script error is how a
            # model ends up rewriting a script that was never the problem.
            detail = (
                f"the {self.engine} engine is installed but not running. "
                "Start it, or choose a different backend and read SECURITY.md "
                "for what that costs."
            )
        elif completed.returncode == 137:
            # 128 + SIGKILL. With --memory set, this is nearly always the OOM
            # killer, and "exited 137" tells a model nothing it can act on.
            detail = (
                "killed by the container runtime, most likely out of memory "
                f"({self.max_memory_bytes >> 20} MB)"
            )
        return CodeResult(
            ok=completed.returncode == 0,
            returncode=completed.returncode,
            stdout=_truncate(completed.stdout),
            stderr=_truncate(completed.stderr),
            artifacts=workspace.artifacts(),
            duration=time.monotonic() - started,
            detail=detail,
        )


def _engine_unreachable(stderr: str) -> bool:
    """Whether the engine itself failed rather than the script inside it."""
    lowered = (stderr or "").lower()
    return any(
        phrase in lowered
        for phrase in (
            "cannot connect to the docker daemon",
            "failed to connect to the docker api",
            "is the docker daemon running",
            "error during connect",
            "cannot connect to podman",
        )
    )
