"""The operation -> capability registry.

One file lists every operation the runtime knows about and which capability it
consumes. Keeping this central rather than scattered across adapters is
deliberate: it is the table a reviewer reads to answer "what can this thing
actually do", and an operation missing from it is denied rather than defaulted.

Adapters may :func:`register` new operations, but registration is explicit and
lands here in one listing at import time.
"""

from __future__ import annotations

from dataclasses import dataclass

from .scopes import KNOWN_CAPABILITIES

__all__ = ["OperationSpec", "capability_for", "known_operations", "register"]


@dataclass(frozen=True, slots=True)
class OperationSpec:
    operation: str
    capability: str
    summary: str

    def __post_init__(self) -> None:
        if self.capability not in KNOWN_CAPABILITIES:
            raise ValueError(
                f"operation {self.operation!r} declares unknown capability {self.capability!r}"
            )


_REGISTRY: dict[str, OperationSpec] = {}


def register(spec: OperationSpec) -> OperationSpec:
    existing = _REGISTRY.get(spec.operation)
    if existing is not None and existing != spec:
        raise ValueError(
            f"operation {spec.operation!r} is already registered as "
            f"{existing.capability!r}; refusing to redefine it"
        )
    _REGISTRY[spec.operation] = spec
    return spec


def capability_for(operation: str) -> str | None:
    """The capability an operation consumes, or ``None`` if unknown.

    ``None`` means deny. An unregistered operation is not a gap to be filled at
    runtime.
    """
    spec = _REGISTRY.get(operation)
    return spec.capability if spec else None


def known_operations() -> dict[str, OperationSpec]:
    return dict(_REGISTRY)


# -- built-in operations ---------------------------------------------------

for _spec in (
    OperationSpec("fs.read", "fs.read", "Read a file's contents"),
    OperationSpec("fs.list", "fs.read", "List a directory"),
    OperationSpec("fs.stat", "fs.read", "Read file metadata"),
    OperationSpec("fs.write", "fs.write", "Write or overwrite a file"),
    OperationSpec("fs.append", "fs.write", "Append to a file"),
    OperationSpec("fs.copy", "fs.write", "Copy a file (also needs fs.read on the source)"),
    OperationSpec("fs.move", "fs.write", "Move a file (also needs fs.delete on the source)"),
    OperationSpec("fs.mkdir", "fs.write", "Create a directory"),
    OperationSpec("fs.delete", "fs.delete", "Delete a file or empty directory"),
    OperationSpec("sheet.list", "fs.read", "List the sheets in a workbook"),
    OperationSpec("sheet.read_cell", "fs.read", "Read one cell"),
    OperationSpec("sheet.read_range", "fs.read", "Read a rectangular range"),
    OperationSpec("sheet.find_row", "fs.read", "Find a row by column value"),
    OperationSpec("sheet.set_cell", "fs.write", "Write one cell"),
    OperationSpec("memory.recall", "memory.read", "Resolve a vague reference to a real path"),
    OperationSpec("memory.recent", "memory.read", "List recently changed files"),
    OperationSpec("app.open", "app.open", "Open a file in the system default application"),
    OperationSpec("proc.spawn", "proc.spawn", "Run an external program"),
    OperationSpec("net.http", "net.http", "Make an HTTP request"),
    OperationSpec("ui.click", "ui.input", "Synthetic mouse click"),
    OperationSpec("ui.type", "ui.input", "Synthetic keyboard input"),
    OperationSpec("ui.key", "ui.input", "Synthetic key chord"),
    OperationSpec("ui.screenshot", "ui.input", "Capture the screen"),
    OperationSpec("clipboard.read", "clipboard.read", "Read the clipboard"),
    OperationSpec("clipboard.write", "clipboard.write", "Write the clipboard"),
    OperationSpec(
        "state.undo",
        "state.undo",
        "Restore a past action's declared targets (human-initiated)",
    ),
):
    register(_spec)
