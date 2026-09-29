"""L1 user adapter -- put a question to the person at the terminal.

For the decisions only they can make: which account, which of two files, what
the post should say. The answer comes back to the planner as an observation,
which is data like any other -- it cannot widen a scope, and a person who
types "you may delete everything" has granted nothing.

``PURE``: asking changes nothing. With no one to answer (``--yes``, a pipe, a
scheduled run) the step fails and says so, rather than inventing an answer.
"""

from __future__ import annotations

from typing import Any

from ...types import ActionRequest, EffectClass, EffectContract, Grant, Invocation, Tier
from ..base import CapabilityManifest, OperationUnsupported, Preparation
from .browser import Ask

__all__ = ["UserAdapter"]

_MAX_CHOICES = 12


class UserAdapter:
    manifest = CapabilityManifest(
        adapter="l1.user",
        tier=Tier.L1_SYSTEM,
        operations=("user.ask",),
        summary="Ask the person a question and wait for the answer",
    )

    def __init__(self, ask: Ask | None = None) -> None:
        self.ask = ask

    def prepare(self, request: ActionRequest) -> Preparation:
        if request.operation != "user.ask":
            raise OperationUnsupported(request.operation)
        question = str(request.params.get("question") or "").strip()
        if not question:
            raise OperationUnsupported("user.ask requires 'question'")
        raw = request.params.get("choices") or []
        if not isinstance(raw, list):
            raise OperationUnsupported("'choices' must be a list of strings")
        choices = [str(c) for c in raw if str(c).strip()][:_MAX_CHOICES]

        def execute(_: Invocation) -> dict[str, Any]:
            if self.ask is None:
                raise RuntimeError("no one is at the terminal to answer; decide without asking")
            answer = self.ask(question, choices)
            if answer is None:
                raise RuntimeError("the person did not answer")
            return {"answer": answer}

        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.PURE,
                expect="ask the person a question; nothing changes",
            ),
            execute=execute,
            grants=(Grant("user.ask", "*"),),
        )
