"""The memory adapter -- the planner asking "which file did you mean?".

The memory layer existed for a while as a standalone command the agent could
not reach. These tests pin the wiring, and more importantly the two properties
that make it safe to expose: recall cannot surface paths outside its roots, and
what it returns is a suggestion rather than an authority.
"""

from __future__ import annotations

import csv
import os
import time
from pathlib import Path

import pytest

from writ.audit import AuditLog
from writ.broker import Broker
from writ.checkpoint import FileCheckpointStore
from writ.memory import MemoryStore
from writ.planner.schemas import operation_for_tool, tool_definitions
from writ.router import Router
from writ.scopes import ScopeSet
from writ.tiers.l1_system import FilesystemAdapter, MemoryAdapter
from writ.tiers.l2_adapters import TabularAdapter
from writ.types import ActionRequest, EffectClass, Tier

DAY = 86_400.0


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    with open(ws / "sales_2025.csv", "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows([["Quarter", "Revenue"], ["Q3", "41800"]])
    (ws / "notes.txt").write_text("meeting notes", encoding="utf-8")

    outside = tmp_path / "secrets"
    outside.mkdir()
    (outside / "passwords.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    return ws


@pytest.fixture
def rig(workspace: Path, tmp_path: Path):
    store = MemoryStore()
    store.index_tree(workspace)
    store.index_tree(tmp_path / "secrets")  # memory knows about it...

    router = Router(
        adapters=(
            FilesystemAdapter(),
            MemoryAdapter(store, roots=(workspace,)),  # ...but recall is rooted
            TabularAdapter(),
        )
    )
    broker = Broker(
        scopes=ScopeSet.parse(
            [
                f"fs.read:{workspace}/**",
                f"fs.write:{workspace}/**",
                f"memory.read:{workspace}/**",
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

    return workspace, store, broker, router, run


def age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


# -- the planner can reach it ---------------------------------------------


def test_memory_tools_are_offered_to_the_planner():
    """The gap this fixes: the tools existed but no model could call them."""
    names = {t["name"] for t in tool_definitions(("memory.recall", "memory.recent"))}
    assert "memory_recall" in names
    assert "memory_recent" in names
    assert operation_for_tool("memory_recall") == "memory.recall"


def test_recall_routes_to_l1(rig):
    _, _, _, router, _ = rig
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="which file",
            operation="memory.recall",
            params={"phrase": "the spreadsheet"},
        )
    )
    assert routed.invocation.tier is Tier.L1_SYSTEM
    assert routed.invocation.adapter == "l1.memory"


def test_recall_is_pure(rig):
    _, _, _, _, run = rig
    out = run("memory.recall", phrase="the spreadsheet")
    assert out.invocation.contract.effect_class is EffectClass.PURE
    assert out.checkpoint_id is None


# -- resolution ------------------------------------------------------------


def test_recall_resolves_a_vague_reference(rig):
    workspace, _, _, _, run = rig
    out = run("memory.recall", phrase="the spreadsheet")

    assert out.status == "ok"
    assert out.result["path"] == str(workspace / "sales_2025.csv")


def test_recall_explains_itself(rig):
    """An unexplained resolution is an unauditable one."""
    _, _, _, _, run = rig
    out = run("memory.recall", phrase="the spreadsheet")
    assert "matches the kind of file you named" in out.result["why"]


def test_recall_reports_when_it_is_unsure(rig):
    workspace, store, _unused_broker, _unused_router, run = rig
    for name in ("sales_a.csv", "sales_b.csv"):
        (workspace / name).write_text("x,y\n", encoding="utf-8")
        age(workspace / name, 1.2 * DAY)
    age(workspace / "sales_2025.csv", 90 * DAY)
    store.index_tree(workspace)

    out = run("memory.recall", phrase="the sales spreadsheet from yesterday")
    assert out.result["confident"] is False
    assert out.result["alternatives"]


def test_recall_on_nothing_matching(rig):
    _, _, _, _, run = rig
    out = run("memory.recall", phrase="the quarterly pdf")
    assert out.result["path"] is None
    assert out.result["confident"] is False


def test_recall_without_a_phrase_declines(rig):
    from writ.router import NoAdapter

    _, _, _, router, _ = rig
    with pytest.raises(NoAdapter, match="declined"):
        router.route(ActionRequest(goal_id="t", intent="x", operation="memory.recall", params={}))


# -- containment -----------------------------------------------------------


def test_recall_cannot_surface_paths_outside_its_roots(rig):
    """Memory indexed the secrets folder; recall must not reveal it.

    A listing you could not have obtained is a leak even if you cannot open
    what it names.
    """
    workspace, store, _, _, run = rig
    assert any("passwords" in f.name for f in store.files())  # memory knows

    out = run("memory.recall", phrase="passwords")
    assert out.status == "ok"

    # It may still name an in-root file -- resolution always ranks something.
    # The property that matters is that nothing outside the roots is reachable,
    # by any route.
    named = [out.result["path"], *out.result.get("alternatives", [])]
    for path in filter(None, named):
        assert "secrets" not in path
        assert Path(path).is_relative_to(workspace)


def test_recall_denied_without_the_memory_scope(workspace, tmp_path):
    store = MemoryStore()
    store.index_tree(workspace)
    router = Router(adapters=(MemoryAdapter(store, roots=(workspace,)),))
    broker = Broker(
        scopes=ScopeSet.parse([f"fs.read:{workspace}/**"]),  # no memory.read
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="recall",
            operation="memory.recall",
            params={"phrase": "the spreadsheet"},
        )
    )
    assert broker.submit(routed.invocation, routed.execute).status == "denied"


def test_a_recalled_path_is_still_gated(rig, tmp_path):
    """Resolution names a path; the broker still decides. Suggestion, not authority."""
    workspace, _store, _broker, router, run = rig
    out = run("memory.recall", phrase="the spreadsheet")
    suggested = out.result["path"]
    assert suggested

    tight = Broker(
        scopes=ScopeSet.parse([f"memory.read:{workspace}/**"]),  # no fs.read
        audit=AuditLog(tmp_path / "audit2.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp2"),
    )
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="read what memory suggested",
            operation="fs.read",
            params={"path": suggested},
        )
    )
    assert tight.submit(routed.invocation, routed.execute).status == "denied"


