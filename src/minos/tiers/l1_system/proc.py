"""L1 process adapter.

Every spawn is classified **IRREVERSIBLE**, and that is not conservatism for its
own sake: once control passes to an external binary, the runtime has no model of
what it did. It may have written files nobody declared, sent a request, or
mutated a database. There is no honest contract to declare and no oracle that
can read back "whatever that program decided to do".

So spawning always prompts. The alternative -- guessing at an effect class --
would put a confident-looking entry in the audit log that nobody should believe.

The route to *not* prompting for routine work is a typed L2 adapter for the
specific application, which can declare real targets and a real oracle. That is
the whole argument for preferring L2 over shelling out.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from ...oracles import NullOracle
from ...types import ActionRequest, EffectClass, EffectContract, Grant, Invocation, Tier
from ..base import CapabilityManifest, OperationUnsupported, Preparation

__all__ = ["ProcessAdapter", "ProcessResult"]


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    """All three are **untrusted**: a subprocess's output can carry injected
    instructions straight into the planner's context."""


class ProcessAdapter:
    manifest = CapabilityManifest(
        adapter="l1.proc",
        tier=Tier.L1_SYSTEM,
        operations=("proc.spawn",),
        summary="Run an external program. Always irreversible, always prompts.",
    )

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout

    def prepare(self, request: ActionRequest) -> Preparation:
        if request.operation != "proc.spawn":
            raise OperationUnsupported(request.operation)

        command = request.params.get("command")
        if not command:
            raise OperationUnsupported("proc.spawn requires 'command'")

        # Resolve to an absolute program path so the scope matches what will
        # actually run, not what PATH happened to mean at check time.
        resolved = shutil.which(str(command)) or str(command)
        args = [str(a) for a in request.params.get("args", [])]
        cwd = request.params.get("cwd")
        timeout = float(request.params.get("timeout", self.timeout))

        def execute(_: Invocation) -> ProcessResult:
            completed = subprocess.run(
                [resolved, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                check=False,
                shell=False,
            )
            return ProcessResult(
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

        printable = " ".join([resolved, *args])
        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.IRREVERSIBLE,
                targets=(),
                # Honest: we cannot read back what an arbitrary program did.
                oracle=NullOracle(
                    reason="an external program's effects are not observable to this runtime"
                ),
                expect=f"runs: {printable}",
            ),
            execute=execute,
            grants=(Grant("proc.spawn", resolved),),
        )
