"""Keeping a planner's conversation inside its context window.

A long task fails here quietly. Every step appends an assistant turn and a tool
result; eventually the request no longer fits, and what happens next depends on
the server -- none of it good:

* **Ollama** keeps the newest tokens and drops the oldest, which are the system
  prompt and the tool schemas. The model stops being able to call tools and
  nobody is told.
* **OpenAI-compatible hosted APIs** return a 400, which reads like a bug.
* **Claude** returns ``prompt is too long``, or stops mid-answer with
  ``model_context_window_exceeded``.

This module is the client half of the answer, for the OpenAI-shaped planners:
estimate what a request costs, fit the conversation to a budget *before* it is
sent, and say so when it cannot be made to fit. The Claude planner does not use
it to edit history -- on current Claude models an edited history invalidates
thinking blocks -- and instead asks the API to clear old tool results
server-side (see :mod:`minos.planner.claude`).

**Estimates, calibrated.** Four characters to a token is wrong for every
tokenizer, in a known direction per model. Servers report what a request really
cost (``usage.prompt_tokens``), so :class:`Estimator` scales its guess by the
observed ratio. The point is not precision; it is never being off by enough to
overflow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Budget",
    "ContextOverflow",
    "Estimator",
    "Fitted",
    "fit_messages",
    "is_overflow_error",
    "shrink_text",
]

CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD = 4
"""Tokens a chat template spends framing each message (role markers etc.)."""


class ContextOverflow(Exception):
    """The request cannot be made to fit, even with everything optional dropped."""


@dataclass
class Estimator:
    """Characters-to-tokens, corrected by what the server says it counted."""

    ratio: float = 1.0
    """Observed tokens / estimated tokens. Starts neutral."""

    samples: int = 0

    def raw(self, value: Any) -> int:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        return len(text) // CHARS_PER_TOKEN + 1

    def tokens(self, value: Any) -> int:
        return int(self.raw(value) * self.ratio) + 1

    def message(self, message: dict[str, Any]) -> int:
        return self.tokens(message) + MESSAGE_OVERHEAD

    def calibrate(self, estimated_raw: int, actual: int) -> None:
        """Fold one server-reported count into the ratio.

        Only ever lets the ratio grow quickly and shrink slowly: under-estimating
        overflows the window, over-estimating merely compacts a little early.
        """
        if estimated_raw <= 0 or actual <= 0:
            return
        observed = actual / estimated_raw
        if self.samples == 0:
            self.ratio = max(observed, 0.5)
        elif observed > self.ratio:
            self.ratio = observed
        else:
            self.ratio = 0.8 * self.ratio + 0.2 * observed
        self.samples += 1


@dataclass(frozen=True, slots=True)
class Budget:
    """How many tokens the conversation itself may use."""

    window: int
    fixed: int
    """Tools and system prompt: sent every time, never compactable."""

    reserve: int
    """Room left for the answer. Generation counts against the window too."""

    @property
    def available(self) -> int:
        return self.window - self.fixed - self.reserve

    @classmethod
    def for_window(cls, window: int, fixed: int, max_output: int = 0) -> Budget:
        reserve = max_output or max(1024, window // 8)
        return cls(window=window, fixed=fixed, reserve=reserve)


@dataclass
class Fitted:
    """A conversation fitted to a budget, and what fitting it cost."""

    messages: list[dict[str, Any]]
    tokens: int
    dropped_steps: int = 0
    shrunk_results: int = 0
    notes: list[str] = field(default_factory=list)


def shrink_text(text: str, keep_chars: int) -> str:
    """Cut text to ``keep_chars``, keeping both ends and saying what went."""
    if len(text) <= keep_chars:
        return text
    if keep_chars < 200:
        return text[: max(keep_chars, 0)] + " ... [truncated to fit the context window]"
    head = keep_chars * 2 // 3
    tail = keep_chars - head
    dropped = len(text) - head - tail
    return (
        f"{text[:head]}\n... [{dropped} characters cut to fit the context window] ...\n"
        f"{text[-tail:]}"
    )


def _units(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group messages so a tool result never travels without its call.

    An assistant message carrying ``tool_calls`` and the ``tool`` messages that
    answer it are one unit: dropping one half is a request some servers reject
    outright, and others answer with nonsense.
    """
    units: list[list[dict[str, Any]]] = []
    for message in messages:
        if message.get("role") == "tool" and units and _opens_calls(units[-1]):
            units[-1].append(message)
        else:
            units.append([message])
    return units


