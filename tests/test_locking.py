"""Single-writer locking over a state directory.

The audit log is a hash chain. Two processes appending at once each compute
`prev_hash` from a head the other has already moved, so the chain breaks with no
attacker involved. These tests cover the lock that prevents that, and the
reclaim path that stops a crashed run from locking the directory forever.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from minos.locking import LockBusy, StateLock, lock_state


def test_a_second_acquire_fails_immediately(tmp_path):
    with lock_state(tmp_path), pytest.raises(LockBusy):
        lock_state(tmp_path).acquire()


def test_the_error_says_who_holds_it(tmp_path):
    """A lock error that does not name the holder is a puzzle, not a message."""
    with lock_state(tmp_path), pytest.raises(LockBusy) as excinfo:
        lock_state(tmp_path).acquire()

    assert str(os.getpid()) in str(excinfo.value)


def test_releasing_allows_the_next_acquire(tmp_path):
    lock = lock_state(tmp_path).acquire()
    lock.release()

    with lock_state(tmp_path):
        pass  # no raise


def test_release_is_idempotent(tmp_path):
    lock = lock_state(tmp_path).acquire()
    lock.release()
    lock.release()  # must not raise or delete someone else's lock


def test_the_lock_is_released_when_the_body_raises(tmp_path):
    with pytest.raises(ValueError), lock_state(tmp_path):
        raise ValueError("boom")

    with lock_state(tmp_path):
        pass


def test_a_dead_holder_is_reclaimed(tmp_path):
    """A crashed run must not lock the directory forever."""
    lock_path = tmp_path / ".lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 999_999_999,  # not a live pid
                "acquired_at": "2020-01-01T00:00:00",
                "acquired_monotonic": 0,  # long ago
                "command": "minos run",
            }
        )
    )

    with lock_state(tmp_path):
        pass  # reclaimed


def test_a_fresh_lock_from_an_unknown_pid_is_not_stolen(tmp_path):
    """Do not race a process that is still starting up."""
    import time

    lock_path = tmp_path / ".lock"
    lock_path.write_text(
        json.dumps({"pid": 999_999_999, "acquired_monotonic": time.time(), "command": "minos run"})
    )

    with pytest.raises(LockBusy):
        lock_state(tmp_path).acquire()


def test_a_live_holder_is_never_reclaimed(tmp_path):
    """The whole point: two live processes must not both write."""
    lock_path = tmp_path / ".lock"
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "acquired_monotonic": 0, "command": "minos run"})
    )

    with pytest.raises(LockBusy):
        lock_state(tmp_path).acquire()


def test_a_corrupt_lock_file_is_aged_out_not_trusted(tmp_path):
    lock = tmp_path / ".lock"
    lock.write_text("{ this is not json")
    # Aged: a *fresh* unreadable file is usually a live process mid-write, and
    # must not be reclaimed (see the race test below).
    long_ago = lock.stat().st_mtime - 120
    os.utime(lock, (long_ago, long_ago))

    with lock_state(tmp_path):
        pass


def test_timeout_waits_then_succeeds(tmp_path):
    """--wait should wait, not fail fast."""
    lock = lock_state(tmp_path).acquire()
    lock.release()

    waiting = StateLock(tmp_path / ".lock", timeout=1.0, poll_interval=0.01)
    with waiting:
        pass


def test_release_does_not_delete_a_lock_we_no_longer_own(tmp_path):
    """If ours was reclaimed and retaken, deleting it frees a live holder."""
    lock = lock_state(tmp_path).acquire()

    # Someone else reclaimed and took it.
    (tmp_path / ".lock").write_text(
        json.dumps({"pid": os.getpid() + 1, "acquired_monotonic": 0, "command": "other"})
    )

    lock.release()
    assert (tmp_path / ".lock").exists()


# -- the failure this exists to prevent ------------------------------------


def test_two_real_processes_cannot_both_hold_it(tmp_path):
    """Cross-process, not just cross-object: O_EXCL is the whole mechanism."""
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(Path("src").resolve())!r})
        from minos.locking import LockBusy, lock_state
        try:
            lock_state({str(tmp_path)!r}).acquire()
            print("ACQUIRED")
        except LockBusy:
            print("BUSY")
    """)

    with lock_state(tmp_path):
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
        )

    assert "BUSY" in result.stdout, result.stderr


def test_concurrent_runs_do_not_break_the_audit_chain(tmp_path):
    """The end-to-end property. Without the lock this chain breaks."""
    from conftest import make_invocation
    from minos.audit import AuditLog
    from minos.broker import Broker
    from minos.checkpoint import FileCheckpointStore
    from minos.scopes import ScopeSet

    state = tmp_path / ".minos"
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def run_one(n: int) -> None:
        with lock_state(state, timeout=60.0):
            broker = Broker(
                scopes=ScopeSet.parse([f"fs.write:{workspace}/**"]),
                audit=AuditLog(state / "audit.jsonl"),
                store=FileCheckpointStore(state / "checkpoints"),
            )
            target = workspace / f"f{n}.txt"
            broker.submit(
                make_invocation(targets=(target,)),
                lambda i, t=target: t.write_text("written", encoding="utf-8"),
            )

    import threading

    errors: list[BaseException] = []

    def guarded(n: int) -> None:
        # A thread that raises does not fail a test on its own; it only warns.
        # A run that timed out waiting for the lock used to pass unnoticed.
        try:
            run_one(n)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=guarded, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert AuditLog(state / "audit.jsonl").verify() == []
    assert len(AuditLog(state / "audit.jsonl").entries()) == 6


def test_a_lock_file_still_being_written_is_not_reclaimed(tmp_path):
    """Found by CI: between O_EXCL creating the file and the pid landing in it,
    a second process read an empty file, called its owner dead and infinitely
    old, deleted it, and took the lock too. Two writers broke the audit chain."""
    lock = tmp_path / ".minos" / ".lock"
    lock.parent.mkdir()
    lock.write_text("")  # created, pid not written yet

    with pytest.raises(LockBusy):
        lock_state(tmp_path / ".minos").acquire()
    assert lock.exists()


def test_an_empty_lock_file_left_by_a_crash_is_reclaimed_eventually(tmp_path):
    lock = tmp_path / ".minos" / ".lock"
    lock.parent.mkdir()
    lock.write_text("")
    long_ago = lock.stat().st_mtime - 120
    os.utime(lock, (long_ago, long_ago))

    held = lock_state(tmp_path / ".minos").acquire()
    try:
        assert json.loads(lock.read_text())["pid"] == os.getpid()
    finally:
        held.release()
