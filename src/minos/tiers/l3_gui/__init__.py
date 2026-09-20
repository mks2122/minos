"""L3: synthetic input, used only when nothing better applies.

The weakest tier, and honest about it. A click has no inverse and the runtime
has no model of what it caused, so L3 effects are irreversible and always
prompt unless the caller can name the files that will change. Its oracle is a
screen digest, which is evidence that something rendered differently -- not
that anything is true.
"""

from .adapter import GuiAdapter, ScreenOracle
from .driver import CuaDriver, GuiDriver, ScreenState, StubDriver

__all__ = [
    "CuaDriver",
    "GuiAdapter",
    "GuiDriver",
    "ScreenOracle",
    "ScreenState",
    "StubDriver",
]
