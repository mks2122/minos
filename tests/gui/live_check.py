"""Drive the real desktop and check what actually arrived.

    python tests/gui/live_check.py

Not part of the pytest run: it takes the real mouse and keyboard for about
thirty seconds. It only ever sends input to its own disposable windows
(tests/gui/testwindow.ps1), and every check reads what the window *received*
from the window's own event log rather than trusting what the driver believes
it sent.

Windows only. Needs the gui extra (comtypes).
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes
import io
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from minos.audit import AuditLog  # noqa: E402
from minos.broker import Broker  # noqa: E402
from minos.checkpoint import FileCheckpointStore  # noqa: E402
from minos.router import Router  # noqa: E402
from minos.scopes import ScopeSet  # noqa: E402
from minos.tiers.l3_gui import GuiAdapter  # noqa: E402
from minos.tiers.l3_gui.uia import AmbiguousElement, UiaTree  # noqa: E402
from minos.tiers.l3_gui.windows import (  # noqa: E402
    FocusLost,
    PanicAbort,
    WindowsDriver,
    find_window,
    panic_watcher,
)
from minos.types import ActionRequest, EffectClass  # noqa: E402

WINDOW = "Minos GUI Test"
OTHER = "Minos GUI Test (decoy)"
SCRIPT = ROOT / "tests" / "gui" / "testwindow.ps1"


class Window:
    """One disposable test window and the log of what it received."""

    def __init__(self, title: str, directory: Path) -> None:
        self.title = title
        self.log = directory / f"{title.replace(' ', '_').replace('(', '').replace(')', '')}.log"
        self.log.unlink(missing_ok=True)
        self.process = subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-Log",
                str(self.log),
                "-Seconds",
                "120",
                "-Title",
                title,
            ]
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            with contextlib.suppress(LookupError):
                find_window(title)
                if self.events():
                    return
            time.sleep(0.1)
        raise RuntimeError(f"{title!r} never appeared")

    def events(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text(encoding="utf-8-sig").splitlines() if line]

    def wait_for(self, prefix: str, timeout: float = 3.0) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.events():
                if event.startswith(prefix):
                    return event
            time.sleep(0.05)
        return None

    def forget(self) -> None:
        self.log.write_text("", encoding="utf-8")

    def close(self) -> None:
        self.process.terminate()


results: list[tuple[str, bool, str]] = []


def check(name: str) -> Callable[[Callable[[], str]], None]:
    def run(body: Callable[[], str]) -> None:
        try:
            detail = body()
            results.append((name, True, detail))
        except Exception as exc:
            results.append((name, False, f"{type(exc).__name__}: {exc}"))
        mark = "PASS" if results[-1][1] else "FAIL"
        print(f"  [{mark}] {name}: {results[-1][2]}")

    return run


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def hold_panic_chord() -> None:
    """Press Ctrl+Alt+Esc the way a person would, then let go."""
    user32 = ctypes.WinDLL("user32")
    keys = (0x11, 0x12, 0x1B)
    for vk in keys:
        user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.3)
    for vk in reversed(keys):
        user32.keybd_event(vk, 0, 2, 0)


def main() -> int:
    if sys.platform != "win32":
        print("Windows only.")
        return 2
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    work = Path(tempfile.mkdtemp(prefix="minos-gui-"))
    print(f"logs in {work}\n")

    main_window = Window(WINDOW, work)
    decoy = Window(OTHER, work)
    driver = WindowsDriver()

    try:

        @check("focus brings a background window to the front")
        def _() -> str:
            driver.focus(OTHER)
            driver.focus(WINDOW)
            handle, _ = find_window(WINDOW)
            expect(driver._user32.GetForegroundWindow() == handle, "not in front")
            return "in front"

        @check("controls() lists what the model can click by name")
        def _() -> str:
            driver.focus(WINDOW)
            controls = driver.controls()
            for wanted in ("button 'Save'", "button 'Save as copy'", "button 'Clear'"):
                expect(wanted in controls, f"{wanted} missing from {controls}")
            return ", ".join(controls)

        @check("two controls named Save is refused, with both listed")
        def _() -> str:
            driver.focus(WINDOW)
            try:
                UiaTree().find("Save")
            except AmbiguousElement as exc:
                expect(len(exc.candidates) == 2, f"{len(exc.candidates)} candidates")
                return str(exc)
            raise AssertionError("picked one")

        @check("invoke presses a button without moving the cursor")
        def _() -> str:
            driver.focus(WINDOW)
            main_window.forget()
            before = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(before))
            tree = UiaTree()
            expect(tree.invoke(tree.find("Clear")), "invoke returned False")
            after = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(after))
            expect(main_window.wait_for("clear|") is not None, "window saw no click")
            expect((before.x, before.y) == (after.x, after.y), "cursor moved")
            return "window received 'clear', cursor did not move"

        @check("a control that cannot be invoked says so instead of crashing")
        def _() -> str:
            driver.focus(WINDOW)
            tree = UiaTree()
            label = tree.find("Status: ready")
            expect(tree.invoke(label) is False, "claimed to invoke a label")
            return f"{label.describe()} -> invoke() is False"

        @check("click by coordinate, then type unicode text")
        def _() -> str:
            driver.focus(WINDOW)
            main_window.forget()
            body = UiaTree().find("Body")
            driver.click(*body.centre)
            driver.key("ctrl+a")
            driver.key("backspace")
            text = "héllo wörld — ✓ 日本"
            driver.type_text(text)
            driver.key("ctrl+s")
            got = main_window.wait_for("ctrl+s|")
            expect(got == f"ctrl+s|{text}", f"window received {got!r}")
            return f"window received {text!r} exactly"

        @check("key chords edit the text")
        def _() -> str:
            driver.focus(WINDOW)
            main_window.forget()
            driver.key("end")
            driver.key("backspace")
            driver.key("backspace")
            driver.key("ctrl+s")
            got = main_window.wait_for("ctrl+s|")
            expect(got == "ctrl+s|héllo wörld — ✓ ", f"window received {got!r}")
            return "two backspaces removed two characters"

        @check("input stops when another window steals focus")
        def _() -> str:
            driver.focus(WINDOW)
            main_window.forget()
            decoy.forget()
            thief = WindowsDriver()
            thief.focus(OTHER)
            try:
                driver.type_text("SHOULD NOT ARRIVE")
            except FocusLost as exc:
                time.sleep(0.5)
                typed = [e for e in main_window.events() + decoy.events() if "SHOULD" in e]
                expect(not typed, f"text leaked: {typed}")
                return str(exc)
            raise AssertionError("typed into the wrong window")

        @check("Ctrl+Alt+Esc aborts before the next event")
        def _() -> str:
            panic = panic_watcher()
            guarded = WindowsDriver(panic=panic)
            with panic:
                guarded.focus(WINDOW)
                hold_panic_chord()
                deadline = time.monotonic() + 2
                while not panic.triggered.is_set() and time.monotonic() < deadline:
                    time.sleep(0.02)
                expect(panic.triggered.is_set(), "watcher never saw the chord")
                try:
                    guarded.type_text("SHOULD NOT ARRIVE")
                except PanicAbort as exc:
                    return str(exc)
            raise AssertionError("typed after the panic key")

        # -- the full path: router, broker, approval, adapter, real driver --

        def rig(approve: bool) -> tuple[Router, Broker, list[str]]:
            asked: list[str] = []

            def approver(invocation: object, decision: object) -> bool:
                asked.append(getattr(invocation, "operation", "?"))
                return approve

            state = work / ("approved" if approve else "denied")
            broker = Broker(
                scopes=ScopeSet.parse([f"ui.input:{WINDOW}"]),
                audit=AuditLog(state / "audit.jsonl"),
                store=FileCheckpointStore(state / "cp"),
                approver=approver,
            )
            return Router(adapters=(GuiAdapter(driver),)), broker, asked

        def submit(router: Router, broker: Broker, op: str, **params: object) -> Any:
            routed = router.route(
                ActionRequest(goal_id="live", intent=op, operation=op, params=params)
            )
            return routed, broker.submit(routed.invocation, routed.execute)

        @check("a GUI click is irreversible and asks first; denied means not sent")
        def _() -> str:
            main_window.forget()
            router, broker, asked = rig(approve=False)
            routed, outcome = submit(
                router, broker, "ui.click", element="Save as copy", window=WINDOW
            )
            expect(
                routed.preparation.contract.effect_class is EffectClass.IRREVERSIBLE,
                "not irreversible",
            )
            expect(len(asked) == 1, f"approver asked {len(asked)} times")
            expect(outcome.status == "denied", outcome.status)
            time.sleep(0.5)
            expect(not main_window.wait_for("save-copy", 0.5), "clicked anyway")
            return "asked once, denied, window received nothing"

        @check("approved: the click lands, through the broker")
        def _() -> str:
            main_window.forget()
            router, broker, asked = rig(approve=True)
            _, outcome = submit(router, broker, "ui.click", element="Save as copy", window=WINDOW)
            expect(len(asked) == 1, f"approver asked {len(asked)} times")
            expect(outcome.status == "ok", f"{outcome.status}: {outcome.error}")
            expect(main_window.wait_for("save-copy|") is not None, "window saw nothing")
            return f"{outcome.result}"

        @check("through the broker, an ambiguous name fails and clicks nothing")
        def _() -> str:
            main_window.forget()
            router, broker, _ = rig(approve=True)
            _, outcome = submit(router, broker, "ui.click", element="Save", window=WINDOW)
            expect(outcome.status == "failed", outcome.status)
            time.sleep(0.5)
            expect(not main_window.wait_for("save-", 0.5), "clicked one of them")
            return outcome.error

        @check("a window outside the grant is refused before any input")
        def _() -> str:
            decoy.forget()
            router, broker, asked = rig(approve=True)
            _, outcome = submit(router, broker, "ui.type", text="nope", window=OTHER)
            expect(outcome.status == "denied", outcome.status)
            expect(not asked, "asked for approval of an out-of-scope action")
            time.sleep(0.5)
            expect(not decoy.events(), f"decoy received {decoy.events()}")
            return outcome.decision.reason if hasattr(outcome.decision, "reason") else "denied"

        @check("screenshot lists the named window's controls")
        def _() -> str:
            router, broker, _ = rig(approve=True)
            _, outcome = submit(router, broker, "ui.screenshot", window=WINDOW)
            expect(outcome.status == "ok", f"{outcome.status}: {outcome.error}")
            expect("button 'Save as copy'" in outcome.result["elements"], str(outcome.result))
            seen = outcome.result
            return f"{len(seen['elements'])} controls, window {seen['focused_window']!r}"

    finally:
        driver.release()
        main_window.close()
        decoy.close()
        threading.Event().wait(0.2)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
