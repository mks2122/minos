"""Per-platform confinement: what the kernel will enforce, and what it will not.

One object per platform, chosen by :func:`detect`, each answering the same three
questions: how is the child started, what is genuinely enforced, and what is
merely patched. The answer differs per platform and this module's job is to say
so out loud -- ``describe()`` is printed by ``minos doctor`` and recorded in the
audit log, because "confined" with no detail is the kind of claim this project
does not make.

============  ==========================================  ================
Platform      Mechanism                                   Kernel-enforced
============  ==========================================  ================
Linux         Landlock (paths) + seccomp-bpf (syscalls)   yes, both
macOS         ``sandbox-exec`` seatbelt profile           yes
Windows       Low-integrity token + Job Object            writes and limits
other         nothing                                     no
============  ==========================================  ================

Linux applies its own confinement from inside the child (see
:mod:`minos.sandbox._child_confine`), because Landlock and seccomp survive
``exec`` and are irrevocable; this module only detects and reports it. macOS and
Windows need the parent to act -- a wrapper command and a token respectively --
so those two carry real work here.

**Detection is a probe, not a version check.** ``sandbox-exec`` is deprecated
and may be removed; a kernel may have Landlock compiled out. Both are tested by
trying them once and caching the answer, because a capability table that is
right in theory is how a security control ends up absent in practice.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .winspawn import SpawnOutcome

__all__ = [
    "Confinement",
    "ConfinementNotApplied",
    "LinuxConfinement",
    "NoConfinement",
    "SeatbeltConfinement",
    "WindowsConfinement",
    "detect",
]


class ConfinementNotApplied(OSError):
    """A run that required the kernel boundary could not get it, so it did not run."""


@dataclass(frozen=True)
class Confinement:
    """What the platform can enforce. The base case enforces nothing.

    Subclasses override the two hooks they need. ``wrap`` changes the command
    line; ``spawn`` takes the run over entirely and returns ``None`` when the
    ordinary :mod:`subprocess` path should be used instead. Everything else --
    the in-child layers -- is the same everywhere.
    """

    name: str = "none"
    kernel_enforced: bool = False
    detail: str = "no kernel confinement on this platform"
    notes: tuple[str, ...] = field(default_factory=tuple)

    def prepare(self, workspace_root: Path) -> None:
        """Anything the workspace needs before a confined child can use it."""

    def wrap(self, argv: list[str], workspace_root: Path) -> list[str]:
        return argv

    def spawn(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        max_memory_bytes: int,
        output_dir: Path | None = None,
        require: bool = False,
    ) -> SpawnOutcome | None:
        """Take the run over, or return ``None`` to use plain ``subprocess``."""
        return None

    def describe(self) -> str:
        suffix = f" -- {self.detail}" if self.detail else ""
        return f"{self.name}{suffix}"


# -- Linux -------------------------------------------------------------------


@dataclass(frozen=True)
class LinuxConfinement(Confinement):
    """Landlock and seccomp, applied by the child to itself.

    Nothing to do here: the mechanisms are inherited across ``exec`` and
    unprivileged processes can apply them to themselves, so the child is the
    right place. This object exists so the parent can *report* what the child
    will manage, which it knows because both read the same probes.
    """

    name: str = "landlock+seccomp"


def _linux() -> Confinement:
    from ._child_confine import landlock_abi, seccomp_arch

    abi = landlock_abi()
    arch = seccomp_arch()
    if not abi and not arch:
        return NoConfinement(
            name="none",
            detail="this kernel has neither Landlock nor a seccomp table we know",
        )
    parts = []
    if abi:
        parts.append(f"Landlock ABI {abi} confines the filesystem to the workspace")
    else:
        parts.append("Landlock unavailable (kernel older than 5.13, or disabled)")
    if arch:
        parts.append(f"seccomp-bpf refuses the socket syscalls on {arch}")
    else:
        parts.append(f"no seccomp syscall table for {os.uname().machine}")  # type: ignore[attr-defined,unused-ignore]
    return LinuxConfinement(
        name="landlock+seccomp" if (abi and arch) else ("landlock" if abi else "seccomp"),
        kernel_enforced=bool(abi or arch),
        detail="; ".join(parts),
    )


# -- macOS -------------------------------------------------------------------

_SEATBELT_PROFILE = """(version 1)
(deny default)
(allow process-fork)
(allow process-exec)
(allow signal (target self))
(allow sysctl-read)
(allow mach-lookup)
(allow file-read-metadata)
(deny network*)
{reads}
{writes}
"""


@dataclass(frozen=True)
class SeatbeltConfinement(Confinement):
    """``sandbox-exec``: deprecated by Apple, still the only portable option.

    Endpoint Security is the supported replacement and needs an entitlement
    Apple grants to registered organisations, which is not a dependency an
    open-source runtime can take. So: seatbelt, probed at startup, and named in
    SECURITY.md as the weakest of the three kernel stories.
    """

    name: str = "sandbox-exec"
    kernel_enforced: bool = True
    read_roots: tuple[str, ...] = field(default_factory=tuple)

    def profile(self, workspace_root: Path) -> str:
        reads = "\n".join(f'(allow file-read* (subpath "{path}"))' for path in self.read_roots)
        writable = [str(workspace_root)] + [
            os.environ[name] for name in ("TMPDIR",) if os.environ.get(name)
        ]
        writes = "\n".join(f'(allow file-write* (subpath "{path}"))' for path in writable)
        # Devices a normal interpreter cannot start without, and which carry no
        # information: null, zero, random, and the tty for stderr.
        writes += '\n(allow file-write-data (literal "/dev/null") (literal "/dev/stdout"))'
        return _SEATBELT_PROFILE.format(reads=reads, writes=writes)

    def wrap(self, argv: list[str], workspace_root: Path) -> list[str]:
        return ["/usr/bin/sandbox-exec", "-p", self.profile(workspace_root), *argv]


def _macos() -> Confinement:
    if not Path("/usr/bin/sandbox-exec").exists():
        return NoConfinement(detail="sandbox-exec is not present on this system")

    roots = tuple(
        sorted(
            {
                "/usr/lib",
                "/usr/bin",
                "/System",
                "/Library",
                "/private/var/db/dyld",
                os.path.realpath(sys.prefix),
                os.path.realpath(sys.base_prefix),
                *(os.path.realpath(p) for p in sys.path if p),
            }
        )
    )
    candidate = SeatbeltConfinement(read_roots=roots)
    if not _seatbelt_works(candidate):
        return NoConfinement(detail="sandbox-exec is present but refused a trial run")
    return candidate


@functools.lru_cache(maxsize=1)
def _seatbelt_works(candidate: SeatbeltConfinement) -> bool:
    """Run the real profile against the real interpreter, once.

    A trial run costs about a tenth of a second at startup and is the difference
    between knowing and assuming. Cached, so it is paid once per process.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        argv = candidate.wrap([sys.executable, "-I", "-c", "print('ok')"], Path(scratch))
        try:
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return False
    return completed.returncode == 0 and "ok" in completed.stdout


