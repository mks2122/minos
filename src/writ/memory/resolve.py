"""Deictic resolution -- "the Excel we were working on yesterday".

Turns a vague reference into a concrete path, **and explains how it decided**.
The explanation is not a nicety. An unexplained resolution is an unauditable
one, and this runtime does not do unauditable: if the agent is about to edit a
file because it guessed that is what you meant, you are entitled to see the
guess.

Scoring is deliberately simple and legible -- additive signals, each of which
can be named in a sentence. A learned ranker would probably score better and
would be impossible to explain in an approval prompt, which is the wrong
trade for this component.

**A resolution is a suggestion, never an authority.** It names a path; the
broker still decides whether that path is in scope. Nothing here can widen
anything, which matters because the phrase being resolved may itself have come
from an injected file.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .store import FileRecord, MemoryStore, Touch

__all__ = ["Candidate", "Resolution", "resolve"]

DAY = 86_400.0

# Words people use for a kind of document, mapped to suffixes.
_TYPE_WORDS: dict[str, tuple[str, ...]] = {
    "excel": (".xlsx", ".xls", ".ods", ".csv"),
    "spreadsheet": (".xlsx", ".xls", ".ods", ".csv"),
    "workbook": (".xlsx", ".xls", ".ods", ".csv"),
    "sheet": (".xlsx", ".xls", ".ods", ".csv"),
    "csv": (".csv",),
    "doc": (".docx", ".doc", ".odt", ".md", ".txt"),
    "document": (".docx", ".doc", ".odt", ".md", ".txt"),
    "notes": (".md", ".txt"),
    "text": (".txt", ".md"),
    "pdf": (".pdf",),
    "presentation": (".pptx", ".ppt", ".odp"),
    "slides": (".pptx", ".ppt", ".odp"),
    "deck": (".pptx", ".ppt", ".odp"),
}

# Time phrases, as (lower_bound_ago, upper_bound_ago) in seconds.
_TIME_WORDS: dict[str, tuple[float, float]] = {
    "today": (0.0, DAY),
    "yesterday": (DAY, 2 * DAY),
    "this week": (0.0, 7 * DAY),
    "last week": (7 * DAY, 14 * DAY),
    "this month": (0.0, 31 * DAY),
    "last month": (31 * DAY, 62 * DAY),
    "recently": (0.0, 3 * DAY),
    "just now": (0.0, 3600.0),
    "earlier": (0.0, DAY),
}

_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "we",
        "i",
        "was",
        "were",
        "on",
        "in",
        "at",
        "my",
        "our",
        "that",
        "this",
        "it",
        "working",
        "work",
        "worked",
        "open",
        "opened",
        "editing",
        "edited",
        "edit",
        "file",
        "one",
        "last",
        "of",
        "to",
        "from",
        "with",
        "and",
        "for",
        "please",
        "again",
    }
)


@dataclass(frozen=True, slots=True)
class Candidate:
    path: str
    score: float
    reasons: tuple[str, ...]
    mtime: float
    last_touch: Touch | None = None


@dataclass(frozen=True, slots=True)
class Resolution:
    phrase: str
    best: Candidate | None
    candidates: tuple[Candidate, ...] = field(default_factory=tuple)

    @property
    def path(self) -> Path | None:
        return Path(self.best.path) if self.best else None

    @property
    def confident(self) -> bool:
        """A clear winner, not a coin flip.

        Ambiguity should surface as a question to the user, not as a silent
        pick -- especially when the next step writes to whatever was chosen.
        """
        if not self.best or self.best.score <= 0:
            return False
        if len(self.candidates) < 2:
            return True
        return self.best.score >= self.candidates[1].score * 1.5

    def explain(self) -> str:
        if not self.best:
            return f"Could not resolve {self.phrase!r}: nothing in memory matched."

        lines = [f"Resolved {self.phrase!r} to:", f"  {self.best.path}", "", "Because:"]
        lines.extend(f"  - {reason}" for reason in self.best.reasons)

        if len(self.candidates) > 1:
            lines.append("")
            verdict = "Ruled out" if self.confident else "Close alternatives (ambiguous)"
            lines.append(f"{verdict}:")
            for candidate in self.candidates[1:4]:
                lines.append(f"  {candidate.path}  (score {candidate.score:.1f})")
        if not self.confident:
            lines.append("")
            lines.append("  ^ Not confident. Ask rather than assume.")
        return "\n".join(lines)


def resolve(
    phrase: str,
    store: MemoryStore,
    *,
    now: float | None = None,
    limit: int = 10,
) -> Resolution:
    now = now if now is not None else time.time()
    lowered = phrase.lower()

    suffixes = _suffixes_for(lowered)
    window = _window_for(lowered)
    keywords = _keywords(lowered)

    pool: dict[str, FileRecord] = {record.path: record for record in store.files(suffixes=suffixes)}
    for record in store.search(" ".join(keywords)) if keywords else []:
        pool.setdefault(record.path, record)

    touches = {t.path: t for t in store.recently_touched()}

    scored: list[Candidate] = []
    for record in pool.values():
        score = 0.0
        reasons: list[str] = []

        if suffixes and record.suffix in suffixes:
            score += 3.0
            reasons.append(
                f"{record.suffix} matches the kind of file you named ({_type_word(lowered)})"
            )

        touch = touches.get(record.path)
        reference_ts = max(record.mtime, touch.ts if touch else 0.0)

        if window:
            low, high = window
            age = now - reference_ts
            if low <= age < high:
                score += 4.0
                reasons.append(f"last changed {_ago(age)}, which is {_time_word(lowered)}")
            else:
                score -= 2.0
                reasons.append(f"last changed {_ago(age)} -- outside the window you gave")

        matched = [k for k in keywords if k in record.name.lower()]
        if matched:
            score += 2.0 * len(matched)
            reasons.append(f"filename contains {', '.join(repr(m) for m in matched)}")

        if touch is not None:
            score += 2.0 if touch.mutating else 0.5
            what = "changed" if touch.mutating else "read"
            goal = f" during the task {touch.goal!r}" if touch.goal else ""
            reasons.append(f"this runtime {what} it{goal} ({touch.operation})")

        # Recency, mildly, so a tie breaks toward the more recent file.
        age_days = max(0.0, (now - reference_ts) / DAY)
        score += max(0.0, 1.5 - 0.1 * age_days)

        if score > 0 or reasons:
            scored.append(
                Candidate(
                    path=record.path,
                    score=round(score, 3),
                    reasons=tuple(reasons) or ("no specific signal; ranked by recency",),
                    mtime=record.mtime,
                    last_touch=touch,
                )
            )

    scored.sort(key=lambda c: (-c.score, -c.mtime))
    top = tuple(scored[:limit])
    return Resolution(phrase=phrase, best=top[0] if top else None, candidates=top)


# -- phrase parsing --------------------------------------------------------


def _suffixes_for(lowered: str) -> tuple[str, ...]:
    for word, suffixes in _TYPE_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return suffixes
    return ()


def _type_word(lowered: str) -> str:
    for word in _TYPE_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return word
    return "that type"


def _window_for(lowered: str) -> tuple[float, float] | None:
    # Longest phrase first, so "last week" beats "week".
    for word in sorted(_TIME_WORDS, key=len, reverse=True):
        if word in lowered:
            return _TIME_WORDS[word]
    return None


def _time_word(lowered: str) -> str:
    for word in sorted(_TIME_WORDS, key=len, reverse=True):
        if word in lowered:
            return word
    return "the window you gave"


def _keywords(lowered: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", lowered)
    return [w for w in words if w not in _STOPWORDS and w not in _TYPE_WORDS and len(w) > 2]


def _ago(seconds: float) -> str:
    if seconds < 60:
        return "moments ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} minutes ago"
    if seconds < DAY:
        return f"{int(seconds // 3600)} hours ago"
    days = int(seconds // DAY)
    return "1 day ago" if days == 1 else f"{days} days ago"
