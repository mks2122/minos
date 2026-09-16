"""L2 tabular adapter — the tier's argument, in one file.

This is what "prefer typed adapters over pixels" actually buys. The same task
expressed three ways:

    L3   screenshot -> find the cell -> click (412, 288) -> type "48200" -> hope
    L1   rewrite the whole file and diff the bytes
    L2   sheet.set_cell(path, "Q3", "B14", 48200)

Only the L2 form can declare a contract worth verifying. ``FileHashOracle`` can
say *the file changed*; :class:`CellOracle` reads the workbook back and says
*B14 is now 48200 and no other cell moved*. That difference is the whole reason
the hierarchy prefers this tier.

Backends are pluggable. CSV works from the standard library so the tier is
testable everywhere; ODS/XLSX register themselves when their optional
dependency is importable, and are simply absent otherwise — which the router
reports as an ordinary degradation rather than a crash.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ...types import (
    ActionRequest,
    EffectClass,
    EffectContract,
    Grant,
    Invocation,
    Tier,
)
from ..base import CapabilityManifest, OperationUnsupported, Preparation

__all__ = [
    "CellOracle",
    "CsvBackend",
    "TabularAdapter",
    "TabularBackend",
    "cell_ref",
    "register_backend",
]

_CELL_RE = re.compile(r"^([A-Za-z]+)([1-9][0-9]*)$")


def cell_ref(ref: str) -> tuple[int, int]:
    """``"B14"`` -> ``(row=13, col=1)``, zero-indexed.

    Raises :class:`OperationUnsupported` on anything malformed so the router can
    fall onward rather than the adapter throwing something unexpected.
    """
    match = _CELL_RE.match(ref.strip())
    if not match:
        raise OperationUnsupported(f"not a cell reference: {ref!r}")
    letters, digits = match.groups()
    col = 0
    for ch in letters.upper():
        col = col * 26 + (ord(ch) - ord("A") + 1)
    return int(digits) - 1, col - 1


# -- backends --------------------------------------------------------------


class TabularBackend(Protocol):
    """Reads and writes one workbook format.

    ``suffixes`` is a read-only property rather than a bare attribute so that
    frozen-dataclass backends satisfy the protocol.
    """

    @property
    def suffixes(self) -> tuple[str, ...]: ...

    def sheets(self, path: Path) -> list[str]: ...
    def read(self, path: Path, sheet: str | None) -> list[list[str]]: ...
    def write(self, path: Path, sheet: str | None, rows: list[list[str]]) -> None: ...


@dataclass(frozen=True, slots=True)
class CsvBackend:
    """Standard-library CSV. One sheet, named after the file stem."""

    suffixes: tuple[str, ...] = (".csv", ".tsv")

    def _dialect(self, path: Path) -> dict[str, Any]:
        return {"delimiter": "\t"} if path.suffix.lower() == ".tsv" else {}

    def sheets(self, path: Path) -> list[str]:
        return [path.stem]

    def read(self, path: Path, sheet: str | None) -> list[list[str]]:
        if sheet not in (None, path.stem):
            raise OperationUnsupported(f"{path.name} has no sheet {sheet!r}")
        with open(path, newline="", encoding="utf-8") as fh:
            return [list(row) for row in csv.reader(fh, **self._dialect(path))]

    def write(self, path: Path, sheet: str | None, rows: list[list[str]]) -> None:
        if sheet not in (None, path.stem):
            raise OperationUnsupported(f"{path.name} has no sheet {sheet!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh, **self._dialect(path)).writerows(rows)


_BACKENDS: list[TabularBackend] = [CsvBackend()]


def register_backend(backend: TabularBackend) -> None:
    """Register a workbook format. ODS/XLSX plug in here when available."""
    _BACKENDS.insert(0, backend)


def backend_for(path: Path) -> TabularBackend:
    suffix = path.suffix.lower()
    for backend in _BACKENDS:
        if suffix in backend.suffixes:
            return backend
    raise OperationUnsupported(
        f"no tabular backend for {suffix or '(no suffix)'}; "
        f"have {sorted({s for b in _BACKENDS for s in b.suffixes})}"
    )


# -- oracle ----------------------------------------------------------------


@dataclass(slots=True)
class CellOracle:
    """Reads specific cells back out of the workbook.

    This is a system-of-record read in the strict sense: it reopens the file and
    reports the value at a coordinate. A file hash would confirm that *something*
    changed; this confirms *what*.
    """

    path: Path
    sheet: str | None
    cells: tuple[str, ...]
    kind: str = field(default="cell", init=False)

    def observe(self) -> dict[str, Any]:
        path = Path(self.path).expanduser().resolve()
        try:
            rows = backend_for(path).read(path, self.sheet)
        except (OSError, OperationUnsupported):
            return dict.fromkeys(self.cells)
        out: dict[str, Any] = {}
        for ref in self.cells:
            row, col = cell_ref(ref)
            try:
                out[ref] = rows[row][col]
            except IndexError:
                out[ref] = None
        return out

    def verifiable(self) -> bool:
        return True


# -- adapter ---------------------------------------------------------------

_OPERATIONS = (
    "sheet.list",
    "sheet.read_cell",
    "sheet.read_range",
    "sheet.find_row",
    "sheet.set_cell",
)


class TabularAdapter:
    """Typed spreadsheet operations with exact, verifiable contracts."""

    manifest = CapabilityManifest(
        adapter="l2.tabular",
        tier=Tier.L2_ADAPTER,
        operations=_OPERATIONS,
        summary="Cell-level workbook operations; CSV built in, others pluggable",
    )

    def prepare(self, request: ActionRequest) -> Preparation:
        op = request.operation
        if op not in _OPERATIONS:
            raise OperationUnsupported(op)

        path = request.params.get("path")
        if path is None:
            raise OperationUnsupported(f"{op} requires 'path'")
        resolved = Path(str(path)).expanduser().resolve()
        backend_for(resolved)  # declines here if the format is unsupported
        sheet = request.params.get("sheet")

        handler = getattr(self, f"_prepare_{op.split('.', 1)[1]}")
        return handler(request, resolved, sheet)  # type: ignore[no-any-return]

    # -- reads -------------------------------------------------------------

    def _prepare_list(self, request: ActionRequest, path: Path, sheet: str | None) -> Preparation:
        def execute(_: Invocation) -> list[str]:
            return backend_for(path).sheets(path)

        return self._pure(path, sheet, (), f"list sheets in {path.name}", execute)

    def _prepare_read_cell(
        self, request: ActionRequest, path: Path, sheet: str | None
    ) -> Preparation:
        ref = str(request.params.get("cell", ""))
        row, col = cell_ref(ref)

        def execute(_: Invocation) -> str | None:
            rows = backend_for(path).read(path, sheet)
            try:
                return rows[row][col]
            except IndexError:
                return None

        return self._pure(path, sheet, (ref,), f"read {ref} from {path.name}", execute)

    def _prepare_read_range(
        self, request: ActionRequest, path: Path, sheet: str | None
    ) -> Preparation:
        start = str(request.params.get("start", "A1"))
        end = request.params.get("end")
        r0, c0 = cell_ref(start)

        def execute(_: Invocation) -> list[list[str]]:
            rows = backend_for(path).read(path, sheet)
            r1, c1 = cell_ref(str(end)) if end else (len(rows) - 1, max(map(len, rows)) - 1)
            return [row[c0 : c1 + 1] for row in rows[r0 : r1 + 1]]

        return self._pure(
            path, sheet, (), f"read range {start}:{end or 'end'} from {path.name}", execute
        )

    def _prepare_find_row(
        self, request: ActionRequest, path: Path, sheet: str | None
    ) -> Preparation:
        column = str(request.params.get("column", "A"))
        needle = str(request.params.get("value", ""))
        _, col = cell_ref(f"{column}1")

        def execute(_: Invocation) -> int | None:
            rows = backend_for(path).read(path, sheet)
            for index, row in enumerate(rows):
                if col < len(row) and row[col] == needle:
                    return index + 1  # 1-indexed, as a spreadsheet user would say it
            return None

        return self._pure(
            path, sheet, (), f"find {needle!r} in column {column} of {path.name}", execute
        )

    # -- write -------------------------------------------------------------

    def _prepare_set_cell(
        self, request: ActionRequest, path: Path, sheet: str | None
    ) -> Preparation:
        ref = str(request.params.get("cell", ""))
        if "value" not in request.params:
            raise OperationUnsupported("sheet.set_cell requires 'value'")
        value = str(request.params["value"])
        row, col = cell_ref(ref)

        def execute(_: Invocation) -> None:
            backend = backend_for(path)
            rows = backend.read(path, sheet) if path.exists() else []
            while len(rows) <= row:
                rows.append([])
            while len(rows[row]) <= col:
                rows[row].append("")
            rows[row][col] = value
            backend.write(path, sheet, rows)

        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.REVERSIBLE,
                targets=(path,),
                # Reads the cell back rather than hashing the file: we can state
                # what the value became, not merely that bytes moved.
                oracle=CellOracle(path=path, sheet=sheet, cells=(ref,)),
                expect=f"{ref} becomes {value!r} in {path.name}",
            ),
            execute=execute,
            grants=(Grant("fs.write", str(path)),),
        )

    # -- helper ------------------------------------------------------------

    def _pure(
        self,
        path: Path,
        sheet: str | None,
        cells: tuple[str, ...],
        expect: str,
        execute: Any,
    ) -> Preparation:
        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.PURE,
                targets=(),
                oracle=CellOracle(path=path, sheet=sheet, cells=cells),
                expect=f"{expect}; nothing changes",
            ),
            execute=execute,
            grants=(Grant("fs.read", str(path)),),
        )
