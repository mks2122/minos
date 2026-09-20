"""Promoting a trajectory into a skill.

The guardrails here are the reason this is worth doing at all. Published work
found that naive experience accumulation can make an agent *worse* -- one
framework degraded when failure traces were added to its library -- so what a
system refuses to promote matters more than what it promotes.

Refused:

* trajectories that did not succeed
* trajectories containing an **unverified** effect. Replaying something we could
  not confirm the first time is exactly how a cache becomes a liability
* trajectories with no admitted actions
* trajectories that halted with ``reconciliation_required`` -- the runtime lost
  track of state, so nothing it did is trustworthy enough to repeat
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..planner.base import Trajectory
from ..types import EffectClass, Outcome
from .skill import Skill, SkillStep, parameters_in

__all__ = ["PromotionRefused", "promote", "why_not_promotable"]


class PromotionRefused(Exception):
    """This trajectory must not become a skill."""


@dataclass(frozen=True, slots=True)
class _Substitution:
    name: str
    literal: str

    def apply(self, value: Any) -> Any:
        if isinstance(value, str) and self.literal and self.literal in value:
            return value.replace(self.literal, f"${{{self.name}}}")
        if isinstance(value, list):
            return [self.apply(v) for v in value]
        if isinstance(value, dict):
            return {k: self.apply(v) for k, v in value.items()}
        return value


def why_not_promotable(trajectory: Trajectory) -> str | None:
    """The reason this trajectory cannot become a skill, or ``None``."""
    admitted = [o for o in trajectory.outcomes if o.status == "ok"]

    if any(o.halted for o in trajectory.outcomes):
        return (
            "the run halted with reconciliation_required: the runtime lost track "
            "of state, so nothing it did is trustworthy enough to repeat"
        )
    if not trajectory.succeeded:
        return "the run did not succeed"
    if not admitted:
        return "no action was admitted, so there is nothing to replay"

    unverified = [
        o
        for o in admitted
        if o.observed is None or not o.observed.verifiable or not o.observed.matched
    ]
    if unverified:
        operations = ", ".join(o.invocation.request.operation for o in unverified[:3])
        return (
            f"{len(unverified)} step(s) were not verified against a system of "
            f"record ({operations}); replaying an unconfirmed effect is how a "
            "cache becomes a liability"
        )
    return None


def promote(
    trajectory: Trajectory,
    *,
    name: str,
    description: str = "",
    parameters: dict[str, str] | None = None,
    workspace: Path | str | None = None,
) -> Skill:
    """Turn a verified trajectory into a replayable skill.

    ``parameters`` maps a placeholder name to the literal it replaces, and
    ``workspace`` is sugar for the common case of parameterising a root
    directory. Substitution is **explicit** rather than inferred: guessing which
    literals were incidental is how a skill silently generalises into something
    nobody agreed to.
    """
    refusal = why_not_promotable(trajectory)
    if refusal:
        raise PromotionRefused(refusal)

    substitutions: list[_Substitution] = []
    if workspace is not None:
        substitutions.append(
            _Substitution("workspace", str(Path(workspace).expanduser().resolve()))
        )
    substitutions.extend(_Substitution(key, value) for key, value in (parameters or {}).items())

    steps: list[SkillStep] = []
    scopes: set[str] = set()

    for outcome in trajectory.outcomes:
        if outcome.status != "ok":
            continue
        steps.append(_step(outcome, substitutions))
        for grant in outcome.invocation.grants:
            subject = _substitute(grant.subject, substitutions)
            scopes.add(f"{grant.capability}:{subject}")

    declared = set()
    for step in steps:
        declared |= parameters_in(step.params)
    for scope in scopes:
        declared |= parameters_in(scope)

    return Skill(
        name=name,
        description=description or trajectory.goal,
        steps=tuple(steps),
        parameters=tuple(sorted(declared)),
        scopes=tuple(sorted(scopes)),
        source_goal=trajectory.goal,
        verified_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def _step(outcome: Outcome, substitutions: list[_Substitution]) -> SkillStep:
    invocation = outcome.invocation
    contract = invocation.contract
    observed = outcome.observed
    return SkillStep(
        operation=invocation.request.operation,
        params=_substitute(dict(invocation.request.params), substitutions),
        effect_class=EffectClass(str(contract.effect_class)),
        expect=_substitute(contract.expect, substitutions),
        # The world as it looked before the original action. Drift is measured
        # against this, so it is parameterised like everything else.
        precondition=_substitute(dict(observed.before) if observed else {}, substitutions),
        oracle_kind=observed.kind if observed else "",
    )


def _substitute(value: Any, substitutions: list[_Substitution]) -> Any:
    for substitution in substitutions:
        value = substitution.apply(value)
    return value
