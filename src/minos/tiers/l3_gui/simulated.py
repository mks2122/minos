"""A simulated desktop: windows with named controls, for the eval suite.

The real driver takes the developer's mouse, so a test suite must not run it,
and CI has no desktop to run it on. This is a desktop in memory that behaves
the way the tier's contracts assume a desktop does: input goes only to the
window in front, typing goes to the focused control, a control is found by
name and two controls with the same name are a question rather than a coin
flip. Its applications write their results to files, so an eval task can be
checked against a system of record, exactly as a real one would be.

It is not evidence about real applications. The live harness in
``tests/gui/live_check.py`` is; this is how the *runtime's* handling of GUI
work -- grants per window, grounding by name, approval, verification -- is
measured on every run, on every platform.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .driver import ScreenState
from .uia import AmbiguousElement, Element, ElementNotFound

__all__ = ["Control", "SimulatedDesktop", "SimulatedWindow", "WindowNotFound"]


class WindowNotFound(LookupError):
    """No window, or more than one, has that title."""


@dataclass
class Control:
    name: str
    kind: str = "button"
    """``button`` (pressed by a click) or ``edit`` (focused by a click, typed into)."""

    x: int = 0
    y: int = 0
    width: int = 120
    height: int = 30
    text: str = ""
    on_press: Callable[[], None] | None = None

    @property
    def centre(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height


@dataclass
class SimulatedWindow:
    title: str
    controls: list[Control] = field(default_factory=list)
    keys: dict[str, Callable[[], None]] = field(default_factory=dict)
    """Shortcuts, by chord: ``{"ctrl+s": save}``."""

    received: list[str] = field(default_factory=list)
    """Every event this window got, in order. What the checks read."""

    focused: Control | None = None

    def control(self, name: str) -> Control:
        return next(c for c in self.controls if c.name == name)


class SimulatedDesktop:
    """A :class:`~minos.tiers.l3_gui.driver.GuiDriver` over in-memory windows."""

    name = "simulated"

    def __init__(self, windows: list[SimulatedWindow], log: Path | None = None) -> None:
        self.windows = windows
        self.front: SimulatedWindow | None = None
        self.log = log
        """Where every window's received events are written after each input,
        so a check that only sees the filesystem can see what arrived where."""

    def _flush(self) -> None:
        if self.log is not None:
            self.log.write_text(
                json.dumps({w.title: w.received for w in self.windows}, indent=2),
                encoding="utf-8",
            )

    # -- targeting ---------------------------------------------------------

    def focus(self, window: str) -> str:
        """Bring the window with this title to the front. Exact, or one unique match."""
        exact = [w for w in self.windows if w.title == window]
        matches = exact or [w for w in self.windows if window.lower() in w.title.lower()]
        if not matches:
            raise WindowNotFound(f"no open window is called {window!r}")
        if len(matches) > 1:
            titles = ", ".join(repr(w.title) for w in matches)
            raise WindowNotFound(f"{window!r} matches {len(matches)} windows: {titles}")
        self.front = matches[0]
        self.front.received.append("focus")
        self._flush()
        return self.front.title

    def release(self) -> None:
        self.front = None

    def _window(self) -> SimulatedWindow:
        if self.front is None:
            raise WindowNotFound("no window is in front; name one with `window`")
        return self.front

    # -- observation -------------------------------------------------------

    def observe(self) -> ScreenState:
        window = self.front
        state = "|".join(
            f"{w.title}:" + ",".join(f"{c.name}={c.text}" for c in w.controls) for w in self.windows
        )
        return ScreenState(
            digest=hashlib.sha256(state.encode()).hexdigest(),
            width=1920,
            height=1080,
            focused_window=window.title if window else "",
            elements=tuple(self.controls()),
        )

    def controls(self) -> list[str]:
        window = self.front
        if window is None:
            return [f'window "{w.title}"' for w in self.windows]
        return [
            f'{c.kind} "{c.name}"' + (f" = {c.text!r}" if c.kind == "edit" and c.text else "")
            for c in window.controls
        ]

    # -- grounding ---------------------------------------------------------

    def element(self, control: Control) -> Element:
        """A control as the real accessibility tree would describe it."""
        return Element(
            name=control.name,
            control_type=control.kind,
            left=control.x,
            top=control.y,
            width=control.width,
            height=control.height,
            invokable=True,
        )

    def locate(self, element: str) -> Element:
        """A control in the front window, by name. Ambiguity raises, as the real tree does."""
        window = self._window()
        found = [c for c in window.controls if c.name == element] or [
            c for c in window.controls if element.lower() in c.name.lower()
        ]
        if not found:
            names = ", ".join(repr(c.name) for c in window.controls)
            raise ElementNotFound(
                f"no control named {element!r} in {window.title!r}; it has {names}"
            )
        if len(found) > 1:
            raise AmbiguousElement(element, tuple(self.element(c) for c in found))
        return self.element(found[0])

    def invoke(self, located: Element) -> bool:
        window = self._window()
        self._activate(window, window.control(located.name))
        self._flush()
        return True

    # -- input -------------------------------------------------------------

    def click(self, x: int, y: int, button: str = "left") -> None:
        window = self._window()
        window.received.append(f"click {x},{y}")
        for control in window.controls:
            if control.contains(x, y):
                self._activate(window, control)
                break
        self._flush()

    def type_text(self, text: str) -> None:
        window = self._window()
        window.received.append(f"type {text!r}")
        if window.focused is not None and window.focused.kind == "edit":
            window.focused.text += text
        # Otherwise typed at nothing, as a real window would ignore it.
        self._flush()

    def key(self, chord: str) -> None:
        window = self._window()
        normalised = chord.lower().replace(" ", "")
        window.received.append(f"key {normalised}")
        action = window.keys.get(normalised)
        if action is not None:
            action()
        self._flush()

    def _activate(self, window: SimulatedWindow, control: Control) -> None:
        window.received.append(f"press {control.name!r}")
        if control.kind == "edit":
            window.focused = control
        elif control.on_press is not None:
            control.on_press()
