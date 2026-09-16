"""Effect oracles.

An oracle reads the **system of record** and answers two questions: did the
declared effect happen, and did anything else happen too.

Screenshots are never oracles. Screen-only verification has been measured
silently accepting ~75% of wrong effects; one system-of-record read cuts that to
~12.5%.

:class:`NullOracle` exists so that "we could not verify this" is a recorded,
counted, publishable fact rather than a silent gap. Its frequency is a headline
metric.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .checkpoint import file_digest

__all__ = ["FileHashOracle", "FileTreeOracle", "NullOracle", "compare"]


@dataclass(slots=True)
class FileHashOracle:
    """Verifies that specific files changed (or did not)."""

    paths: tuple[Path, ...]
    kind: str = field(default="file_hash", init=False)

    def observe(self) -> dict[str, Any]:
        return {str(p): file_digest(Path(p).expanduser().resolve()) for p in self.paths}

    def verifiable(self) -> bool:
        return True


@dataclass(slots=True)
class FileTreeOracle:
    """Manifest of a directory tree — catches **collateral** writes.

    This is what makes copy-before-write checkpointing honest: the checkpoint
    covers only declared targets, so we need to know when an action touched
    something it did not declare. When it has, reversal is known-incomplete and
    the broker halts instead of claiming a clean rollback.
    """

    root: Path
    declared: tuple[Path, ...] = ()
    max_entries: int = 50_000
    kind: str = field(default="file_tree", init=False)

    def observe(self) -> dict[str, Any]:
        root = Path(self.root).expanduser().resolve()
        manifest: dict[str, Any] = {}
        count = 0
        truncated = False
        if root.exists():
            for path in sorted(root.rglob("*")):
                if path.is_dir():
                    continue
                count += 1
                if count > self.max_entries:
                    truncated = True
                    break
                manifest[str(path)] = file_digest(path)
        return {"_files": manifest, "_truncated": truncated}

    def verifiable(self) -> bool:
        return True

    def collateral(self, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
        """Paths that changed but were not declared targets."""
        declared = {str(Path(p).expanduser().resolve()) for p in self.declared}
        b: dict[str, Any] = before.get("_files", {})
        a: dict[str, Any] = after.get("_files", {})
        changed = {p for p in b.keys() | a.keys() if b.get(p) != a.get(p)}
        return sorted(changed - declared)


@dataclass(slots=True)
class NullOracle:
    """Explicitly unverifiable. Allowed, loud, and counted."""

    reason: str = "no system-of-record reader available for this effect"
    kind: str = field(default="null", init=False)

    def observe(self) -> dict[str, Any]:
        return {"_unverifiable": True, "_reason": self.reason}

    def verifiable(self) -> bool:
        return False


def compare(
    oracle: object,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    expect_change: bool,
) -> tuple[bool, str, list[str]]:
    """Compare observations. Returns ``(matched, detail, collateral)``.

    Deliberately coarse for v0: it answers "did the declared targets change in
    the expected direction, and did anything undeclared change". Richer
    predicates (cell values, row counts) belong in adapter-specific oracles.
    """
    if isinstance(oracle, NullOracle):
        return True, "unverified (NullOracle)", []

    if isinstance(oracle, FileTreeOracle):
        collateral = oracle.collateral(before, after)
        declared = {str(Path(p).expanduser().resolve()) for p in oracle.declared}
        b: dict[str, Any] = before.get("_files", {})
        a: dict[str, Any] = after.get("_files", {})
        declared_changed = any(b.get(p) != a.get(p) for p in declared)
        if expect_change and declared and not declared_changed:
            return False, "declared targets did not change", collateral
        if collateral:
            return False, f"{len(collateral)} undeclared path(s) changed", collateral
        return True, "declared targets changed; no collateral", []

    changed = [k for k in before.keys() | after.keys() if before.get(k) != after.get(k)]
    if expect_change and not changed:
        return False, "no declared target changed", []
    if not expect_change and changed:
        return False, f"unexpected change to {len(changed)} target(s)", []
    return True, f"{len(changed)} target(s) changed as declared", []
