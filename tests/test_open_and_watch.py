"""Opening files, watching the filesystem, and app history.

Three gaps closed together, because they are the same gap seen from different
angles: the runtime only knew about the world it had itself touched, at the
moment it touched it.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from minos.audit import AuditLog
from minos.broker import Broker
from minos.checkpoint import FileCheckpointStore
from minos.memory import FileWatcher, MemoryStore, scan
from minos.memory.watch import diff
from minos.planner.schemas import operation_for_tool, tool_definitions
from minos.router import NoAdapter, Router
from minos.scopes import ScopeSet
from minos.tiers.l1_system import AppAdapter, FilesystemAdapter
from minos.tiers.l1_system.app import default_handler
from minos.types import ActionRequest, EffectClass, Tier


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")
    (ws / "notes.txt").write_text("notes", encoding="utf-8")
    return ws


class FakeAppAdapter(AppAdapter):
    """Real contract, no window. Launching a GUI in CI is not a test."""

    def __init__(self) -> None:
        super().__init__()
        self.opened: list[str] = []

    def prepare(self, request):  # type: ignore[no-untyped-def]
        prep = super().prepare(request)
        path = str(Path(str(request.params["path"])).expanduser().resolve())

        def execute(_inv):  # type: ignore[no-untyped-def]
            if not Path(path).exists():
                raise FileNotFoundError(path)
            self.opened.append(path)
            return {"opened": path, "handler": "fake"}

        return type(prep)(contract=prep.contract, execute=execute, grants=prep.grants)


@pytest.fixture
def rig(workspace: Path, tmp_path: Path):
    adapter = FakeAppAdapter()
    router = Router(adapters=(FilesystemAdapter(), adapter))
    broker = Broker(
        scopes=ScopeSet.parse(
            [
                f"fs.read:{workspace}/**",
                f"fs.write:{workspace}/**",
                f"app.open:{workspace}/**",
            ]
        ),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )

    def run(operation: str, **params):
        routed = router.route(
            ActionRequest(goal_id="t", intent=operation, operation=operation, params=params)
        )
        return broker.submit(routed.invocation, routed.execute)

    return workspace, adapter, broker, router, run


# -- app.open: the missing verb -------------------------------------------


def test_the_planner_is_offered_an_open_tool():
    """The gap: memory could find your image and nothing could show it."""
    names = {t["name"] for t in tool_definitions(("app.open",))}
    assert "app_open" in names
    assert operation_for_tool("app_open") == "app.open"


def test_open_routes_to_l1(rig):
    workspace, _, _, router, _ = rig
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="show me",
            operation="app.open",
            params={"path": str(workspace / "photo.png")},
        )
    )
    assert routed.invocation.tier is Tier.L1_SYSTEM
    assert routed.invocation.adapter == "l1.app"


def test_opening_a_file_works(rig):
    workspace, adapter, _, _, run = rig
    out = run("app.open", path=str(workspace / "photo.png"))

    assert out.status == "ok"
    assert out.result["opened"].endswith("photo.png")
    assert adapter.opened


def test_open_is_reversible_and_expects_no_change(rig):
    """A viewer should not move the file, so "unchanged" is success.

    Without expects_change=False the broker would read "nothing changed" as a
    failed effect and roll back a perfectly good open.
    """
    workspace, _, _, router, _ = rig
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="show me",
            operation="app.open",
            params={"path": str(workspace / "photo.png")},
        )
    )
    contract = routed.invocation.contract
    assert contract.effect_class is EffectClass.REVERSIBLE
    assert contract.change_expected is False
    assert contract.targets


def test_open_is_checkpointed_so_a_rewriting_handler_is_caught(rig):
    """The reason it is REVERSIBLE rather than PURE."""
    workspace, _, broker, router, _ = rig
    target = workspace / "photo.png"
    original = target.read_bytes()

    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="open",
            operation="app.open",
            params={"path": str(target)},
        )
    )

    def rewriting_handler(_inv):
        target.write_bytes(b"clobbered by the viewer")
        return {"opened": str(target), "handler": "bad"}

    out = broker.submit(routed.invocation, rewriting_handler)

    assert out.status == "failed"
    assert out.reversal is not None and out.reversal.succeeded
    assert target.read_bytes() == original


def test_open_outside_scope_is_denied(rig, tmp_path):
    _, adapter, _, _, run = rig
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(b"x")

    assert run("app.open", path=str(outside)).status == "denied"
    assert not adapter.opened


def test_open_needs_its_own_capability(workspace, tmp_path):
    """fs.read is not enough. Launching an application is a separate grant."""
    router = Router(adapters=(FakeAppAdapter(),))
    broker = Broker(
        scopes=ScopeSet.parse([f"fs.read:{workspace}/**"]),  # no app.open
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="open",
            operation="app.open",
            params={"path": str(workspace / "photo.png")},
        )
    )
    assert broker.submit(routed.invocation, routed.execute).status == "denied"


def test_opening_a_missing_file_fails_cleanly(rig):
    workspace, _, _, _, run = rig
    out = run("app.open", path=str(workspace / "nope.png"))
    assert out.status in ("failed", "reconciliation_required")
    assert "FileNotFoundError" in out.error


def test_open_without_a_path_declines(rig):
    _, _, _, router, _ = rig
    with pytest.raises(NoAdapter, match="declined"):
        router.route(ActionRequest(goal_id="t", intent="open", operation="app.open", params={}))


def test_the_handler_is_platform_appropriate():
    handler = default_handler()
    if sys.platform == "win32":
        assert handler[0] == "cmd"
    elif sys.platform == "darwin":
        assert handler == ("open",)
    else:
        assert handler == ("xdg-open",)


# -- app history -----------------------------------------------------------


def test_memory_records_what_was_opened(workspace):
    """ARCHITECTURE.md promised app history; this is the honest subset."""
    store = MemoryStore()
    store.record_open(workspace / "photo.png", "preview", goal="show me the photo")

    last = store.last_opened()
    assert last is not None
    assert last.path.endswith("photo.png")
    assert last.handler == "preview"
    assert last.goal == "show me the photo"


def test_openings_are_ordered_newest_first(workspace):
    store = MemoryStore()
    store.record_open(workspace / "notes.txt", "editor")
    time.sleep(0.01)
    store.record_open(workspace / "photo.png", "viewer")

    openings = store.openings()
    assert openings[0].path.endswith("photo.png")
    assert len(openings) == 2


def test_an_open_is_not_inferred_from_a_read(workspace, tmp_path):
    """Reading a file and opening it in an app are different events."""
    store = MemoryStore()
    router = Router(adapters=(FilesystemAdapter(),))
    broker = Broker(
        scopes=ScopeSet.parse([f"fs.read:{workspace}/**"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="read",
            operation="fs.read",
            params={"path": str(workspace / "notes.txt")},
        )
    )
    store.record_outcome(broker.submit(routed.invocation, routed.execute))

    assert store.touches()  # it was touched
    assert store.openings() == []  # but not opened


# -- the watcher -----------------------------------------------------------


def test_scan_snapshots_a_tree(workspace):
    snapshot = scan([workspace])
    assert len(snapshot) == 2
    assert any(p.endswith("photo.png") for p in snapshot)


def test_scan_skips_hidden(workspace):
    (workspace / ".hidden").write_text("x", encoding="utf-8")
    assert all(".hidden" not in p for p in scan([workspace]))


def test_scan_is_bounded(workspace):
    for i in range(20):
        (workspace / f"f{i}.txt").write_text("x", encoding="utf-8")
    assert len(scan([workspace], max_files=5)) == 5


def test_scan_tolerates_a_missing_root(tmp_path):
    assert scan([tmp_path / "nope"]) == {}


def test_diff_detects_each_kind():
    before = {"a": (1.0, 10), "b": (1.0, 10)}
    after = {"a": (2.0, 12), "c": (1.0, 5)}
    kinds = {c.path: c.kind for c in diff(before, after)}
    assert kinds == {"a": "modified", "b": "deleted", "c": "created"}


def test_watcher_sees_an_edit_made_outside_the_runtime(workspace):
    """The point: someone saving in Excel while minos is not looking."""
    store = MemoryStore()
    watcher = FileWatcher(store, roots=(workspace,))
    watcher.prime()

    (workspace / "notes.txt").write_text("edited by a human", encoding="utf-8")
    changes = watcher.poll_once()

    assert any(c.path.endswith("notes.txt") and c.kind == "modified" for c in changes)


def test_watcher_records_changes_into_memory(workspace):
    store = MemoryStore()
    watcher = FileWatcher(store, roots=(workspace,))
    watcher.prime()

    (workspace / "new.txt").write_text("appeared", encoding="utf-8")
    watcher.poll_once()

    assert any(f.name == "new.txt" for f in store.files())


def test_watcher_reports_nothing_when_nothing_moves(workspace):
    store = MemoryStore()
    watcher = FileWatcher(store, roots=(workspace,))
    watcher.prime()
    assert watcher.poll_once() == []


def test_watcher_drains_once(workspace):
    store = MemoryStore()
    watcher = FileWatcher(store, roots=(workspace,))
    watcher.prime()
    (workspace / "a.txt").write_text("x", encoding="utf-8")
    watcher.poll_once()

    assert watcher.drain()
    assert watcher.drain() == []  # drained, not duplicated


def test_watcher_runs_and_stops_on_a_thread(workspace):
    store = MemoryStore()
    seen: list[str] = []
    watcher = FileWatcher(
        store,
        roots=(workspace,),
        interval=0.05,
        on_change=lambda changes: seen.extend(c.path for c in changes),
    )
    with watcher:
        assert watcher.running
        (workspace / "threaded.txt").write_text("x", encoding="utf-8")
        deadline = time.time() + 5
        while time.time() < deadline and not seen:
            time.sleep(0.05)

    assert not watcher.running
    assert any("threaded.txt" in p for p in seen)


def test_watcher_survives_a_scan_error(workspace, monkeypatch):
    """A watcher that dies on one locked file is worse than one that misses it."""
    import minos.memory.watch as watch_module

    store = MemoryStore()
    watcher = FileWatcher(store, roots=(workspace,), interval=0.05)

    calls = {"n": 0}
    real_scan = watch_module.scan

    def flaky(*args, **kwargs):
        calls["n"] += 1
        # Let the baseline succeed; fail the loop's first poll.
        if calls["n"] == 2:
            raise OSError("device not ready")
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(watch_module, "scan", flaky)
    watcher.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and calls["n"] < 3:
            time.sleep(0.05)
        assert watcher.running  # still alive after the failure
    finally:
        watcher.stop()

    assert calls["n"] >= 3
    # The failure is recorded, not swallowed: a silent watcher is
    # indistinguishable from a working one that found nothing.
    assert any("device not ready" in e for e in watcher.errors)


def test_the_store_is_usable_from_another_thread(workspace):
    """Regression: sqlite3 connections are thread-bound by default.

    The watcher records from its own thread, so a thread-bound connection
    raised every cycle -- and the loop's broad except made that look identical
    to a watcher that worked and found nothing.
    """
    import threading

    store = MemoryStore()
    errors: list[BaseException] = []

    def record() -> None:
        try:
            store.record_file(workspace / "notes.txt")
            store.record_open(workspace / "photo.png", "viewer")
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=record)
    thread.start()
    thread.join(timeout=5)

    assert not errors, f"store is not thread-safe: {errors}"
    assert store.last_opened() is not None


def test_a_failed_baseline_does_not_take_the_run_down(workspace, monkeypatch):
    """A watcher is an accessory. One that crashes the run it serves is worse
    than one that misses an edit -- but it must say so rather than go quiet."""
    import minos.memory.watch as watch_module

    store = MemoryStore()
    watcher = FileWatcher(store, roots=(workspace,))

    def always_fails(*args, **kwargs):
        raise OSError("volume disappeared")

    monkeypatch.setattr(watch_module, "scan", always_fails)

    watcher.start()  # must not raise
    assert not watcher.running
    assert any("baseline" in e for e in watcher.errors)
    watcher.stop()
