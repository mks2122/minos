"""User-facing undo, and checkpoint retention.

The broker only ever reversed on its *own* failure. These cover the other half:
a person deciding, later, that they want a past action put back.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from conftest import make_invocation
from minos.audit import AuditLog
from minos.checkpoint import FileCheckpointStore
from minos.undo import UndoError, find, perform_undo, undoable_actions


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def acted(broker, workspace):
    """A workspace where the agent has changed one file through the broker."""
    target = workspace / "report.txt"
    write(target, "the original text")
    outcome = broker.submit(
        make_invocation(targets=(target,)),
        lambda i: write(target, "what the agent wrote"),
    )
    assert outcome.status == "ok"
    return target


# -- listing ---------------------------------------------------------------


def test_a_completed_action_is_undoable(broker, acted):
    actions = undoable_actions(broker.audit, broker.store)

    assert len(actions) == 1
    assert actions[0].operation == "fs.write"
    assert str(acted) in actions[0].targets[0]
    assert actions[0].clean


def test_actions_with_no_checkpoint_are_not_offered(broker, workspace):
    """A denied action changed nothing, so there is nothing to put back."""
    broker.submit(make_invocation(targets=(workspace.parent / "outside.txt",)), lambda i: None)

    assert undoable_actions(broker.audit, broker.store) == []


def test_pruned_checkpoints_are_not_offered(broker, acted):
    """Listing an undo that would fail is worse than not listing it."""
    assert undoable_actions(broker.audit, broker.store)

    for obj in broker.store.objects.iterdir():
        obj.unlink()

    assert undoable_actions(broker.audit, broker.store) == []


def test_newest_first(broker, workspace):
    for i in range(3):
        target = workspace / f"f{i}.txt"
        write(target, "before")
        broker.submit(make_invocation(targets=(target,)), lambda i, t=target: write(t, "after"))

    actions = undoable_actions(broker.audit, broker.store)
    assert [a.seq for a in actions] == sorted((a.seq for a in actions), reverse=True)


# -- performing ------------------------------------------------------------


def test_undo_restores_the_original_contents(broker, acted):
    assert acted.read_text() == "what the agent wrote"

    action = find(broker.audit, broker.store, "last")
    result, _ = perform_undo(broker.audit, broker.store, action)

    assert result.succeeded
    assert acted.read_text() == "the original text"


def test_undo_is_itself_undoable(broker, acted):
    """Restoring is a write like any other, so it gets a checkpoint first."""
    action = find(broker.audit, broker.store, "last")
    result, redo_id = perform_undo(broker.audit, broker.store, action)

    assert result.succeeded
    assert redo_id is not None
    assert acted.read_text() == "the original text"

    assert broker.store.restore(redo_id).succeeded
    assert acted.read_text() == "what the agent wrote"


def test_undo_is_recorded_in_the_audit_chain(broker, acted):
    """A log that omits undos is a record of the agent, not of what happened."""
    before = broker.audit.count

    action = find(broker.audit, broker.store, "last")
    perform_undo(broker.audit, broker.store, action)

    assert broker.audit.count == before + 1
    entry = broker.audit.entries()[-1]
    assert entry["invocation"]["request"]["operation"] == "state.undo"
    assert entry["status"] == "ok"
    assert "human" in entry["decision"]["rationale"]
    assert broker.audit.verify() == []


def test_undo_by_sequence_number(broker, acted):
    seq = undoable_actions(broker.audit, broker.store)[0].seq

    action = find(broker.audit, broker.store, str(seq))
    result, _ = perform_undo(broker.audit, broker.store, action)

    assert result.succeeded
    assert acted.read_text() == "the original text"


def test_unknown_selector_is_an_error_not_a_guess(broker, acted):
    with pytest.raises(UndoError, match="no undoable action matches"):
        find(broker.audit, broker.store, "nonsense")


def test_undo_with_nothing_to_undo(broker):
    with pytest.raises(UndoError, match="nothing to undo"):
        find(broker.audit, broker.store, "last")


def test_collateral_is_surfaced_as_partial(broker, workspace, audit):
    """An action that touched undeclared paths cannot be fully undone.

    Undo is still offered -- it is the user's call -- but it must not be
    described as clean.
    """
    from minos.oracles import FileTreeOracle

    declared = workspace / "declared.txt"
    undeclared = workspace / "undeclared.txt"
    write(declared, "before")

    def writes_more_than_it_declared(invocation):
        write(declared, "after")
        write(undeclared, "nobody declared this")

    broker.submit(
        make_invocation(
            targets=(declared,),
            oracle=FileTreeOracle(root=workspace, declared=(declared,)),
        ),
        writes_more_than_it_declared,
    )

    actions = undoable_actions(broker.audit, broker.store)
    if actions:  # the action halts; it is still recorded with its checkpoint
        assert not actions[0].clean


# -- retention -------------------------------------------------------------


def test_prune_drops_checkpoints_past_the_age_limit(store, workspace):
    target = workspace / "a.txt"
    write(target, "original")
    cid = store.checkpoint((target,))

    old = time.time() - 30 * 86_400
    manifest = store.manifests / f"{cid}.json"
    import os

    os.utime(manifest, (old, old))

    manifests, objects = store.prune(max_age_days=7.0)
    assert manifests == 1
    assert objects == 1
    assert list(store.objects.iterdir()) == []


def test_prune_keeps_recent_checkpoints(store, workspace):
    target = workspace / "a.txt"
    write(target, "original")
    cid = store.checkpoint((target,))

    store.prune(max_age_days=7.0)

    assert store.ensure_intact(cid)[0]


def test_prune_enforces_the_size_budget(tmp_path, workspace):
    store = FileCheckpointStore(tmp_path / "cp")
    for i in range(5):
        target = workspace / f"f{i}.bin"
        target.write_bytes(bytes([i]) * 4096)
        store.checkpoint((target,))

    assert store.usage_bytes() > 8192
    store.prune(max_age_days=365.0, max_bytes=8192)

    assert store.usage_bytes() <= 8192


def test_prune_never_raises_on_an_empty_store(store):
    assert store.prune() == (0, 0)


# -- cli -------------------------------------------------------------------


def test_cli_undo_lists_then_restores(tmp_path, capsys):
    """End to end through the command line, which is where a person meets this."""
    from minos.__main__ import main

    state = tmp_path / ".minos"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "notes.txt"
    write(target, "the original text")

    audit = AuditLog(state / "audit.jsonl")
    store = FileCheckpointStore(state / "checkpoints")
    from minos.broker import Broker
    from minos.scopes import ScopeSet

    broker = Broker(
        scopes=ScopeSet.parse([f"fs.write:{workspace}/**", f"fs.read:{workspace}/**"]),
        audit=audit,
        store=store,
    )
    broker.submit(
        make_invocation(targets=(target,)),
        lambda i: write(target, "what the agent wrote"),
    )

    assert main(["undo", "--state", str(state)]) == 0
    listing = capsys.readouterr().out
    assert "undoable actions" in listing
    assert "fs.write" in listing

    assert main(["undo", "--last", "--yes", "--state", str(state)]) == 0
    assert "restored" in capsys.readouterr().out
    assert target.read_text() == "the original text"


def test_cli_undo_with_no_state_is_an_error(tmp_path, capsys):
    from minos.__main__ import main

    assert main(["undo", "--state", str(tmp_path / "nope")]) == 2
    assert "no audit log" in capsys.readouterr().err


# -- undoing an undo is a redo, not the same undo again ---------------------


def test_undoing_an_undo_redoes_it(broker, acted):
    """`minos undo` lists past undos too, so they must mean the right thing.

    Recording the restored-from checkpoint would make undoing an undo apply
    the same undo a second time -- a no-op that looks like a redo.
    """
    assert acted.read_text() == "what the agent wrote"

    first = find(broker.audit, broker.store, "last")
    perform_undo(broker.audit, broker.store, first)
    assert acted.read_text() == "the original text"

    # The undo is now itself the most recent undoable action.
    second = find(broker.audit, broker.store, "last")
    assert second.operation == "state.undo"

    result, _ = perform_undo(broker.audit, broker.store, second)

    assert result.succeeded
    assert acted.read_text() == "what the agent wrote", "undoing the undo should redo"


def test_the_redo_checkpoint_is_what_gets_recorded(broker, acted):
    action = find(broker.audit, broker.store, "last")
    original_checkpoint = action.checkpoint_id

    _, redo_id = perform_undo(broker.audit, broker.store, action)

    recorded = broker.audit.entries()[-1]["checkpoint_id"]
    assert recorded == redo_id
    assert recorded != original_checkpoint
