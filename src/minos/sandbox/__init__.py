"""The sandbox: a scratchpad for code the runtime has no typed tool for.

Sixteen milestones bought 22 operations, because every one of them cost an
adapter, a contract, an oracle and a reversal path. That cost never amortizes,
and "do almost anything" is not a list you can finish.

So this is the other move: one capability that generates its own specifics at
runtime. The model writes a script, the script runs here against copies, and
what it produced becomes a *retroactive* effect contract that the broker
checkpoints and verifies like any other. The computation is unverified; the
effect on the real machine is not.

See docs/PLAN-GENERALITY.md for the argument, and `runner` for what the jail
does and does not enforce.
"""

from __future__ import annotations

from .packages import (
    RECOMMENDED,
    PackageStatus,
    available_packages,
    describe_for_planner,
    missing_import,
    survey,
)
from .runner import CodeResult, SandboxBackend, SubprocessSandbox, scrubbed_environment
from .workspace import Artifact, Workspace

__all__ = [
    "RECOMMENDED",
    "Artifact",
    "CodeResult",
    "PackageStatus",
    "SandboxBackend",
    "SubprocessSandbox",
    "Workspace",
    "available_packages",
    "describe_for_planner",
    "missing_import",
    "scrubbed_environment",
    "survey",
]
