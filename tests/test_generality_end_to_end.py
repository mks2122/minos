"""The claim of PLAN-GENERALITY.md, tested end to end.

A task with no typed adapter — a format conversion — is completed by writing
code, running it in the sandbox, and promoting the artifact through the broker.
The promotion is an ordinary checkpointed, hash-verified, reversible `fs.write`.

If this file passes, adding a capability no longer costs an adapter, a contract,
an oracle and a reversal path. That is the whole point of the milestone.
"""

from __future__ import annotations

import json

import pytest

from minos.audit import AuditLog
from minos.broker import Broker
from minos.checkpoint import FileCheckpointStore
from minos.router import Router
from minos.scopes import ScopeSet
from minos.tiers.l2_code import CodeAdapter
from minos.types import ActionRequest, Tier
from minos.undo import find, perform_undo, undoable_actions

CONVERT = """
import csv, json
from pathlib import Path

rows = list(csv.DictReader(Path('materials/input.csv').open()))
Path('out/converted.json').write_text(json.dumps(rows, indent=2))
print(f"converted {len(rows)} rows")
"""


@pytest.fixture
def rig(tmp_path):
    """A real broker, a real router, a real sandbox. No fakes."""
    state = tmp_path / ".minos"
    workspace = tmp_path / "ws"
    workspace.mkdir()

    source = workspace / "input.csv"
    source.write_text("name,qty\nwidget,3\ngadget,5\n", encoding="utf-8")

    adapter = CodeAdapter(state=state)
    broker = Broker(
        scopes=ScopeSet.parse(
            [
                f"fs.read:{workspace}/**",
                f"fs.write:{workspace}/**",
                f"code.run:{state}/**",
            ]
        ),
        audit=AuditLog(state / "audit.jsonl"),
        store=FileCheckpointStore(state / "checkpoints"),
    )
    return {
        "state": state,
        "workspace": workspace,
        "source": source,
        "adapter": adapter,
        "broker": broker,
        "router": Router(adapters=(adapter,)),
    }


def act(rig, operation, **params):
    request = ActionRequest(
        goal_id="convert", intent=f"{operation}", operation=operation, params=params
    )
    routed = rig["router"].route(request)
    return rig["broker"].submit(routed.invocation, routed.execute)


# -- the loop --------------------------------------------------------------


def test_a_task_with_no_adapter_is_completed_by_writing_code(rig):
    """CSV to JSON. Nobody wrote a csv-to-json adapter, and nobody will."""
    destination = rig["workspace"] / "converted.json"

    ran = act(rig, "code.run", code=CONVERT, materials=[str(rig["source"])])
    assert ran.status == "ok", ran.error
    assert "converted 2 rows" in ran.result.stdout

    promoted = act(rig, "code.materialize", artifact="converted.json", path=str(destination))
    assert promoted.status == "ok", promoted.error

    assert json.loads(destination.read_text()) == [
        {"name": "widget", "qty": "3"},
        {"name": "gadget", "qty": "5"},
    ]


def test_the_promotion_is_checkpointed_and_verified(rig):
    """The computation is unverified; the effect on the real machine is not."""
    destination = rig["workspace"] / "converted.json"

    act(rig, "code.run", code=CONVERT, materials=[str(rig["source"])])
    promoted = act(rig, "code.materialize", artifact="converted.json", path=str(destination))

    assert promoted.checkpoint_id is not None
    assert promoted.observed is not None
    assert promoted.observed.verifiable
    assert promoted.observed.matched


def test_the_promotion_is_reversible(rig):
    """Full undo of a capability nobody implemented."""
    destination = rig["workspace"] / "converted.json"
    destination.write_text("the file that was there before", encoding="utf-8")

    act(rig, "code.run", code=CONVERT, materials=[str(rig["source"])])
    act(rig, "code.materialize", artifact="converted.json", path=str(destination))
    assert "widget" in destination.read_text()

    action = find(rig["broker"].audit, rig["broker"].store, "last")
    result, _ = perform_undo(rig["broker"].audit, rig["broker"].store, action)

    assert result.succeeded
    assert destination.read_text() == "the file that was there before"


def test_running_code_touches_nothing_outside_the_sandbox(rig):
    """code.run declares no targets because it cannot reach any."""
    before = sorted(p.name for p in rig["workspace"].iterdir())

    act(rig, "code.run", code=CONVERT, materials=[str(rig["source"])])

    assert sorted(p.name for p in rig["workspace"].iterdir()) == before


def test_the_code_tier_is_recorded_as_l2_5(rig):
    """Degradation must be auditable (I3), including to the new tier."""
    outcome = act(rig, "code.run", code="print('x')")

    assert outcome.invocation.tier is Tier.L2_CODE
    entry = rig["broker"].audit.entries()[-1]
    assert entry["invocation"]["tier"] == "L2.5"
    assert entry["invocation"]["tier_reason"]


# -- scopes still govern it ------------------------------------------------


def test_promotion_outside_the_granted_scope_is_denied(rig, tmp_path):
    """materialize is an ordinary fs.write, so the user's scopes apply."""
    act(rig, "code.run", code=CONVERT, materials=[str(rig["source"])])

    outside = tmp_path / "elsewhere" / "stolen.json"
    outcome = act(rig, "code.materialize", artifact="converted.json", path=str(outside))

    assert outcome.status == "denied"
    assert not outside.exists()


def test_a_material_outside_the_granted_scope_is_denied(rig, tmp_path):
    """A script must not be handed a file the task was never granted."""
    secret = tmp_path / "secrets" / "private.txt"
    secret.parent.mkdir()
    secret.write_text("private")

    outcome = act(rig, "code.run", code="pass", materials=[str(secret)])

    assert outcome.status == "denied"
    assert "fs.read" in outcome.decision.rationale


def test_a_failing_script_is_recorded_and_promotes_nothing(rig):
    ran = act(rig, "code.run", code="raise RuntimeError('the model wrote a bug')")

    assert ran.status == "ok", "the run completed; the script inside it failed"
    assert not ran.result.ok
    assert "the model wrote a bug" in ran.result.stderr
    assert ran.result.artifacts == ()


def test_everything_lands_in_the_audit_chain(rig):
    destination = rig["workspace"] / "converted.json"

    act(rig, "code.run", code=CONVERT, materials=[str(rig["source"])])
    act(rig, "code.materialize", artifact="converted.json", path=str(destination))

    operations = [e["invocation"]["request"]["operation"] for e in rig["broker"].audit.entries()]
    assert operations == ["code.run", "code.materialize"]
    assert rig["broker"].audit.verify() == []


def test_the_promotion_is_offered_by_undo_but_the_run_is_not(rig):
    """code.run changed nothing on the real machine, so there is nothing to undo."""
    destination = rig["workspace"] / "converted.json"

    act(rig, "code.run", code=CONVERT, materials=[str(rig["source"])])
    act(rig, "code.materialize", artifact="converted.json", path=str(destination))

    actions = undoable_actions(rig["broker"].audit, rig["broker"].store)

    assert [a.operation for a in actions] == ["code.materialize"]


def test_dry_run_promotes_nothing(rig):
    destination = rig["workspace"] / "converted.json"
    act(rig, "code.run", code=CONVERT, materials=[str(rig["source"])])

    rig["broker"].dry_run = True
    outcome = act(rig, "code.materialize", artifact="converted.json", path=str(destination))

    assert outcome.status == "dry_run"
    assert not destination.exists()
