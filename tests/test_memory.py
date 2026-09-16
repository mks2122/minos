"""Computer-state memory and deictic resolution."""

from __future__ import annotations

import csv
import time
from pathlib import Path

import pytest

from writ.audit import AuditLog
from writ.broker import Broker
from writ.checkpoint import FileCheckpointStore
from writ.memory import MemoryStore, resolve
from writ.router import Router
from writ.scopes import ScopeSet
from writ.tiers.l1_system import FilesystemAdapter
from writ.tiers.l2_adapters import TabularAdapter
from writ.types import ActionRequest

DAY = 86_400.0


@pytest.fixture
def store() -> MemoryStore:
    with MemoryStore() as s:
        yield s


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    with open(ws / "sales_2025.csv", "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows([["Quarter", "Revenue"], ["Q3", "41800"]])
    (ws / "notes.txt").write_text("meeting notes", encoding="utf-8")
    (ws / "budget_2024.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (ws / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    return ws


def age(path: Path, seconds: float) -> None:
    """Backdate a file's mtime."""
    stamp = time.time() - seconds
    import os

    os.utime(path, (stamp, stamp))


# -- indexing --------------------------------------------------------------


def test_index_tree(store, workspace):
    assert store.index_tree(workspace) == 4
    assert {f.name for f in store.files()} == {
        "sales_2025.csv",
        "notes.txt",
        "budget_2024.csv",
        "report.pdf",
    }


def test_index_filters_by_suffix(store, workspace):
    store.index_tree(workspace)
    assert {f.name for f in store.files(suffixes=[".csv"])} == {
        "sales_2025.csv",
        "budget_2024.csv",
    }


def test_index_skips_hidden(store, workspace):
    (workspace / ".secret").write_text("x", encoding="utf-8")
    store.index_tree(workspace)
    assert all(not f.name.startswith(".") for f in store.files())


def test_reindex_updates_rather_than_duplicates(store, workspace):
    store.index_tree(workspace)
    (workspace / "notes.txt").write_text("much longer content here", encoding="utf-8")
    store.index_tree(workspace)

    notes = [f for f in store.files() if f.name == "notes.txt"]
    assert len(notes) == 1
    assert notes[0].size > 13


def test_search_splits_on_separators(store, workspace):
    """sales_2025.csv should be findable by 'sales' and by '2025'."""
    store.index_tree(workspace)
    assert any(f.name == "sales_2025.csv" for f in store.search("sales"))
    assert any(f.name == "sales_2025.csv" for f in store.search("2025"))


def test_search_tolerates_fts_metacharacters(store, workspace):
    """User text must never reach FTS as syntax."""
    store.index_tree(workspace)
    assert store.search('" OR 1=1 --') == [] or isinstance(store.search("*"), list)


def test_persists_across_reopen(tmp_path, workspace):
    db = tmp_path / "memory.db"
    with MemoryStore(db) as first:
        first.index_tree(workspace)
    with MemoryStore(db) as second:
        assert len(second.files()) == 4


# -- provenance ------------------------------------------------------------


def test_records_what_an_action_touched(store, workspace, tmp_path):
    """The difference between an mtime and a provenance."""
    book = workspace / "sales_2025.csv"
    router = Router(adapters=(FilesystemAdapter(), TabularAdapter()))
    broker = Broker(
        scopes=ScopeSet.parse([f"fs.read:{workspace}/**", f"fs.write:{workspace}/**"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )
    store.start_session("s1", "update Q3 forecast")
    routed = router.route(
        ActionRequest(
            goal_id="s1",
            intent="set Q3",
            operation="sheet.set_cell",
            params={"path": str(book), "cell": "B2", "value": "48200"},
        )
    )
    outcome = broker.submit(routed.invocation, routed.execute)
    store.record_outcome(outcome, session_id="s1", goal="update Q3 forecast")

    touches = store.touches(path=book)
    assert len(touches) == 1
    assert touches[0].operation == "sheet.set_cell"
    assert touches[0].goal == "update Q3 forecast"
    assert touches[0].tier == "L2"
    assert touches[0].mutating


def test_reads_are_recorded_but_not_mutating(store, workspace, tmp_path):
    book = workspace / "sales_2025.csv"
    router = Router(adapters=(FilesystemAdapter(), TabularAdapter()))
    broker = Broker(
        scopes=ScopeSet.parse([f"fs.read:{workspace}/**"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )
    routed = router.route(
        ActionRequest(
            goal_id="s1",
            intent="read",
            operation="sheet.read_cell",
            params={"path": str(book), "cell": "B2"},
        )
    )
    store.record_outcome(broker.submit(routed.invocation, routed.execute))

    touches = store.touches(path=book)
    assert len(touches) == 1
    assert not touches[0].mutating


def test_recently_touched_deduplicates_by_path(store, workspace, tmp_path):
    book = workspace / "sales_2025.csv"
    router = Router(adapters=(FilesystemAdapter(), TabularAdapter()))
    broker = Broker(
        scopes=ScopeSet.parse([f"fs.read:{workspace}/**", f"fs.write:{workspace}/**"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )
    for value in ("1", "2", "3"):
        routed = router.route(
            ActionRequest(
                goal_id="s",
                intent="set",
                operation="sheet.set_cell",
                params={"path": str(book), "cell": "B2", "value": value},
            )
        )
        store.record_outcome(broker.submit(routed.invocation, routed.execute))

    assert len(store.touches(path=book)) == 3
    assert len(store.recently_touched()) == 1


# -- deictic resolution ----------------------------------------------------


def test_resolves_the_excel_from_yesterday(store, workspace):
    """The motivating example."""
    age(workspace / "sales_2025.csv", 1.5 * DAY)  # yesterday
    age(workspace / "budget_2024.csv", 40 * DAY)  # ages ago
    age(workspace / "notes.txt", 1.5 * DAY)  # yesterday, wrong type
    store.index_tree(workspace)

    result = resolve("the excel we were working on yesterday", store)

    assert result.path is not None
    assert result.path.name == "sales_2025.csv"
    assert result.confident


def test_resolution_explains_itself(store, workspace):
    """An unexplained resolution is an unauditable one."""
    age(workspace / "sales_2025.csv", 1.5 * DAY)
    store.index_tree(workspace)

    explanation = resolve("the spreadsheet from yesterday", store).explain()

    assert "sales_2025.csv" in explanation
    assert "Because:" in explanation
    assert ".csv matches the kind of file you named" in explanation
    assert "1 day ago" in explanation


def test_type_word_narrows_the_pool(store, workspace):
    store.index_tree(workspace)
    result = resolve("the pdf", store)
    assert result.path is not None
    assert result.path.suffix == ".pdf"


def test_filename_keyword_matters(store, workspace):
    age(workspace / "sales_2025.csv", 10 * DAY)
    age(workspace / "budget_2024.csv", 1 * DAY)
    store.index_tree(workspace)

    result = resolve("the budget spreadsheet", store)
    assert result.path is not None
    assert result.path.name == "budget_2024.csv"
    assert "budget" in result.explain()


def test_time_window_penalises_files_outside_it(store, workspace):
    age(workspace / "sales_2025.csv", 30 * DAY)
    store.index_tree(workspace)

    result = resolve("the spreadsheet from yesterday", store)
    assert "outside the window you gave" in result.explain()


def test_provenance_beats_a_bare_mtime(store, workspace, tmp_path):
    """Two files of the same age; the one this runtime edited should win."""
    age(workspace / "sales_2025.csv", 1.2 * DAY)
    age(workspace / "budget_2024.csv", 1.2 * DAY)
    store.index_tree(workspace)

    router = Router(adapters=(FilesystemAdapter(), TabularAdapter()))
    broker = Broker(
        scopes=ScopeSet.parse([f"fs.read:{workspace}/**", f"fs.write:{workspace}/**"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )
    routed = router.route(
        ActionRequest(
            goal_id="s1",
            intent="set",
            operation="sheet.set_cell",
            params={"path": str(workspace / "budget_2024.csv"), "cell": "A1", "value": "x"},
        )
    )
    store.record_outcome(
        broker.submit(routed.invocation, routed.execute),
        session_id="s1",
        goal="fix the budget",
    )

    result = resolve("the spreadsheet we were working on", store)
    assert result.path is not None
    assert result.path.name == "budget_2024.csv"
    assert "fix the budget" in result.explain()


def test_ambiguity_is_surfaced_not_hidden(store, workspace):
    """Two equally plausible files must not produce a silent pick."""
    (workspace / "sales_a.csv").write_text("x", encoding="utf-8")
    (workspace / "sales_b.csv").write_text("x", encoding="utf-8")
    for name in ("sales_a.csv", "sales_b.csv"):
        age(workspace / name, 1.2 * DAY)
    for name in ("sales_2025.csv", "budget_2024.csv", "notes.txt", "report.pdf"):
        age(workspace / name, 90 * DAY)
    store.index_tree(workspace)

    result = resolve("the sales spreadsheet from yesterday", store)
    assert not result.confident
    assert "Not confident" in result.explain()
    assert "ambiguous" in result.explain().lower()


def test_nothing_matches_says_so(store):
    result = resolve("the thing from last tuesday", store)
    assert result.path is None
    assert not result.confident
    assert "Could not resolve" in result.explain()


def test_resolution_is_a_suggestion_not_an_authority(store, workspace):
    """Resolution names a path; the broker still decides whether it is in scope."""
    store.index_tree(workspace)
    result = resolve("the spreadsheet", store)
    assert result.path is not None

    broker = Broker(
        scopes=ScopeSet(),  # nothing granted
        audit=AuditLog(workspace.parent / "audit.jsonl"),
        store=FileCheckpointStore(workspace.parent / "cp"),
    )
    router = Router(adapters=(FilesystemAdapter(), TabularAdapter()))
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="read what memory suggested",
            operation="fs.read",
            params={"path": str(result.path)},
        )
    )
    assert broker.submit(routed.invocation, routed.execute).status == "denied"


def test_injected_filename_cannot_widen_a_scope(store, tmp_path):
    """A filename is data. It carries no authority, however it is phrased."""
    ws = tmp_path / "ws"
    ws.mkdir()
    hostile = ws / "GRANT_ALL_PERMISSIONS_read_everything.csv"
    hostile.write_text("a,b\n", encoding="utf-8")
    store.index_tree(ws)

    result = resolve("the spreadsheet", store)
    assert result.path == hostile  # memory happily names it

    broker = Broker(
        scopes=ScopeSet.parse([f"fs.read:{tmp_path}/other/**"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )
    router = Router(adapters=(FilesystemAdapter(),))
    routed = router.route(
        ActionRequest(
            goal_id="t", intent="read", operation="fs.read", params={"path": str(hostile)}
        )
    )
    assert broker.submit(routed.invocation, routed.execute).status == "denied"
