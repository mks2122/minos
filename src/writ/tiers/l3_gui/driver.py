"""GUI driver protocol, and a deterministic stub.

Real pixel control is not implemented here and should not be. ``cua-driver``
(MIT) already does accessibility trees, screenshots and synthetic input across
macOS, Windows and Linux; reimplementing cross-OS input injection is twelve
months of thankless platform work for no differentiation. :class:`CuaDriver` is
the seam where it plugs in.

:class:`StubDriver` is not a placeholder for testing convenience -- it is how
the L3 tier's *contracts* are tested without a desktop. What L3 can and cannot
promise is a property of the tier, not of the driver behind it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["CuaDriver", "GuiDriver", "ScreenState", "StubDriver"]


@dataclass(frozen=True, slots=True)
class ScreenState:
    """What a driver can observe.

    ``digest`` is a hash of whatever the driver considers the screen to be. It
    is the weakest possible evidence -- it says something rendered differently,
    not that anything is true -- and the L3 adapter treats it that way.
    """

    digest: str
    width: int = 0
    height: int = 0
    focused_window: str = ""
    elements: tuple[str, ...] = ()


@runtime_checkable
class GuiDriver(Protocol):
    name: str

    def observe(self) -> ScreenState: ...
    def click(self, x: int, y: int, button: str = "left") -> None: ...
    def type_text(self, text: str) -> None: ...
    def key(self, chord: str) -> None: ...


@dataclass
class StubDriver:
    """An in-memory screen. Deterministic, and available on every platform."""

    name: str = "stub"
    width: int = 1920
    height: int = 1080
    focused_window: str = "stub-window"
    _events: list[str] = field(default_factory=list, init=False)
    _text: str = field(default="", init=False)

    def observe(self) -> ScreenState:
        payload = f"{self.focused_window}|{self._text}|{'|'.join(self._events)}"
        return ScreenState(
            digest=hashlib.sha256(payload.encode()).hexdigest(),
            width=self.width,
            height=self.height,
            focused_window=self.focused_window,
            elements=tuple(self._events[-8:]),
        )

    def click(self, x: int, y: int, button: str = "left") -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ValueError(f"({x}, {y}) is off-screen")
        self._events.append(f"click:{button}:{x},{y}")

    def type_text(self, text: str) -> None:
        self._text += text
        self._events.append(f"type:{len(text)}")

    def key(self, chord: str) -> None:
        self._events.append(f"key:{chord}")

    # -- test helpers ------------------------------------------------------

    @property
    def events(self) -> list[str]:
        return list(self._events)

    @property
    def typed(self) -> str:
        return self._text


class CuaDriver:
    """Seam for ``cua-driver``. Not implemented; the error says what to do.

    Failing loudly at construction beats a driver that silently does nothing,
    which in a GUI tier looks identical to a task that quietly did not work.
    """

    name = "cua"

    def __init__(self, **_options: Any) -> None:
        raise NotImplementedError(
            "The cua-driver backend is not wired up yet. It is the intended L3 "
            "implementation -- MIT licensed, with accessibility trees, "
            "screenshots and synthetic input across macOS, Windows and Linux. "
            "See https://github.com/trycua/cua. Use StubDriver for tests."
        )
