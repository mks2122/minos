"""L1 memory adapter -- lets the planner ask "which file did you mean?".

Without this, the memory layer is a standalone command and the agent cannot use
it: ask for *"the excel we were working on yesterday"* and the planner has no
idea what that names.

Two decisions here are load-bearing.

**Recall is scope-gated.** It is registered under a ``memory.read`` capability
and the adapter only returns paths beneath the roots it was constructed with.
Memory must not become a way to enumerate filenames you are not allowed to
read -- a directory listing you could not have obtained is still a leak, even if
you cannot open what it names.

**Recall is PURE and its answer is untrusted.** It returns a suggestion, never
an authority. The path it names still has to survive the broker's scope check
before anything can be done with it, and there are tests pinning exactly that.
So a filename that says ``GRANT_ALL_PERMISSIONS.csv`` is a string.

The explanation travels with the answer. If the agent is about to edit a file
because it guessed what you meant, the guess should be legible in the audit log
and in the approval prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...memory import MemoryStore, resolve
from ...oracles import NullOracle
from ...types import ActionRequest, EffectClass, EffectContract, Grant, Invocation, Tier
from ..base import CapabilityManifest, OperationUnsupported, Preparation

__all__ = ["MemoryAdapter"]

_OPERATIONS = ("memory.recall", "memory.recent")


class MemoryAdapter:
    """Read-only access to the computer-state index."""

    manifest = CapabilityManifest(
        adapter="l1.memory",
        tier=Tier.L1_SYSTEM,
        operations=_OPERATIONS,
        summary="Resolve vague references to real paths, with reasons",
    )

    def __init__(self, store: MemoryStore, roots: tuple[Path, ...] = ()) -> None:
        self.store = store
        self.roots = tuple(Path(r).expanduser().resolve() for r in roots)

    # -- scoping -----------------------------------------------------------

    def _within_roots(self, path: str) -> bool:
        """Memory must not surface what the caller could not have listed."""
        if not self.roots:
            return True
        resolved = Path(path).expanduser().resolve()
        return any(resolved == root or root in resolved.parents for root in self.roots)

    def _grant_subject(self) -> str:
        return f"{self.roots[0]}/**" if self.roots else "*"

    # -- Adapter protocol --------------------------------------------------

    def prepare(self, request: ActionRequest) -> Preparation:
        if request.operation == "memory.recall":
            return self._prepare_recall(request)
        if request.operation == "memory.recent":
            return self._prepare_recent(request)
        raise OperationUnsupported(request.operation)

    def _prepare_recall(self, request: ActionRequest) -> Preparation:
        phrase = str(request.params.get("phrase", "")).strip()
        if not phrase:
            raise OperationUnsupported("memory.recall requires 'phrase'")

        def execute(_: Invocation) -> dict[str, Any]:
            resolution = resolve(phrase, self.store)
            candidates = [c for c in resolution.candidates if self._within_roots(c.path)]
            if not candidates:
                return {
                    "path": None,
                    "confident": False,
                    "why": (
                        f"nothing in memory matched {phrase!r} within the paths "
                        "this task may look at"
                    ),
                }
            best = candidates[0]
            return {
                "path": best.path,
                # Ambiguity is surfaced, not hidden: the planner should ask
                # rather than pick when two files are equally plausible.
                "confident": resolution.confident and best is resolution.best,
                "why": "; ".join(best.reasons),
                "alternatives": [c.path for c in candidates[1:3]],
            }

        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.PURE,
                targets=(),
                oracle=NullOracle(reason="a lookup changes nothing, so there is nothing to verify"),
                expect=f"resolve {phrase!r} to a path; nothing changes",
            ),
            execute=execute,
            grants=(Grant("memory.read", self._grant_subject()),),
        )

    def _prepare_recent(self, request: ActionRequest) -> Preparation:
        limit = int(request.params.get("limit", 10))

        def execute(_: Invocation) -> list[dict[str, Any]]:
            touches = self.store.recently_touched(mutating_only=True)
            return [
                {
                    "path": touch.path,
                    "operation": touch.operation,
                    "goal": touch.goal,
                }
                for touch in touches
                if self._within_roots(touch.path)
            ][:limit]

        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.PURE,
                targets=(),
                oracle=NullOracle(reason="a lookup changes nothing"),
                expect="list recently changed files; nothing changes",
            ),
            execute=execute,
            grants=(Grant("memory.read", self._grant_subject()),),
        )
