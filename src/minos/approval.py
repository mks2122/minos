"""Asking a person, well.

The broker decides *whether* to ask; this decides how the question looks. The
answer has to be an informed one, so beyond yes and no a person can:

``[s]how``    see what will change -- a diff against the file as it is now, or
              the full arguments -- before answering
``[w]hy``     read what the model said while choosing this step
``[a]lways``  approve this operation on these targets for the rest of the
              session, so a retry does not ask twice

``[a]lways`` is never offered for an ``IRREVERSIBLE`` effect. Those re-prompt
every time by design, and a standing yes would quietly turn the one guarantee
that matters most into a preference.

The one standing policy is for GUI input, and it is opt-in by flag
(``--gui-confirm commit``, the CLI default): ordinary typing, keys and clicks
on named controls go through, and only a *commit* -- a click on Post, Send,
Delete, Pay...; a modified Enter; typed text holding a newline; a click by
coordinate, where the runtime cannot tell what is underneath -- stops for a
person. The audit log records those as policy approvals, never as a human's.

Everything shown here comes from the planner or from a file the planner chose,
so it is untrusted: every line goes through :func:`minos.trace.printable`
before it reaches the terminal. ASCII only, for the cp1252 console.

This module also decides which missing grants a person may be *offered* after
a run (:func:`grant_offers`). The offer never widens a running task -- scopes
are immutable during one -- it seeds the next.
"""

from __future__ import annotations

import difflib
import glob
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .scopes import resolve
from .trace import Event, _short, _truncate, printable
from .types import AdmissionDecision, EffectClass, Grant, Invocation

__all__ = [
    "COMMIT_WORDS",
    "OFFERABLE",
    "LastThinking",
    "SessionApprover",
    "grant_offers",
    "is_commit",
    "preview",
    "terminal_ask",
]

OFFERABLE = frozenset({"fs.write", "fs.delete", "app.open"})
"""Capabilities a denied step may turn into a one-file grant offer.

Deliberately short. Process spawning, GUI input and sandboxed code are not
things to grant in reply to a model asking for them; they are typed flags."""

COMMIT_WORDS = frozenset(
    {
        "post", "send", "submit", "publish", "share", "repost", "reply", "comment",
        "tweet", "delete", "remove", "discard", "pay", "buy", "purchase", "order",
        "checkout", "transfer", "confirm", "accept", "apply", "sign", "upload",
        "save", "follow", "unfollow", "connect", "like", "invite", "schedule",
    }
)  # fmt: skip
"""The first word of a control that commits something to the world.

The *first* word, because button labels are verbs: "Post" commits, "Start a
post" opens an editor. Matching anywhere would stop at the compose button and
teach a person to wave prompts through."""

_GUI_INPUT = frozenset({"ui.click", "ui.type", "ui.key", "web.click", "web.fill", "web.press"})
_MODIFIERS = frozenset({"ctrl", "control", "alt", "shift", "win", "cmd", "meta"})

_MAX_PREVIEW_LINES = 60
_MAX_DIFF_BYTES = 256 * 1024


@dataclass
class LastThinking:
    """An observer that remembers what the planner last said, for ``[w]hy``."""

    text: str = ""

    def __call__(self, event: Event) -> None:
        if event.kind == "waiting":
            self.text = ""  # a new step; the old reasoning is not about it
        elif event.kind == "thinking":
            self.text = event.text

    def get(self) -> str:
        return self.text


