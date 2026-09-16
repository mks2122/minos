"""L3 GUI tier.

These test what L3 can and cannot *promise*, which is a property of the tier
rather than of whichever driver sits behind it -- so a stub driver is the right
instrument, not a compromise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from writ.audit import AuditLog
from writ.broker import Broker
from writ.checkpoint import FileCheckpointStore
from writ.router import Router
from writ.scopes import ScopeSet
from writ.tiers.l1_system import FilesystemAdapter
from writ.tiers.l2_adapters import TabularAdapter
from writ.tiers.l3_gui import CuaDriver, GuiAdapter, StubDriver
from writ.types import ActionRequest, EffectClass, Tier


@pytest.fixture
def driver() -> StubDriver:
    return StubDriver()


@pytest.fixture
def rig(tmp_path: Path, driver: StubDriver):
    ws = tmp_path / "ws"
    ws.mkdir()
    router = Router(adapters=(FilesystemAdapter(), TabularAdapter(), GuiAdapter(driver)))
    broker = Broker(
        scopes=ScopeSet.parse(
            [
                f"fs.read:{ws}/**",
                f"fs.write:{ws}/**",
                "ui.input:*",
            ]
        ),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
        approver=lambda inv, dec: True,
    )

    def run(operation: str, **params):
        routed = router.route(
            ActionRequest(goal_id="t", intent=operation, operation=operation, params=params)
        )
        return broker.submit(routed.invocation, routed.execute)

    return ws, broker, router, run


# -- the tier's promises ---------------------------------------------------


def test_a_click_is_irreversible_and_prompts(rig):
    """No inverse, no checkpoint, so consent every time."""
    _, broker, router, _ = rig
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="click",
            operation="ui.click",
            params={"x": 100, "y": 200},
        )
    )
    assert routed.invocation.contract.effect_class is EffectClass.IRREVERSIBLE
    assert broker.admit(routed.invocation).verdict == "prompt"


def test_a_click_without_approval_does_not_happen(tmp_path, driver):
    router = Router(adapters=(GuiAdapter(driver),))
    broker = Broker(
        scopes=ScopeSet.parse(["ui.input:*"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )  # default approver denies
    routed = router.route(
        ActionRequest(goal_id="t", intent="click", operation="ui.click", params={"x": 10, "y": 10})
    )
    assert broker.submit(routed.invocation, routed.execute).status == "denied"
    assert driver.events == []


def test_the_contract_admits_it_does_not_know_what_changed(rig):
    _, _, router, _ = rig
    routed = router.route(
        ActionRequest(goal_id="t", intent="click", operation="ui.click", params={"x": 1, "y": 1})
    )
    assert "effects unknown to this runtime" in routed.invocation.contract.expect


def test_screenshot_is_pure(rig):
    _, _, _, run = rig
    out = run("ui.screenshot")
    assert out.status == "ok"
    assert out.invocation.contract.effect_class is EffectClass.PURE
    assert out.checkpoint_id is None
    assert "digest" in out.result


def test_declared_targets_make_an_interaction_reversible(rig):
    """If the caller can name what will change, L3 stops being a one-way door."""
    ws, broker, router, _ = rig
    target = ws / "doc.txt"
    target.write_text("before", encoding="utf-8")

    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="type into the editor",
            operation="ui.type",
            params={"text": "hello", "targets": [str(target)]},
        )
    )
    assert routed.invocation.contract.effect_class is EffectClass.REVERSIBLE
    assert broker.admit(routed.invocation).verdict == "allow"

    out = broker.submit(
        routed.invocation,
        lambda inv: target.write_text("after", encoding="utf-8"),
    )
    assert out.status == "ok"
    assert out.checkpoint_id is not None

    # Verification switched to the files, because the contract promised
    # something about a file. A screen digest would be the wrong instrument.
    assert out.observed is not None
    assert out.observed.kind == "file_hash"

    assert broker.store.restore(out.checkpoint_id).succeeded
    assert target.read_text() == "before"


def test_the_screen_oracle_labels_itself_as_weak(rig):
    _, _, _, run = rig
    out = run("ui.screenshot")
    assert out.observed is not None
    assert out.observed.kind == "screen"
    assert "not a system-of-record read" in out.observed.before["_weak"]


def test_ui_input_scope_is_enforced(tmp_path, driver):
    router = Router(adapters=(GuiAdapter(driver),))
    broker = Broker(
        scopes=ScopeSet.parse(["ui.input:window.class=soffice"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
        approver=lambda inv, dec: True,
    )
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="click elsewhere",
            operation="ui.click",
            params={"x": 1, "y": 1, "window": "window.class=chrome"},
        )
    )
    assert broker.submit(routed.invocation, routed.execute).status == "denied"
    assert driver.events == []


# -- routing ---------------------------------------------------------------


def test_l3_is_never_chosen_when_l1_or_l2_can_serve(rig):
    """Reaching pixels for something a typed adapter handles is a bug."""
    ws, _, router, _ = rig
    for operation, params in (
        ("fs.write", {"path": str(ws / "a.txt"), "content": "x"}),
        ("sheet.set_cell", {"path": str(ws / "b.csv"), "cell": "A1", "value": "1"}),
    ):
        routed = router.route(
            ActionRequest(goal_id="t", intent=operation, operation=operation, params=params)
        )
        assert routed.invocation.tier is not Tier.L3_GUI


def test_reaching_l3_is_recorded_as_a_degradation(rig):
    _, _, router, _ = rig
    routed = router.route(
        ActionRequest(goal_id="t", intent="click", operation="ui.click", params={"x": 5, "y": 5})
    )
    assert routed.invocation.tier is Tier.L3_GUI
    assert routed.invocation.tier_reason


def test_gui_rate_is_published(rig):
    _, _, router, run = rig
    run("ui.screenshot")
    summary = router.stats.summary()
    assert summary["L3"] == 1
    assert summary["gui_rate"] == 1.0


# -- driver ----------------------------------------------------------------


def test_stub_driver_records_input(driver):
    driver.click(10, 20)
    driver.type_text("hi")
    driver.key("ctrl+s")
    assert driver.events == ["click:left:10,20", "type:2", "key:ctrl+s"]
    assert driver.typed == "hi"


def test_stub_driver_rejects_off_screen_clicks(driver):
    with pytest.raises(ValueError, match="off-screen"):
        driver.click(99_999, 10)


def test_screen_digest_changes_with_state(driver):
    before = driver.observe().digest
    driver.type_text("something")
    assert driver.observe().digest != before


def test_cua_driver_fails_loudly_rather_than_silently(rig):
    """A driver that quietly does nothing looks exactly like a task that failed."""
    with pytest.raises(NotImplementedError, match="cua-driver"):
        CuaDriver()


def test_bad_click_params_decline_to_the_router(rig):
    from writ.router import NoAdapter

    _, _, router, _ = rig
    with pytest.raises(NoAdapter, match="declined"):
        router.route(
            ActionRequest(
                goal_id="t", intent="click", operation="ui.click", params={"x": "left-ish"}
            )
        )
