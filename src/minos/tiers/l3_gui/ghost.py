"""A second pointer that shows where the agent is about to act.

Windows has one system cursor, and the agent shares it with the person at the
desk. So the agent gets its own pointer: a small, always-on-top window drawn in
a colour nobody would mistake for the real one, which glides to each target and
pauses there before anything happens. It never takes focus and every click
passes straight through it, so it cannot itself become the thing that was
clicked.

What it can honestly show depends on how the action is carried out:

* A control clicked **by name** is invoked through UI Automation. The real
  cursor does not move at all; the ghost is the only pointer that does. This is
  the case where the agent and the person genuinely work side by side.
* A click **by coordinate** has to be real input, and real input moves the real
  cursor. The ghost shows the target first, and the driver puts the person's
  cursor back where it was afterwards.
* **Typing** goes to keyboard focus. The ghost points at the focused control.

The window runs its own thread and message loop, so a slow planner or a blocked
broker never leaves it frozen mid-glide.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import Any

__all__ = ["GhostCursor"]

# Fixed geometry. The window's top-left pixel *is* the arrow's tip, so moving
# the window to (x, y) puts the tip exactly on the target.
_WIDTH = 320
_HEIGHT = 56
_KEY = 0x00FF00FF  # magenta: the transparent colour key (COLORREF is 0x00BBGGRR)
_FILL = 0x00006AFF  # orange, RGB(255, 106, 0)
_EDGE = 0x00202020
_TEXT = 0x00FFFFFF

_WS_POPUP = 0x80000000
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_LWA_COLORKEY = 0x1
_SW_HIDE = 0
_SW_SHOWNOACTIVATE = 4
_SWP_NOSIZE = 0x0001
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040
_HWND_TOPMOST = -1
_WM_PAINT = 0x000F
_WM_CLOSE = 0x0010
_WM_DESTROY = 0x0002
_WM_APP_REDRAW = 0x8001
_DT_LEFT_VCENTER_SINGLE = 0x0000 | 0x0004 | 0x0020


class GhostCursor:
    """The agent's own pointer. Safe to construct and ignore off Windows.

    ``point(x, y, label)`` glides there and holds for ``dwell`` seconds, so a
    person watching sees the target *before* the action lands.
    """

    def __init__(self, *, glide: float = 0.35, dwell: float = 0.45) -> None:
        self.glide = glide
        self.dwell = dwell
        self._label = ""
        self._position: tuple[int, int] | None = None
        self._hwnd = 0
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        if sys.platform != "win32":
            return
        self._thread = threading.Thread(target=self._run, name="minos-ghost", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        if self._error is not None:
            raise RuntimeError(f"could not create the ghost cursor: {self._error}")

    # -- public ------------------------------------------------------------

    @property
    def active(self) -> bool:
        return bool(self._hwnd)

    @property
    def position(self) -> tuple[int, int] | None:
        """Where the tip is, or None while hidden."""
        return self._position

    @property
    def label(self) -> str:
        return self._label

    def point(self, x: int, y: int, label: str = "") -> None:
        """Glide to (x, y), show ``label``, and hold there for a moment."""
        self._set_label(label)
        start = self._position
        if not self.active:
            self._position = (x, y)
            return
        if start is None:
            self._move(x, y, show=True)
        else:
            steps = max(int(self.glide / 0.016), 1)
            for step in range(1, steps + 1):
                t = step / steps
                eased = 1 - (1 - t) ** 3  # ease-out: fast, then settle on the target
                self._move(
                    round(start[0] + (x - start[0]) * eased),
                    round(start[1] + (y - start[1]) * eased),
                    show=True,
                )
                time.sleep(self.glide / steps)
        if self.dwell:
            time.sleep(self.dwell)

    def hide(self) -> None:
        self._position = None
        if self.active:
            self._user32.ShowWindow(self._hwnd, _SW_HIDE)

    def close(self) -> None:
        if self.active:
            self._user32.PostMessageW(self._hwnd, _WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._hwnd = 0
        self._position = None

    def __enter__(self) -> GhostCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _set_label(self, label: str) -> None:
        self._label = label if len(label) <= 40 else label[:39] + "…"
        if self.active:
            self._user32.PostMessageW(self._hwnd, _WM_APP_REDRAW, 0, 0)

    def _move(self, x: int, y: int, *, show: bool) -> None:
        self._position = (x, y)
        flags = _SWP_NOSIZE | _SWP_NOACTIVATE | (_SWP_SHOWWINDOW if show else 0)
        self._user32.SetWindowPos(self._hwnd, _HWND_TOPMOST, x, y, 0, 0, flags)

    def _run(self) -> None:
        try:
            self._create()
        except BaseException as exc:  # reported to the constructor
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        message = _MSG()
        while self._user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            self._user32.TranslateMessage(ctypes.byref(message))
            self._user32.DispatchMessageW(ctypes.byref(message))

    def _create(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32 = user32
        self._gdi32 = gdi32

        lresult = ctypes.c_ssize_t
        wndproc_type = getattr(ctypes, "WINFUNCTYPE")(  # noqa: B009 - Windows only
            lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )
        user32.DefWindowProcW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.DefWindowProcW.restype = lresult
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = (
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        )  # fmt: skip
        user32.SetWindowPos.argtypes = (
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        )  # fmt: skip
        user32.PostMessageW.argtypes = (
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        )  # fmt: skip
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.BeginPaint.restype = wintypes.HDC
        user32.BeginPaint.argtypes = (wintypes.HWND, ctypes.c_void_p)
        user32.EndPaint.argtypes = (wintypes.HWND, ctypes.c_void_p)
        user32.FillRect.argtypes = (wintypes.HDC, ctypes.c_void_p, wintypes.HBRUSH)
        user32.DrawTextW.argtypes = (
            wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.UINT,
        )  # fmt: skip
        user32.InvalidateRect.argtypes = (wintypes.HWND, ctypes.c_void_p, wintypes.BOOL)
        user32.GetMessageW.argtypes = (ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT)
        user32.TranslateMessage.argtypes = (ctypes.c_void_p,)
        user32.DispatchMessageW.argtypes = (ctypes.c_void_p,)
        user32.DispatchMessageW.restype = lresult
        gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        gdi32.CreatePen.restype = wintypes.HPEN
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
        gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
        gdi32.Polygon.argtypes = (wintypes.HDC, ctypes.c_void_p, ctypes.c_int)
        gdi32.RoundRect.argtypes = (wintypes.HDC,) + (ctypes.c_int,) * 6
        gdi32.SetBkMode.argtypes = (wintypes.HDC, ctypes.c_int)
        gdi32.SetTextColor.argtypes = (wintypes.HDC, wintypes.COLORREF)
        gdi32.CreateFontW.restype = wintypes.HFONT
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        font = gdi32.CreateFontW(
            -15, 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI"
        )  # 600 weight, CLEARTYPE-free quality so the colour key edges stay crisp

        # An arrow, tip at (0, 0), like the system pointer but in orange.
        arrow = (wintypes.POINT * 7)(
            wintypes.POINT(0, 0),
            wintypes.POINT(0, 22),
            wintypes.POINT(6, 17),
            wintypes.POINT(10, 26),
            wintypes.POINT(14, 24),
            wintypes.POINT(10, 16),
            wintypes.POINT(17, 16),
        )

        def paint(hwnd: Any) -> None:
            ps = (ctypes.c_byte * 72)()  # PAINTSTRUCT, opaque here
            dc = user32.BeginPaint(hwnd, ps)
            whole = wintypes.RECT(0, 0, _WIDTH, _HEIGHT)
            key = gdi32.CreateSolidBrush(_KEY)
            fill = gdi32.CreateSolidBrush(_FILL)
            pen = gdi32.CreatePen(0, 2, _EDGE)
            user32.FillRect(dc, ctypes.byref(whole), key)
            old_brush = gdi32.SelectObject(dc, fill)
            old_pen = gdi32.SelectObject(dc, pen)
            gdi32.Polygon(dc, arrow, 7)
            if self._label:
                gdi32.RoundRect(dc, 20, 24, _WIDTH - 2, _HEIGHT - 2, 10, 10)
                old_font = gdi32.SelectObject(dc, font)
                gdi32.SetBkMode(dc, 1)  # TRANSPARENT
                gdi32.SetTextColor(dc, _TEXT)
                text = wintypes.RECT(30, 24, _WIDTH - 10, _HEIGHT - 2)
                user32.DrawTextW(
                    dc, "minos: " + self._label, -1, ctypes.byref(text), _DT_LEFT_VCENTER_SINGLE
                )
                gdi32.SelectObject(dc, old_font)
            gdi32.SelectObject(dc, old_brush)
            gdi32.SelectObject(dc, old_pen)
            for handle in (key, fill, pen):
                gdi32.DeleteObject(handle)
            user32.EndPaint(hwnd, ps)

        def wndproc(hwnd: Any, msg: int, wparam: int, lparam: int) -> int:
            if msg == _WM_PAINT:
                paint(hwnd)
                return 0
            if msg == _WM_APP_REDRAW:
                user32.InvalidateRect(hwnd, None, True)
                return 0
            if msg == _WM_DESTROY:
                gdi32.DeleteObject(font)
                user32.PostQuitMessage(0)
                return 0
            return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))

        # Kept on self: if the callback is garbage collected while the window
        # lives, the next message calls freed memory.
        self._wndproc = wndproc_type(wndproc)
        instance = kernel32.GetModuleHandleW(None)
        name = f"MinosGhost{id(self)}"
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        wc.hInstance = instance
        wc.lpszClassName = name
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            raise OSError(ctypes.get_last_error(), "RegisterClassExW failed")

        hwnd = user32.CreateWindowExW(
            _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_TOPMOST
            | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE,
            name, "", _WS_POPUP, 0, 0, _WIDTH, _HEIGHT, None, None, instance, None,
        )  # fmt: skip
        if not hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
        user32.SetLayeredWindowAttributes(hwnd, _KEY, 0, _LWA_COLORKEY)
        self._hwnd = hwnd


# Plain ctypes rather than wintypes, which raises on import off Windows; the
# layouts are identical.
_PTR = ctypes.c_void_p
_UINT = ctypes.c_uint


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = (
        ("cbSize", _UINT),
        ("style", _UINT),
        ("lpfnWndProc", _PTR),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", _PTR),
        ("hIcon", _PTR),
        ("hCursor", _PTR),
        ("hbrBackground", _PTR),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", _PTR),
    )


class _MSG(ctypes.Structure):
    _fields_ = (
        ("hwnd", _PTR),
        ("message", _UINT),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_ulong),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
        ("lPrivate", ctypes.c_ulong),
    )
