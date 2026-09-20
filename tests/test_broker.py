"""Broker tests — the admission pipeline, dry run, verification and reversal."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_invocation
from minos.oracles import FileHashOracle, FileTreeOracle, NullOracle
from minos.types import ActionRequest, EffectClass, Invocation, Tier


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# -- invariants ------------------------------------------------------------


def test_planner_types_cannot_execute():
    """I1, at the type level: ActionRequest has no way to act."""
    request = ActionRequest(goal_id="g", intent="i", operation="fs.write")
    for attr in ("execute", "run", "apply", "__call__"):
        assert not hasattr(request, attr)


def test_tier_reason_is_required():
    """I3: degradation must be auditable, so the field cannot be blank."""
    with pytest.raises(ValueError, match="tier_reason"):
        Invocation(
            request=ActionRequest(goal_id="g", intent="i", operation="fs.write"),
            tier=Tier.L3_GUI,
            adapter="gui",
            tier_reason="   ",
            contract=make_invocation().contract,
        )


def test_every_tier_uses_the_same_gate(broker, workspace):
    """I2: an L3 click out of scope is denied exactly like an L1 write."""
    outside = workspace.parent / "elsewhere.txt"
    for tier in (Tier.L1_SYSTEM, Tier.L2_ADAPTER, Tier.L3_GUI):
        inv = make_invocation(targets=(outside,), tier=tier)
        assert broker.admit(inv).verdict == "deny"


# -- admission -------------------------------------------------------------


def test_allows_in_scope_reversible(broker, workspace):
    inv = make_invocation(targets=(workspace / "a.txt",))
    assert broker.admit(inv).verdict == "allow"


def test_denies_out_of_scope(broker, workspace):
    inv = make_invocation(targets=(workspace.parent / "outside.txt",))
    decision = broker.admit(inv)
    assert decision.verdict == "deny"
    assert "outside the granted scopes" in decision.rationale


def test_unknown_operation_denied(broker, workspace):
    inv = make_invocation(operation="fs.obliterate", targets=(workspace / "a.txt",))
    assert broker.admit(inv).verdict == "deny"


def test_no_subject_denied(broker):
    """Fail closed: an operation that declares nothing to check is refused."""
    inv = make_invocation(targets=(), params={})
    assert broker.admit(inv).verdict == "deny"


def test_irreversible_always_prompts_even_in_scope(broker, workspace):
    inv = make_invocation(targets=(workspace / "a.txt",), effect_class=EffectClass.IRREVERSIBLE)
    decision = broker.admit(inv)
    assert decision.verdict == "prompt"
    assert "irreversible" in decision.rationale.lower()


def test_compensable_requires_a_compensation():
    from minos.types import EffectContract

    with pytest.raises(ValueError, match="COMPENSABLE"):
        EffectContract(effect_class=EffectClass.COMPENSABLE)


def test_compensable_allowed_when_compensation_in_scope(broker, workspace):
    inv = make_invocation(
        targets=(workspace / "a.txt",),
        effect_class=EffectClass.COMPENSABLE,
        compensation=ActionRequest(
            goal_id="g1",
            intent="undo",
            operation="fs.delete",
            params={"path": str(workspace / "a.txt")},
        ),
    )
    # fs.delete is not granted in the fixture, so this must not auto-allow.
    assert broker.admit(inv).verdict == "prompt"


def test_compensable_prompts_when_compensation_out_of_scope(broker, workspace):
    inv = make_invocation(
        targets=(workspace / "a.txt",),
        effect_class=EffectClass.COMPENSABLE,
        compensation=ActionRequest(
            goal_id="g1",
            intent="undo elsewhere",
            operation="fs.write",
            params={"path": str(workspace.parent / "outside.txt")},
        ),
    )
    decision = broker.admit(inv)
    assert decision.verdict == "prompt"
    assert "reversible" in decision.rationale


def test_denied_action_never_runs(broker, workspace):
    ran = []
    inv = make_invocation(targets=(workspace.parent / "outside.txt",))
    outcome = broker.submit(inv, lambda i: ran.append(i))
    assert outcome.status == "denied"
    assert ran == []


def test_default_approver_denies(broker, workspace):
    """No approver wired up must not mean silent auto-approval."""
    ran = []
    inv = make_invocation(targets=(workspace / "a.txt",), effect_class=EffectClass.IRREVERSIBLE)
    outcome = broker.submit(inv, lambda i: ran.append(i))
    assert outcome.status == "denied"
    assert ran == []


def test_approved_irreversible_runs(broker, workspace):
    broker.approver = lambda inv, dec: True
    target = workspace / "sent.txt"
    inv = make_invocation(
        targets=(target,),
        effect_class=EffectClass.IRREVERSIBLE,
        oracle=FileHashOracle((target,)),
    )
    outcome = broker.submit(inv, lambda i: write(target, "sent"))
    assert outcome.status == "ok"
    assert "human approved" in outcome.decision.rationale


# -- dry run ---------------------------------------------------------------


def test_dry_run_predicts_and_does_not_execute(broker, workspace):
    broker.dry_run = True
    target = workspace / "a.txt"
    ran = []
    inv = make_invocation(targets=(target,), expect="a.txt contains 'hello'")
    outcome = broker.submit(inv, lambda i: ran.append(i))

    assert outcome.status == "dry_run"
    assert ran == []
    assert not target.exists()
    assert outcome.predicted is not None
    assert str(target) in outcome.predicted["would_touch"]
    assert outcome.predicted["reversible"] is True


def test_dry_run_still_denies_out_of_scope(broker, workspace):
    broker.dry_run = True
    inv = make_invocation(targets=(workspace.parent / "outside.txt",))
    assert broker.submit(inv, lambda i: None).status == "denied"


# -- verification and reversal --------------------------------------------


def test_successful_action_verified(broker, workspace):
    target = workspace / "a.txt"
    write(target, "before")
    inv = make_invocation(targets=(target,), oracle=FileHashOracle((target,)))
    outcome = broker.submit(inv, lambda i: write(target, "after"))

    assert outcome.status == "ok"
    assert outcome.observed is not None
    assert outcome.observed.matched
    assert target.read_text() == "after"


def test_no_change_when_change_expected_is_reversed(broker, workspace):
    target = workspace / "a.txt"
    write(target, "before")
    inv = make_invocation(targets=(target,), oracle=FileHashOracle((target,)))
    outcome = broker.submit(inv, lambda i: None)  # executor does nothing

    assert outcome.status == "failed"
    assert outcome.reversal is not None and outcome.reversal.succeeded
    assert target.read_text() == "before"


def test_executor_exception_rolls_back(broker, workspace):
    target = workspace / "a.txt"
    write(target, "original")

    def boom(inv):
        write(target, "half-written")
        raise RuntimeError("tier blew up")

    inv = make_invocation(targets=(target,), oracle=FileHashOracle((target,)))
    outcome = broker.submit(inv, boom)

    assert outcome.status == "failed"
    assert "tier blew up" in outcome.error
    assert target.read_text() == "original"


def test_created_file_is_removed_on_reversal(broker, workspace):
    """The easy-to-forget case: the checkpoint must record absence."""
    target = workspace / "new.txt"

    def create_then_fail(inv):
        write(target, "created")
        raise RuntimeError("failed after creating")

    inv = make_invocation(targets=(target,), oracle=FileHashOracle((target,)))
    outcome = broker.submit(inv, create_then_fail)

    assert outcome.status == "failed"
    assert not target.exists()


def test_collateral_write_halts_with_reconciliation_required(broker, workspace):
    """Undeclared writes are outside the checkpoint, so we must not claim a clean undo."""
    declared = workspace / "declared.txt"
    collateral = workspace / "collateral.txt"
    write(declared, "before")

    inv = make_invocation(
        targets=(declared,),
        oracle=FileTreeOracle(root=workspace, declared=(declared,)),
    )

    def touch_both(i):
        write(declared, "after")
        write(collateral, "surprise")

    outcome = broker.submit(inv, touch_both)

    assert outcome.observed is not None
    assert str(collateral) in outcome.observed.collateral
    # Reversal restores the declared target but cannot undo the collateral file,
    # so verification of the checkpoint fails and the task must halt.
    assert outcome.halted
    assert outcome.status == "reconciliation_required"


def test_mismatch_without_checkpoint_halts(broker, workspace):
    """PURE actions take no checkpoint, so a mismatch cannot be undone."""
    target = workspace / "a.txt"
    write(target, "before")
    inv = make_invocation(
        operation="fs.read",
        targets=(target,),
        effect_class=EffectClass.PURE,
        oracle=FileHashOracle((target,)),
    )
    outcome = broker.submit(inv, lambda i: write(target, "mutated by a 'read'"))

    assert outcome.status == "reconciliation_required"
    assert outcome.halted


def test_null_oracle_is_counted_not_hidden(broker, workspace):
    target = workspace / "a.txt"
    inv = make_invocation(targets=(target,), oracle=NullOracle())
    outcome = broker.submit(inv, lambda i: write(target, "x"))

    assert outcome.status == "ok"
    assert outcome.observed is not None
    assert outcome.observed.verifiable is False
    assert outcome.observed.kind == "null"
