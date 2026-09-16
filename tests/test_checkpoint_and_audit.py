"""Checkpoint store and audit log."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_invocation
from writ.audit import GENESIS, AuditLog
from writ.checkpoint import FileCheckpointStore
from writ.types import AdmissionDecision, ProvenanceRecord


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# -- checkpoint ------------------------------------------------------------


def test_restore_returns_original_bytes(store, workspace):
    target = workspace / "a.txt"
    write(target, "original")
    cid = store.checkpoint((target,))
    write(target, "modified")

    assert store.restore(cid).succeeded
    assert target.read_text() == "original"
    assert store.verify(cid)


def test_restore_deletes_a_file_that_did_not_exist(store, workspace):
    target = workspace / "new.txt"
    cid = store.checkpoint((target,))
    write(target, "created afterwards")

    assert store.restore(cid).succeeded
    assert not target.exists()
    assert store.verify(cid)


def test_verify_detects_drift_after_restore(store, workspace):
    target = workspace / "a.txt"
    write(target, "original")
    cid = store.checkpoint((target,))
    store.restore(cid)
    write(target, "changed again behind our back")

    assert not store.verify(cid)


def test_content_addressed_dedup(store, workspace):
    a = workspace / "a.txt"
    b = workspace / "b.txt"
    write(a, "identical")
    write(b, "identical")
    store.checkpoint((a, b))

    assert len(list(store.objects.iterdir())) == 1


def test_binary_roundtrip(store, workspace):
    target = workspace / "blob.bin"
    payload = bytes(range(256)) * 64
    target.write_bytes(payload)
    cid = store.checkpoint((target,))
    target.write_bytes(b"clobbered")

    assert store.restore(cid).succeeded
    assert target.read_bytes() == payload


def test_multiple_targets(store, workspace):
    files = {workspace / f"f{i}.txt": f"content-{i}" for i in range(5)}
    for path, text in files.items():
        write(path, text)
    cid = store.checkpoint(tuple(files))
    for path in files:
        write(path, "clobbered")

    assert store.restore(cid).succeeded
    for path, text in files.items():
        assert path.read_text() == text


def test_store_survives_reinstantiation(tmp_path, workspace):
    target = workspace / "a.txt"
    write(target, "original")
    cid = FileCheckpointStore(tmp_path / "cp").checkpoint((target,))
    write(target, "modified")

    assert FileCheckpointStore(tmp_path / "cp").restore(cid).succeeded
    assert target.read_text() == "original"


# -- audit -----------------------------------------------------------------


def _record(audit: AuditLog, status: str = "ok") -> ProvenanceRecord:
    return ProvenanceRecord(
        seq=audit.next_seq(),
        prev_hash=audit.head,
        ts=ProvenanceRecord.now(),
        invocation=make_invocation(),
        decision=AdmissionDecision(verdict="allow", rationale="test"),
        checkpoint_id=None,
        observed=None,
        reversal=None,
        status=status,
    )


def test_chain_starts_at_genesis(audit):
    assert audit.head == GENESIS
    assert audit.count == 0


def test_chain_verifies(audit):
    for _ in range(5):
        audit.append(_record(audit))
    assert audit.verify() == []
    assert audit.count == 5


def test_tampering_is_detected(audit, tmp_path):
    for _ in range(3):
        audit.append(_record(audit))

    lines = audit.path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["status"] = "denied"  # rewrite history
    lines[1] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    audit.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    breaks = AuditLog(audit.path).verify()
    assert breaks, "tampering must be detected"
    assert any(b.seq == 2 for b in breaks)


def test_deleting_an_entry_is_detected(audit):
    for _ in range(4):
        audit.append(_record(audit))
    lines = audit.path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    audit.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert AuditLog(audit.path).verify()


def test_reopen_continues_the_chain(audit):
    audit.append(_record(audit))
    head = audit.head

    reopened = AuditLog(audit.path)
    assert reopened.head == head
    assert reopened.next_seq() == 2
    reopened.append(_record(reopened))
    assert reopened.verify() == []


def test_broker_records_every_outcome(broker, workspace, audit):
    ok = workspace / "ok.txt"
    broker.submit(make_invocation(targets=(ok,)), lambda i: write(ok, "x"))
    broker.submit(make_invocation(targets=(workspace.parent / "no.txt",)), lambda i: None)

    entries = audit.entries()
    assert len(entries) == 2
    assert {e["status"] for e in entries} == {"ok", "denied"}
    assert audit.verify() == []


def test_audit_records_tier_reason(broker, workspace, audit):
    """I3 is only real if the reason actually lands in the log."""
    broker.submit(
        make_invocation(targets=(workspace / "a.txt",)),
        lambda i: write(workspace / "a.txt", "x"),
    )
    entry = audit.entries()[0]
    assert entry["invocation"]["tier_reason"] == "test fixture"
