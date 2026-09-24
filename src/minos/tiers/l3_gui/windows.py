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

*Input goes only to the window it was granted for.* An action that names a
window brings it to the front first, and every event checks it is still there;
if something else took focus, nothing is sent. Clicks on controls found by name
go through the accessibility layer where the control allows it, so the cursor
does not move. Typing and coordinate clicks are real input, and do.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .driver import ScreenState

# getattr, not attribute access: these exist only on Windows, and CI
# typechecks every platform. The code that calls them runs only on Windows.
_WinDLL: Any = getattr(ctypes, "WinDLL", None)
_last_error: Callable[[], int] = getattr(ctypes, "get_last_error", lambda: 0)

__all__ = [
    "FocusLost",
    "PanicAbort",
    "WindowNotFound",
    "WindowsDriver",
    "find_window",
    "panic_watcher",
]

# Plain ctypes aliases rather than ctypes.wintypes: importing wintypes raises on
# Linux and macOS, and CI typechecks and imports this module on all three. The
# definitions are identical -- _DWORD *is* c_ulong.
_LONG = ctypes.c_long
_DWORD = ctypes.c_ulong
_WORD = ctypes.c_ushort
_ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)


class PanicAbort(Exception):
    """The panic key was pressed. Stop, release input, do not retry."""


class WindowNotFound(LookupError):
    """No top-level window matches the title the action was granted for."""


class FocusLost(OSError):
    """The window the input was meant for is no longer in front.

    Raised instead of sending: keystrokes meant for one window and delivered to
    whatever took focus -- a notification, a password prompt, the user's own
    editor -- are exactly the input the grant did not cover.
    """


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


class _POINT(ctypes.Structure):
    _fields_ = (("x", _LONG), ("y", _LONG))


class _RECT(ctypes.Structure):
    _fields_ = (("left", _LONG), ("top", _LONG), ("right", _LONG), ("bottom", _LONG))


def _user32() -> Any:
    if sys.platform != "win32":  # pragma: no cover - guarded by the caller
        raise RuntimeError("WindowsDriver requires Windows")
    return _WinDLL("user32", use_last_error=True)


def _title(user32: Any, handle: int) -> str:
    length = user32.GetWindowTextLengthW(handle)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, length + 1)
    return buffer.value


