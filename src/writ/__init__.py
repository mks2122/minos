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
from .checkpoint import FileCheckpointStore
from .oracles import FileHashOracle, FileTreeOracle, NullOracle
from .scopes import Scope, ScopeSet, ScopeViolation
from .types import (
    ActionRequest,
    AdmissionDecision,
    EffectClass,
    EffectContract,
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
    "EffectClass",
    "EffectContract",
    "FileCheckpointStore",
    "FileHashOracle",
    "FileTreeOracle",
    "Invocation",
    "NullOracle",
    "Outcome",
    "Scope",
    "ScopeSet",
    "ScopeViolation",
    "Tier",
    "__version__",
    "cli_approver",
]
