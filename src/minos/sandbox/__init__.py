"""The sandbox: a scratchpad for code the runtime has no typed tool for.

Sixteen milestones bought 22 operations, because every one of them cost an
adapter, a contract, an oracle and a reversal path. That cost never amortizes,
and "do almost anything" is not a list you can finish.

So this is the other move: one capability that generates its own specifics at
runtime. The model writes a script, the script runs here against copies, and
what it produced becomes a *retroactive* effect contract that the broker
checkpoints and verifies like any other. The computation is unverified; the
effect on the real machine is not.

What contains the script depends on where the code came from. `origin` holds
that policy: code the local planner wrote for this user's own task runs in the
confined subprocess of `runner`, and code from anywhere else runs in the
container of `container`, which does not share a namespace with the user. See
docs/PLAN-GENERALITY.md for the argument, `confine` for what each platform's
kernel will actually enforce, and SECURITY.md for what none of it stops.
"""

from __future__ import annotations

from .confine import Confinement
from .confine import detect as detect_confinement
from .container import ContainerSandbox, ContainerUnavailable
from .origin import BackendChoice, CodeOrigin, select_backend
from .packages import (
    RECOMMENDED,
    PackageStatus,
    available_packages,
    describe_for_planner,
    missing_import,
    survey,
)
from .runner import (
    CodeResult,
    SandboxBackend,
    SubprocessSandbox,
    Unconfined,
    scrubbed_environment,
)
from .workspace import Artifact, Workspace

__all__ = [
    "RECOMMENDED",
    "Artifact",
    "BackendChoice",
    "CodeOrigin",
    "CodeResult",
    "Confinement",
    "ContainerSandbox",
    "ContainerUnavailable",
    "PackageStatus",
    "SandboxBackend",
    "SubprocessSandbox",
    "Unconfined",
    "Workspace",
    "available_packages",
    "describe_for_planner",
    "detect_confinement",
    "missing_import",
    "scrubbed_environment",
    "select_backend",
    "survey",
]
