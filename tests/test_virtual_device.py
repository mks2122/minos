"""The virtual device: a real mouse and keyboard on the real desktop.

⚠️ **These tests never synthesise real input.** `SendInput` is intercepted and
the events are inspected instead. Actually delivering a click would move the
cursor on whoever's machine is running the suite and click on whatever happened
to be under it, which is not something a test suite gets to do.

That leaves a genuine gap, stated rather than papered over: the encoding of
every event is verified, and the delivery of it is not. Delivery is exercised by
hand, and `docs/STATUS.md` says so.
"""

from __future__ import annotations

import sys
import threading
from typing import ClassVar

import pytest

windows = pytest.importorskip(
    "minos.tiers.l3_gui.windows",
    reason="the Windows backend only imports its DLLs on Windows",
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows backend")

from minos.tiers.l3_gui import PanicAbort, WindowsDriver, panic_watcher  # noqa: E402


class FakeUser32:
    """Records what would have been delivered, and delivers nothing."""

    def __init__(self, width: int = 1920, height: int = 1080, title: str = "Untitled - Notepad"):
        self.width = width
        self.height = height
        self.title = title
        self.sent: list[windows._INPUT] = []
        self.delivered_count: int | None = None

    def GetSystemMetrics(self, index: int) -> int:
        return self.width if index == 0 else self.height

    def SendInput(self, count, array, size):
        self.sent.extend(array[i] for i in range(count))
        return count if self.delivered_count is None else self.delivered_count

    def GetForegroundWindow(self):
        return 1

    def GetWindowTextLengthW(self, handle):
        return len(self.title)

    def GetWindowTextW(self, handle, buffer, length):
        buffer.value = self.title
        return len(self.title)

    def VkKeyScanW(self, char):
        return ord(chr(char).upper())

    def SetProcessDPIAware(self):
        return True

    def GetAsyncKeyState(self, vk):
        return 0

    cursor = (7, 9)
    moved_to: ClassVar[list[tuple[int, int]]] = []

    def GetCursorPos(self, point):
        point._obj.x, point._obj.y = self.cursor
        return True

    def SetCursorPos(self, x, y):
        self.moved_to.append((x, y))
        return True


@pytest.fixture
def driver(monkeypatch):
    fake = FakeUser32()
    monkeypatch.setattr(windows, "_user32", lambda: fake)
    device = WindowsDriver(settle=0.0)
    device._user32 = fake
    return device, fake


# -- it satisfies the tier's protocol --------------------------------------


def test_it_is_a_gui_driver(driver):
    """The adapter and everything above it must not need to know which backend."""
    from minos.tiers.l3_gui import GuiDriver

    device, _ = driver
    assert isinstance(device, GuiDriver)


def test_observe_reports_the_real_screen(driver):
    device, fake = driver
    state = device.observe()

    assert (state.width, state.height) == (fake.width, fake.height)
    assert state.focused_window == fake.title
    assert state.digest


def test_the_digest_is_not_over_live_pixels(driver):
    """A pixel digest changes when a clock ticks, which makes it meaningless."""
    device, _ = driver

    assert device.observe().digest == device.observe().digest


# -- clicks ----------------------------------------------------------------


def test_a_click_sends_move_down_up(driver):
    device, fake = driver
    device.click(100, 200)

    assert len(fake.sent) == 3
    flags = [event.union.mi.dwFlags for event in fake.sent]
    assert flags[0] & windows._MOUSEEVENTF_MOVE
    assert flags[1] & windows._MOUSEEVENTF_LEFTDOWN
    assert flags[2] & windows._MOUSEEVENTF_LEFTUP


def test_coordinates_are_normalised_to_the_absolute_range(driver):
    """SendInput takes 0..65535, not pixels. Getting this wrong misses silently."""
    device, fake = driver
    device.click(fake.width - 1, fake.height - 1)

    move = fake.sent[0].union.mi
    assert move.dx == 65535
    assert move.dy == 65535


def test_the_origin_maps_to_zero(driver):
    device, fake = driver
    device.click(0, 0)

    assert (fake.sent[0].union.mi.dx, fake.sent[0].union.mi.dy) == (0, 0)


def test_offscreen_clicks_are_refused(driver):
    device, fake = driver

    with pytest.raises(ValueError, match="off-screen"):
        device.click(fake.width + 10, 5)
    assert fake.sent == []


def test_negative_coordinates_are_refused(driver):
    device, _ = driver
    with pytest.raises(ValueError, match="off-screen"):
        device.click(-1, 0)


def test_right_and_middle_buttons(driver):
    device, fake = driver
    device.click(5, 5, button="right")
    device.click(5, 5, button="middle")

    flags = [event.union.mi.dwFlags for event in fake.sent]
    assert any(flag & windows._MOUSEEVENTF_RIGHTDOWN for flag in flags)
    assert any(flag & windows._MOUSEEVENTF_MIDDLEDOWN for flag in flags)


def test_an_unknown_button_is_refused(driver):
    device, _ = driver
    with pytest.raises(ValueError, match="unknown mouse button"):
        device.click(5, 5, button="scroll")


# -- typing ----------------------------------------------------------------


def test_typing_uses_unicode_scancodes_not_virtual_keys(driver):
    """Virtual keys type whatever the user's layout maps them to."""
    device, fake = driver
    device.type_text("hi")

    assert len(fake.sent) == 4  # down/up per character
    for event in fake.sent:
        assert event.union.ki.dwFlags & windows._KEYEVENTF_UNICODE
        assert event.union.ki.wVk == 0


def test_typed_characters_round_trip(driver):
    device, fake = driver
    device.type_text("aB")

    scans = [e.union.ki.wScan for e in fake.sent]
    assert scans == [ord("a"), ord("a"), ord("B"), ord("B")]


def test_non_ascii_text_types_correctly(driver):
    """The reason for unicode scancodes, stated as a test."""
    device, fake = driver
    device.type_text("é€")

    scans = {e.union.ki.wScan for e in fake.sent}
    assert scans == {ord("é"), ord("€")}


def test_typing_nothing_sends_nothing(driver):
    device, fake = driver
    device.type_text("")

    assert fake.sent == []


# -- chords ----------------------------------------------------------------


def test_a_chord_presses_then_releases_in_reverse(driver):
    """ctrl down, s down, s up, ctrl up. Any other order leaves a key stuck."""
    device, fake = driver
    device.key("ctrl+s")

    assert len(fake.sent) == 4
    ups = [bool(e.union.ki.dwFlags & windows._KEYEVENTF_KEYUP) for e in fake.sent]
    assert ups == [False, False, True, True]
    assert fake.sent[0].union.ki.wVk == windows._VK["ctrl"]
    assert fake.sent[3].union.ki.wVk == windows._VK["ctrl"]


def test_named_keys_are_recognised(driver):
    device, fake = driver
    device.key("alt+f4")

    codes = {e.union.ki.wVk for e in fake.sent}
    assert windows._VK["alt"] in codes
    assert windows._VK["f4"] in codes


def test_an_unknown_key_is_refused(driver):
    device, _ = driver
    with pytest.raises(ValueError, match="unknown key"):
        device.key("ctrl+nonsense")


def test_an_empty_chord_is_refused(driver):
    device, _ = driver
    with pytest.raises(ValueError, match="empty key chord"):
        device.key("  ")


# -- the panic key ---------------------------------------------------------


def test_the_panic_key_aborts_the_next_action(driver):
    """An abort has to land within one action, not at the end of the task."""
    device, _ = driver
    watcher = panic_watcher()
    device.panic = watcher

    device.click(5, 5)
    watcher.triggered.set()

    with pytest.raises(PanicAbort, match="input released"):
        device.click(5, 5)


def test_panic_blocks_typing_and_chords_too(driver):
    device, _ = driver
    watcher = panic_watcher()
    watcher.triggered.set()
    device.panic = watcher

    with pytest.raises(PanicAbort):
        device.type_text("x")
    with pytest.raises(PanicAbort):
        device.key("ctrl+s")


def test_no_panic_watcher_means_no_interference(driver):
    device, fake = driver
    device.panic = None

    device.click(5, 5)
    assert fake.sent


def test_the_watcher_stops_cleanly(driver):
    """A daemon thread that outlives the run is a leak, not a feature."""
    watcher = panic_watcher(interval=0.01)
    with watcher:
        pass

    assert watcher._thread is None or not watcher._thread.is_alive()


def test_focus_loss_does_not_abort(driver):
    """Deliberate: focus changes constantly during legitimate automation."""
    device, fake = driver
    device.panic = panic_watcher()
    fake.title = "Something Else Entirely"

    device.click(5, 5)  # must not raise


def test_panic_detection_requires_all_three_keys():
    held = {windows._VK["ctrl"], windows._VK["alt"]}

    class Partial:
        def GetAsyncKeyState(self, vk):
            return 0x8000 if vk in held else 0

    assert not windows._panic_pressed(Partial())
    held.add(windows._VK["esc"])
    assert windows._panic_pressed(Partial())


# -- delivery failures -----------------------------------------------------


def test_partial_delivery_is_reported_not_swallowed(driver):
    """UIPI blocks input to elevated windows. "The click did nothing" is no answer."""
    device, fake = driver
    fake.delivered_count = 1

    with pytest.raises(OSError, match="elevated"):
        device.click(5, 5)


def test_the_driver_refuses_to_construct_off_windows(monkeypatch):
    monkeypatch.setattr(windows.sys, "platform", "linux")

    with pytest.raises(RuntimeError, match="requires Windows"):
        WindowsDriver()


# -- integration with the tier --------------------------------------------


def test_the_l3_adapter_accepts_this_driver(driver, tmp_path):
    """The seam holds: nothing above the driver changes."""
    from minos.tiers.l3_gui import GuiAdapter

    device, fake = driver
    adapter = GuiAdapter(driver=device)

    from minos.types import ActionRequest

    prepared = adapter.prepare(
        ActionRequest(
            goal_id="g", intent="click ok", operation="ui.click", params={"x": 10, "y": 10}
        )
    )
    prepared.execute(None)  # type: ignore[arg-type]

    assert fake.sent, "the adapter drove the real backend"


def test_threading_event_is_shareable_across_the_run():
    """One watcher, many actions: the flag is the shared thing, not the driver."""
    watcher = panic_watcher()
    assert isinstance(watcher.triggered, threading.Event)


def test_the_persons_cursor_is_put_back_after_a_click(driver):
    """The agent borrows the one system cursor for a click; it gives it back."""
    device, fake = driver
    fake.moved_to = []
    device.click(100, 200)
    assert fake.moved_to == [fake.cursor]


def test_the_ghost_points_before_the_adapter_acts(driver):
    from minos.tiers.l3_gui import GuiAdapter
    from minos.types import ActionRequest

    device, _fake = driver
    order: list[str] = []

    class Ghost:
        def point(self, x, y, label):
            order.append(f"ghost {x},{y} {label}")

    device.ghost = Ghost()
    real_send = device._send

    def send(*events):
        order.append("input")
        return real_send(*events)

    device._send = send
    prep = GuiAdapter(device).prepare(
        ActionRequest(goal_id="g", intent="c", operation="ui.click", params={"x": 10, "y": 20})
    )
    prep.execute(None)
    assert order == ["ghost 10,20 click (10, 20)", "input"]
