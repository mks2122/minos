"""The policy broker — the only component with execution authority.

Invariant I1: the planner emits :class:`~minos.types.ActionRequest` objects and
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .capabilities import capability_for
from .checkpoint import CheckpointStore, UnprotectableTarget
from .oracles import NullOracle, compare
from .scopes import ScopeSet
from .types import (
    ActionRequest,
    AdmissionDecision,
    CheckpointId,
    EffectClass,
    EffectContract,
    Grant,
    Invocation,
    OracleResult,
    Outcome,
    ProvenanceRecord,
    ReversalOutcome,
)

__all__ = [
    "Approver",
    "AutoDeny",
    "Broker",
    "Compensator",
    "Executor",
    "always_deny",
    "cli_approver",
]

Executor = Callable[[Invocation], Any]
"""Runs the invocation. Supplied by the tier, called only by the broker."""

Approver = Callable[[Invocation, AdmissionDecision], bool]
"""Asks a human. Returning True admits the action."""

Compensator = Callable[["ActionRequest"], "Outcome"]
"""Runs a declared inverse.

Supplied by whoever owns a router -- the broker deliberately does not, because
invariant I1 keeps planning and execution apart and a broker that could route
its own requests would blur that. Routing a compensation back through
:meth:`Broker.submit` is what gives it a scope check and an audit entry of its
own, rather than a privileged side channel."""


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


@dataclass
class Broker:
    scopes: ScopeSet
    audit: AuditLog
    store: CheckpointStore
    approver: Approver = always_deny
    compensator: Compensator | None = None
    """Without one, a COMPENSABLE effect that fails says so rather than
    pretending the declared inverse ran."""

    dry_run: bool = False
    _compensating: bool = field(default=False, init=False, repr=False)

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

        if capability_for(invocation.request.operation) is None:
            return AdmissionDecision(
                verdict="deny",
                rationale=(
                    f"unknown operation {invocation.request.operation!r}; "
                    "operations must be registered in minos.capabilities"
                ),
            )

        grants = invocation.grants or self._derive_grants(invocation)
        if not grants:
            return AdmissionDecision(
                verdict="deny",
                rationale=f"{invocation.request.operation} declared no subject to check",
            )

        matched: list[str] = []
        for grant in grants:
            permitted, scope = self.scopes.check(grant.capability, grant.subject)
            if not permitted:
                return AdmissionDecision(
                    verdict="deny",
                    rationale=(
                        f"{grant.capability} on {grant.subject} is outside the granted scopes"
                    ),
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
            comp_capability = capability_for(compensation.operation)
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

    def _derive_grants(self, invocation: Invocation) -> tuple[Grant, ...]:
        """Conservative fallback when an adapter declared no explicit grants.

        Prefers the contract's declared targets -- they are what will actually be
        touched, and the checkpoint covers exactly them -- falling back to
        conventional param keys. An operation that smuggles a path under an
        unrecognised key yields nothing, and an empty grant set is denied.
        """
        capability = capability_for(invocation.request.operation)
        if capability is None:
            return ()
        subjects = (
            [str(t) for t in invocation.contract.targets]
            if invocation.contract.targets
            else _params_subjects(invocation.request.params)
        )
        return tuple(Grant(capability, subject) for subject in subjects)

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
            try:
                checkpoint_id = self.store.checkpoint(tuple(Path(t) for t in contract.targets))
            except UnprotectableTarget as exc:
                # A target we cannot copy is a target we cannot put back. Acting
                # anyway would mean claiming REVERSIBLE for an effect that is
                # not, which is the one lie this component must never tell.
                return self._record(
                    invocation,
                    decision,
                    status="failed",
                    error=f"refused: {exc}",
                )

            intact, missing = self.store.ensure_intact(checkpoint_id)
            if not intact:
                # Discovering this at rollback time is discovering it too late.
                return self._record(
                    invocation,
                    decision,
                    status="failed",
                    checkpoint_id=checkpoint_id,
                    error=(
                        f"refused: checkpoint is incomplete, {len(missing)} object(s) "
                        "missing from the store"
                    ),
                )

        before = oracle.observe()

        if checkpoint_id is not None:
            # Close the gap between copying and acting. Something that changed
            # in that window would be rolled back to a state the user never had.
            unchanged, drifted = self.store.unchanged_since(checkpoint_id)
            if not unchanged:
                return self._record(
                    invocation,
                    decision,
                    status="failed",
                    checkpoint_id=checkpoint_id,
                    error=(
                        f"refused: {len(drifted)} target(s) changed between the "
                        f"checkpoint and the action: {', '.join(drifted[:3])}"
                    ),
                )

        try:
            result = executor(invocation)
        except Exception as exc:
            reversal = self._reverse(checkpoint_id) or self._compensate(contract)
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
        expect_change = contract.change_expected
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
                result=result,
            )

        # Mismatch: reality did not match the contract. Try to put it back.
        reversal = self._reverse(checkpoint_id) or self._compensate(contract)

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

    def _compensate(self, contract: EffectContract) -> ReversalOutcome | None:
        """Run a COMPENSABLE effect's declared inverse.

        Until now these were declared and scope-checked but never executed,
        which made COMPENSABLE indistinguishable from IRREVERSIBLE in practice
        while looking like it was handled. PLAN.md calls that the project's top
        risk, and it was right.

        The inverse goes back through :meth:`submit` via the compensator, so it
        is admitted, scoped and recorded like any other action. A compensation
        that reaches the world through a privileged side channel would be a hole
        in exactly the component that exists to prevent holes.
        """
        if contract.effect_class is not EffectClass.COMPENSABLE:
            return None

        compensation = contract.compensation
        if compensation is None:  # pragma: no cover - EffectContract enforces this
            return None

        if self.compensator is None:
            # Saying "no inverse was run" is the whole point. Reporting a clean
            # reversal here would be the lie this component exists to prevent.
            return ReversalOutcome(
                attempted=False,
                succeeded=False,
                detail=(
                    f"declared inverse {compensation.operation!r} was not run: "
                    "no compensator is configured, so this external effect stands"
                ),
            )

        if self._compensating:
            # A compensation that fails must not trigger its own compensation.
            # One level, then stop and say so.
            return ReversalOutcome(
                attempted=False,
                succeeded=False,
                detail="refusing to compensate a compensation; reconcile by hand",
            )

        self._compensating = True
        try:
            outcome = self.compensator(compensation)
        except Exception as exc:
            return ReversalOutcome(
                attempted=True,
                succeeded=False,
                detail=f"inverse {compensation.operation!r} raised {type(exc).__name__}: {exc}",
            )
        finally:
            self._compensating = False

        if outcome.status == "ok":
            return ReversalOutcome(
                attempted=True,
                succeeded=True,
                detail=f"ran declared inverse {compensation.operation!r}, verified",
            )
        return ReversalOutcome(
            attempted=True,
            succeeded=False,
            detail=(
                f"declared inverse {compensation.operation!r} did not succeed "
                f"({outcome.status}: {outcome.error or outcome.decision.rationale})"
            ),
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
        result: Any = None,
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
            result=result,
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
