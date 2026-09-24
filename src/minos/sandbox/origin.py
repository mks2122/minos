"""Where the code came from, and therefore what has to contain it.

This is the actual security decision. The sandbox is only the mechanism that
carries it out, and choosing a mechanism without choosing a threat is how a
runtime ends up with one setting that is simultaneously too slow for the common
case and too weak for the dangerous one.

Two questions decide it:

*Who wrote the script?* A local model working on the user's own task produces
code that fails by being **wrong**. A downloaded skill, a shared recipe or a
remote planner someone else's prompt can reach produces code that may fail by
being **hostile**, and hostile code walks around every in-process restriction
the subprocess backend has.

*What does the user still have if it escapes?* Under the subprocess backend:
everything they can reach, because it runs as them. Under the container
backend: the workspace, because there is nothing else in the namespace.

So the policy is one line -- trusted origin gets the fast jail, untrusted origin
gets the container -- and the interesting part is what happens when the
container is not available. :func:`select_backend` does **not** silently fall
back. An untrusted origin with no container engine raises, because quietly
downgrading the containment of code you do not trust is precisely the failure
this module exists to prevent. The caller may pass ``allow_downgrade=True`` and
own that decision explicitly; it is recorded in the returned
:class:`BackendChoice` either way, so the audit log can carry it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = ["BackendChoice", "CodeOrigin", "select_backend"]


class CodeOrigin(StrEnum):
    """Who wrote the script that is about to run.

    A string enum so it survives a round trip through the audit log and a
    config file without a codec.
    """

    LOCAL_PLANNER = "local-planner"
    """A model running on this machine, on a task this user typed."""

    REMOTE_PLANNER = "remote-planner"
    """A hosted model. The task is still the user's, but the code was produced
    somewhere the user does not control and by a context they cannot see."""

    SHARED_SKILL = "shared-skill"
    """A skill promoted from someone else's trajectory, or imported."""

    DOWNLOADED = "downloaded"
    """Arrived from the network. Assume it is trying."""

    @property
    def trusted(self) -> bool:
        """Only the local planner, and only because of what it cannot see.

        Note what this is *not* claiming: not that the local model is aligned,
        not that it is correct. Only that its input is the user's own task
        rather than an attacker's, so the failure mode is a bad script and not a
        targeted one. Everything else in the runtime -- scopes, checkpoints,
        the broker -- is what handles a bad script.
        """
        return self is CodeOrigin.LOCAL_PLANNER


@dataclass(frozen=True, slots=True)
class BackendChoice:
    """The backend, and the reasoning, so both can be printed and recorded."""

    backend: object
    origin: CodeOrigin
    reason: str
    downgraded: bool = False

    def describe(self) -> str:
        prefix = "DOWNGRADED: " if self.downgraded else ""
        return (
            f"{prefix}{self.origin.value} -> {getattr(self.backend, 'name', '?')} ({self.reason})"
        )


def select_backend(
    origin: CodeOrigin = CodeOrigin.LOCAL_PLANNER,
    *,
    prefer: str = "auto",
    allow_downgrade: bool = False,
    **backend_kwargs: Any,
) -> BackendChoice:
    """Pick the containment that matches the origin.

    ``prefer`` overrides the policy with an explicit ``"subprocess"`` or
    ``"container"``; ``"auto"`` applies the policy. An explicit choice of
    ``"subprocess"`` for untrusted code is still a downgrade and is still
    recorded as one -- naming a backend is not the same as arguing the threat
    away.
    """
    from .container import ContainerSandbox, ContainerUnavailable
    from .runner import SubprocessSandbox

    if prefer == "container":
        return BackendChoice(
            ContainerSandbox(**_for(ContainerSandbox, backend_kwargs)), origin, "requested"
        )
    if prefer == "subprocess":
        # Asking for the subprocess backend by name for untrusted code gets the
        # same floor as the automatic downgrade: kernel confinement or no run.
        # Naming the backend chooses the mechanism, not a weaker version of it.
        return BackendChoice(
            SubprocessSandbox(
                require_confinement=not origin.trusted, **_for(SubprocessSandbox, backend_kwargs)
            ),
            origin,
            "requested",
            downgraded=not origin.trusted,
        )

    if origin.trusted:
        return BackendChoice(
            SubprocessSandbox(**_for(SubprocessSandbox, backend_kwargs)),
            origin,
            "code written locally for this task; the subprocess jail is the right trade",
        )

    try:
        container = ContainerSandbox(**_for(ContainerSandbox, backend_kwargs))
    except ContainerUnavailable as exc:
        if not allow_downgrade:
            raise ContainerUnavailable(f"{origin.value} code needs a container and {exc}") from exc
        return BackendChoice(
            SubprocessSandbox(require_confinement=True, **_for(SubprocessSandbox, backend_kwargs)),
            origin,
            "no container engine; running under the strictest subprocess jail instead",
            downgraded=True,
        )
    return BackendChoice(container, origin, "code of untrusted origin is not run in-namespace")


def _for(cls: type, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Only the keyword arguments this backend actually has.

    The two backends share some knobs (``max_memory_bytes``, ``default_timeout``)
    and not others, and a caller should not have to know which is which to set a
    memory cap.
    """
    fields = getattr(cls, "__dataclass_fields__", {})
    return {name: value for name, value in kwargs.items() if name in fields}
