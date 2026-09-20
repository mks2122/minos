"""What the sandbox is allowed to import.

The sandbox has no network — that is the decision recorded in
docs/PLAN-GENERALITY.md, and it keeps ``--offline`` a guarantee rather than a
slogan. The cost is that a script cannot ``pip install`` its way out of a
missing dependency, so the set of importable packages has to be decided up
front and reported honestly.

Two things follow, and both matter more than they look:

**The planner must be told.** A model that does not know ``pypdf`` is available
will write a worse script, or refuse. A model that assumes ``requests`` is
available will write one that fails. So the roster goes into the tool
description, not into a README nobody reads.

**A missing package must fail by name.** "ModuleNotFoundError: no module named
'docx'" buried in a traceback is a worse answer than "python-docx is not
installed; minos doctor --packages lists what is". The runtime knows which
import failed, so it should say so.

Nothing here installs anything. Resolution is a *report* on the environment the
sandbox will actually get, which is the same interpreter the runtime runs on.
Installing is the user's decision, made with the user's package manager.
"""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "RECOMMENDED",
    "PackageStatus",
    "available_packages",
    "missing_import",
    "survey",
]


@dataclass(frozen=True, slots=True)
class PackageStatus:
    """One package the sandbox might be able to import."""

    module: str
    """The import name, which is what a script actually writes."""

    distribution: str
    """The install name, which is what the user actually types."""

    purpose: str
    available: bool = False

    @property
    def install_hint(self) -> str:
        return f"uv pip install {self.distribution}"


RECOMMENDED: tuple[tuple[str, str, str], ...] = (
    # (import name, install name, what it unlocks)
    ("pypdf", "pypdf", "read and split PDFs"),
    ("fitz", "pymupdf", "render PDF pages, extract layout and images"),
    ("docx", "python-docx", "read and write Word documents"),
    ("openpyxl", "openpyxl", "read and write Excel workbooks"),
    ("pptx", "python-pptx", "read and write PowerPoint decks"),
    ("PIL", "pillow", "image conversion, resizing, format changes"),
    ("pandas", "pandas", "tabular data, joins, aggregation"),
    ("numpy", "numpy", "numeric arrays"),
    ("bs4", "beautifulsoup4", "parse HTML and XML"),
    ("lxml", "lxml", "fast XML and HTML parsing"),
    ("markdown", "markdown", "Markdown to HTML"),
    ("yaml", "pyyaml", "read and write YAML"),
    ("chardet", "chardet", "detect the encoding of a mystery text file"),
    ("dateutil", "python-dateutil", "parse dates written by humans"),
    ("PyPDF2", "pypdf2", "legacy PDF reading, superseded by pypdf"),
)
"""The packages worth having for "convert this to that" work.

Deliberately a *recommendation*, not a requirement. The runtime has zero
dependencies and that stays true: every one of these is optional, the sandbox
works without them, and a task that needs one says so by name.
"""


def survey(modules: tuple[tuple[str, str, str], ...] = RECOMMENDED) -> tuple[PackageStatus, ...]:
    """Which of the recommended packages this interpreter can import.

    Uses ``find_spec`` rather than importing: a survey that executes third-party
    package initialisation to find out whether it exists is a survey that can
    crash, and `minos doctor` must never crash.
    """
    found: list[PackageStatus] = []
    for module, distribution, purpose in modules:
        try:
            available = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            available = False
        found.append(
            PackageStatus(
                module=module,
                distribution=distribution,
                purpose=purpose,
                available=available,
            )
        )
    return tuple(found)


def available_packages() -> tuple[str, ...]:
    """Import names a sandboxed script can rely on. Goes into the tool schema."""
    return tuple(status.module for status in survey() if status.available)


_MISSING = re.compile(r"No module named ['\"]([\w.]+)['\"]")


def missing_import(stderr: str) -> PackageStatus | None:
    """Turn a ModuleNotFoundError traceback into something actionable.

    Returns the recommended package whose import failed, so the caller can say
    which distribution to install rather than leaving a traceback as the answer.
    """
    match = _MISSING.search(stderr or "")
    if not match:
        return None
    wanted = match.group(1).split(".")[0]
    for module, distribution, purpose in RECOMMENDED:
        if module == wanted:
            return PackageStatus(module, distribution, purpose, available=False)
    return PackageStatus(wanted, wanted, "not one of the recommended packages", available=False)


def describe_for_planner() -> str:
    """One line for the tool description. The model needs the real roster."""
    names = available_packages()
    if not names:
        return "Only the Python standard library is available."
    return "Available beyond the standard library: " + ", ".join(sorted(names)) + "."


def cache_dir(state: Path | str) -> Path:
    """Where a future pre-baked wheel cache would live.

    Not populated yet: resolution today reports on the interpreter the runtime
    already has. The path is fixed now so that the offline story has one place
    to put wheels when someone builds the bundle, rather than three.
    """
    return Path(state).expanduser() / "wheels"
