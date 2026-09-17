"""L1 app adapter -- hand a file to the system's default handler.

The missing verb. Without it the agent can find your image and then has nothing
to do with it: `fs.read` returns bytes, `proc.spawn` needs a specific binary,
and neither is "show me this".

Classification, and why it is not `IRREVERSIBLE`
------------------------------------------------

`proc.spawn` is irreversible because it runs an arbitrary program with arbitrary
arguments and this runtime has no model of what it did. Opening a file is
narrower: it is what double-clicking does, the handler is chosen by the OS, and
the usual outcome is a viewer that changes nothing.

So this declares the file as a target with a hash oracle expecting **no change**,
which makes it `REVERSIBLE`: if the handler rewrites the file on open, the
runtime notices and puts it back.

**The honest limit.** Verification happens at launch. A viewer that autosaves
five minutes later is outside this runtime's control entirely -- the process
outlives the action. `app.open` is therefore a capability you grant
deliberately (it is not in the default scope set), not one that is inferred.

Treating "show me this image" with the same ceremony as "send this email" would
be miscalibrated, and miscalibrated prompts are their own security failure: they
teach people to click through.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from ...oracles import FileHashOracle
from ...types import ActionRequest, EffectClass, EffectContract, Grant, Invocation, Tier
from ..base import CapabilityManifest, OperationUnsupported, Preparation

__all__ = ["AppAdapter", "default_handler"]


def default_handler() -> tuple[str, ...]:
    """The platform's "open this with whatever is registered" command."""
    if sys.platform == "win32":
        # cmd's `start` is a builtin, hence the shell hop. The empty "" is the
        # window title, without which a quoted path is read as the title.
        return ("cmd", "/c", "start", "")
    if sys.platform == "darwin":
        return ("open",)
    return ("xdg-open",)


class AppAdapter:
    """Open a file in whatever application the OS has registered for it."""

    manifest = CapabilityManifest(
        adapter="l1.app",
        tier=Tier.L1_SYSTEM,
        operations=("app.open",),
        summary="Open a file with the system default application",
    )

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def prepare(self, request: ActionRequest) -> Preparation:
        if request.operation != "app.open":
            raise OperationUnsupported(request.operation)

        raw = request.params.get("path")
        if not raw:
            raise OperationUnsupported("app.open requires 'path'")
        path = Path(str(raw)).expanduser().resolve()

        handler = default_handler()
        if not shutil.which(handler[0]):
            raise OperationUnsupported(
                f"no handler available on this platform ({handler[0]} not found)"
            )

        def execute(_: Invocation) -> dict[str, object]:
            if not path.exists():
                raise FileNotFoundError(f"{path} does not exist")
            if sys.platform == "win32":
                # Detached, so the agent is not held open by a GUI app.
                subprocess.Popen(
                    [*handler, str(path)],
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                    close_fds=True,
                )
            else:
                subprocess.Popen(
                    [*handler, str(path)],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            return {"opened": str(path), "handler": handler[0]}

        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.REVERSIBLE,
                # Declared so the file is checkpointed: if the handler rewrites
                # it on open, that is caught and undone.
                targets=(path,),
                oracle=FileHashOracle((path,)),
                expect=f"open {path.name} in the default application; the file is unchanged",
                # Success is the file NOT moving. Without this the broker
                # would read "nothing changed" as a failed effect and
                # roll back a perfectly good open.
                expects_change=False,
            ),
            execute=execute,
            grants=(Grant("app.open", str(path)),),
        )
