"""Scope tests, written adversarially.

The scope layer is the thing standing between an injected planner and the user's
files, so these lean toward "prove it cannot" rather than "show it works".
"""

from __future__ import annotations

import os

import pytest

from minos.scopes import Scope, ScopeSet, ScopeViolation


def test_parse_basic():
    scope = Scope.parse("fs.read:~/Invoices/**")
    assert scope.capability == "fs.read"
    assert not scope.deny


def test_parse_deny():
    scope = Scope.parse("!fs.write:/etc/**")
    assert scope.deny


@pytest.mark.parametrize(
    "raw",
    ["", "fs.read", "nonsense:foo", "fs.read:", ":pattern"],
)
def test_parse_rejects_malformed(raw):
    with pytest.raises(ValueError):
        Scope.parse(raw)


def test_allows_within_grant(tmp_path):
    target = tmp_path / "invoices" / "q3.ods"
    scopes = ScopeSet.parse([f"fs.write:{tmp_path}/invoices/**"])
    permitted, _ = scopes.check("fs.write", target)
    assert permitted


def test_denies_outside_grant(tmp_path):
    scopes = ScopeSet.parse([f"fs.write:{tmp_path}/invoices/**"])
    permitted, _ = scopes.check("fs.write", tmp_path / "secrets" / "keys.txt")
    assert not permitted


def test_default_deny_with_no_scopes(tmp_path):
    permitted, scope = ScopeSet().check("fs.read", tmp_path / "anything")
    assert not permitted
    assert scope is None


def test_capabilities_do_not_leak(tmp_path):
    """A read grant must not confer write."""
    scopes = ScopeSet.parse([f"fs.read:{tmp_path}/**"])
    assert scopes.check("fs.read", tmp_path / "a.txt")[0]
    assert not scopes.check("fs.write", tmp_path / "a.txt")[0]
    assert not scopes.check("fs.delete", tmp_path / "a.txt")[0]


def test_deny_beats_allow(tmp_path):
    scopes = ScopeSet.parse([f"fs.write:{tmp_path}/**", f"!fs.write:{tmp_path}/protected/**"])
    assert scopes.check("fs.write", tmp_path / "ok.txt")[0]
    assert not scopes.check("fs.write", tmp_path / "protected" / "no.txt")[0]


def test_dotdot_cannot_escape(tmp_path):
    """The classic escape. Scopes match the resolved real path."""
    scopes = ScopeSet.parse([f"fs.write:{tmp_path}/invoices/**"])
    escape = tmp_path / "invoices" / ".." / ".." / "etc" / "passwd"
    assert not scopes.check("fs.write", escape)[0]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_symlink_cannot_escape(tmp_path):
    inside = tmp_path / "invoices"
    outside = tmp_path / "secrets"
    inside.mkdir()
    outside.mkdir()
    (outside / "keys.txt").write_text("sensitive")
    (inside / "link").symlink_to(outside)

    scopes = ScopeSet.parse([f"fs.read:{tmp_path}/invoices/**"])
    assert not scopes.check("fs.read", inside / "link" / "keys.txt")[0]


def test_ambiguous_tie_denies(tmp_path):
    """Two equally specific allows resolve to deny, not to a coin flip."""
    scopes = ScopeSet.parse([f"fs.write:{tmp_path}/a?c/x", f"fs.write:{tmp_path}/ab?/x"])
    assert not scopes.check("fs.write", tmp_path / "abc" / "x")[0]


def test_directory_itself_matches_double_star(tmp_path):
    scopes = ScopeSet.parse([f"fs.read:{tmp_path}/invoices/**"])
    assert scopes.check("fs.read", tmp_path / "invoices")[0]


def test_require_raises_outside_scope(tmp_path):
    scopes = ScopeSet.parse([f"fs.read:{tmp_path}/**"])
    with pytest.raises(ScopeViolation):
        scopes.require("fs.write", tmp_path / "a.txt")


def test_no_mutation_api():
    """Immutability is structural, not a convention."""
    scopes = ScopeSet.parse(["fs.read:/tmp/**"])
    assert not hasattr(scopes, "add")
    assert not hasattr(scopes, "extend")
    with pytest.raises((AttributeError, TypeError)):
        scopes.scopes = ()  # type: ignore[misc]


def test_narrowing_allowed(tmp_path):
    parent = ScopeSet.parse([f"fs.write:{tmp_path}/**"])
    child = ScopeSet.parse([f"fs.write:{tmp_path}/invoices/**"])
    assert parent.narrowed(child) is child


def test_widening_refused(tmp_path):
    parent = ScopeSet.parse([f"fs.write:{tmp_path}/invoices/**"])
    child = ScopeSet.parse([f"fs.write:{tmp_path}/**"])
    with pytest.raises(ScopeViolation):
        parent.narrowed(child)


def test_widening_to_new_capability_refused(tmp_path):
    parent = ScopeSet.parse([f"fs.read:{tmp_path}/**"])
    child = ScopeSet.parse([f"fs.write:{tmp_path}/**"])
    with pytest.raises(ScopeViolation):
        parent.narrowed(child)


def test_non_path_capability_matching():
    scopes = ScopeSet.parse(["net.http:api.example.com:443"])
    assert scopes.check("net.http", "api.example.com:443")[0]
    assert not scopes.check("net.http", "evil.example.com:443")[0]
