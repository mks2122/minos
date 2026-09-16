"""Skills: verified procedures promoted from trajectories.

What makes these different from every other agent skill library is not the
loop -- that is the field's 2026 default and ships free elsewhere. It is that a
skill here **carries its own capability manifest and its own effect oracles**.
It is not a blob of remembered steps; it is a scoped, verifiable procedure.

Three consequences follow, and they are the whole point:

* Replay runs under the skill's declared scopes, which must be a *subset* of the
  task's. A cached procedure cannot quietly do more than the live agent could.
* Every replayed action is re-verified by its oracle. Past success is not
  evidence of present success -- the world moved on in between.
* ``IRREVERSIBLE`` steps re-prompt on **every** replay. The point of caching is
  to skip the thinking, not the consent.

The payoff: a task that succeeded once as N stochastic steps becomes N
deterministic ones whose reliability no longer depends on a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..types import ActionRequest, EffectClass

__all__ = ["Binding", "Skill", "SkillStep", "render"]

_PARAM_RE = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

Binding = dict[str, str]


@dataclass(frozen=True, slots=True)
class SkillStep:
    """One action, parameterised, with what it expected to be true.

    ``precondition`` is the oracle's observation *before* the original action,
    with parameters substituted out. On replay it is what drift is measured
    against: if the world does not look the way it looked last time, the cached
    procedure is not applicable and must not run.
    """

    operation: str
    params: dict[str, Any]
    effect_class: EffectClass
    expect: str = ""
    precondition: dict[str, Any] = field(default_factory=dict)
    oracle_kind: str = ""

    @property
    def needs_approval(self) -> bool:
        return self.effect_class is EffectClass.IRREVERSIBLE

    def render(self, bindings: Binding) -> ActionRequest:
        return ActionRequest(
            goal_id="skill",
            intent=f"{self.operation} (replayed from a verified skill)",
            operation=self.operation,
            params={k: render(v, bindings) for k, v in self.params.items()},
        )


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    steps: tuple[SkillStep, ...]
    parameters: tuple[str, ...] = ()
    """Names that must be supplied at replay time, e.g. ``("workspace",)``."""

    scopes: tuple[str, ...] = ()
    """Parameterised scopes. Replay narrows the task's scopes to these, never
    widens them."""

    source_goal: str = ""
    verified_at: str = ""
    runs: int = 0
    failures: int = 0

    @property
    def requires_approval(self) -> bool:
        return any(step.needs_approval for step in self.steps)

    @property
    def deterministic(self) -> bool:
        """True when replay makes no model call at all."""
        return bool(self.steps)

    def missing_bindings(self, bindings: Binding) -> tuple[str, ...]:
        return tuple(p for p in self.parameters if p not in bindings)

    def rendered_scopes(self, bindings: Binding) -> tuple[str, ...]:
        return tuple(render(scope, bindings) for scope in self.scopes)

    def render_steps(self, bindings: Binding) -> tuple[ActionRequest, ...]:
        return tuple(step.render(bindings) for step in self.steps)


def render(value: Any, bindings: Binding) -> Any:
    """Substitute ``${name}`` placeholders.

    Recurses through lists and dicts so a parameter inside a nested structure
    is still substituted. An unbound placeholder is left intact rather than
    rendered as empty: a half-substituted path is worse than an obvious one.
    """
    if isinstance(value, str):
        return _PARAM_RE.sub(lambda m: bindings.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [render(v, bindings) for v in value]
    if isinstance(value, tuple):
        return tuple(render(v, bindings) for v in value)
    if isinstance(value, dict):
        return {k: render(v, bindings) for k, v in value.items()}
    return value


def parameters_in(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(_PARAM_RE.findall(value))
    if isinstance(value, (list, tuple)):
        return set().union(*(parameters_in(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return set().union(*(parameters_in(v) for v in value.values())) if value else set()
    return set()
