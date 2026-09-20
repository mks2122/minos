"""Regression tests for the guarantees checkpointing actually has to make.

Every test here corresponds to a way the copy-before-write journal could lie
about having protected something. The headline case is the first one: an
unreadable file used to be recorded as an absent file, so rollback *deleted* it
and ``verify()`` then confirmed the deletion as correct.
"""

from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path

import pytest

from conftest import make_invocation
from minos.checkpoint import (
    UNREADABLE,
    FileCheckpointStore,
    UnprotectableTarget,
    file_digest,
    probe,
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# -- unreadable targets ----------------------------------------------------


def test_unreadable_target_is_refused_not_treated_as_absent(store, workspace, monkeypatch):
    """The bug this whole module exists for.

    A file locked by another application (routine on Windows, and inevitable
    once `app.open` hands a document to Word) could not be read. The old code
    recorded it as ``None``, which the manifest defined as "did not exist", and
    restore duly deleted it.
    """
    target = workspace / "locked.docx"
    write(target, "IRREPLACEABLE")

    real_open = builtins.open

    def locked_open(path, *args, **kwargs):
        if str(path).endswith("locked.docx"):
            raise PermissionError(13, "being used by another process")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", locked_open)

    with pytest.raises(UnprotectableTarget, match="cannot read"):
        store.checkpoint((target,))

    monkeypatch.undo()
    assert target.read_text() == "IRREPLACEABLE"


def test_refusing_a_checkpoint_leaves_the_store_empty(store, workspace, monkeypatch):
    """Probe everything before copying anything: a refusal is all-or-nothing."""
    good = workspace / "good.txt"
    bad = workspace / "bad.txt"
    write(good, "fine")
    write(bad, "unreadable")

    real_open = builtins.open

    def locked_open(path, *args, **kwargs):
        if str(path).endswith("bad.txt"):
            raise PermissionError(13, "locked")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", locked_open)
    with pytest.raises(UnprotectableTarget):
        store.checkpoint((good, bad))

    monkeypatch.undo()
    assert list(store.objects.iterdir()) == []
    assert list(store.manifests.iterdir()) == []


def test_file_digest_distinguishes_unreadable_from_absent(workspace, monkeypatch):
    """Oracles need the same distinction: a locked file is not a deleted one."""
    target = workspace / "locked.bin"
    write(target, "x")

    real_open = builtins.open

    def locked_open(path, *args, **kwargs):
        if str(path).endswith("locked.bin"):
            raise PermissionError(13, "locked")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", locked_open)

    assert file_digest(target) == UNREADABLE
    assert file_digest(workspace / "never-existed.bin") is None


# -- directories -----------------------------------------------------------


def test_directory_is_recorded_as_a_directory(store, workspace):
    """`fs.mkdir` and `fs.delete` make directory targets a live path.

    A directory used to probe as ``None`` — indistinguishable from absent — so
    rollback tried to unlink it and every such action ended in a bogus
    ``reconciliation_required``.
    """
    d = workspace / "reports"
    d.mkdir()
    (d / "keep.txt").write_text("contents are not declared targets")

    cid = store.checkpoint((d,))
    assert store.verify(cid)

    result = store.restore(cid)
    assert result.succeeded
    assert d.is_dir()
    assert (d / "keep.txt").exists()


def test_restore_removes_a_directory_the_action_created(store, workspace):
    """The mkdir case: absent at checkpoint time, so rollback removes it."""
    d = workspace / "new-dir"
    cid = store.checkpoint((d,))
    d.mkdir()

    assert store.restore(cid).succeeded
    assert not d.exists()


def test_restore_never_recurses_into_a_directory(store, workspace):
    """Only declared targets may be removed. A non-empty dir fails loudly."""
    d = workspace / "new-dir"
    cid = store.checkpoint((d,))
    d.mkdir()
    (d / "user-put-this-here.txt").write_text("not ours to delete")

    result = store.restore(cid)
    assert not result.succeeded
    assert (d / "user-put-this-here.txt").exists()


# -- symlinks --------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privilege on Windows")
def test_symlink_is_relinked_not_replaced_by_a_copy(store, workspace):
    """Resolving the final component made a link indistinguishable from its target."""
    real = workspace / "real.txt"
    write(real, "the actual contents")
    link = workspace / "link.txt"
    os.symlink(real, link)

    cid = store.checkpoint((link,))
    link.unlink()
    write(link, "clobbered by a regular file")

    assert store.restore(cid).succeeded
    assert link.is_symlink()
    assert Path(os.readlink(link)) == real


# -- metadata --------------------------------------------------------------


def test_restore_puts_mtime_back(store, workspace):
    """The memory index keys on mtime, so a fresh mtime is a phantom edit."""
    target = workspace / "a.txt"
    write(target, "original")
    os.utime(target, (1_600_000_000, 1_600_000_000))

    cid = store.checkpoint((target,))
    write(target, "modified")

    assert store.restore(cid).succeeded
    assert target.stat().st_mtime == pytest.approx(1_600_000_000, abs=2)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_restore_puts_the_executable_bit_back(store, workspace):
    target = workspace / "script.sh"
    write(target, "#!/bin/sh\necho hi\n")
    target.chmod(0o755)

    cid = store.checkpoint((target,))
    target.chmod(0o644)
    write(target, "clobbered")

    assert store.restore(cid).succeeded
    assert target.stat().st_mode & 0o111


# -- limits ----------------------------------------------------------------


def test_oversized_target_is_refused(workspace, tmp_path):
    """Documented in MODELS.md, previously unenforced in code."""
    store = FileCheckpointStore(tmp_path / "cp", max_target_bytes=16)
    target = workspace / "big.bin"
    target.write_bytes(b"x" * 64)

    with pytest.raises(UnprotectableTarget, match="over the"):
        store.checkpoint((target,))


def test_device_and_socket_targets_are_refused(workspace, monkeypatch):
    """Not a regular file means not something a copy can capture."""
    import stat as stat_mod

    target = workspace / "weird"
    write(target, "x")
    real_lstat = os.lstat

    def fake_lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if str(path).endswith("weird"):
            return os.stat_result(
                (stat_mod.S_IFCHR | 0o644, *tuple(st)[1:])  # type: ignore[arg-type]
            )
        return st

    monkeypatch.setattr(os, "lstat", fake_lstat)
    with pytest.raises(UnprotectableTarget, match="not a regular file"):
        probe(target)


# -- integrity -------------------------------------------------------------


def test_missing_object_refuses_a_partial_restore(store, workspace):
    """Finding a pruned object halfway through used to leave mixed state."""
    a = workspace / "a.txt"
    b = workspace / "b.txt"
    write(a, "alpha")
    write(b, "beta")
    cid = store.checkpoint((a, b))
    write(a, "clobbered-a")
    write(b, "clobbered-b")

    for obj in store.objects.iterdir():
        obj.unlink()

    result = store.restore(cid)
    assert not result.succeeded
    assert "Nothing was changed" in result.detail
    assert a.read_text() == "clobbered-a"
    assert b.read_text() == "clobbered-b"


def test_ensure_intact_detects_a_pruned_object(store, workspace):
    target = workspace / "a.txt"
    write(target, "original")
    cid = store.checkpoint((target,))
    assert store.ensure_intact(cid)[0]

    for obj in store.objects.iterdir():
        obj.unlink()
    intact, missing = store.ensure_intact(cid)
    assert not intact
    assert len(missing) == 1


def test_unchanged_since_detects_drift_between_checkpoint_and_action(store, workspace):
    target = workspace / "a.txt"
    write(target, "original")
    cid = store.checkpoint((target,))
    assert store.unchanged_since(cid)[0]

    write(target, "someone else edited this")
    unchanged, drifted = store.unchanged_since(cid)
    assert not unchanged
    assert len(drifted) == 1


# -- retention -------------------------------------------------------------


def test_gc_reclaims_unreferenced_objects(store, workspace):
    target = workspace / "a.txt"
    write(target, "original")
    cid = store.checkpoint((target,))
    assert len(list(store.objects.iterdir())) == 1

    store.forget(cid)
    assert store.gc() == 1
    assert list(store.objects.iterdir()) == []


def test_gc_keeps_objects_a_live_manifest_still_needs(store, workspace):
    keep = workspace / "keep.txt"
    drop = workspace / "drop.txt"
    write(keep, "keep me")
    write(drop, "drop me")
    keep_cid = store.checkpoint((keep,))
    drop_cid = store.checkpoint((drop,))

    store.forget(drop_cid)
    store.gc()

    assert store.ensure_intact(keep_cid)[0]


# -- manifest compatibility ------------------------------------------------


def test_version_1_manifests_are_still_readable(store, workspace):
    """Old checkpoints must not become unrestorable by upgrading."""
    target = workspace / "a.txt"
    write(target, "original")
    cid = store.checkpoint((target,))

    digest = next(iter(store.objects.iterdir())).name
    legacy = store.manifests / f"{cid}.json"
    legacy.write_text(f'{{"{str(target).replace(chr(92), chr(92) * 2)}": "{digest}"}}')

    write(target, "modified")
    assert store.restore(cid).succeeded
    assert target.read_text() == "original"


# -- broker integration ----------------------------------------------------


def test_broker_refuses_to_act_on_an_unprotectable_target(broker, workspace, monkeypatch):
    """The refusal has to reach the broker, or the fix is only half a fix.

    Acting anyway would mean recording REVERSIBLE for an effect that is not.
    """
    target = workspace / "locked.txt"
    write(target, "precious")

    real_open = builtins.open

    def locked_open(path, *args, **kwargs):
        if str(path).endswith("locked.txt"):
            raise PermissionError(13, "locked")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", locked_open)

    ran = False

    def executor(invocation):
        nonlocal ran
        ran = True

    outcome = broker.submit(make_invocation(targets=(target,)), executor)

    assert outcome.status == "failed"
    assert "refused" in outcome.error
    assert not ran, "the action must not run when its target cannot be protected"


def test_broker_refuses_when_a_target_drifts_before_the_action(broker, workspace, monkeypatch):
    """Close the window between copying and acting."""
    target = workspace / "a.txt"
    write(target, "original")

    original_checkpoint = broker.store.checkpoint

    def checkpoint_then_meddle(targets):
        cid = original_checkpoint(targets)
        write(target, "a human edited this in the gap")
        return cid

    monkeypatch.setattr(broker.store, "checkpoint", checkpoint_then_meddle)

    outcome = broker.submit(make_invocation(targets=(target,)), lambda i: write(target, "agent"))

    assert outcome.status == "failed"
    assert "changed between the checkpoint and the action" in outcome.error
    assert target.read_text() == "a human edited this in the gap"
