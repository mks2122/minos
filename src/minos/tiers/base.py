"""Adapter contract shared by every tier.

An adapter turns an :class:`~minos.types.ActionRequest` into a
:class:`Preparation`: the effect contract that declares what will change, the
exact grants required, and a callable that performs it.

Adapters are **trusted** (see SECURITY.md). They are the component that decides
an effect's class and declares its targets, and the broker relies on both being
honest. Vendor deliberately, and review what you vendor.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..types import ActionRequest, EffectContract, Grant, Invocation, Tier

__all__ = [
    "Adapter",
    "CapabilityManifest",
    "OperationUnsupported",
    "Preparation",
    "current_platform",
]

ANY_PLATFORM = "*"


def current_platform() -> str:
    """``linux``, ``darwin`` or ``win32``."""
    return sys.platform


class OperationUnsupported(Exception):
    """This adapter cannot prepare that request. Try the next tier."""


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    """What an adapter can do, and where.

    The router filters by platform, so an adapter that is unavailable on the
    current OS is an ordinary degradation with a recorded reason rather than a
    special case. Platform gaps are absorbed by a mechanism the architecture
    already has.
    """

    adapter: str
    tier: Tier
    operations: tuple[str, ...]
    platforms: tuple[str, ...] = (ANY_PLATFORM,)
    summary: str = ""

    def available_here(self) -> bool:
        return ANY_PLATFORM in self.platforms or current_platform() in self.platforms

    def handles(self, operation: str) -> bool:
        return operation in self.operations


@dataclass(frozen=True, slots=True)
class Preparation:
    """An adapter's answer: what will change, what it needs, and how to do it."""

    contract: EffectContract
    execute: Callable[[Invocation], Any]
    grants: tuple[Grant, ...] = field(default_factory=tuple)


@runtime_checkable
class Adapter(Protocol):
    manifest: CapabilityManifest

    def prepare(self, request: ActionRequest) -> Preparation:
        """Raise :class:`OperationUnsupported` to decline; the router falls onward."""
        ...
