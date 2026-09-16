"""Core types.

These encode the architecture's three invariants at the type level:

I1  The planner never executes. It produces :class:`ActionRequest` and nothing else;
    there is deliberately no ``execute()`` on it.
I2  Every tier passes the same gate. :class:`Invocation` wraps a request for *any*
    tier and is the only thing the broker accepts.
I3  Degradation is auditable. :attr:`Invocation.tier_reason` is required, not optional.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

__all__ = [
    "ActionRequest",
    "AdmissionDecision",
    "CheckpointId",
    "EffectClass",
    "EffectContract",
    "Grant",
    "Invocation",
    "Oracle",
    "OracleResult",
    "Outcome",
    "ProvenanceRecord",
    "ReversalOutcome",
    "Tier",
    "Verdict",
]


class Tier(enum.StrEnum):
    """Control tiers, in preference order. Lower is always preferred."""

    L1_SYSTEM = "L1"
    L2_ADAPTER = "L2"
    L3_GUI = "L3"


class EffectClass(enum.StrEnum):
    """Classification by reversibility. This taxonomy is the point of the project.

    A runtime that only ever emits ``PURE`` and ``REVERSIBLE`` is a checkpoint
    system. The value is in handling the other two honestly.
    """

    PURE = "pure"
    """Reads. Changes nothing. Auto-admitted within scope."""

    REVERSIBLE = "reversible"
    """Covered by a checkpoint we take first. Auto-admitted within scope."""

    COMPENSABLE = "compensable"
    """External, but has a declared inverse. Auto-admitted only if the
    compensation is declared *and* itself within scope."""

    IRREVERSIBLE = "irreversible"
    """Sent mail. Took payment. Published. **Never** auto-admitted, never
    replayed from a skill without fresh confirmation."""


Verdict = Literal["allow", "prompt", "deny"]

CheckpointId = str


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """What the planner emits. Note the absence of any way to run it."""

    goal_id: str
    intent: str
    """Natural language, for the audit log and the approval prompt."""

    operation: str
    """Dotted operation name, e.g. ``fs.write``, ``office.set_cell``."""

    params: dict[str, Any] = field(default_factory=dict)
    tier_hint: Tier | None = None
    """Advisory only. The router decides; a hint never binds it."""


@runtime_checkable
class Oracle(Protocol):
    """Reads the system of record to determine what actually happened.

    A screenshot is never an oracle. Screen-only verification has been measured
    accepting ~75% of wrong effects; a single system-of-record read cuts that to
    ~12.5%.
    """

    kind: str

    def observe(self) -> dict[str, Any]:
        """Snapshot the observable state this oracle is responsible for."""
        ...

    def verifiable(self) -> bool:
        """False for :class:`~writ.oracles.NullOracle`, which is counted and published."""
        ...


@dataclass(frozen=True, slots=True)
class OracleResult:
    """Before/after observations plus the verdict."""

    kind: str
    verifiable: bool
    before: dict[str, Any]
    after: dict[str, Any]
    matched: bool
    """True when the observed change is consistent with the contract."""

    detail: str = ""

    @property
    def collateral(self) -> list[str]:
        """Paths that changed but were not declared targets.

        Populated by tree-shaped oracles. Non-empty means reversal is
        known-incomplete: the checkpoint only covers declared targets.
        """
        raw = self.after.get("_collateral", [])
        return list(raw) if isinstance(raw, list) else []


@dataclass(frozen=True, slots=True)
class EffectContract:
    """Declared *before* the action runs: what should change, and how we will know.

    Declaring targets up front is what makes portable checkpointing possible —
    we copy these files, rather than snapshotting a volume.
    """

    effect_class: EffectClass
    targets: tuple[Path, ...] = ()
    oracle: Oracle | None = None
    expect: str = ""
    """Human-readable statement of intent, shown in approval prompts."""

    compensation: ActionRequest | None = None
    """Required for ``COMPENSABLE``; the declared inverse."""

    def __post_init__(self) -> None:
        if self.effect_class is EffectClass.COMPENSABLE and self.compensation is None:
            raise ValueError(
                "COMPENSABLE effects must declare a compensation. "
                "An external effect with no stated inverse is IRREVERSIBLE."
            )


@dataclass(frozen=True, slots=True)
class Grant:
    """One (capability, subject) pair an action needs in order to be admitted."""

    capability: str
    subject: str

    def __str__(self) -> str:
        return f"{self.capability}:{self.subject}"


@dataclass(frozen=True, slots=True)
class Invocation:
    """A request bound to a concrete tier and adapter, carrying its contract.

    This is the only thing the broker accepts — I2.
    """

    request: ActionRequest
    tier: Tier
    adapter: str
    tier_reason: str
    """Required (I3). Why this tier and not a preferred one.

    e.g. ``"no L2 adapter for app=com.acme.legacy; fell back to pixels"``.
    """

    contract: EffectContract

    grants: tuple[Grant, ...] = ()
    """Exactly which (capability, subject) pairs this action needs.

    Declared by the adapter, which is trusted. An operation like ``fs.copy``
    needs ``fs.read`` on the source *and* ``fs.write`` on the destination, and
    only the adapter knows that. When empty, the broker derives a conservative
    set from the operation and the contract's targets.
    """

    def __post_init__(self) -> None:
        if not self.tier_reason.strip():
            raise ValueError(
                "tier_reason is required: degradation must be auditable (invariant I3)"
            )


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    verdict: Verdict
    rationale: str
    """Human-readable. Shown verbatim in the approval prompt, so write it for a person."""

    matched_scopes: tuple[str, ...] = ()
    denied_by: str | None = None

    @property
    def permitted(self) -> bool:
        return self.verdict == "allow"


@dataclass(frozen=True, slots=True)
class ReversalOutcome:
    attempted: bool
    succeeded: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Outcome:
    """What the broker hands back to the planner."""

    status: Literal["ok", "denied", "failed", "reconciliation_required", "dry_run"]
    invocation: Invocation
    decision: AdmissionDecision
    observed: OracleResult | None = None
    reversal: ReversalOutcome | None = None
    checkpoint_id: CheckpointId | None = None
    predicted: dict[str, Any] | None = None
    """Populated for dry runs: what *would* have happened."""

    result: Any = None
    """Whatever the tier returned -- file contents, a directory listing, stdout.

    **Untrusted.** This is data read from the world and it reaches the planner's
    context, so it is a prompt-injection carrier. Nothing derived from it may
    widen a scope.
    """

    error: str = ""

    @property
    def halted(self) -> bool:
        """True when the task must stop. Do not retry, do not improvise."""
        return self.status == "reconciliation_required"


@dataclass(frozen=True, slots=True)
class ProvenanceRecord:
    """One entry in the hash-chained audit log."""

    seq: int
    prev_hash: str
    ts: datetime
    invocation: Invocation
    decision: AdmissionDecision
    checkpoint_id: CheckpointId | None
    observed: OracleResult | None
    reversal: ReversalOutcome | None
    status: str

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)
