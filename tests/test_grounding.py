"""Grounding: clicking controls by name instead of by coordinate.

Coordinate clicking fails silently -- the click lands on whatever happens to be
at that pixel. These tests cover the lookup rules, and in particular the two
that decide whether an agent clicks the right button: ambiguity is raised rather
than resolved, and resolution happens late rather than at planning time.

The real UI Automation tree needs a live Windows desktop, so the tree itself is
faked here. What is tested is the *policy* around it, which is where the
mistakes that matter live.
"""

from __future__ import annotations

import pytest

from minos.tiers.base import OperationUnsupported
from minos.tiers.l3_gui import GuiAdapter, StubDriver
from minos.tiers.l3_gui.uia import (
    AmbiguousElement,
    Element,
    ElementNotFound,
    _normalise,
)
from minos.types import ActionRequest


def element(name, control_type="button", **kwargs):
    return Element(name=name, control_type=control_type, width=80, height=24, **kwargs)


class FakeTree:
    """A stand-in for the live accessibility tree."""

    def __init__(self, elements, invokable=True):
        self._elements = tuple(elements)
        self._invokable = invokable
        self.invoked: list[str] = []

    def elements(self, window="*"):
        return self._elements

    def invoke(self, target):
        if not self._invokable:
            return False
        self.invoked.append(target.name)
        return True

    # Mirrors UiaTree.find so the policy under test is the real one.
    find = None


@pytest.fixture
def fake_uia(monkeypatch):
    """Install a fake tree, and let each test decide what is on screen."""
    from minos.tiers.l3_gui import uia

    holder = {}

    class Tree(uia.UiaTree):
        def __init__(self):
            self.name = "fake"
            self._automation = None

        def elements(self, window="*"):
            return holder["elements"]

        def invoke(self, target):
            if not holder.get("invokable", True):
                return False
            holder.setdefault("invoked", []).append(target.name)
            return True

    monkeypatch.setattr(uia, "UiaTree", Tree)
    monkeypatch.setattr(uia, "available", lambda: True)
    return holder


# -- matching rules --------------------------------------------------------


def test_accelerators_and_ellipses_are_ignored():
    """A task says "Save"; the control is called "&Save..."."""
    assert _normalise("&Save...") == _normalise("save")
    assert _normalise("  Open &File  ") == "open file"


def test_an_exact_name_wins_over_a_substring(fake_uia):
    from minos.tiers.l3_gui.uia import UiaTree

    fake_uia["elements"] = (element("Save As"), element("Save"))

    assert UiaTree().find("Save").name == "Save"


def test_a_substring_matches_when_nothing_is_exact(fake_uia):
    from minos.tiers.l3_gui.uia import UiaTree

    fake_uia["elements"] = (element("Save document as..."),)

    assert UiaTree().find("save document").name == "Save document as..."


def test_two_matches_is_a_question_not_a_coin_flip(fake_uia):
    """This is how an agent clicks "Don't Save"."""
    from minos.tiers.l3_gui.uia import UiaTree

    fake_uia["elements"] = (element("Save", control_type="button"), element("Save", "menuitem"))

    with pytest.raises(AmbiguousElement) as excinfo:
        UiaTree().find("Save")

    assert len(excinfo.value.candidates) == 2
    assert "more precisely" in str(excinfo.value)


def test_control_type_disambiguates(fake_uia):
    from minos.tiers.l3_gui.uia import UiaTree

    fake_uia["elements"] = (element("Save", "button"), element("Save", "menuitem"))

    assert UiaTree().find("Save", control_type="menuitem").control_type == "menuitem"


def test_a_missing_control_is_reported_not_guessed(fake_uia):
    from minos.tiers.l3_gui.uia import UiaTree

    fake_uia["elements"] = (element("Cancel"),)

    with pytest.raises(ElementNotFound, match="no control named"):
        UiaTree().find("Save")


def test_a_disabled_control_says_so(fake_uia):
    """ "Disabled" and "absent" need different answers from the caller."""
    from minos.tiers.l3_gui.uia import UiaTree

    fake_uia["elements"] = (element("Save", enabled=False),)

    with pytest.raises(ElementNotFound, match="disabled"):
        UiaTree().find("Save")


def test_the_centre_is_where_a_fallback_click_lands():
    assert element("Save", left=100, top=200).centre == (140, 212)


# -- the adapter -----------------------------------------------------------


def test_clicking_by_name_invokes_without_moving_the_cursor(fake_uia):
    """The mitigation for sharing the desktop, tested rather than promised."""
    fake_uia["elements"] = (element("Save", invokable=True),)
    driver = StubDriver()
    adapter = GuiAdapter(driver=driver)

    prepared = adapter.prepare(
        ActionRequest(goal_id="g", intent="save", operation="ui.click", params={"element": "Save"})
    )
    result = prepared.execute(None)

    assert result["method"] == "invoke"
    assert fake_uia["invoked"] == ["Save"]
    assert driver.events == [], "the cursor must not move when invoking"