@dataclass
class SessionApprover:
    """The interactive approver. Keep one per session so ``[a]lways`` lasts."""

    why: Callable[[], str] = lambda: ""
    ask: Callable[[str], str] = field(default=input)
    out: Any = None
    """Where to print. None means ``sys.stdout`` at call time, so a test that
    captures stdout sees it."""

    always: set[tuple[str, tuple[str, ...]]] = field(default_factory=set)

    gui: str = "all"
    """``all`` asks for every GUI input; ``commit`` only for :func:`is_commit`."""

    def __call__(self, invocation: Invocation, decision: AdmissionDecision) -> bool | str:
        operation = invocation.request.operation
        if self.gui == "commit" and operation in _GUI_INPUT and not is_commit(invocation):
            self._say(f"        auto-approved: ordinary GUI input ({operation})")
            return "policy approved (--gui-confirm commit; not a commit control)"

        key = _key(invocation)
        if key in self.always:
            self._say(f"\n  approved (always, this session): {invocation.request.operation}")
            return True

        self._say_all(describe(invocation, decision))
        remember = _may_remember(invocation)
        options = ["[y]es", "[N]o", "[s]how"]
        if self.why().strip():
            options.append("[w]hy")
        if remember:
            options.append("[a]lways")

        while True:
            try:
                answer = self.ask(f"  allow? {' '.join(options)}: ").strip().lower()
            except EOFError:
                # No one is there to answer. That is a no, not a crash.
                self._say("")
                return False
            if answer in {"y", "yes"}:
                return True
            if answer in {"", "n", "no"}:
                return False
            if answer in {"s", "show"}:
                self._say_all(preview(invocation))
            elif answer in {"w", "why"}:
                self._say_all(_why_lines(self.why()))
            elif answer in {"a", "always"} and remember:
                self.always.add(key)
                return True
            else:
                self._say(f"  answer one of: {' '.join(options)}")

    def _say(self, line: str) -> None:
        print(line, file=self.out if self.out is not None else sys.stdout)

    def _say_all(self, lines: Iterable[str]) -> None:
        for line in lines:
            self._say(line)


def describe(invocation: Invocation, decision: AdmissionDecision) -> list[str]:
    """The approval header: what, where, how reversible, and why it is asking."""
    contract = invocation.contract
    lines = [
        "\n-- approval required " + "-" * 38,
        f"  intent   : {printable(invocation.request.intent)}",
        f"  operation: {invocation.request.operation}  [{invocation.tier}]",
        f"  effect   : {contract.effect_class}",
    ]
    lines += [f"  target   : {printable(str(target))}" for target in contract.targets]
    if contract.expect:
        lines.append(f"  expect   : {printable(contract.expect)}")
    lines.append(f"  why      : {decision.rationale}")
    if contract.effect_class is EffectClass.IRREVERSIBLE:
        lines.append("  !! THIS CANNOT BE UNDONE")
    return lines


def preview(invocation: Invocation, limit: int = _MAX_PREVIEW_LINES) -> list[str]:
    """What the step will do, in full enough to decide on.

    New file content is shown as a diff against the file as it is now, which
    is the question a person actually has. Everything else is its arguments.
    """
    params = invocation.request.params
    targets = invocation.contract.targets
    content = params.get("content")
    lines: list[str] = []
    diffed = isinstance(content, str) and len(targets) == 1
    if diffed:
        lines += _diff(Path(targets[0]), str(content))
    for name, value in params.items():
        if name == "content" and diffed:
            continue
        if name == "code":
            lines.append("  code:")
            lines += [f"    | {line}" for line in str(value).rstrip().splitlines()]
        else:
            lines.append(f"  {name}: {_short(value)}")
    if not lines:
        lines.append("  (no arguments)")
    if len(lines) > limit:
        lines = [*lines[:limit], f"  ... (+{len(lines) - limit} more lines)"]
    return ["\n-- preview " + "-" * 48, *(printable(line) for line in lines)]


def grant_offers(missing: Iterable[Grant], workspace: Path) -> list[str]:
    """Scopes worth offering a person after a run was denied for lacking them.

    Each is one exact path, never a directory glob, and only inside the
    workspace. A prompt-injected model can make any request it likes; what it
    cannot do is turn a yes about ``summary.txt`` into write access to
    everything. Anything outside the workspace is not offered at all: that
    grant is a thing to type, not a thing to accept.
    """
    root = resolve(workspace)
    offers: list[str] = []
    for grant in missing:
        if grant.capability not in OFFERABLE:
            continue
        target = resolve(grant.subject)
        if not target.is_relative_to(root) or target == root:
            continue
        # Escaped, so a file named "q[1].csv" is granted as itself and not as
        # the pattern that matches "q1.csv".
        scope = f"{grant.capability}:{glob.escape(str(target))}"
        if scope not in offers:
            offers.append(scope)
    return offers


