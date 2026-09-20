"""Guard: anything the library prints to a console must be ASCII-encodable.

A plain Windows console is cp1252 and raises ``UnicodeEncodeError`` on box
drawing, em-dashes or warning glyphs. An approval prompt that crashes is an
approval that never happened -- so this is a correctness property, not cosmetics.

Caught for real during M1: the first cli_approver used box-drawing characters
and died on the maintainer's own machine.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "minos"
EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"


def _printed_strings(path: pathlib.Path) -> list[tuple[int, str]]:
    """Every string literal that reaches a print() call, including f-string parts."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print"):
            continue
        for arg in node.args:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    found.append((sub.lineno, sub.value))
    return found


@pytest.mark.parametrize(
    "path",
    sorted(SRC.rglob("*.py")) + sorted(EXAMPLES.rglob("*.py")),
    ids=lambda p: p.name,
)
def test_printed_strings_are_cp1252_safe(path: pathlib.Path) -> None:
    offenders = []
    for lineno, text in _printed_strings(path):
        try:
            text.encode("cp1252")
        except UnicodeEncodeError:
            offenders.append(f"{path.name}:{lineno}: {text!r}")
    assert not offenders, "console output must survive a cp1252 terminal:\n" + "\n".join(offenders)
