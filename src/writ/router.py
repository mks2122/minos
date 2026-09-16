"""Tier router.

Preference order is always L1 -> L2 -> L3: native tools, then typed application
adapters, then pixels. The router asks each adapter in that order and takes the
first that can prepare the request.

Invariant I3 lives here. Every routing decision records *why* that tier and not
a preferred one -- which adapters were tried, and what was missing. A runtime
that quietly drifts into clicking everything has failed without telling anyone,
so fallback rate is a first-class metric rather than a log line.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .capabilities import capability_for
from .tiers.base import (
    Adapter,
    CapabilityManifest,
    OperationUnsupported,
    Preparation,
    current_platform,
)
from .types import ActionRequest, Invocation, Tier

__all__ = ["NoAdapter", "Routed", "Router", "RoutingStats"]

_TIER_ORDER = {Tier.L1_SYSTEM: 0, Tier.L2_ADAPTER: 1, Tier.L3_GUI: 2}


class NoAdapter(Exception):
    """No adapter on any tier could prepare this request."""


@dataclass(frozen=True, slots=True)
class Routed:
    invocation: Invocation
    preparation: Preparation

    @property
    def execute(self) -> Callable[[Invocation], Any]:
        return self.preparation.execute


@dataclass
class RoutingStats:
    """Published in eval output and the README, not buried in a log."""

    routed: dict[Tier, int] = field(default_factory=dict)

    def record(self, tier: Tier) -> None:
        self.routed[tier] = self.routed.get(tier, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.routed.values())

    @property
    def fallback_rate(self) -> float:
        """Share of actions that needed something worse than a native tool.

        Rising fallback rate means the adapter ecosystem is losing to the long
        tail. That is something the project needs to know publicly.
        """
        if not self.total:
            return 0.0
        native = self.routed.get(Tier.L1_SYSTEM, 0)
        return (self.total - native) / self.total

    @property
    def gui_rate(self) -> float:
        if not self.total:
            return 0.0
        return self.routed.get(Tier.L3_GUI, 0) / self.total

    def summary(self) -> dict[str, float | int]:
        return {
            "total": self.total,
            "L1": self.routed.get(Tier.L1_SYSTEM, 0),
            "L2": self.routed.get(Tier.L2_ADAPTER, 0),
            "L3": self.routed.get(Tier.L3_GUI, 0),
            "fallback_rate": round(self.fallback_rate, 4),
            "gui_rate": round(self.gui_rate, 4),
        }


@dataclass
class Router:
    adapters: tuple[Adapter, ...] = ()
    stats: RoutingStats = field(default_factory=RoutingStats)

    def __post_init__(self) -> None:
        self.adapters = tuple(sorted(self.adapters, key=lambda a: _TIER_ORDER[a.manifest.tier]))

    def route(self, request: ActionRequest) -> Routed:
        if capability_for(request.operation) is None:
            raise NoAdapter(
                f"operation {request.operation!r} is not registered in writ.capabilities"
            )

        attempted: list[str] = []
        skipped_platform: list[str] = []

        for adapter in self.adapters:
            manifest = adapter.manifest
            if not manifest.handles(request.operation):
                continue
            if not manifest.available_here():
                skipped_platform.append(
                    f"{manifest.adapter} (not available on {current_platform()})"
                )
                continue

            try:
                preparation = adapter.prepare(request)
            except OperationUnsupported as exc:
                attempted.append(f"{manifest.adapter} declined: {exc}")
                continue

            invocation = Invocation(
                request=request,
                tier=manifest.tier,
                adapter=manifest.adapter,
                tier_reason=self._reason(manifest, attempted, skipped_platform),
                contract=preparation.contract,
                grants=preparation.grants,
            )
            self.stats.record(manifest.tier)
            return Routed(invocation=invocation, preparation=preparation)

        detail = "; ".join(attempted + skipped_platform) or "no adapter declares it"
        raise NoAdapter(f"no adapter could prepare {request.operation!r}: {detail}")

    def _reason(
        self,
        manifest: CapabilityManifest,
        attempted: list[str],
        skipped_platform: list[str],
    ) -> str:
        """I3: say why this tier, and what was missing above it."""
        if manifest.tier is Tier.L1_SYSTEM and not attempted and not skipped_platform:
            return f"{manifest.adapter} handles {manifest.tier} natively; no fallback needed"

        parts: list[str] = []
        if attempted:
            parts.append("tried " + ", ".join(attempted))
        if skipped_platform:
            parts.append("skipped " + ", ".join(skipped_platform))
        parts.append(f"fell back to {manifest.adapter} at {manifest.tier}")
        return "; ".join(parts)
