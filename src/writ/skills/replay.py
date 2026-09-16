"""Replaying a skill.

:class:`SkillPlanner` satisfies the ordinary ``Planner`` protocol, so a replayed
skill travels exactly the same path as a model-planned action: routed, admitted,
checkpointed, executed, verified, recorded. Nothing about caching is privileged.
That is invariant I2 applied to time as well as to tiers.

Drift detection is the part that keeps this honest. A skill records what the
world looked like before each original step; before replaying that step, the
runtime looks again. If the world moved, the cached procedure is not applicable
and the planner says so rather than proceeding into a shape it was not verified
against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..planner.base import Done, Observation, Planner, Step
from ..scopes import ScopeSet, ScopeViolation
from .skill import Binding, Skill

__all__ = ["DriftDetected", "SkillPlanner", "check_drift"]


class DriftDetected(Exception):
    """The world no longer matches what the skill was verified against."""


def check_drift(expected: dict[str, Any], actual: dict[str, Any]) -> tuple[bool, str]:
    """Compare a recorded precondition against the world now.

    Only keys the skill actually recorded are compared. New keys appearing is
    not drift -- the world having more in it than last time is normal.
    """
    if not expected:
        return False, ""
    differences = [
        f"{key}: expected {expected[key]!r}, found {actual.get(key)!r}"
        for key in expected
        if actual.get(key) != expected[key]
    ]
    if differences:
        return True, "; ".join(differences[:3])
    return False, ""


@dataclass
class SkillPlanner:
    """Replays a verified skill. Makes no model call.

    ``fallback`` is handed control when the skill cannot be applied -- drift,
    a missing binding, or a step that failed. Falling back to real planning is
    the correct response to a stale cache; pressing on is not.
    """

    skill: Skill
    bindings: Binding
    fallback: Planner | None = None
    strict_drift: bool = True
    _index: int = field(default=0, init=False)
    _abandoned: str = field(default="", init=False)

    def __post_init__(self) -> None:
        missing = self.skill.missing_bindings(self.bindings)
        if missing:
            raise ValueError(f"skill {self.skill.name!r} needs bindings for: {', '.join(missing)}")

    def narrowed_scopes(self, task_scopes: ScopeSet) -> ScopeSet:
        """The scopes replay runs under: the skill's, verified as a subset.

        A cached procedure must not be able to reach anywhere the live agent
        could not. Widening raises rather than silently clamping, because a
        skill asking for more than its caller has is a bug worth seeing.
        """
        declared = ScopeSet.parse(list(self.skill.rendered_scopes(self.bindings)))
        return task_scopes.narrowed(declared)

    # -- Planner protocol --------------------------------------------------

    def next_action(self, goal: str, observations: list[Observation], scopes: ScopeSet) -> Step:
        if self._abandoned:
            return self._delegate(goal, observations, scopes)

        # A failed step means the recording no longer describes reality.
        if observations and observations[-1].status not in ("ok", "dry_run"):
            self._abandoned = (
                f"step {self._index} of skill {self.skill.name!r} did not succeed: "
                f"{observations[-1].status}"
            )
            return self._delegate(goal, observations, scopes)

        if self._index >= len(self.skill.steps):
            return Done(
                summary=(
                    f"replayed {len(self.skill.steps)} verified step(s) from skill "
                    f"{self.skill.name!r} with no model call"
                ),
                succeeded=True,
            )

        step = self.skill.steps[self._index]
        self._index += 1
        return step.render(self.bindings)

    # -- drift -------------------------------------------------------------

    def drift_check(self, observed_before: dict[str, Any]) -> tuple[bool, str]:
        """Called by a caller that can observe before replaying a step."""
        if self._index >= len(self.skill.steps):
            return False, ""
        expected = self.skill.steps[self._index].precondition
        from .skill import render

        rendered = {render(k, self.bindings): render(v, self.bindings) for k, v in expected.items()}
        return check_drift(rendered, observed_before)

    def abandon(self, reason: str) -> None:
        self._abandoned = reason

    @property
    def abandoned(self) -> str:
        return self._abandoned

    def _delegate(self, goal: str, observations: list[Observation], scopes: ScopeSet) -> Step:
        if self.fallback is None:
            return Done(summary=f"skill abandoned: {self._abandoned}", succeeded=False)
        return self.fallback.next_action(goal, observations, scopes)


def replay_is_safe(skill: Skill, task_scopes: ScopeSet, bindings: Binding) -> str | None:
    """Reason this skill must not be replayed here, or ``None``.

    Checked before replay so the refusal is legible, rather than surfacing as a
    denial three steps in.
    """
    missing = skill.missing_bindings(bindings)
    if missing:
        return f"missing bindings: {', '.join(missing)}"
    try:
        task_scopes.narrowed(ScopeSet.parse(list(skill.rendered_scopes(bindings))))
    except ScopeViolation as exc:
        return str(exc)
    return None
