"""User-facing undo, over the checkpoints the broker already takes.

Every `REVERSIBLE` action copies its declared targets before acting, and every
action lands in the audit log carrying its checkpoint id. Those two facts are
enough to let a person say *"put that back"* long after the agent has moved on —
the broker only ever reversed on its **own** failure, which is the narrower and
less useful half of what the store can do.

Two properties this module refuses to give up:

**Undo is itself undoable.** Restoring is a write like any other, so the current
state is checkpointed first. Undoing an undo is just undoing the checkpoint it
took.

**Undo is recorded.** It runs through the audit log as a human-initiated action.
An undo that left no trace would make the log a record of what the agent did
rather than a record of what happened, and those must not diverge.

**Undo is not a time machine.** It restores the *declared targets* of one action.
Collateral changes were never in the checkpoint, and an action that reported
collateral says so in its audit entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .checkpoint import CheckpointStore, RestoreResult
from .types import (
    ActionRequest,
    AdmissionDecision,
    EffectClass,
    EffectContract,
    Invocation,
    ProvenanceRecord,
    ReversalOutcome,
    Tier,
)

__all__ = ["UndoError", "Undoable", "perform_undo", "undoable_actions"]


class UndoError(Exception):
    """The requested undo cannot be performed."""


@dataclass(frozen=True, slots=True)
class Undoable:
    """One past action that still has a restorable checkpoint behind it."""

    seq: int
    ts: str
    operation: str
    intent: str
    checkpoint_id: str
    targets: tuple[str, ...]
    status: str
    collateral: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        """False when the action touched paths the checkpoint never covered.

        Undoing is still allowed — it is the user's call — but the result is a
        partial restoration and must be described as one.
        """
        return not self.collateral

    @property
    def when(self) -> str:
        try:
            return datetime.fromisoformat(self.ts).strftime("%d %b %H:%M")
        except ValueError:
            return self.ts


def undoable_actions(audit: AuditLog, store: CheckpointStore) -> list[Undoable]:
    """Past actions whose checkpoints are still intact, newest first.

    An entry whose objects have been pruned is not offered. Listing an undo that
    would fail is worse than not listing it.
    """
    found: list[Undoable] = []
    for entry in audit.entries():
        checkpoint_id = entry.get("checkpoint_id")
        if not checkpoint_id:
            continue
        if entry.get("status") not in {"ok", "reconciliation_required"}:
            continue
        try:
            intact, _ = store.ensure_intact(checkpoint_id)
        except KeyError:
            continue
        if not intact:
            continue

        invocation: dict[str, Any] = entry.get("invocation", {})
        request: dict[str, Any] = invocation.get("request", {})
        contract: dict[str, Any] = invocation.get("contract", {})
        observed: dict[str, Any] = entry.get("observed") or {}
        found.append(
            Undoable(
                seq=int(entry.get("seq", 0)),
                ts=str(entry.get("ts", "")),
                operation=str(request.get("operation", "?")),
                intent=str(request.get("intent", "")),
                checkpoint_id=str(checkpoint_id),
                targets=tuple(str(t) for t in contract.get("targets", [])),
                status=str(entry.get("status", "")),
                collateral=tuple(str(c) for c in observed.get("collateral", []) or []),
            )
        )
    found.sort(key=lambda u: u.seq, reverse=True)
    return found


def find(audit: AuditLog, store: CheckpointStore, selector: str) -> Undoable:
    """Resolve a checkpoint id, an audit sequence number, or ``last``."""
    candidates = undoable_actions(audit, store)
    if not candidates:
        raise UndoError("nothing to undo: no action has a restorable checkpoint")

    if selector == "last":
        return candidates[0]

    for candidate in candidates:
        if candidate.checkpoint_id == selector or str(candidate.seq) == selector:
            return candidate

    raise UndoError(
        f"no undoable action matches {selector!r}. Run `minos undo` to see what is available."
    )


def perform_undo(
    audit: AuditLog,
    store: CheckpointStore,
    action: Undoable,
) -> tuple[RestoreResult, str | None]:
    """Restore one action's declared targets.

    Returns the restore result and the id of the checkpoint taken *before*
    restoring, which is what makes the undo reversible in turn.
    """
    targets = tuple(Path(t) for t in action.targets)

    redo_id: str | None = None
    if targets:
        # Capture where we are now, so this undo can itself be undone. If the
        # current state cannot be copied we stop: replacing state we could not
        # save is exactly the trade this project exists to refuse.
        redo_id = store.checkpoint(targets)

    result = store.restore(action.checkpoint_id)

    verified = result.succeeded and store.verify(action.checkpoint_id)
    if result.succeeded and not verified:
        result = RestoreResult(
            succeeded=False,
            restored=result.restored,
            failed=result.failed,
            detail="restore reported success but verification failed",
        )

    _record(audit, action, result, verified)
    return result, redo_id


def _record(
    audit: AuditLog,
    action: Undoable,
    result: RestoreResult,
    verified: bool,
) -> None:
    """Write the undo into the chain, as a human-initiated action."""
    request = ActionRequest(
        goal_id=f"undo-{action.checkpoint_id[:8]}",
        intent=f"human undo of #{action.seq} ({action.operation}): {action.intent}",
        operation="state.undo",
        params={"checkpoint_id": action.checkpoint_id, "undoes_seq": action.seq},
    )
    invocation = Invocation(
        request=request,
        tier=Tier.L1_SYSTEM,
        adapter="undo",
        tier_reason="undo restores files directly; no adapter or tier applies",
        contract=EffectContract(
            effect_class=EffectClass.REVERSIBLE,
            targets=tuple(Path(t) for t in action.targets),
            expect=f"declared targets of #{action.seq} returned to their prior contents",
        ),
    )
    decision = AdmissionDecision(
        verdict="allow",
        rationale="initiated by a human at the command line, not by the planner",
    )
    detail = result.detail or ("restored and verified" if verified else "")
    audit.append(
        ProvenanceRecord(
            seq=audit.next_seq(),
            prev_hash=audit.head,
            ts=ProvenanceRecord.now(),
            invocation=invocation,
            decision=decision,
            checkpoint_id=action.checkpoint_id,
            observed=None,
            reversal=ReversalOutcome(
                attempted=True,
                succeeded=result.succeeded and verified,
                detail=detail,
            ),
            status="ok" if (result.succeeded and verified) else "failed",
        )
    )
