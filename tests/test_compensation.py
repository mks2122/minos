"""Executing COMPENSABLE inverses.

Until M25 these were declared, scope-checked at admission, and then never run.
That made COMPENSABLE indistinguishable from IRREVERSIBLE in practice while
looking like it was handled -- the worst of both, because the audit log said
"compensable" about an effect nothing could undo.

PLAN.md calls this the project's top risk. These tests are what closes it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minos.agent import Agent, AgentLimits
from minos.audit import AuditLog
from minos.broker import Broker
from minos.checkpoint import FileCheckpointStore
from minos.oracles import PathExistsOracle
from minos.router import Router
from minos.scopes import ScopeSet
from minos.tiers.l1_system import FilesystemAdapter
from minos.types import (
    ActionRequest,
    EffectClass,
    EffectContract,
    Grant,
    Invocation,
    Tier,
)


def compensable(target: Path, inverse: ActionRequest | None = None) -> Invocation:
    """An action that creates a directory, whose inverse deletes it."""
    return Invocation(
        request=ActionRequest(
            goal_id="g",
            intent="make a directory",
            operation="fs.mkdir",
            params={"path": str(target)},
        ),
        tier=Tier.L1_SYSTEM,
        adapter="test",
        tier_reason="test fixture",
        contract=EffectContract(
            effect_class=EffectClass.COMPENSABLE,
            targets=(),
            oracle=PathExistsOracle((target,)),
            expect=f"{target} exists",
            compensation=inverse
            or ActionRequest(
                goal_id="g",
                intent="remove the directory again",
                operation="fs.delete",
                params={"path": str(target)},
            ),
        ),
        grants=(Grant("fs.write", str(target)),),
    )


@pytest.fixture
def rig(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    broker = Broker(
        scopes=ScopeSet.parse(
            [
                f"fs.read:{workspace}/**",
                f"fs.write:{workspace}/**",
                f"fs.delete:{workspace}/**",
            ]
        ),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )
    router = Router(adapters=(FilesystemAdapter(),))

    def compensator(request):
        routed = router.route(request)
        return broker.submit(routed.invocation, routed.execute)

    broker.compensator = compensator
    return broker, workspace


# -- the inverse actually runs ---------------------------------------------


def test_a_failed_compensable_effect_runs_its_inverse(rig):
    """The behaviour that was missing. The directory must not survive."""
    broker, workspace = rig
    target = workspace / "created"

    def make_it_then_fail(_):
        target.mkdir()
        raise RuntimeError("the step failed after creating the directory")

    outcome = broker.submit(compensable(target), make_it_then_fail)

    assert not target.exists(), "the declared inverse did not run"
    assert outcome.reversal is not None
    assert outcome.reversal.attempted
    assert outcome.reversal.succeeded
    assert "fs.delete" in outcome.reversal.detail


def test_the_inverse_is_recorded_in_the_audit_chain(rig):
    """It goes through submit(), so it is admitted, scoped and logged."""
    broker, workspace = rig
    target = workspace / "created"

    def make_it_then_fail(_):
        target.mkdir()
        raise RuntimeError("boom")

    broker.submit(compensable(target), make_it_then_fail)

    operations = [e["invocation"]["request"]["operation"] for e in broker.audit.entries()]
    assert "fs.delete" in operations
    assert broker.audit.verify() == []


def test_a_successful_action_runs_no_inverse(rig):
    broker, workspace = rig
    target = workspace / "created"

    outcome = broker.submit(compensable(target), lambda _: target.mkdir())

    assert outcome.status == "ok"
    assert target.exists()
    assert outcome.reversal is None


# -- honesty when it cannot be run -----------------------------------------


def test_without_a_compensator_it_says_so_rather_than_claiming_reversal(rig):
    """Reporting a clean reversal here would be the exact lie to avoid."""
    broker, workspace = rig
    broker.compensator = None
    target = workspace / "created"

    def make_it_then_fail(_):
        target.mkdir()
        raise RuntimeError("boom")

    outcome = broker.submit(compensable(target), make_it_then_fail)

    assert outcome.reversal is not None
    assert not outcome.reversal.attempted
    assert not outcome.reversal.succeeded
    assert "no compensator is configured" in outcome.reversal.detail
    assert outcome.status == "reconciliation_required"
    assert target.exists(), "nothing undid it, and the runtime says so"


def test_an_inverse_outside_scope_never_gets_admitted(rig):
    """Admission already refuses this, and that is the right place for it.

    An inverse the runtime is not allowed to run means the effect is not
    actually compensable, so the action prompts -- and unattended, prompting
    means denied.
    """
    broker, workspace = rig
    target = workspace / "created"
    outside = workspace.parent / "elsewhere"

    outcome = broker.submit(
        compensable(
            target,
            inverse=ActionRequest(
                goal_id="g",
                intent="bad inverse",
                operation="fs.delete",
                params={"path": str(outside)},
            ),
        ),
        lambda _: target.mkdir(),
    )

    assert outcome.status == "denied"
    assert not target.exists(), "a denied action must not have run"


def test_an_inverse_that_fails_at_execution_halts_the_run(rig):
    """A compensation that did not work means state is unknown."""
    broker, workspace = rig
    target = workspace / "created"

    # In scope, so it is admitted -- but it names a path that will not be there,
    # so the delete itself fails.
    missing = workspace / "never-existed"
    invocation = compensable(
        target,
        inverse=ActionRequest(
            goal_id="g", intent="bad inverse", operation="fs.delete", params={"path": str(missing)}
        ),
    )

    def make_it_then_fail(_):
        target.mkdir()
        raise RuntimeError("boom")

    outcome = broker.submit(invocation, make_it_then_fail)

    assert outcome.status == "reconciliation_required"
    assert outcome.reversal is not None
    assert not outcome.reversal.succeeded
    assert "did not succeed" in outcome.reversal.detail


def test_an_inverse_that_raises_is_reported_not_swallowed(rig):
    broker, workspace = rig
    target = workspace / "created"

    def explode(_request):
        raise RuntimeError("the compensator itself is broken")

    broker.compensator = explode

    def make_it_then_fail(_):
        target.mkdir()
        raise RuntimeError("boom")

    outcome = broker.submit(compensable(target), make_it_then_fail)

    assert outcome.reversal is not None
    assert outcome.reversal.attempted
    assert not outcome.reversal.succeeded
    assert "RuntimeError" in outcome.reversal.detail


def test_a_compensation_is_not_itself_compensated(rig):
    """One level, then stop. Otherwise a broken inverse recurses forever."""
    broker, workspace = rig
    target = workspace / "created"
    seen: list[str] = []

    def compensator(request):
        seen.append(request.operation)
        # The inverse is itself a failing COMPENSABLE action.
        return broker.submit(
            compensable(workspace / "nested"),
            lambda _: (_ for _ in ()).throw(RuntimeError("inverse failed too")),
        )

    broker.compensator = compensator

    def make_it_then_fail(_):
        target.mkdir()
        raise RuntimeError("boom")

    outcome = broker.submit(compensable(target), make_it_then_fail)

    assert len(seen) == 1, "the compensation was compensated"
    assert outcome.reversal is not None
    assert not outcome.reversal.succeeded


# -- reversible effects still prefer the checkpoint ------------------------


def test_a_reversible_effect_uses_its_checkpoint_not_a_compensation(rig):
    """Compensation is the fallback for external effects, not the default."""
    broker, workspace = rig
    target = workspace / "a.txt"
    target.write_text("original", encoding="utf-8")

    from conftest import make_invocation

    def write_then_fail(_):
        target.write_text("changed", encoding="utf-8")
        raise RuntimeError("boom")

    outcome = broker.submit(make_invocation(targets=(target,)), write_then_fail)

    assert target.read_text() == "original"
    assert outcome.reversal is not None
    assert "restored" in outcome.reversal.detail


# -- wired up by default ---------------------------------------------------


def test_the_agent_wires_a_compensator_automatically(tmp_path):
    """A broker built by hand has none; one built by the agent does."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    broker = Broker(
        scopes=ScopeSet.parse([f"fs.write:{workspace}/**"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )
    assert broker.compensator is None

    from minos.planner.scripted import ScriptedPlanner

    Agent(
        planner=ScriptedPlanner(script=[]),
        router=Router(adapters=(FilesystemAdapter(),)),
        broker=broker,
        limits=AgentLimits(max_steps=1),
    )

    assert broker.compensator is not None


def test_an_explicit_compensator_is_not_replaced(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def mine(_request):
        raise AssertionError("never called")

    broker = Broker(
        scopes=ScopeSet.parse([f"fs.write:{workspace}/**"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
        compensator=mine,
    )

    from minos.planner.scripted import ScriptedPlanner

    Agent(
        planner=ScriptedPlanner(script=[]),
        router=Router(adapters=(FilesystemAdapter(),)),
        broker=broker,
    )

    assert broker.compensator is mine