def test_an_unrooted_adapter_sees_everything(workspace, tmp_path):
    """The roots are the containment. Without them there is none -- which is why
    the CLI always constructs the adapter with the workspace."""
    store = MemoryStore()
    store.index_tree(tmp_path)
    adapter = MemoryAdapter(store)  # no roots
    assert adapter._within_roots(str(tmp_path / "secrets" / "passwords.csv"))

    rooted = MemoryAdapter(store, roots=(workspace,))
    assert not rooted._within_roots(str(tmp_path / "secrets" / "passwords.csv"))


# -- provenance ------------------------------------------------------------


def test_recent_lists_what_this_runtime_changed(rig):
    workspace, store, broker, router, run = rig
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="edit",
            operation="sheet.set_cell",
            params={"path": str(workspace / "sales_2025.csv"), "cell": "B2", "value": "9"},
        )
    )
    store.record_outcome(
        broker.submit(routed.invocation, routed.execute),
        session_id="s1",
        goal="update Q3",
    )

    out = run("memory.recent")
    assert out.status == "ok"
    assert out.result
    assert out.result[0]["goal"] == "update Q3"
    assert out.result[0]["operation"] == "sheet.set_cell"


def test_recent_respects_the_roots(rig, tmp_path):
    _workspace, store, _broker, router, run = rig
    outside = tmp_path / "secrets" / "passwords.csv"

    wide = Broker(
        scopes=ScopeSet.parse([f"fs.read:{tmp_path}/**", f"fs.write:{tmp_path}/**"]),
        audit=AuditLog(tmp_path / "audit3.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp3"),
    )
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="edit outside",
            operation="sheet.set_cell",
            params={"path": str(outside), "cell": "A1", "value": "x"},
        )
    )
    store.record_outcome(wide.submit(routed.invocation, routed.execute), goal="elsewhere")

    out = run("memory.recent")
    assert all("secrets" not in entry["path"] for entry in out.result)
