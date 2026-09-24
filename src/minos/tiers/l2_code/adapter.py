"""L2.5 — the code tier.

Two operations, and the split between them is the entire safety argument.

``code.run``
    Runs a script in the sandbox. Touches nothing outside the workspace, so it
    declares no targets on the real machine and needs no checkpoint. It is
    unverifiable by construction — no oracle can read back "whatever that
    program decided to compute" — and that is *fine*, because it cannot reach
    anything the user owns.

``code.materialize``
    Copies one artifact out of the sandbox and onto the real filesystem. This is
    an ordinary `fs.write`: it declares its target, takes a checkpoint, and
    verifies with a hash oracle. It consumes the ``fs.write`` capability, so the
    scopes the user already granted govern it with no new flag to learn.

**Why this works.** The broker's contract is declare-then-verify, and arbitrary
code cannot declare its targets up front. Running it in the sandbox first turns
that around: by the time anything touches the real machine, the artifact set is
known exactly, so the contract is precise and the reversal is real. The sandbox
is what makes a retroactive contract possible.

The code is written by the planner, and **the planner is untrusted**. Nothing
here trusts the script's own account of what it did; the artifact set is read
off the filesystem, and every path a materialize request names is resolved
against the output directory and refused if it escapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from ...oracles import FileHashOracle, NullOracle
from ...sandbox import (
    BackendChoice,
    CodeOrigin,
    CodeResult,
    SandboxBackend,
    Workspace,
    missing_import,
    select_backend,
)
from ...types import ActionRequest, EffectClass, EffectContract, Grant, Invocation, Tier
from ..base import CapabilityManifest, OperationUnsupported, Preparation

__all__ = ["CodeAdapter"]


@dataclass
class CodeAdapter:
    """Runs code in a sandbox, and promotes what it produced.

    One workspace per adapter instance, which is one per run. Artifacts persist
    across steps within a run — a script writes a file, a later step promotes it
    — and vanish with the workspace.

    **What contains the script is decided by where it came from**, not by a
    setting someone tuned once. ``origin`` is the input to that decision and
    :func:`minos.sandbox.select_backend` is the policy; the chosen backend and
    the confinement it actually got are written into the effect contract, so the
    audit log records which of them ran this step rather than leaving a reader
    to assume the strongest one.
    """

    state: Path
    origin: CodeOrigin = CodeOrigin.LOCAL_PLANNER
    prefer: str = "auto"
    """``auto`` applies the origin policy; ``subprocess`` or ``container`` override it."""
    allow_downgrade: bool = False
    """Whether untrusted code may run in the subprocess jail when no container
    engine exists. Refusing is the default, because silently weakening the
    containment of code you do not trust is the failure this is here to stop."""
    backend: SandboxBackend | None = None
    """Set explicitly to bypass the policy entirely — mostly for tests."""
    timeout: float = 60.0
    _workspace: Workspace | None = field(default=None, init=False, repr=False)
    _choice: BackendChoice | None = field(default=None, init=False, repr=False)

    @property
    def sandbox(self) -> SandboxBackend:
        """The backend, chosen once per adapter and then kept.

        Chosen lazily so that constructing an adapter cannot fail: an untrusted
        origin with no container engine raises, and that belongs at the first
        ``code.run``, where it can be reported as a refused action, rather than
        at start-up where it would stop the whole runtime.
        """
        backend = self.backend
        if backend is None:
            self._choice = select_backend(
                self.origin,
                prefer=self.prefer,
                allow_downgrade=self.allow_downgrade,
                default_timeout=self.timeout,
            )
            backend = cast(SandboxBackend, self._choice.backend)
            self.backend = backend
        return backend

    def describe_containment(self) -> str:
        """One line for ``doctor``, the trace, and anyone asking what ran where."""
        backend = self.sandbox
        described = getattr(backend, "describe", None)
        return f"{self.origin.value} -> " + (described() if described else backend.name)

    manifest = CapabilityManifest(
        adapter="l2.code",
        tier=Tier.L2_CODE,
        operations=("code.run", "code.materialize"),
        summary="Write and run code in a sandbox, then promote its output through the broker.",
    )

    @property
    def workspace(self) -> Workspace:
        if self._workspace is None:
            self._workspace = Workspace(Path(self.state) / "sandbox" / "current")
        return self._workspace

    def prepare(self, request: ActionRequest) -> Preparation:
        if request.operation == "code.run":
            return self._prepare_run(request)
        if request.operation == "code.materialize":
            return self._prepare_materialize(request)
        raise OperationUnsupported(request.operation)

    # -- code.run ----------------------------------------------------------

    def _prepare_run(self, request: ActionRequest) -> Preparation:
        code = request.params.get("code")
        if not code or not str(code).strip():
            raise OperationUnsupported("code.run requires 'code'")

        workspace = self.workspace
        materials = [str(m) for m in request.params.get("materials", [])]
        timeout = float(request.params.get("timeout", self.timeout))

        # fs.read on every material: a script must not be handed a file the task
        # was never granted. The copy happens at execute time, but the grant is
        # checked before that, which is the correct order.
        grants = tuple(Grant("fs.read", material) for material in materials)
        grants += (Grant("code.run", str(workspace.root)),)

        def execute(_: Invocation) -> CodeResult:
            for material in materials:
                workspace.add_material(Path(material))
            result: CodeResult = self.sandbox.run(workspace, str(code), timeout=timeout)
            if not result.ok:
                # A traceback is a worse answer than the name of the thing to
                # install, and the runtime knows which import failed.
                wanted = missing_import(result.stderr)
                if wanted is not None:
                    result = replace(
                        result,
                        detail=(
                            f"{wanted.distribution} is not installed in the sandbox "
                            f"({wanted.purpose}). Install it with: {wanted.install_hint}"
                        ),
                    )
            return result

        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.PURE,
                # PURE *with respect to the system of record*. The script writes
                # freely inside the workspace, but the workspace is runtime
                # scratch, not the user's data -- so there is nothing to declare
                # and nothing to check point.
                targets=(),
                # Deliberately unverifiable, and counted as such. No oracle can
                # read back "whatever that program decided to compute", and the
                # project's answer to that has always been to publish the number
                # rather than invent a green tick. The effect that *does* touch
                # the user -- code.materialize -- is fully verified.
                oracle=NullOracle(),
                expect=(
                    f"runs a script in the {self.sandbox.name} sandbox "
                    f"({self.describe_containment()}); its result is unverifiable "
                    "by construction and it reaches nothing outside the workspace"
                ),
            ),
            execute=execute,
            grants=grants,
        )

    # -- code.materialize --------------------------------------------------

    def _prepare_materialize(self, request: ActionRequest) -> Preparation:
        artifact_name = request.params.get("artifact")
        destination = request.params.get("path") or request.params.get("destination")
        if not artifact_name or not destination:
            raise OperationUnsupported("code.materialize requires 'artifact' and 'path'")

        workspace = self.workspace
        try:
            # Resolved here, at prepare time, so a traversal attempt is refused
            # before it can reach the broker wearing a plausible target path.
            source = workspace.resolve_artifact(str(artifact_name))
        except ValueError as exc:
            raise OperationUnsupported(str(exc)) from exc

        target = Path(str(destination)).expanduser().resolve()

        def execute(_: Invocation) -> dict[str, Any]:
            import shutil

            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            return {"materialized": str(target), "from": str(artifact_name)}

        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.REVERSIBLE,
                targets=(target,),
                oracle=FileHashOracle(paths=(target,)),
                expect=f"{artifact_name} written to {target}",
            ),
            execute=execute,
            grants=(Grant("fs.write", str(target)),),
        )
