"""L2 tabular adapter: typed cell operations with verifiable contracts."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from minos.audit import AuditLog
from minos.broker import Broker
from minos.checkpoint import FileCheckpointStore
from minos.router import NoAdapter, Router
from minos.scopes import ScopeSet
from minos.tiers.base import OperationUnsupported
from minos.tiers.l1_system import FilesystemAdapter
from minos.tiers.l2_adapters import TabularAdapter
from minos.tiers.l2_adapters.tabular import CellOracle, cell_ref
from minos.types import ActionRequest, EffectClass, Tier

SALES = [
    ["Quarter", "Revenue", "Units"],
    ["Q1", "38100", "412"],
    ["Q2", "39400", "430"],
    ["Q3", "41800", "455"],
    ["Q4", "44200", "470"],
]


@pytest.fixture
def rig(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    book = ws / "sales_2025.csv"
    with open(book, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(SALES)

    router = Router(adapters=(FilesystemAdapter(), TabularAdapter()))
    broker = Broker(
        scopes=ScopeSet.parse([f"fs.read:{ws}/**", f"fs.write:{ws}/**"]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        store=FileCheckpointStore(tmp_path / "cp"),
    )

    def run(operation: str, **params):
        routed = router.route(
            ActionRequest(goal_id="t", intent=operation, operation=operation, params=params)
        )
        return broker.submit(routed.invocation, routed.execute)

    return book, broker, router, run


def read_rows(book: Path) -> list[list[str]]:
    with open(book, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


# -- cell references -------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("A1", (0, 0)),
        ("B14", (13, 1)),
        ("Z1", (0, 25)),
        ("AA1", (0, 26)),
        ("AB3", (2, 27)),
    ],
)
def test_cell_ref(ref, expected):
    assert cell_ref(ref) == expected


@pytest.mark.parametrize("ref", ["", "1A", "A0", "A", "3", "B-1", "A 1"])
def test_cell_ref_rejects_malformed(ref):
    with pytest.raises(OperationUnsupported):
        cell_ref(ref)


# -- routing ---------------------------------------------------------------


def test_sheet_ops_route_to_l2(rig):
    book, _, router, _ = rig
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="read",
            operation="sheet.read_cell",
            params={"path": str(book), "cell": "B4"},
        )
    )
    assert routed.invocation.tier is Tier.L2_ADAPTER
    assert routed.invocation.adapter == "l2.tabular"


def test_unsupported_format_declines_to_the_router(rig, tmp_path):
    _, _, router, _ = rig
    with pytest.raises(NoAdapter, match="declined"):
        router.route(
            ActionRequest(
                goal_id="t",
                intent="read",
                operation="sheet.read_cell",
                params={"path": str(tmp_path / "book.ods"), "cell": "A1"},
            )
        )


def test_fallback_rate_counts_l2(rig):
    book, _, router, run = rig
    run("sheet.read_cell", path=str(book), cell="A1")
    assert router.stats.summary()["L2"] >= 1
    assert router.stats.fallback_rate > 0


# -- reads -----------------------------------------------------------------


def test_read_cell(rig):
    book, _, _, run = rig
    out = run("sheet.read_cell", path=str(book), cell="B4")
    assert out.status == "ok"
    assert out.result == "41800"


def test_read_cell_is_pure(rig):
    book, _, _, run = rig
    out = run("sheet.read_cell", path=str(book), cell="B4")
    assert out.invocation.contract.effect_class is EffectClass.PURE
    assert out.checkpoint_id is None


def test_find_row(rig):
    book, _, _, run = rig
    out = run("sheet.find_row", path=str(book), column="A", value="Q3")
    assert out.result == 4  # 1-indexed, as a spreadsheet user would say it


def test_find_row_missing_returns_none(rig):
    book, _, _, run = rig
    assert run("sheet.find_row", path=str(book), column="A", value="Q9").result is None


def test_read_range(rig):
    book, _, _, run = rig
    out = run("sheet.read_range", path=str(book), start="A1", end="B2")
    assert out.result == [["Quarter", "Revenue"], ["Q1", "38100"]]


def test_list_sheets(rig):
    book, _, _, run = rig
    assert run("sheet.list", path=str(book)).result == ["sales_2025"]


def test_read_cell_out_of_bounds(rig):
    book, _, _, run = rig
    assert run("sheet.read_cell", path=str(book), cell="Z99").result is None


# -- the flagship write ----------------------------------------------------


def test_set_cell_updates_exactly_one_cell(rig):
    book, _, _, run = rig
    out = run("sheet.set_cell", path=str(book), cell="B4", value="48200")
    assert out.status == "ok"

    rows = read_rows(book)
    assert rows[3][1] == "48200"
    # Everything else is untouched.
    assert rows[0] == SALES[0]
    assert rows[1] == SALES[1]
    assert rows[4] == SALES[4]
    assert rows[3][0] == "Q3"
    assert rows[3][2] == "455"


def test_set_cell_contract_names_the_value(rig):
    """The L2 argument: the contract states what the cell becomes."""
    book, _, router, _ = rig
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="edit",
            operation="sheet.set_cell",
            params={"path": str(book), "cell": "B4", "value": "48200"},
        )
    )
    contract = routed.invocation.contract
    assert contract.effect_class is EffectClass.REVERSIBLE
    assert "B4 becomes '48200'" in contract.expect
    assert contract.oracle is not None
    assert contract.oracle.kind == "cell"


def test_set_cell_is_verified_by_reading_it_back(rig):
    book, _, _, run = rig
    out = run("sheet.set_cell", path=str(book), cell="B4", value="48200")
    assert out.observed is not None
    assert out.observed.kind == "cell"
    assert out.observed.before == {"B4": "41800"}
    assert out.observed.after == {"B4": "48200"}


def test_set_cell_rolls_back(rig):
    book, broker, _, run = rig
    out = run("sheet.set_cell", path=str(book), cell="B4", value="48200")
    assert broker.store.restore(out.checkpoint_id).succeeded
    assert read_rows(book) == SALES


def test_set_cell_outside_scope_denied(rig, tmp_path):
    _, broker, router, _ = rig
    outside = tmp_path / "elsewhere.csv"
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="edit",
            operation="sheet.set_cell",
            params={"path": str(outside), "cell": "A1", "value": "x"},
        )
    )
    assert broker.submit(routed.invocation, routed.execute).status == "denied"
    assert not outside.exists()


def test_set_cell_extends_the_sheet(rig):
    book, _, _, run = rig
    out = run("sheet.set_cell", path=str(book), cell="D1", value="Margin")
    assert out.status == "ok"
    assert read_rows(book)[0][3] == "Margin"


def test_a_wrong_write_is_caught_by_the_oracle(rig):
    """A file-hash oracle would pass this. A cell oracle does not.

    The executor writes the right value into the wrong row: the file genuinely
    changes, so "did the bytes move" is satisfied, but the declared cell did not
    become what the contract said it would.
    """
    book, broker, router, _ = rig
    routed = router.route(
        ActionRequest(
            goal_id="t",
            intent="edit",
            operation="sheet.set_cell",
            params={"path": str(book), "cell": "B4", "value": "48200"},
        )
    )

    def wrong_cell(_inv):
        rows = read_rows(book)
        rows[2][1] = "48200"
        with open(book, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)

    out = broker.submit(routed.invocation, wrong_cell)
    assert out.status == "failed"
    assert out.observed is not None
    assert not out.observed.matched
    assert out.reversal is not None
    assert out.reversal.succeeded
    assert read_rows(book) == SALES


# -- oracle ----------------------------------------------------------------


def test_cell_oracle_on_missing_file(tmp_path):
    oracle = CellOracle(path=tmp_path / "nope.csv", sheet=None, cells=("A1",))
    assert oracle.observe() == {"A1": None}
    assert oracle.verifiable()
