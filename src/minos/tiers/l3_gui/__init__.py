"""L3: synthetic input, used only when nothing better applies.

The weakest tier, and honest about it. A click has no inverse and the runtime
has no model of what it caused, so L3 effects are irreversible and always
prompt unless the caller can name the files that will change. Its oracle is a
screen digest, which is evidence that something rendered differently -- not
that anything is true.
"""

from typing import Any

from .adapter import GuiAdapter, ScreenOracle
from .driver import CuaDriver, GuiDriver, ScreenState, StubDriver


def __getattr__(name: str) -> Any:
    """Load the Windows backend on demand.

    Importing it eagerly would drag win32 specifics into every platform's import
    of this package, and the tier is meant to work -- as a stub -- everywhere.
    """
    if name in {"PanicAbort", "WindowsDriver", "panic_watcher"}:
        from . import windows

        return getattr(windows, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CuaDriver",
    "GuiAdapter",
    "GuiDriver",
    "PanicAbort",
    "ScreenOracle",
    "ScreenState",
    "StubDriver",
    "WindowsDriver",
    "panic_watcher",
]
