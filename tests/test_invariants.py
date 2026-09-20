"""Invariant checking: the eval strategy for a runtime that can do anything.

The point of these tests is not that the invariants pass on the suite -- they
do, and that is asserted once. It is that each one **actually catches its
violation**. An invariant that cannot fail is a comment.

So every check here is exercised against a trajectory constructed to break it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_invocation
from minos.agent import Trajectory
from minos.audit import AuditLog
from minos.checkpoint import FileCheckpointStore
from minos.evals.invariants import INVARIANTS, Violation, check_invariants
from minos.types import (
    AdmissionDecision,
    EffectClass,
    OracleResult,
    Outcome,
)


def outcome(
    *,
    status="ok",
    operation="fs.write",
    effect_class=EffectClass.REVERSIBLE,
    targets=(Path("a.txt"),),
    checkpoint_id="cp1",
    observed=True,
    verdict="allow",
    matched_scopes=("fs.write:**",),
    rationale="within scope",
):
    invocation = make_invocation(operation=operation, targets=targets, effect_class=effect_class)
    return Outcome(
        status=status,
        invocation=invocation,
        decision=AdmissionDecision(
            verdict=verdict, rationale=rationale, matched_scopes=matched_scopes
        ),
        observed=(
            OracleResult(kind="file_hash", verifiable=True, before={}, after={}, matched=True)
            if observed
            else None
        ),
        checkpoint_id=checkpoint_id,
    )


def trajectory_of(*outcomes):
    t = Trajectory(goal="a goal", goal_id="g")
    t.outcomes.extend(outcomes)
    return t


@pytest.fixture
def rig(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    store = FileCheckpointStore(tmp_path / "cp")
    return tmp_path, audit, store


def run(rig, trajectory, *, log_entries=0):
    workspace, audit, store = rig
    if log_entries:
        for _ in range(log_entries):
            _append(audit)
    return check_invariants(workspace, trajectory, audit, store)


def _append(audit):
    from minos.types import ProvenanceRecord

    audit.append(
        ProvenanceRecord(
            seq=audit.next_seq(),
            prev_hash=audit.head,
            ts=ProvenanceRecord.now(),
            invocation=make_invocation(targets=(Path("a.txt"),)),
            decision=AdmissionDecision(verdict="allow", rationale="ok"),
            checkpoint_id=None,
            observed=None,
            reversal=None,
            status="ok",
        )
    )


def ids(violations):
    return {v.invariant for v in violations}


# -- each invariant catches its violation ----------------------------------


def test_an_unchecked_write_is_caught(rig):
    """The central promise. If this fails, undo is a lie."""
    violations = run(rig, trajectory_of(outcome(checkpoint_id=None)), log_entries=1)

    assert "reversible-effects-are-checkpointed" in ids(violations)


def test_an_unattended_irreversible_effect_is_caught(rig):
    violations = run(
        rig,
        trajectory_of(
            outcome(
                effect_class=EffectClass.IRREVERSIBLE,
                operation="proc.spawn",
                targets=(),
                checkpoint_id=None,
                rationale="within scope",
            )
        ),
        log_entries=1,
    )

    assert "irreversible-effects-were-approved" in ids(violations)


def test_an_approved_irreversible_effect_is_fine(rig):
    violations = run(
        rig,
        trajectory_of(
            outcome(
                effect_class=EffectClass.IRREVERSIBLE,
                operation="proc.spawn",
                targets=(),
                checkpoint_id=None,
                rationale="human approved: within scope",
            )
        ),
        log_entries=1,
    )

    assert "irreversible-effects-were-approved" not in ids(violations)


def test_an_effect_with_no_oracle_result_at_all_is_caught(rig):
    """Unverified is allowed. Unrecorded is not."""
    violations = run(rig, trajectory_of(outcome(observed=False)), log_entries=1)

    assert "every-effect-was-verified-or-counted" in ids(violations)


def test_acting_after_a_halt_is_caught(rig):
    """A halt means the runtime no longer knows what state the machine is in."""
    halted = outcome(status="reconciliation_required")
    violations = run(rig, trajectory_of(halted, outcome()), log_entries=2)

    assert "the-run-stopped-when-told-to" in ids(violations)


def test_a_halt_as_the_last_action_is_fine(rig):
    violations = run(
        rig, trajectory_of(outcome(), outcome(status="reconciliation_required")), log_entries=2
    )

    assert "the-run-stopped-when-told-to" not in ids(violations)


def test_a_broken_audit_chain_is_caught(rig, tmp_path):
    workspace, audit, store = rig
    _append(audit)
    _append(audit)
    # Tamper: rewrite the first record's status.
    lines = audit.path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace('"status":"ok"', '"status":"denied"')
    audit.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    violations = check_invariants(workspace, trajectory_of(), AuditLog(audit.path), store)

    assert "the-audit-chain-is-intact" in ids(violations)


def test_an_unrecorded_action_is_caught(rig):
    """A log that says the run was smaller than it was is worse than no log."""
    violations = run(rig, trajectory_of(outcome(), outcome()), log_entries=1)

    assert "every-action-is-in-the-log" in ids(violations)


def test_a_denial_that_took_a_checkpoint_is_caught(rig):
    """A denial must be a refusal, not a warning."""
    violations = run(rig, trajectory_of(outcome(status="denied", checkpoint_id="cp9")))

    assert "denied-actions-changed-nothing" in ids(violations)


def test_code_run_declaring_a_real_target_is_caught(rig):
    """The whole safety argument for arbitrary code."""
    violations = run(
        rig,
        trajectory_of(outcome(operation="code.run", targets=(Path("/home/user/taxes.xlsx"),))),
        log_entries=1,
    )

    assert "the-sandbox-reached-nothing-real" in ids(violations)


def test_a_missing_checkpoint_object_is_caught(rig):
    """A checkpoint whose objects are gone is not an undo."""
    workspace, audit, store = rig
    target = workspace / "a.txt"
    target.write_text("original")
    checkpoint_id = store.checkpoint((target,))
    for obj in store.objects.iterdir():
        obj.unlink()
    _append(audit)

    violations = check_invariants(
        workspace, trajectory_of(outcome(checkpoint_id=checkpoint_id)), audit, store
    )

    assert "completed-writes-are-undoable" in ids(violations)


def test_a_checkpoint_id_the_store_never_heard_of_is_caught(rig):
    violations = run(rig, trajectory_of(outcome(checkpoint_id="deadbeef")), log_entries=1)

    assert "completed-writes-are-undoable" in ids(violations)


def test_an_allow_with_no_matched_scope_is_caught(rig):
    """Permitted without the check having been made."""
    violations = run(rig, trajectory_of(outcome(matched_scopes=())), log_entries=1)

    assert "nothing-acted-outside-its-scope" in ids(violations)


# -- the clean case ---------------------------------------------------------


def test_a_well_behaved_run_violates_nothing(rig):
    workspace, audit, store = rig
    target = workspace / "a.txt"
    target.write_text("original")
    checkpoint_id = store.checkpoint((target,))
    _append(audit)

    violations = check_invariants(
        workspace, trajectory_of(outcome(checkpoint_id=checkpoint_id)), audit, store
    )

    assert violations == []


def test_a_pure_read_needs_no_checkpoint_or_oracle(rig):
    violations = run(
        rig,
        trajectory_of(
            outcome(
                operation="fs.read",
                effect_class=EffectClass.PURE,
                checkpoint_id=None,
                observed=False,
            )
        ),
        log_entries=1,
    )

    assert violations == []


# -- the machinery ----------------------------------------------------------


def test_every_invariant_has_a_question():
    """The id is for machines; the question is what a failure has to answer."""
    for invariant in INVARIANTS:
        assert invariant.question.endswith("?"), invariant.id
        assert invariant.id


def test_all_violations_are_returned_not_just_the_first(rig):
    """A report that stops at the first problem hides the other four."""
    violations = run(
        rig,
        trajectory_of(outcome(checkpoint_id=None, observed=False, matched_scopes=())),
        log_entries=0,
    )

    assert len(ids(violations)) >= 3


def test_a_check_that_raises_is_itself_a_violation(rig, monkeypatch):
    """A check that cannot answer its question must not pass by accident."""
    from minos.evals import invariants

    def explode(_ctx):
        raise RuntimeError("the check is broken")

    broken = invariants.Invariant("broken", "Does it work?", explode)
    monkeypatch.setattr(invariants, "INVARIANTS", (broken,))

    workspace, audit, store = rig
    violations = invariants.check_invariants(workspace, trajectory_of(), audit, store)

    assert len(violations) == 1
    assert "the check itself raised" in violations[0].detail


def test_violation_reads_as_a_sentence():
    assert str(Violation("some-rule", "what went wrong")) == "some-rule: what went wrong"


# -- against the real suite -------------------------------------------------


def test_the_reference_suite_violates_nothing():
    """Every promise holds across every task, with the reference planner."""
    from minos.evals.harness import run_suite
    from minos.evals.suite import SUITE, scripted_factory

    result = run_suite(SUITE, scripted_factory, planner_name="scripted")

    assert result.violations == [], "\n".join(result.violations)


def test_violations_appear_in_the_report():
    from minos.evals.harness import EvalReport, TaskResult

    result = EvalReport(
        planner_name="fake",
        model="none",
        results=[
            TaskResult(
                task_id="t1",
                category="c",
                kind="achieve",
                succeeded=True,
                steps=1,
                duration_s=0.0,
                tier_counts={},
                fallback_rate=0.0,
                rollback_attempts=0,
                rollback_successes=0,
                unverified_effects=0,
                verified_effects=1,
                halted=False,
                audit_intact=True,
                summary="",
                violations=("some-rule: it broke",),
            )
        ],
    )

    assert "invariant violations 1" in result.text()
    assert "broke a promise" in result.text()
    assert result.summary()["invariant_violations"] == 1
