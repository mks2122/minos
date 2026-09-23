"""Grounding: find controls by what they are, not by where they are.

Coordinate clicking is the single largest source of GUI-agent flakiness. A
button moves when the window resizes, the theme changes, the DPI changes, or the
user has a different font size, and a coordinate that was right on the machine
the task was recorded on is wrong everywhere else. Worse, it fails *silently* —
the click lands on whatever is there instead.

So the default should be: ask the desktop what controls exist, match the one the
task described, and invoke it. Windows exposes this through UI Automation, and
the parts needed here are reachable over COM without a dependency.

Two things follow that matter more than the lookup itself.

**Invoking is better than clicking.** A control reached through UIA can often be
invoked directly — the physical cursor never moves, and the user can keep
working. That is the mitigation promised in `windows` for sharing the desktop,
and it is real here rather than aspirational.

**Ambiguity must not be resolved by guessing.** Two buttons named "Save" is not
a situation to pick from; :class:`AmbiguousElement` carries both so the caller
can ask, and the L3 adapter turns it into a refusal. Silently choosing the first
match is how an agent clicks "Don't Save".

Falls back to coordinates when the accessibility tree does not have what the
task asked for, because plenty of applications draw their own controls and
expose nothing. The fallback is recorded, not silent: it is exactly the kind of
degradation invariant I3 exists to make auditable.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

__all__ = [
    "AmbiguousElement",
    "Element",
    "ElementNotFound",
    "UiaTree",
    "available",
]


class ElementNotFound(LookupError):
    """No control matched. The caller should fall back or refuse, not guess."""


class AmbiguousElement(LookupError):
    """Several controls matched, so the description was not specific enough.

    Carries the candidates: "which Save?" is a question worth asking, and
    picking one is how an agent clicks the wrong button.
    """

    def __init__(self, name: str, candidates: tuple[Element, ...]) -> None:
        listed = ", ".join(f"{c.name!r} ({c.control_type})" for c in candidates[:5])
        super().__init__(
            f"{len(candidates)} controls match {name!r}: {listed}. "
            "Name the control more precisely, or say which one."
        )
        self.candidates = candidates


@dataclass(frozen=True, slots=True)
class Element:
    """One control in the accessibility tree."""

    name: str
    control_type: str
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0
    enabled: bool = True
    invokable: bool = False
    offscreen: bool = False
    _native: Any = None

    @property
    def visible(self) -> bool:
        """On screen with a real size. A scrolled-away tab reports (0, 0)."""
        return not self.offscreen and self.width > 0 and self.height > 0

    @property
    def centre(self) -> tuple[int, int]:
        """Where to click if invoking is not available."""
        return (self.left + self.width // 2, self.top + self.height // 2)

    def describe(self) -> str:
        return f"{self.control_type} {self.name!r} at {self.centre}"


def available() -> bool:
    """Is UI Automation usable in this process?

    Checked rather than assumed: comtypes is optional, COM initialisation can
    fail inside an already-initialised apartment, and the answer decides whether
    the adapter grounds or falls back to coordinates.
    """
    if sys.platform != "win32":
        return False
    try:
        import comtypes.client  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class UiaTree:
    """The live accessibility tree of the foreground window.

    Deliberately thin. This is not a general UIA wrapper; it answers one
    question -- "which control did the task mean" -- and nothing else, because
    every extra capability here is surface that has to be kept working across
    Windows versions.
    """

    name: str = "uia"
    _automation: Any = None

    def __post_init__(self) -> None:
        if not available():
            raise RuntimeError(
                "UI Automation is not available. Install comtypes "
                "(uv pip install comtypes) for element-based clicking; without "
                "it the GUI tier falls back to raw coordinates."
            )
        import comtypes.client

        # CUIAutomation, the documented entry point. Created once per tree:
        # creating it per lookup is measurably slow on a loaded desktop.
        self._automation = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            interface=comtypes.client.GetModule("UIAutomationCore.dll").IUIAutomation,
        )

    # -- lookup ------------------------------------------------------------

    def elements(self, window: str = "*") -> tuple[Element, ...]:
        """Every control in the foreground window, flattened."""
        root = self._root(window)
        if root is None:
            return ()
        return tuple(self._walk(root))

    def find(self, name: str, *, control_type: str | None = None) -> Element:
        """The one control matching ``name``, or raise.

        Matching is case-insensitive and ignores the ``&`` accelerator markers
        Windows puts in labels, because a task says "Save" and the control is
        called "&Save". Controls that are scrolled away or have no size are not
        candidates: their reported centre is (0, 0), and clicking it hits the
        corner of the screen.
        """
        wanted = _normalise(name)
        # One walk: the tree is the expensive part, and two walks can disagree
        # if the application redraws in between.
        present = [element for element in self.elements() if element.visible]
        matches = [
            element
            for element in present
            if _normalise(element.name) == wanted
            and (control_type is None or element.control_type == control_type)
        ]

        if not matches:
            # Substring, as a second pass only. Doing it in one pass would let a
            # vague match beat an exact one.
            matches = [
                element
                for element in present
                if wanted
                and wanted in _normalise(element.name)
                and (control_type is None or element.control_type == control_type)
            ]

        usable = [element for element in matches if element.enabled]
        if not usable:
            if matches:
                raise ElementNotFound(f"{name!r} is present but disabled")
            raise ElementNotFound(f"no control named {name!r} in the foreground window")
        if len(usable) > 1:
            raise AmbiguousElement(name, tuple(usable))
        return usable[0]

    def invoke(self, element: Element) -> bool:
        """Activate a control without moving the cursor. False if not possible.

        Returning False rather than raising: "this control cannot be invoked" is
        an ordinary fact about a control, and the caller's answer is to click
        its centre instead.
        """
        if not element.invokable or not element._native:
            return False
        try:
            pattern = element._native.GetCurrentPattern(_INVOKE_PATTERN_ID)
            # A NULL pattern pointer is falsy, not None -- see _root.
            if not pattern:
                return False
            import comtypes.client

            invoker = pattern.QueryInterface(
                comtypes.client.GetModule("UIAutomationCore.dll").IUIAutomationInvokePattern
            )
            invoker.Invoke()
            return True
        except Exception:
            # Invoke failing is recoverable by clicking, so it must not escape
            # as a crash; the caller clicks the centre instead.
            return False

    # -- internals ---------------------------------------------------------

    def _root(self, window: str) -> Any:
        """The element whose subtree holds the controls a task means.

        For the focused window that is the *window*, reached through its
        handle -- not ``GetFocusedElement``, which returns the focused control.
        A focused button has no children, so walking from there finds nothing
        and looks exactly like an application with no accessibility support.
        """
        try:
            if window in ("", "*"):
                import ctypes

                # getattr keeps this typecheckable off Windows, where windll
                # does not exist; CI runs mypy on all three platforms.
                user32 = getattr(ctypes, "windll").user32  # noqa: B009
                handle = user32.GetForegroundWindow()
            else:
                from .windows import find_window

                handle, _ = find_window(window)
            root = self._automation.ElementFromHandle(handle) if handle else None
        except LookupError:
            raise
        except Exception:
            return None
        # A COM call returns a NULL *pointer object*, never None, so `is None`
        # is always False and the next attribute access dereferences null.
        # Truthiness is the only check that works here.
        return root if root else None

    def _walk(self, node: Any, depth: int = 0) -> list[Element]:
        """Flatten the tree, bounded.

        A depth and breadth cap because some applications expose thousands of
        elements and an unbounded walk turns a click into a several-second
        pause -- which in an agent loop reads as a hang.
        """
        if depth > _MAX_DEPTH:
            return []
        found: list[Element] = []
        if not node:
            return found
        try:
            children = self._automation.ControlViewWalker
            child = children.GetFirstChildElement(node)
        except Exception:
            return []

        count = 0
        # `while child` and not `while child is not None`: a childless element
        # yields a NULL pointer object, which is not None but is falsy, and
        # dereferencing it raises COMError('Invalid pointer').
        while child and count < _MAX_SIBLINGS:
            element = _to_element(child)
            if element is not None:
                found.append(element)
            found.extend(self._walk(child, depth + 1))
            try:
                child = children.GetNextSiblingElement(child)
            except Exception:
                break
            count += 1
        return found


_MAX_DEPTH = 12
_MAX_SIBLINGS = 200
_INVOKE_PATTERN_ID = 10000
_IS_INVOKE_AVAILABLE = 30031
_IS_OFFSCREEN = 30022


def _to_element(native: Any) -> Element | None:
    if not native:
        return None
    try:
        rect = native.CurrentBoundingRectangle
        return Element(
            name=native.CurrentName or "",
            control_type=_CONTROL_TYPES.get(native.CurrentControlType, "control"),
            left=int(rect.left),
            top=int(rect.top),
            width=int(rect.right - rect.left),
            height=int(rect.bottom - rect.top),
            enabled=bool(native.CurrentIsEnabled),
            # Whether the control supports Invoke, not whether it takes focus:
            # a text box takes focus and cannot be invoked, a toolbar button
            # often the reverse.
            invokable=bool(native.GetCurrentPropertyValue(_IS_INVOKE_AVAILABLE)),
            offscreen=bool(native.GetCurrentPropertyValue(_IS_OFFSCREEN)),
            _native=native,
        )
    except Exception:
        return None


def _normalise(text: str) -> str:
    """Case-folded, accelerator-stripped. "&Save..." and "save" are the same."""
    return (text or "").replace("&", "").replace("...", "").strip().casefold()


_CONTROL_TYPES = {
    50000: "button",
    50001: "calendar",
    50002: "checkbox",
    50003: "combobox",
    50004: "edit",
    50005: "hyperlink",
    50006: "image",
    50007: "listitem",
    50008: "list",
    50009: "menu",
    50011: "menuitem",
    50012: "progressbar",
    50013: "radiobutton",
    50014: "scrollbar",
    50015: "slider",
    50018: "tab",
    50019: "tabitem",
    50020: "text",
    50021: "toolbar",
    50023: "tree",
    50024: "treeitem",
    50032: "window",
    50033: "pane",
}