def _opens_calls(unit: list[dict[str, Any]]) -> bool:
    return bool(unit and unit[0].get("role") == "assistant" and unit[0].get("tool_calls"))


def _called(unit: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for message in unit:
        for call in message.get("tool_calls") or []:
            name = call.get("function", {}).get("name")
            if name:
                names.append(str(name))
    return names


def fit_messages(
    messages: list[dict[str, Any]],
    budget: Budget,
    estimator: Estimator | None = None,
    *,
    head: int = 2,
    max_units: int | None = None,
) -> Fitted:
    """Fit an OpenAI-shaped conversation into ``budget``. Never mutates the input.

    Kept, in priority order:

    1. the first ``head`` messages -- system prompt and goal -- always;
    2. the newest exchanges, as many as fit (and at most ``max_units``);
    3. one line naming every step that was dropped, so the model knows *that*
       it did them even though it can no longer see how.

    If even the newest exchange will not fit beside the head, its tool results
    are shrunk -- both ends kept, the cut announced -- rather than dropped,
    because the latest result is the one the model is about to act on. If the
    head alone does not fit, nothing can be done here, and
    :class:`ContextOverflow` says so instead of letting the server truncate.
    """
    estimator = estimator or Estimator()
    available = budget.available

    fixed = list(messages[:head])
    fixed_cost = sum(estimator.message(m) for m in fixed)
    if fixed_cost > available:
        raise ContextOverflow(
            f"the system prompt and goal need ~{fixed_cost + budget.fixed} tokens with the "
            f"tool schemas, and the context window is {budget.window} "
            f"({budget.reserve} kept free for the answer). Raise the context size or "
            "grant fewer operations."
        )

    units = _units(messages[head:])
    if not units:
        return Fitted(messages=fixed, tokens=fixed_cost)

    note_reserve = 64 + 8 * len(units)  # what the "omitted" line can cost
    room = available - fixed_cost - note_reserve
    kept: list[list[dict[str, Any]]] = []
    used = 0
    for unit in reversed(units):
        if max_units is not None and len(kept) >= max_units:
            break
        cost = sum(estimator.message(m) for m in unit)
        if used + cost > room:
            break
        kept.append(unit)
        used += cost
    kept.reverse()

    shrunk = 0
    if not kept:
        # The newest exchange is too big on its own. Shrink its results.
        newest = [dict(m) for m in units[-1]]
        other = sum(estimator.message(m) for m in newest if m.get("role") != "tool")
        results = [m for m in newest if m.get("role") == "tool"]
        if results and other < room:
            share = (room - other) // len(results)
            for message in results:
                keep_chars = max(
                    int((share - MESSAGE_OVERHEAD) * CHARS_PER_TOKEN / estimator.ratio), 0
                )
                message["content"] = shrink_text(str(message.get("content", "")), keep_chars)
                shrunk += 1
            kept = [newest]
            used = sum(estimator.message(m) for m in newest)
        else:
            raise ContextOverflow(
                f"the latest step alone needs more than the ~{room} tokens left after the "
                "system prompt and goal; nothing can be cut without losing what the "
                "model is about to act on"
            )

    dropped_units = units[: len(units) - len(kept)]
    result = list(fixed)
    notes: list[str] = []
    if dropped_units:
        names = [name for unit in dropped_units for name in _called(unit)]
        note_text = (
            f"[{len(dropped_units)} earlier steps are omitted to fit the context window. "
            + (f"In order, you called: {', '.join(names)}. " if names else "")
            + "Do not repeat work you have already done.]"
        )
        result.append({"role": "user", "content": note_text})
        notes.append(f"dropped {len(dropped_units)} earlier step(s)")
    if shrunk:
        notes.append(f"shrank {shrunk} tool result(s)")
    for unit in kept:
        result.extend(unit)

    return Fitted(
        messages=result,
        tokens=sum(estimator.message(m) for m in result),
        dropped_steps=len(dropped_units),
        shrunk_results=shrunk,
        notes=notes,
    )


_OVERFLOW_MARKERS = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "prompt is too long",
    "too many tokens",
    "reduce the length",
    "input is too long",
    "exceeds the model",
)


def is_overflow_error(detail: str) -> bool:
    """Does a server's error text say the request was too big for the window?

    Servers word this a dozen ways and none sends a portable code, so this
    matches on the phrases they actually use. A false negative costs the retry,
    not correctness: the error is still reported.
    """
    lowered = detail.lower()
    return any(marker in lowered for marker in _OVERFLOW_MARKERS)
