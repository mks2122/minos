"""Capability scopes.

A scope is a bounded grant, set before a task and **immutable during it**. That
immutability is the property that makes planner compromise survivable: an
injected instruction can redirect *what* the agent tries, but not *where* it may
reach.

Grammar::

    <capability>:<pattern>

    fs.read:~/Invoices/**
    fs.write:~/Invoices/2026/**
    proc.spawn:/usr/bin/soffice
    memory.read:~/Invoices/**
    net.http:api.example.com:443
    ui.input:window.class=soffice.bin
    clipboard.read:*

A leading ``!`` makes it a deny rule. Deny always beats allow.

Path patterns are matched against the **resolved real path**, so ``..`` and
symlinks cannot be used to escape a grant.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path, PurePath

__all__ = ["Scope", "ScopeSet", "ScopeViolation"]

_PATH_CAPABILITIES = frozenset({"fs.read", "fs.write", "fs.delete", "proc.spawn", "memory.read"})

KNOWN_CAPABILITIES = frozenset(
    {
        "fs.read",
        "fs.write",
        "fs.delete",
        "proc.spawn",
        "net.http",
        "ui.input",
        "clipboard.read",
        "clipboard.write",
        "memory.read",
    }
)


class ScopeViolation(Exception):
    """Raised when an action is attempted outside its granted scopes."""


@dataclass(frozen=True, slots=True)
class Scope:
    capability: str
    pattern: str
    deny: bool = False

    @classmethod
    def parse(cls, raw: str) -> Scope:
        text = raw.strip()
        if not text:
            raise ValueError("empty scope")
        deny = text.startswith("!")
        if deny:
            text = text[1:].lstrip()
        capability, sep, pattern = text.partition(":")
        if not sep:
            raise ValueError(f"scope must be '<capability>:<pattern>': {raw!r}")
        capability = capability.strip()
        pattern = pattern.strip()
        if capability not in KNOWN_CAPABILITIES:
            raise ValueError(
                f"unknown capability {capability!r}; known: {sorted(KNOWN_CAPABILITIES)}"
            )
        if not pattern:
            raise ValueError(f"scope needs a pattern: {raw!r}")
        return cls(capability=capability, pattern=pattern, deny=deny)

    @property
    def is_path_scope(self) -> bool:
        return self.capability in _PATH_CAPABILITIES

    def _normalised_pattern(self) -> str:
        if not self.is_path_scope:
            return self.pattern
        expanded = os.path.expanduser(self.pattern)
        # Normalise separators so a Windows-authored scope matches a POSIX path
        # pattern and vice versa. We do not resolve() the pattern: it may contain
        # globs and may name files that do not exist yet.
        return PurePath(expanded).as_posix()

    @property
    def specificity(self) -> int:
        """Longer, less-wildcarded patterns win ties. Deny still beats allow."""
        pattern = self._normalised_pattern()
        return len(pattern) - 10 * pattern.count("*")

    def matches(self, capability: str, subject: str | Path) -> bool:
        if capability != self.capability:
            return False
        if self.is_path_scope:
            return self._matches_path(subject)
        return fnmatch.fnmatch(str(subject), self.pattern)

    def _matches_path(self, subject: str | Path) -> bool:
        pattern = self._normalised_pattern()
        target = resolve(subject).as_posix()
        if _case_insensitive_fs():
            pattern = pattern.lower()
            target = target.lower()
        if fnmatch.fnmatch(target, pattern):
            return True
        # `dir/**` should also match `dir` itself, which fnmatch does not do.
        return pattern.endswith("/**") and target == pattern[:-3]

    def __str__(self) -> str:
        return f"{'!' if self.deny else ''}{self.capability}:{self.pattern}"


def _case_insensitive_fs() -> bool:
    """Windows and macOS default to case-insensitive paths; Linux does not.

    Erring toward case-insensitive matching on those platforms is the safe
    direction for *deny* rules and the permissive direction for allow rules —
    which is why deny is evaluated first regardless.
    """
    return os.name == "nt" or sys_platform_is_darwin()


def sys_platform_is_darwin() -> bool:
    import sys

    return sys.platform == "darwin"


def resolve(p: str | Path) -> Path:
    """Resolve to a real absolute path.

    ``strict=False`` so that not-yet-created files still normalise; ``..`` and
    symlinks are collapsed either way, which is what stops scope escapes.
    """
    return Path(os.path.expanduser(str(p))).resolve()


@dataclass(frozen=True, slots=True)
class ScopeSet:
    """An immutable set of grants for one task.

    Immutability is enforced structurally: there is no ``add()``. Widening
    requires constructing a new set, which in practice means a new task and a
    human.
    """

    scopes: tuple[Scope, ...] = ()

    @classmethod
    def parse(cls, raw: object) -> ScopeSet:
        if isinstance(raw, str):
            items: list[str] = [line for line in raw.splitlines() if line.strip()]
        elif isinstance(raw, (list, tuple)):
            items = [str(item) for item in raw]
        else:
            raise TypeError(f"cannot parse scopes from {type(raw).__name__}")
        return cls(tuple(Scope.parse(item) for item in items))

    def narrowed(self, subset: ScopeSet) -> ScopeSet:
        """Return ``subset``, verifying it grants nothing this set does not.

        Used when a skill or sub-task runs with *less* authority than its parent.
        Narrowing is always allowed; widening never is.
        """
        for scope in subset.scopes:
            if scope.deny:
                continue
            if not any(
                parent.capability == scope.capability
                and not parent.deny
                and _pattern_covers(parent, scope)
                for parent in self.scopes
            ):
                raise ScopeViolation(
                    f"cannot widen scope: {scope} is not covered by the parent scope set"
                )
        return subset

    def check(self, capability: str, subject: str | Path) -> tuple[bool, Scope | None]:
        """Return ``(permitted, deciding_scope)``.

        Resolution order: **deny beats allow**; among allows, highest
        specificity wins; ties deny (by falling through to default-deny).
        """
        denies = [s for s in self.scopes if s.deny and s.matches(capability, subject)]
        if denies:
            return False, max(denies, key=lambda s: s.specificity)

        allows = [s for s in self.scopes if not s.deny and s.matches(capability, subject)]
        if not allows:
            return False, None

        best = max(s.specificity for s in allows)
        winners = [s for s in allows if s.specificity == best]
        if len(winners) > 1:
            # Ambiguity resolves to deny. An operator who wants this allowed can
            # say so unambiguously.
            return False, None
        return True, winners[0]

    def require(self, capability: str, subject: str | Path) -> Scope:
        permitted, scope = self.check(capability, subject)
        if not permitted:
            raise ScopeViolation(
                f"{capability} on {subject!s} is outside the granted scopes"
                + (f" (denied by {scope})" if scope else "")
            )
        assert scope is not None
        return scope

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.scopes)

    def __len__(self) -> int:
        return len(self.scopes)

    def __str__(self) -> str:
        return "\n".join(str(s) for s in self.scopes)


def _pattern_covers(parent: Scope, child: Scope) -> bool:
    """Conservative containment test for narrowing.

    Exact equality, or the parent being a ``**`` prefix of the child. Anything
    subtler is rejected — a false "cannot narrow" is a nuisance, a false
    "narrowing is safe" is a security bug.
    """
    p = parent._normalised_pattern()
    c = child._normalised_pattern()
    if p == c:
        return True
    if p.endswith("/**"):
        return c.startswith(p[:-2]) or c == p[:-3]
    return p in ("*", "**")
