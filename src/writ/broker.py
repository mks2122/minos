"""The policy broker — the only component with execution authority.

Invariant I1: the planner emits :class:`~writ.types.ActionRequest` objects and
has no way to act. Everything that touches the world goes through
:meth:`Broker.submit`.

The pipeline, in order:

1. scope check          — is this within the grants set before the task?
2. effect class         — can it be auto-admitted at all?
3. admission            — allow / prompt / deny
4. dry run              — predict and stop, if enabled
5. checkpoint           — copy declared targets (REVERSIBLE only)
6. execute              — call the tier
7. verify               — oracle reads the system of record
8. reverse on mismatch  — or halt loudly if reversal fails
9. record               — append to the hash-chained log
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .checkpoint import CheckpointStore
from .oracles import NullOracle, compare
from .scopes import ScopeSet
from .types import (
    AdmissionDecision,
    CheckpointId,
    EffectClass,
    Invocation,
    OracleResult,
    Outcome,
    ProvenanceRecord,
    ReversalOutcome,
)

__all__ = ["Approver", "AutoDeny", "Broker", "Executor", "always_deny", "cli_approver"]

Executor = Callable[[Invocation], Any]
"""Runs the invocation. Supplied by the tier, called only by the broker."""

Approver = Callable[[Invocation, AdmissionDecision], bool]
"""Asks a human. Returning True admits the action."""


class AutoDeny(Exception):
    """Raised when no approver is configured but one is required."""


def always_deny(invocation: Invocation, decision: AdmissionDecision) -> bool:
    """Default approver: refuse.

    An unattended runtime must not silently auto-approve irreversible effects
    just because nobody wired up a prompt.
    """
    return False


def cli_approver(invocation: Invocation, decision: AdmissionDecision) -> bool:
    contract = invocation.contract
    # ASCII only: a plain Windows console is cp1252 and raises on box drawing.
    # An approval prompt that crashes is an approval that never happened.
    print("\n-- approval required " + "-" * 38)
    print(f"  intent   : {invocation.request.intent}")
    print(f"  operation: {invocation.request.operation}  [{invocation.tier}]")
    print(f"  effect   : {contract.effect_class}")
    if contract.targets:
        for target in contract.targets:
            print(f"  target   : {target}")
    if contract.expect:
        print(f"  expect   : {contract.expect}")
    print(f"  why      : {decision.rationale}")
    if contract.effect_class is EffectClass.IRREVERSIBLE:
        print("  !! THIS CANNOT BE UNDONE")
    return input("  allow? [y/N] ").strip().lower() in {"y", "yes"}


# Which capability an operation needs, and which scope subject it applies to.
_OPERATION_CAPABILITY: dict[str, str] = {
    "fs.read": "fs.read",
    "fs.write": "fs.write",
    "fs.delete": "fs.delete",
    "fs.copy": "fs.write",
    "fs.move": "fs.write",
    "proc.spawn": "proc.spawn",
    "net.http": "net.http",
    "ui.click": "ui.input",
    "ui.type": "ui.input",
    "clipboard.read": "clipboard.read",
    "clipboard.write": "clipboard.write",
}


@dataclass
class Broker:
    scopes: ScopeSet
    audit: AuditLog
    store: CheckpointStore
    approver: Approver = always_deny
    dry_run: bool = False

    # -- public surface ----------------------------------------------------

    def submit(self, invocation: Invocation, executor: Executor | None = None) -> Outcome:
        decision = self.admit(invocation)

        if decision.verdict == "deny":
            return self._record(invocation, decision, status="denied")

        if decision.verdict == "prompt":
            if not self.approver(invocation, decision):
                denied = AdmissionDecision(
                    verdict="deny",
                    rationale=f"human declined: {decision.rationale}",
                    matched_scopes=decision.matched_scopes,
                )
                return self._record(invocation, denied, status="denied")
            decision = AdmissionDecision(
                verdict="allow",
                rationale=f"human approved: {decision.rationale}",
                matched_scopes=decision.matched_scopes,
            )

        if self.dry_run:
            return self._record(
                invocation,
                decision,
                status="dry_run",
                predicted=self._predict(invocation),
            )

        if executor is None:
            raise ValueError("an executor is required for a non-dry-run submit")

        return self._execute(invocation, decision, executor)

    def admit(self, invocation: Invocation) -> AdmissionDecision:
        """Steps 1-3. Pure: no side effects, safe to call for preview."""
        contract = invocation.contract
        capability = _OPERATION_CAPABILITY.get(invocation.request.operation)

        if capability is None:
            return AdmissionDecision(
                verdict="deny",
                rationale=(
                    f"unknown operation {invocation.request.operation!r}; "
                    "operations must map to a declared capability"
                ),
            )

        subjects = self._subjects(invocation, capability)
        if not subjects:
            return AdmissionDecision(
                verdict="deny",
                rationale=f"{invocation.request.operation} declared no subject to check",
            )

        matched: list[str] = []
        for subject in subjects:
            permitted, scope = self.scopes.check(capability, subject)
            if not permitted:
                return AdmissionDecision(
                    verdict="deny",
                    rationale=f"{capability} on {subject} is outside the granted scopes",
                    denied_by=str(scope) if scope else None,
                )
            assert scope is not None
            matched.append(str(scope))

        # Step 2: effect class.
        if contract.effect_class is EffectClass.IRREVERSIBLE:
            return AdmissionDecision(
                verdict="prompt",
                rationale="irreversible effect: always requires explicit human approval",
                matched_scopes=tuple(matched),
            )

        if contract.effect_class is EffectClass.COMPENSABLE:
            compensation = contract.compensation
            assert compensation is not None  # enforced in EffectContract.__post_init__
            comp_capability = _OPERATION_CAPABILITY.get(compensation.operation)
            if comp_capability is None:
                return AdmissionDecision(
                    verdict="prompt",
                    rationale=(
                        f"compensation {compensation.operation!r} maps to no known "
                        "capability, so its reversibility cannot be established"
                    ),
                    matched_scopes=tuple(matched),
                )
            for subject in _params_subjects(compensation.params):
                permitted, _ = self.scopes.check(comp_capability, subject)
                if not permitted:
                    return AdmissionDecision(
                        verdict="prompt",
                        rationale=(
                            "compensation is itself outside the granted scopes, so the "
                            "effect cannot be guaranteed reversible"
                        ),
                        matched_scopes=tuple(matched),
                    )

        return AdmissionDecision(
            verdict="allow",
            rationale=f"within scope; effect is {contract.effect_class}",
            matched_scopes=tuple(matched),
        )

    # -- internals ---------------------------------------------------------

    def _subjects(self, invocation: Invocation, capability: str) -> list[str]:
        """What the scope check is applied to.

        Prefer the contract's declared targets — they are what will actually be
        touched, and the checkpoint covers exactly them. Fall back to params for
        non-path capabilities.
        """
        if invocation.contract.targets:
            return [str(t) for t in invocation.contract.targets]
        return _params_subjects(invocation.request.params)

    def _predict(self, invocation: Invocation) -> dict[str, Any]:
        contract = invocation.contract
        return {
            "operation": invocation.request.operation,
            "tier": str(invocation.tier),
            "effect_class": str(contract.effect_class),
            "would_touch": [str(t) for t in contract.targets],
            "expect": contract.expect,
            "reversible": contract.effect_class
            in (EffectClass.REVERSIBLE, EffectClass.COMPENSABLE),
            "oracle": getattr(contract.oracle, "kind", None),
        }

    def _execute(
        self, invocation: Invocation, decision: AdmissionDecision, executor: Executor
    ) -> Outcome:
        contract = invocation.contract
        oracle = contract.oracle or NullOracle()

        checkpoint_id: CheckpointId | None = None
        if contract.effect_class is EffectClass.REVERSIBLE and contract.targets:
            checkpoint_id = self.store.checkpoint(tuple(Path(t) for t in contract.targets))

        before = oracle.observe()

        try:
            executor(invocation)
        except Exception as exc:
            reversal = self._reverse(checkpoint_id)
            status = "reconciliation_required" if reversal and not reversal.succeeded else "failed"
            return self._record(
                invocation,
                decision,
                status=status,
                checkpoint_id=checkpoint_id,
                reversal=reversal,
                error=f"{type(exc).__name__}: {exc}",
            )

        after = oracle.observe()
        expect_change = contract.effect_class is not EffectClass.PURE
        matched, detail, collateral = compare(oracle, before, after, expect_change=expect_change)
        if collateral:
            after = {**after, "_collateral": collateral}

        observed = OracleResult(
            kind=getattr(oracle, "kind", "unknown"),
            verifiable=oracle.verifiable(),
            before=before,
            after=after,
            matched=matched,
            detail=detail,
        )

        if matched:
            return self._record(
                invocation,
                decision,
                status="ok",
                checkpoint_id=checkpoint_id,
                observed=observed,
            )

        # Mismatch: reality did not match the contract. Try to put it back.
        reversal = self._reverse(checkpoint_id)

        if collateral:
            # The checkpoint only ever covered the *declared* targets, so even a
            # "successful" restore leaves the undeclared changes in place. Saying
            # "reversed cleanly" here would be the exact lie this component exists
            # to prevent.
            if reversal is not None and reversal.succeeded:
                reversal = ReversalOutcome(
                    attempted=True,
                    succeeded=False,
                    detail=(
                        f"declared targets restored, but {len(collateral)} undeclared "
                        "path(s) were changed and are outside the checkpoint"
                    ),
                )
            return self._record(
                invocation,
                decision,
                status="reconciliation_required",
                checkpoint_id=checkpoint_id,
                observed=observed,
                reversal=reversal,
                error=(
                    f"undeclared paths changed and cannot be reversed: "
                    f"{', '.join(collateral[:5])}"
                    + (f" (+{len(collateral) - 5} more)" if len(collateral) > 5 else "")
                ),
            )

        if reversal is None:
            # Nothing to reverse with — we cannot make this right ourselves.
            return self._record(
                invocation,
                decision,
                status="reconciliation_required",
                observed=observed,
                error=f"effect did not match contract ({detail}) and no checkpoint exists",
            )
        if not reversal.succeeded:
            return self._record(
                invocation,
                decision,
                status="reconciliation_required",
                checkpoint_id=checkpoint_id,
                observed=observed,
                reversal=reversal,
                error=f"effect mismatch ({detail}); reversal failed: {reversal.detail}",
            )
        return self._record(
            invocation,
            decision,
            status="failed",
            checkpoint_id=checkpoint_id,
            observed=observed,
            reversal=reversal,
            error=f"effect did not match contract ({detail}); reversed cleanly",
        )

    def _reverse(self, checkpoint_id: CheckpointId | None) -> ReversalOutcome | None:
        if checkpoint_id is None:
            return None
        result = self.store.restore(checkpoint_id)
        if not result.succeeded:
            return ReversalOutcome(
                attempted=True, succeeded=False, detail=result.detail or "restore failed"
            )
        if not self.store.verify(checkpoint_id):
            return ReversalOutcome(
                attempted=True,
                succeeded=False,
                detail="restore reported success but verification failed",
            )
        return ReversalOutcome(
            attempted=True,
            succeeded=True,
            detail=f"restored {len(result.restored)} target(s), verified",
        )

    def _record(
        self,
        invocation: Invocation,
        decision: AdmissionDecision,
        *,
        status: str,
        checkpoint_id: CheckpointId | None = None,
        observed: OracleResult | None = None,
        reversal: ReversalOutcome | None = None,
        predicted: dict[str, Any] | None = None,
        error: str = "",
    ) -> Outcome:
        record = ProvenanceRecord(
            seq=self.audit.next_seq(),
            prev_hash=self.audit.head,
            ts=ProvenanceRecord.now(),
            invocation=invocation,
            decision=decision,
            checkpoint_id=checkpoint_id,
            observed=observed,
            reversal=reversal,
            status=status,
        )
        self.audit.append(record)
        return Outcome(
            status=status,  # type: ignore[arg-type]
            invocation=invocation,
            decision=decision,
            observed=observed,
            reversal=reversal,
            checkpoint_id=checkpoint_id,
            predicted=predicted,
            error=error,
        )


def _params_subjects(params: dict[str, Any]) -> list[str]:
    """Extract scope subjects from request params.

    Conventional keys only. An operation that smuggles a path under an
    unrecognised key yields no subject, and :meth:`Broker.admit` denies on an
    empty subject list — fail closed.
    """
    keys = ("path", "paths", "target", "targets", "url", "host", "command", "window")
    out: list[str] = []
    for key in keys:
        value = params.get(key)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            out.extend(str(v) for v in value)
        else:
            out.append(str(value))
    return out
