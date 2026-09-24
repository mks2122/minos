"""A single-writer lock over a state directory.

The audit log is a hash chain: each record carries the hash of the one before
it. Two processes appending concurrently interleave their writes and both
compute ``prev_hash`` from a head that the other has already moved, so the chain
breaks and `AuditLog.verify` reports a break that no attacker caused. The
checkpoint store has the same shape of problem from the other direction — one
process's `prune` can delete objects another process is about to restore from.

Neither is theoretical once this is something other people run. The fix is
boring and belongs at the boundary: one writer per state directory, enforced by
an atomically-created lock file.

**Why not `fcntl` / `msvcrt` locking.** Advisory byte-range locks differ
meaningfully across the three platforms, release inconsistently when a process
dies, and behave badly on network filesystems. ``O_EXCL`` file creation is
atomic everywhere, including on SMB, and the failure mode we actually have to
survive — a process that was killed and left its lock behind — is handled by
recording the pid and checking whether it still exists.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

__all__ = ["LockBusy", "StateLock", "lock_state"]

_STALE_AFTER = 4 * 3600
"""Seconds. A lock older than this whose owner is gone is reclaimed."""

_UNREADABLE_GRACE = 30.0
"""Seconds an empty or unreadable lock file is left alone. Writing the pid takes
microseconds, so a file still empty after this was left by a process that died
between the two steps."""


class LockBusy(Exception):
    """Another process holds the lock.

    Carries the holder's details so the message can say *who*, which is the
    difference between a usable error and a puzzling one.
    """

    def __init__(self, path: Path, holder: dict[str, object]) -> None:
        pid = holder.get("pid", "?")
        since = holder.get("acquired_at", "?")
        command = holder.get("command", "?")
        super().__init__(
            f"another minos process holds {path.parent} "
            f"(pid {pid}, since {since}, {command}). "
            "Wait for it to finish, or remove the lock if that process is gone."
        )
        self.holder = holder


def _process_alive(pid: int) -> bool:
    """Is this pid still running? Conservative: unknown means assume alive."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        # PROCESS_QUERY_LIMITED_INFORMATION. Opening succeeds for a live
        # process even when we may not read anything else about it.
        # getattr keeps this typecheckable on the platforms where windll does
        # not exist; CI runs mypy on Linux and macOS too.
        kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


@dataclass
class StateLock:
    """Exclusive access to one state directory, as a context manager.

    ::

        with lock_state(Path(".minos")):
            ...            # audit and checkpoints are ours alone

    Re-entrant within a process is *not* supported on purpose: nesting would
    mean two components each believing they hold it, which is the bug this
    exists to prevent.
    """

    path: Path
    timeout: float = 0.0
    poll_interval: float = 0.25
    _held: bool = False

    def acquire(self) -> StateLock:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._create()
                self._held = True
                return self
            except FileExistsError:
                holder = self._read_holder()
                if self._reclaim_if_dead(holder):
                    continue
                if time.monotonic() >= deadline:
                    raise LockBusy(self.path, holder) from None
                time.sleep(self.poll_interval)

    def release(self) -> None:
        if not self._held:
            return
        # Only remove a lock we still own. If ours was reclaimed as stale and
        # another process took it, deleting it would hand the directory to a
        # third one while the second is mid-write.
        #
        # Retried, because on Windows a file cannot be deleted while anyone has
        # it open, and a waiter reading the holder at that instant is routine.
        # One failed unlink used to leave the lock behind, owned by a live pid,
        # so every other run waited out its whole timeout.
        for _ in range(100):
            try:
                if self._read_holder(strict=True).get("pid") == os.getpid():
                    self.path.unlink()
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.01)
        self._held = False

    def __enter__(self) -> StateLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    # -- internals ---------------------------------------------------------

    def _create(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "acquired_monotonic": time.time(),
                "command": " ".join(sys.argv[:3]),
            }
        )
        # O_EXCL is the whole mechanism: atomic on every platform we target.
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)

    def _read_holder(self, *, strict: bool = False) -> dict[str, object]:
        """Who holds the lock. ``strict`` lets a transient read error through,
        so the caller can retry rather than mistake it for "nobody"."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError:
            if strict:
                raise
            return {}
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _reclaim_if_dead(self, holder: dict[str, object]) -> bool:
        """Take over a lock whose owner died. Returns True if we removed it.

        A crashed run must not lock the directory forever, but reclaiming too
        eagerly is worse than waiting: it would let two live processes write at
        once, which is the exact failure the lock exists to prevent. So both
        conditions must hold — the pid is gone *and* it is not fresh.
        """
        raw_pid = holder.get("pid")
        if not isinstance(raw_pid, int):
            # Empty or unreadable. Usually that is not a dead process at all: it
            # is a live one between creating the file and writing its pid into
            # it. Treating that as infinitely old handed the lock to a second
            # writer and broke the audit chain, so the file's own age decides.
            try:
                written = self.path.stat().st_mtime
            except OSError:
                return False  # gone already; the next create attempt will tell
            if time.time() - written < _UNREADABLE_GRACE:
                return False
            with contextlib.suppress(OSError):
                self.path.unlink()
            return True

        if _process_alive(raw_pid):
            return False

        acquired = holder.get("acquired_monotonic")
        age = time.time() - acquired if isinstance(acquired, int | float) else _STALE_AFTER + 1
        if age < 5:
            # Freshly created by a pid we cannot see. Give it a moment rather
            # than racing a process that is still starting up.
            return False

        with contextlib.suppress(OSError):
            self.path.unlink()
        return True


def lock_state(state: Path | str, *, timeout: float = 0.0) -> StateLock:
    """Lock a state directory (``.minos/`` by convention).

    ``timeout`` of 0 fails immediately, which is right for a CLI: telling
    someone another run is in progress beats hanging with no output.
    """
    return StateLock(Path(state).expanduser() / ".lock", timeout=timeout)
