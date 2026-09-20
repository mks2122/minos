"""Filesystem watching.

Without this, memory only learns about the world at the moment a run starts.
Edit a spreadsheet in Excel while `minos` is not running and it has no idea until
the next scan -- and by then it sees a changed mtime with no notion that
anything happened in between.

A **polling** watcher, deliberately
-----------------------------------

`inotify` / `FSEvents` / `ReadDirectoryChangesW` are faster and cheaper, and all
three are different APIs with different semantics and a dependency each. This
runtime is Tier-1 on Windows and Linux and Tier-2 on macOS, and a watcher that
behaves differently per platform is a watcher whose bugs only appear on the
machine you do not own.

So: one implementation, standard library only, identical everywhere. It rescans
on an interval and diffs. Slower and completely predictable, which is the right
trade for something whose job is to be trusted about what changed.

Scanning is bounded by ``max_files`` and skips hidden paths, so pointing it at a
home directory degrades rather than hangs.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .store import MemoryStore

__all__ = ["Change", "FileWatcher", "scan"]


@dataclass(frozen=True, slots=True)
class Change:
    path: str
    kind: str
    """``created``, ``modified`` or ``deleted``."""

    mtime: float = 0.0
    size: int = 0


def scan(
    roots: Iterable[Path | str], *, skip_hidden: bool = True, max_files: int = 100_000
) -> dict[str, tuple[float, int]]:
    """A snapshot of ``{path: (mtime, size)}``."""
    snapshot: dict[str, tuple[float, int]] = {}
    count = 0
    for raw_root in roots:
        root = Path(raw_root).expanduser().resolve()
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if count >= max_files:
                return snapshot
            if skip_hidden and any(part.startswith(".") for part in path.parts):
                continue
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            snapshot[str(path)] = (stat.st_mtime, stat.st_size)
            count += 1
    return snapshot


def diff(before: dict[str, tuple[float, int]], after: dict[str, tuple[float, int]]) -> list[Change]:
    changes: list[Change] = []
    for path, (mtime, size) in after.items():
        previous = before.get(path)
        if previous is None:
            changes.append(Change(path, "created", mtime, size))
        elif previous != (mtime, size):
            changes.append(Change(path, "modified", mtime, size))
    for path in before.keys() - after.keys():
        changes.append(Change(path, "deleted"))
    return sorted(changes, key=lambda c: c.path)


@dataclass
class FileWatcher:
    """Rescans roots on an interval and records what moved.

    Runs on a daemon thread so it cannot keep the process alive, and swallows
    per-file errors: a watcher that dies because one file was locked is worse
    than one that misses it.
    """

    store: MemoryStore
    roots: tuple[Path, ...]
    interval: float = 2.0
    on_change: Callable[[list[Change]], None] | None = None
    skip_hidden: bool = True
    max_files: int = 100_000

    _thread: threading.Thread | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _snapshot: dict[str, tuple[float, int]] = field(default_factory=dict, init=False)
    _changes: list[Change] = field(default_factory=list, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _errors: list[str] = field(default_factory=list, init=False)

    # -- lifecycle ---------------------------------------------------------

    def prime(self) -> None:
        """Take the baseline. Everything already present is not a change."""
        self._snapshot = scan(self.roots, skip_hidden=self.skip_hidden, max_files=self.max_files)

    def poll_once(self) -> list[Change]:
        """One scan-and-diff. The whole watcher, minus the thread."""
        current = scan(self.roots, skip_hidden=self.skip_hidden, max_files=self.max_files)
        changes = diff(self._snapshot, current)
        self._snapshot = current

        for change in changes:
            self.store.record_file(change.path)

        if changes:
            with self._lock:
                self._changes.extend(changes)
            if self.on_change:
                self.on_change(changes)
        return changes

    def start(self) -> None:
        """Begin watching. Never raises: a watcher is an accessory to a run,
        and one that takes the run down with it is worse than one that misses
        an edit. Failures land in :attr:`errors` rather than being swallowed.
        """
        if self._thread is not None:
            return
        try:
            self.prime()
        except OSError as exc:
            self._note(f"could not take a baseline scan: {exc}")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="minos-file-watcher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def __enter__(self) -> FileWatcher:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- observation -------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def drain(self) -> list[Change]:
        """Everything seen since the last drain."""
        with self._lock:
            changes, self._changes = self._changes, []
        return changes

    @property
    def errors(self) -> list[str]:
        """Scan failures, so a broken watcher is visible rather than quiet.

        Found the hard way: sqlite3 connections are thread-bound by default, so
        recording from the watcher thread raised every cycle. A bare
        ``except Exception`` made that look exactly like a watcher that worked
        and found nothing.
        """
        with self._lock:
            return list(self._errors)

    def _note(self, message: str) -> None:
        with self._lock:
            if message not in self._errors:
                self._errors.append(message)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                self._note(f"{type(exc).__name__}: {exc}")
                time.sleep(self.interval)
                continue
            self._stop.wait(self.interval)