# -- Windows -----------------------------------------------------------------


@dataclass(frozen=True)
class WindowsConfinement(Confinement):
    """A Low-integrity token and a Job Object, both applied by the parent.

    ``kernel_enforced`` is True but deliberately narrow: what the kernel
    enforces here is *writes* and *resource limits*, not reads. Windows'
    mandatory integrity policy is no-write-up by default, so a Low process still
    reads what the user can read. Closing that needs an AppContainer with an
    explicit capability set, which is the next step and is not this one.
    """

    name: str = "low-integrity+job"
    kernel_enforced: bool = True
    detail: str = (
        "writes confined to the workspace by mandatory integrity; "
        "memory and process count bounded by a Job Object; reads are not confined"
    )
    low_integrity: bool = True

    def prepare(self, workspace_root: Path) -> None:
        from . import winspawn

        # Without the label the confined child cannot write its own out/
        # directory, so this is load-bearing rather than hardening.
        winspawn.label_low_integrity(workspace_root)

    def spawn(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        max_memory_bytes: int,
        output_dir: Path | None = None,
        require: bool = False,
    ) -> SpawnOutcome | None:
        from . import winspawn

        return winspawn.spawn_confined(
            list(argv),
            cwd=Path(cwd),
            env=dict(env),
            timeout=timeout,
            max_memory_bytes=max_memory_bytes,
            low_integrity=self.low_integrity,
            output_dir=Path(output_dir) if output_dir else None,
            require=require,
        )


def _windows() -> Confinement:
    from . import winspawn

    if not winspawn.supported():
        return NoConfinement()
    return WindowsConfinement()


# -- the null case -----------------------------------------------------------


@dataclass(frozen=True)
class NoConfinement(Confinement):
    """Nothing the kernel will enforce. Named, so it cannot pass for the others."""


@functools.lru_cache(maxsize=1)
def detect() -> Confinement:
    """The best confinement this machine actually offers, probed once.

    Cached for the process: the answer cannot change while it runs, and the
    macOS probe starts an interpreter.
    """
    try:
        if sys.platform.startswith("linux"):
            return _linux()
        if sys.platform == "darwin":
            return _macos()
        if os.name == "nt":
            return _windows()
    except Exception as exc:  # pragma: no cover - a probe must never be fatal
        return NoConfinement(detail=f"confinement probe failed: {exc}")
    return NoConfinement(detail=f"no confinement implemented for {sys.platform}")


def container_engine() -> str:
    """The container engine on PATH, or ``""``. Used by the container backend."""
    for engine in ("docker", "podman"):
        if shutil.which(engine):
            return engine
    return ""