def test_it_falls_back_to_clicking_the_centre(fake_uia):
    """Plenty of applications draw their own controls and invoke nothing."""
    fake_uia["elements"] = (element("Save", left=100, top=200),)
    fake_uia["invokable"] = False
    driver = StubDriver()

    prepared = GuiAdapter(driver=driver).prepare(
        ActionRequest(goal_id="g", intent="save", operation="ui.click", params={"element": "Save"})
    )
    result = prepared.execute(None)

    assert result["method"] == "click"
    assert result["at"] == [140, 212]
    assert driver.events == ["click:left:140,212"]


def test_resolution_happens_at_execute_time_not_at_prepare(fake_uia):
    """A control found while planning may have moved or closed by admission."""
    fake_uia["elements"] = ()
    adapter = GuiAdapter(driver=StubDriver())

    # Preparing against an empty screen must not fail: nothing has been
    # admitted yet, and the tree is live.
    prepared = adapter.prepare(
        ActionRequest(goal_id="g", intent="save", operation="ui.click", params={"element": "Save"})
    )

    fake_uia["elements"] = (element("Save"),)
    assert prepared.execute(None)["method"] == "invoke"


def test_coordinates_still_work_when_no_element_is_named():
    driver = StubDriver()
    prepared = GuiAdapter(driver=driver).prepare(
        ActionRequest(goal_id="g", intent="click", operation="ui.click", params={"x": 5, "y": 6})
    )
    prepared.execute(None)

    assert driver.events == ["click:left:5,6"]


def test_without_uia_the_adapter_says_what_to_do(monkeypatch):
    from minos.tiers.l3_gui import uia

    monkeypatch.setattr(uia, "available", lambda: False)
    prepared = GuiAdapter(driver=StubDriver()).prepare(
        ActionRequest(goal_id="g", intent="save", operation="ui.click", params={"element": "Save"})
    )

    with pytest.raises(OperationUnsupported, match="comtypes"):
        prepared.execute(None)


def test_ambiguity_reaches_the_caller_through_the_adapter(fake_uia):
    fake_uia["elements"] = (element("Save", "button"), element("Save", "menuitem"))
    prepared = GuiAdapter(driver=StubDriver()).prepare(
        ActionRequest(goal_id="g", intent="save", operation="ui.click", params={"element": "Save"})
    )

    with pytest.raises(AmbiguousElement):
        prepared.execute(None)


def test_the_contract_names_the_control(fake_uia):
    """The approval prompt should say what will be clicked, not a coordinate."""
    fake_uia["elements"] = (element("Save"),)
    prepared = GuiAdapter(driver=StubDriver()).prepare(
        ActionRequest(goal_id="g", intent="save", operation="ui.click", params={"element": "Save"})
    )

    assert "'Save'" in prepared.contract.expect


def test_the_planner_is_told_to_prefer_names():
    """A model given both options will use coordinates unless told not to."""
    from minos.planner.schemas import tool_definitions

    click = next(t for t in tool_definitions(("ui.click",)) if t["name"] == "ui_click")

    assert "element" in click["input_schema"]["properties"]
    assert "silently" in click["description"]


# -- COM returns NULL pointers, not None -----------------------------------
#
# Both bugs below were invisible until the tree was pointed at a real desktop:
# M22 only ever tested against a fake. They are kept as unit tests because a
# live accessibility tree is not something CI can rely on.


def test_a_null_native_element_is_skipped_not_dereferenced():
    """comtypes yields a NULL *pointer object* for a childless element.

    It is not None, so `is not None` passes -- and the next attribute access
    raises COMError('Invalid pointer'). Truthiness is the only check that works.
    """
    from minos.tiers.l3_gui.uia import _to_element

    class NullPointer:
        def __bool__(self):
            return False

        def __getattr__(self, name):
            raise OSError("Invalid pointer")

    assert _to_element(NullPointer()) is None


def test_an_element_that_raises_midway_is_skipped():
    """A node can go stale between being walked and being read."""
    from minos.tiers.l3_gui.uia import _to_element

    class Stale:
        def __bool__(self):
            return True

        @property
        def CurrentBoundingRectangle(self):
            raise OSError("element no longer available")

    assert _to_element(Stale()) is None


def test_walking_a_falsy_node_returns_nothing(monkeypatch):
    from minos.tiers.l3_gui import uia

    class Tree(uia.UiaTree):
        def __init__(self):
            self._automation = None
            self.name = "fake"

    class NullNode:
        def __bool__(self):
            return False

    assert Tree()._walk(NullNode()) == []


def test_the_focused_window_is_the_window_not_the_focused_control():
    """GetFocusedElement returns a control, whose subtree is usually empty.

    Walking from there finds nothing and is indistinguishable from an app with
    no accessibility support -- so the root is resolved from the window handle.
    """
    import inspect

    from minos.tiers.l3_gui.uia import UiaTree

    source = inspect.getsource(UiaTree._root)
    assert "GetForegroundWindow" in source
    assert "ElementFromHandle" in source
