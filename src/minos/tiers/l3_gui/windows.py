"""A real mouse and keyboard on the real desktop. Windows.

The design decision, recorded in docs/PLAN-GENERALITY.md: **no separate
device.** No virtual display, no second session, no mirrored VM. The agent
drives the machine the user is actually using, because everything worth
automating lives there — the logged-in sessions, the installed fonts, the real
files — and a mirror needs a sync bridge, a credential bridge and a display
bridge, each harder and more dangerous than the agent itself.

The consequence is accepted rather than hidden: **the agent shares the desktop
and takes the cursor while it works.**

*A panic key.* ``Ctrl+Alt+Esc`` aborts the run and releases input, checked
before every synthesised event so an abort lands within one action. Focus loss
deliberately does **not** abort: focus changes constantly during legitimate
automation, and a driver that gave up whenever a tooltip stole focus would be
useless.

*Window-targeted input is not implemented here yet.* Driving a control through
the accessibility layer, without moving the physical cursor, is the next
milestone; today every click is a real cursor move. Said plainly because the
difference is what determines whether someone can keep working while this runs.

This module uses ``ctypes`` against ``user32`` rather than taking a dependency.
Synthetic input is four API calls; the packages that wrap it bring far more
surface than they save, and `SECURITY.md` has to describe the real mechanism
either way.

**Not verifiable.** Everything here is L3, and L3's oracle is a screen digest,
which `oracles` already documents as the weakest evidence in the system. A click
that "worked" means pixels changed, not that anything true happened. Prefer L1,
L2 or the code tier whenever one of them can do the job.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .driver import ScreenState

__all__ = ["PanicAbort", "WindowsDriver", "panic_watcher"]

# Plain ctypes aliases rather than ctypes.wintypes: importing wintypes raises on
# Linux and macOS, and CI typechecks and imports this module on all three. The
# definitions are identical -- _DWORD *is* c_ulong.
_LONG = ctypes.c_long
_DWORD = ctypes.c_ulong
_WORD = ctypes.c_ushort
_ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)


class PanicAbort(Exception):
    """The panic key was pressed. Stop, release input, do not retry."""


# -- win32 plumbing --------------------------------------------------------

_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1

_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040

_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

_VK = {
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}

_BUTTONS = {
    "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
    "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
    "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
}


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", _LONG),
        ("dy", _LONG),
        ("mouseData", _DWORD),
        ("dwFlags", _DWORD),
        ("time", _DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    )


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", _WORD),
        ("wScan", _WORD),
        ("dwFlags", _DWORD),
        ("time", _DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT))


class _INPUT(ctypes.Structure):
    _fields_ = (("type", _DWORD), ("union", _INPUTUNION))


def _user32() -> Any:
    if sys.platform != "win32":  # pragma: no cover - guarded by the caller
        raise RuntimeError("WindowsDriver requires Windows")
    return ctypes.WinDLL("user32", use_last_error=True)


# -- the panic key ---------------------------------------------------------


def _panic_pressed(user32: Any) -> bool:
    """Ctrl+Alt+Esc, read directly rather than through a hook.

    A low-level keyboard hook would be more responsive and would also mean
    running a message pump on a background thread and handling the case where
    Windows silently unhooks a slow one. Polling three key states is enough for
    an abort control and cannot itself wedge.
    """
    pressed = user32.GetAsyncKeyState
    return all(pressed(vk) & 0x8000 for vk in (_VK["ctrl"], _VK["alt"], _VK["esc"]))


@dataclass
class panic_watcher:
    """Watch for the panic chord while the agent drives the desktop.

    ``triggered`` is checked by the driver before every synthesised event, so an
    abort takes effect at the next action rather than at the end of the task.
    """

    interval: float = 0.05
    triggered: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)

    def __enter__(self) -> panic_watcher:
        if sys.platform != "win32":
            return self
        user32 = _user32()

        def watch() -> None:
            while not self._stop.wait(self.interval):
                try:
                    if _panic_pressed(user32):
                        self.triggered.set()
                        return
                except OSError:  # pragma: no cover - session teardown
                    return

        self._thread = threading.Thread(target=watch, name="minos-panic", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def check(self) -> None:
        if self.triggered.is_set():
            raise PanicAbort("Ctrl+Alt+Esc pressed; input released and the run abandoned")


# -- the driver ------------------------------------------------------------


@dataclass
class WindowsDriver:
    """Synthetic mouse and keyboard against the live desktop.

    Satisfies :class:`~minos.tiers.l3_gui.driver.GuiDriver`, so the L3 adapter
    and everything above it are unchanged. `StubDriver` remains what the tier's
    *contracts* are tested against; this is what actually moves the cursor.
    """

    name: str = "windows"
    settle: float = 0.05
    """Pause after each event. Real UIs need a frame to react, and a burst of
    input with no gaps is the classic way to make automation look flaky."""

    panic: panic_watcher | None = None
    _user32: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError(
                "WindowsDriver requires Windows. macOS and Linux backends are "
                "not implemented; see docs/STATUS.md."
            )
        self._user32 = _user32()
        # Per-monitor DPI awareness, or every coordinate is silently wrong on a
        # scaled display -- which is most laptops, including the one this was
        # written on.
        make_dpi_aware = getattr(self._user32, "SetProcessDPIAware", None)
        if make_dpi_aware is not None:
            # Already set by the host process is fine; anything else here is not
            # worth failing a run over.
            with contextlib.suppress(OSError):
                make_dpi_aware()

    # -- observation -------------------------------------------------------

    def observe(self) -> ScreenState:
        """The weakest evidence in the system, and labelled as such.

        A window title and a size. Deliberately not a screenshot digest: a
        digest over live pixels changes when a clock ticks, which would make
        every comparison meaningless rather than merely weak.
        """
        width = self._user32.GetSystemMetrics(0)
        height = self._user32.GetSystemMetrics(1)
        title = self._foreground_title()
        return ScreenState(
            digest=hashlib.sha256(f"{title}|{width}x{height}".encode()).hexdigest(),
            width=width,
            height=height,
            focused_window=title,
            elements=(),
        )

    def _foreground_title(self) -> str:
        handle = self._user32.GetForegroundWindow()
        if not handle:
            return ""
        length = self._user32.GetWindowTextLengthW(handle)
        buffer = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(handle, buffer, length + 1)
        return buffer.value

    # -- input -------------------------------------------------------------

    def click(self, x: int, y: int, button: str = "left") -> None:
        self._guard()
        width = self._user32.GetSystemMetrics(0)
        height = self._user32.GetSystemMetrics(1)
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError(f"({x}, {y}) is off-screen ({width}x{height})")
        if button not in _BUTTONS:
            raise ValueError(f"unknown mouse button {button!r}")

        # SendInput takes 0..65535 normalised absolute coordinates, not pixels.
        absolute_x = int(x * 65535 / max(width - 1, 1))
        absolute_y = int(y * 65535 / max(height - 1, 1))
        down, up = _BUTTONS[button]

        self._send(
            self._mouse(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE, absolute_x, absolute_y),
            self._mouse(down, absolute_x, absolute_y),
            self._mouse(up, absolute_x, absolute_y),
        )

    def type_text(self, text: str) -> None:
        """Unicode scan codes, so this types what it was given.

        Virtual-key codes would type whatever the user's keyboard layout maps
        them to, which silently mangles text on any non-US layout.
        """
        self._guard()
        events: list[_INPUT] = []
        for character in text:
            for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
                events.append(self._key(0, flags, scan=ord(character)))
        self._send(*events)

    def key(self, chord: str) -> None:
        """A chord like ``ctrl+s`` or ``alt+f4``: modifiers down, key, up."""
        self._guard()
        parts = [part.strip().lower() for part in chord.split("+") if part.strip()]
        if not parts:
            raise ValueError("empty key chord")

        codes = []
        for part in parts:
            if part in _VK:
                codes.append(_VK[part])
            elif len(part) == 1:
                codes.append(self._user32.VkKeyScanW(ord(part)) & 0xFF)
            else:
                raise ValueError(f"unknown key {part!r} in chord {chord!r}")

        events = [self._key(code, 0) for code in codes]
        events += [self._key(code, _KEYEVENTF_KEYUP) for code in reversed(codes)]
        self._send(*events)

    # -- internals ---------------------------------------------------------

    def _guard(self) -> None:
        """Checked before every event, so abort lands within one action."""
        if self.panic is not None:
            self.panic.check()

    def _mouse(self, flags: int, x: int = 0, y: int = 0) -> _INPUT:
        return _INPUT(
            type=_INPUT_MOUSE,
            union=_INPUTUNION(mi=_MOUSEINPUT(dx=x, dy=y, mouseData=0, dwFlags=flags, time=0)),
        )

    def _key(self, vk: int, flags: int, scan: int = 0) -> _INPUT:
        return _INPUT(
            type=_INPUT_KEYBOARD,
            union=_INPUTUNION(ki=_KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0)),
        )

    def _send(self, *events: _INPUT) -> None:
        if not events:
            return
        array = (_INPUT * len(events))(*events)
        sent = self._user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))
        if sent != len(events):
            # UIPI blocks input to a window running at higher integrity than
            # this process. Saying so beats "the click did nothing".
            raise OSError(
                f"SendInput delivered {sent}/{len(events)} events "
                f"(error {ctypes.get_last_error()}). The focused window may be "
                "elevated, which blocks synthetic input from a normal process."
            )
        if self.settle:
            time.sleep(self.settle)
