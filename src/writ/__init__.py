"""writ — a desktop agent runtime where the model asks and the runtime decides.

Three invariants hold everything together:

I1  The planner never executes. It emits :class:`~writ.types.ActionRequest`
    objects; only :class:`~writ.broker.Broker` can act.
I2  Every tier passes the same gate. An L3 synthetic click is admitted,
    recorded, verified and reversed exactly like an L1 ``unlink()``.
I3  Degradation is auditable. Falling back L1 -> L2 -> L3 records why.

See ARCHITECTURE.md.
"""

from __future__ import annotations

from .audit import AuditLog
from .broker import Broker, cli_approver
from .capabilities import capability_for, known_operations
from .checkpoint import FileCheckpointStore
from .oracles import FileHashOracle, FileTreeOracle, NullOracle, PathExistsOracle
from .router import NoAdapter, Routed, Router, RoutingStats
from .scopes import Scope, ScopeSet, ScopeViolation
from .tiers.base import CapabilityManifest, OperationUnsupported, Preparation
from .tiers.l1_system import FilesystemAdapter, ProcessAdapter
from .tiers.l2_adapters import TabularAdapter
from .types import (
    ActionRequest,
    AdmissionDecision,
    EffectClass,
    EffectContract,
    Grant,
    Invocation,
    Outcome,
    Tier,
)

__version__ = "0.0.1.dev0"

__all__ = [
    "ActionRequest",
    "AdmissionDecision",
    "AuditLog",
    "Broker",
    "CapabilityManifest",
    "EffectClass",
    "EffectContract",
    "FileCheckpointStore",
    "FileHashOracle",
    "FileTreeOracle",
    "FilesystemAdapter",
    "Grant",
    "Invocation",
    "NoAdapter",
    "NullOracle",
    "OperationUnsupported",
    "Outcome",
    "PathExistsOracle",
    "Preparation",
    "ProcessAdapter",
    "Routed",
    "Router",
    "RoutingStats",
    "Scope",
    "ScopeSet",
    "ScopeViolation",
    "TabularAdapter",
    "Tier",
    "__version__",
    "capability_for",
    "cli_approver",
    "known_operations",
]