def is_commit(invocation: Invocation) -> bool:
    """Whether a GUI input may commit something a person should see first."""
    request = invocation.request
    params = request.params
    if request.operation == "ui.click":
        element = str(params.get("element") or "").strip()
        if not element:
            return True  # a coordinate: whatever is under it, unknown
        return _commit_word(element)
    if request.operation == "web.click":
        # The adapter's name for the control, resolved from the page -- not the
        # planner's description of it. Unknown is treated like a coordinate.
        control = invocation.contract.control.strip()
        return not control or _commit_word(control)
    if request.operation == "web.fill":
        return False  # text in a field sends nothing until something is clicked
    if request.operation in ("ui.key", "web.press"):
        chord = params.get("chord") or params.get("key") or ""
        parts = [p.strip().lower() for p in str(chord).split("+")]
        return parts[-1:] in (["enter"], ["return"]) and bool(_MODIFIERS & set(parts[:-1]))
    if request.operation == "ui.type":
        text = str(params.get("text") or "")
        return "\n" in text or "\r" in text
    return False


def _commit_word(name: str) -> bool:
    words = "".join(c if c.isalnum() else " " for c in name.lower()).split()
    return bool(words) and words[0] in COMMIT_WORDS


def terminal_ask(
    question: str,
    choices: Iterable[str] = (),
    *,
    ask: Callable[[str], str] = input,
    out: Any = None,
) -> str | None:
    """Put a planner's question to the person. ``None`` when no one answers.

    The question is the model's words, so it is printed through
    :func:`printable` like everything else it says.
    """
    stream = out if out is not None else sys.stdout
    options = list(choices)
    print("\n-- the agent is asking " + "-" * 36, file=stream)
    for line in str(question).splitlines() or [""]:
        print(f"  {printable(line)}", file=stream)
    for number, option in enumerate(options, 1):
        print(f"    {number}. {printable(option)}", file=stream)
    hint = f"  answer (1-{len(options)}, or type your own): " if options else "  answer: "
    while True:
        try:
            reply = ask(hint).strip()
        except EOFError:
            print("", file=stream)
            return None
        if not reply:
            continue
        if options and reply.isdigit() and 1 <= int(reply) <= len(options):
            return options[int(reply) - 1]
        return reply


# -- internals ---------------------------------------------------------------


def _key(invocation: Invocation) -> tuple[str, tuple[str, ...]]:
    targets = tuple(sorted(str(resolve(t)) for t in invocation.contract.targets))
    return invocation.request.operation, targets


def _may_remember(invocation: Invocation) -> bool:
    return invocation.contract.effect_class is not EffectClass.IRREVERSIBLE and bool(
        invocation.contract.targets
    )


def _why_lines(thinking: str) -> list[str]:
    text = _truncate(thinking, 1200) if thinking.strip() else "(the model gave no reasoning)"
    body = [f"    {printable(line.strip())}" for line in text.splitlines() if line.strip()]
    return ["\n  the model's reasoning for this step (its own words, not verified):", *body]


def _diff(path: Path, new: str) -> list[str]:
    try:
        if not path.is_file():
            return [f"  new file: {path}", *(f"  + {line}" for line in new.splitlines())]
        if path.stat().st_size > _MAX_DIFF_BYTES:
            return [f"  {path} is too large to diff here; the new content is {len(new)} chars"]
        old = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"  could not read {path} to compare: {exc}"]
    if old == new:
        return [f"  {path}: the content would not change"]
    diff = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile=f"{path} (now)",
        tofile=f"{path} (after)",
        lineterm="",
    )
    return [f"  {line}" for line in diff]