def find_window(title: str) -> tuple[int, str]:
    """The one visible top-level window called ``title``.

    Exact (case-insensitive) matches win; a substring match is accepted only
    when it is the only one. Two windows both containing "Notepad" is the same
    question as two buttons named "Save", and gets the same answer.
    """
    user32 = _user32()
    windows: list[tuple[int, str]] = []

    def collect(handle: int, _: Any) -> bool:
        if user32.IsWindowVisible(handle):
            name = _title(user32, handle)
            if name:
                windows.append((handle, name))
        return True

    # getattr: WINFUNCTYPE exists only on Windows, and CI typechecks all three.
    functype = getattr(ctypes, "WINFUNCTYPE")  # noqa: B009
    callback = functype(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(callback(collect), 0)
    wanted = title.strip().casefold()
    exact = [w for w in windows if w[1].casefold() == wanted]
    if len(exact) == 1:
        return exact[0]
    loose = exact or [w for w in windows if wanted and wanted in w[1].casefold()]
    if not loose:
        raise WindowNotFound(f"no open window is called {title!r}")
    if len(loose) > 1:
        names = ", ".join(repr(name) for _, name in loose[:5])
        raise WindowNotFound(f"{len(loose)} windows match {title!r}: {names}. Give the full title.")
    return loose[0]


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
    ghost: Any = None
    """A :class:`~minos.tiers.l3_gui.ghost.GhostCursor`, or None for no ghost."""

    restore_cursor: bool = True
    """Put the person's cursor back after a coordinate click. The agent borrows
    the one system cursor for the click; it should not keep it."""
    _user32: Any = field(default=None, init=False, repr=False)
    _target: int = field(default=0, init=False, repr=False)

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
        return _title(self._user32, handle) if handle else ""

    def controls(self, limit: int = 60) -> tuple[str, ...]:
        """The named, visible controls of the foreground window.

        This is what lets a model click by name: without it the planner has to
        guess what the buttons are called. Empty when UI Automation is missing.
        """
        from .uia import UiaTree, available

        if not available():
            return ()
        seen: list[str] = []
        for element in UiaTree().elements():
            if element.name and element.visible:
                label = f"{element.control_type} {element.name!r}"
                if label not in seen:
                    seen.append(label)
            if len(seen) >= limit:
                break
        return tuple(seen)

    # -- focus -------------------------------------------------------------

    def focus(self, window: str) -> str:
        """Bring the named window to the front, and hold input to it.

        Windows refuses ``SetForegroundWindow`` from a process that is not
        already in front, so attach to the foreground thread's input queue for
        the call. If it still does not come forward, raise: typing into
        whatever happens to be in front instead is the failure this exists to
        prevent. Every later event checks the window is still in front.
        """
        self._target = 0
        self._guard()
        handle, title = find_window(window)
        user32 = self._user32
        if user32.GetForegroundWindow() != handle:
            if user32.IsIconic(handle):
                user32.ShowWindow(handle, 9)  # SW_RESTORE
            ours = _WinDLL("kernel32").GetCurrentThreadId()
            theirs = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            attached = bool(
                theirs and theirs != ours and user32.AttachThreadInput(ours, theirs, True)
            )
            try:
                user32.BringWindowToTop(handle)
                user32.SetForegroundWindow(handle)
            finally:
                if attached:
                    user32.AttachThreadInput(ours, theirs, False)
            deadline = time.monotonic() + 2.0
            while user32.GetForegroundWindow() != handle and time.monotonic() < deadline:
                time.sleep(0.05)
            if user32.GetForegroundWindow() != handle:
                raise FocusLost(
                    f"could not bring {title!r} to the front "
                    f"({self._foreground_title()!r} is); nothing was sent"
                )
        self._target = handle
        return title

    def release(self) -> None:
        """Stop holding input to a window. The next action may go anywhere."""
        self._target = 0

    # -- showing intent ----------------------------------------------------

    def show(self, x: int, y: int, label: str) -> None:
        """Point the ghost at (x, y) before acting there. No-op without one."""
        if self.ghost is not None:
            self._guard()
            self.ghost.point(x, y, label)

    def show_focus(self, label: str) -> None:
        """Point the ghost at whatever has keyboard focus: where typing lands."""
        if self.ghost is None:
            return
        at = self._focus_point()
        if at is not None:
            self.show(*at, label)

    def _focus_point(self) -> tuple[int, int] | None:
        from .uia import UiaTree, available

        if available():
            with contextlib.suppress(Exception):
                focused = UiaTree().focused()
                if focused is not None and focused.visible:
                    # Near the left edge, where a caret usually is, rather than
                    # the middle of a wide field.
                    return (focused.left + min(12, focused.width // 2), focused.centre[1])
        handle = self._user32.GetForegroundWindow()
        rect = _RECT()
        if handle and self._user32.GetWindowRect(handle, ctypes.byref(rect)):
            return ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        return None

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

        before = _POINT()
        had_position = bool(self._user32.GetCursorPos(ctypes.byref(before)))
        self._send(
            self._mouse(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE, absolute_x, absolute_y),
            self._mouse(down, absolute_x, absolute_y),
            self._mouse(up, absolute_x, absolute_y),
        )
        if self.restore_cursor and had_position:
            self._user32.SetCursorPos(before.x, before.y)

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
        """Checked before every event, so abort lands within one action.

        Also refuses to send when a window was focused and something else has
        since come to the front. That is not the panic key's job -- focus moves
        for innocent reasons -- but input meant for one window must never land
        in another, so the action fails and the planner can focus again.
        """
        if self.panic is not None:
            self.panic.check()
        if self._target and self._user32.GetForegroundWindow() != self._target:
            raise FocusLost(
                f"the target window is no longer in front ({self._foreground_title()!r} "
                "is); nothing was sent"
            )

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
                f"(error {_last_error()}). The focused window may be "
                "elevated, which blocks synthetic input from a normal process."
            )
        if self.settle:
            time.sleep(self.settle)
