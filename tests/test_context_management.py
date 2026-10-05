"""Context management: every request fits, and when one cannot, it says so.

The failure these guard against is silent. Ollama drops the oldest tokens --
the system prompt and tool schemas -- and the model quietly stops calling
tools; a hosted API returns a 400 that reads like a bug; Claude stops with
``prompt is too long``. Each test pins one piece of the answer.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import pytest

from minos.planner.base import Done, Observation
from minos.planner.claude import CONTEXT_EDITING_BETA, ClaudePlanner
from minos.planner.context import (
    Budget,
    ContextOverflow,
    Estimator,
    fit_messages,
    is_overflow_error,
    shrink_text,
)
from minos.planner.local import HOSTED_DEFAULT_WINDOW, LocalPlanner, discover_context
from minos.scopes import ScopeSet
from minos.types import ActionRequest

SCOPES = ScopeSet.parse(["fs.read:/ws/**"])


def _conversation(steps: int, result_chars: int = 400) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "THE GOAL"},
    ]
    for i in range(steps):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": f"c{i}", "function": {"name": f"fs_read_{i}", "arguments": "{}"}}
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * result_chars})
    return messages


# -- the estimator -----------------------------------------------------------


def test_calibration_grows_fast_and_shrinks_slowly():
    """Under-estimating overflows; over-estimating only compacts early."""
    estimator = Estimator()
    estimator.calibrate(1000, 1500)
    assert estimator.ratio == pytest.approx(1.5)
    estimator.calibrate(1000, 2000)
    assert estimator.ratio == pytest.approx(2.0)
    estimator.calibrate(1000, 1000)
    assert 1.7 < estimator.ratio < 2.0


def test_calibration_ignores_nonsense():
    estimator = Estimator()
    estimator.calibrate(0, 500)
    estimator.calibrate(500, 0)
    assert estimator.ratio == 1.0


# -- fitting -----------------------------------------------------------------


def test_a_conversation_that_fits_is_sent_whole():
    messages = _conversation(3)
    fitted = fit_messages(messages, Budget(window=100_000, fixed=0, reserve=0))
    assert fitted.messages == messages
    assert fitted.dropped_steps == 0


def test_the_budget_not_a_count_decides_what_is_kept():
    messages = _conversation(40, result_chars=2000)
    budget = Budget(window=6000, fixed=1000, reserve=1000)
    fitted = fit_messages(messages, budget)

    assert fitted.tokens <= budget.available
    assert fitted.dropped_steps > 0
    assert fitted.messages[0]["content"] == "SYSTEM PROMPT"
    assert fitted.messages[1]["content"] == "THE GOAL"
    assert "fs_read_0" in fitted.messages[2]["content"]
    assert fitted.messages[-1]["content"] == "x" * 2000  # the newest result, intact


def test_a_tool_result_never_loses_its_call():
    fitted = fit_messages(_conversation(30), Budget(window=3000, fixed=0, reserve=500))
    for previous, message in zip(fitted.messages, fitted.messages[1:], strict=False):
        if message["role"] == "tool":
            assert previous["role"] in ("assistant", "tool")
            assert previous["role"] != "assistant" or previous.get("tool_calls")


def test_fitting_never_mutates_the_history():
    messages = _conversation(30, result_chars=5000)
    snapshot = json.dumps(messages)
    fit_messages(messages, Budget(window=2500, fixed=0, reserve=500))
    assert json.dumps(messages) == snapshot


def test_an_oversized_latest_result_is_shrunk_not_dropped():
    """The newest result is what the model is about to act on."""
    messages = _conversation(1, result_chars=200_000)
    fitted = fit_messages(messages, Budget(window=4000, fixed=0, reserve=500))

    result = fitted.messages[-1]["content"]
    assert fitted.shrunk_results == 1
    assert "cut to fit the context window" in result
    assert result.startswith("x") and result.endswith("x")
    assert fitted.tokens <= 3500


def test_a_head_that_cannot_fit_is_an_error_not_a_truncation():
    messages = [{"role": "system", "content": "s" * 40_000}, {"role": "user", "content": "g"}]
    with pytest.raises(ContextOverflow, match="context window is 4096"):
        fit_messages(messages, Budget(window=4096, fixed=0, reserve=512))


def test_the_count_ceiling_still_applies():
    fitted = fit_messages(
        _conversation(20), Budget(window=1_000_000, fixed=0, reserve=0), max_units=3
    )
    assert fitted.dropped_steps == 17


def test_shrink_text_keeps_both_ends():
    text = "BEGIN" + "m" * 10_000 + "END"
    out = shrink_text(text, 1000)
    assert out.startswith("BEGIN") and out.endswith("END") and len(out) < 1200


@pytest.mark.parametrize(
    "detail",
    [
        "This model's maximum context length is 8192 tokens.",
        "prompt is too long: 210000 tokens > 200000 maximum",
        "Input is too long for requested model.",
        "context_length_exceeded",
    ],
)
def test_overflow_errors_are_recognised(detail):
    assert is_overflow_error(detail)


def test_other_errors_are_not_overflow():
    assert not is_overflow_error("invalid api key")


# -- the OpenAI-shaped planner -----------------------------------------------


def _reply(name: str = "fs_read", usage: int | None = None) -> bytes:
    body: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call",
                            "function": {"name": name, "arguments": json.dumps({"path": "/ws/a"})},
                        }
                    ],
                }
            }
        ]
    }
    if usage is not None:
        body["usage"] = {"prompt_tokens": usage}
    return json.dumps(body).encode()


class _Server:
    """A fake chat endpoint that records what each request carried."""

    def __init__(self, replies: list[Any] | None = None) -> None:
        self.replies = replies or []
        self.requests: list[dict[str, Any]] = []

    def __call__(self, req, timeout=None):
        if req.data is None:
            raise urllib.error.URLError("no /models here")
        self.requests.append(json.loads(req.data))
        reply = self.replies.pop(0) if self.replies else _reply()
        if isinstance(reply, Exception):
            raise reply

        class Response:
            def read(self_inner):
                return reply

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        return Response()


def _observation(chars: int = 3000) -> Observation:
    return Observation(
        request=ActionRequest(goal_id="t", intent="", operation="fs.read", params={}),
        status="ok",
        result="y" * chars,
    )


def test_a_long_run_never_sends_a_request_over_budget(monkeypatch):
    """Sixty steps into a 6144-token window, the system prompt is still there."""
    server = _Server()
    monkeypatch.setattr(urllib.request, "urlopen", server)
    planner = LocalPlanner(operations=("fs.read",), context_tokens=6144, keep_exchanges=100)

    planner.next_action("THE GOAL", [], SCOPES)
    for _ in range(60):
        step = planner.next_action("THE GOAL", [_observation()], SCOPES)
        assert isinstance(step, ActionRequest)

    budget = planner.budget()
    for body in server.requests:
        assert body["messages"][0]["role"] == "system"
        assert "THE GOAL" in body["messages"][1]["content"]
        sent = Estimator().tokens(body["messages"]) + budget.fixed
        assert sent <= budget.window - budget.reserve
    assert planner.last_context is not None and planner.last_context.dropped_steps > 0


def test_a_server_overflow_is_retried_once_with_half_the_budget(monkeypatch):
    overflow = urllib.error.HTTPError(
        "u",
        400,
        "Bad Request",
        {},
        io.BytesIO(b'{"error":{"message":"maximum context length is 4096 tokens"}}'),
    )
    server = _Server([overflow, _reply()])
    monkeypatch.setattr(urllib.request, "urlopen", server)
    planner = LocalPlanner(operations=("fs.read",), context_tokens=16384)

    step = planner.next_action("goal", [], SCOPES)

    assert isinstance(step, ActionRequest)
    assert len(server.requests) == 2


def test_an_overflow_that_persists_is_explained(monkeypatch):
    def overflow():
        return urllib.error.HTTPError(
            "u", 400, "Bad Request", {}, io.BytesIO(b'{"error":{"message":"prompt is too long"}}')
        )

    monkeypatch.setattr(urllib.request, "urlopen", _Server([overflow(), overflow()]))
    done = LocalPlanner(operations=("fs.read",)).next_action("goal", [], SCOPES)

    assert isinstance(done, Done) and not done.succeeded
    assert "larger than the model's context window" in done.summary


def test_any_other_400_keeps_its_own_message(monkeypatch):
    bad = urllib.error.HTTPError(
        "u", 400, "Bad Request", {}, io.BytesIO(b'{"error":{"message":"tools[0] is invalid"}}')
    )
    monkeypatch.setattr(urllib.request, "urlopen", _Server([bad]))
    done = LocalPlanner(operations=("fs.read",)).next_action("goal", [], SCOPES)

    assert isinstance(done, Done)
    assert "tools[0] is invalid" in done.summary


def test_a_window_too_small_for_the_schemas_fails_with_a_reason(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _Server())
    planner = LocalPlanner(operations=("fs.read", "fs.write", "sheet.set_cell"), context_tokens=900)

    done = planner.next_action("goal", [], SCOPES)

    assert isinstance(done, Done) and not done.succeeded
    assert "no longer fits" in done.summary


def test_the_server_count_calibrates_the_estimate(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _Server([_reply(usage=50_000)]))
    planner = LocalPlanner(operations=("fs.read",))
    planner.next_action("goal", [], SCOPES)
    assert planner._estimator.ratio > 2


def test_a_hosted_window_is_discovered_from_models(monkeypatch):
    class Response:
        def read(self):
            return json.dumps(
                {"data": [{"id": "other"}, {"id": "openai/gpt-4.1", "context_length": 1047576}]}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: Response())
    assert discover_context("https://x/api/v1", "openai/gpt-4.1", {}) == 1047576


def test_an_unknown_hosted_window_falls_back(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _Server())
    planner = LocalPlanner(operations=("fs.read",), local_server=False, api_key="k")
    assert planner.window() == HOSTED_DEFAULT_WINDOW


def test_run_fits_to_what_ollama_really_serves(monkeypatch):
    from minos import __main__ as cli
    from minos import doctor

    monkeypatch.setattr(doctor, "_served_context", lambda url, model: 4096)
    planner = LocalPlanner(operations=("fs.read",), context_tokens=16384)
    cli._context_warning(planner, "http://localhost:11434/v1")
    assert planner.window() == 4096


# -- the Claude planner ------------------------------------------------------


@dataclass
class Block:
    type: str
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    text: str = ""


@dataclass
class Response:
    content: list
    stop_reason: str = "tool_use"
    context_management: Any = None


@dataclass
class Messages:
    replies: list
    calls: list = field(default_factory=list)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@dataclass
class Beta:
    messages: Messages


@dataclass
class ClientWithBeta:
    messages: Messages
    beta: Beta


def _tool_use() -> Response:
    return Response([Block(type="tool_use", id="tu", name="fs_read", input={"path": "/ws/a"})])


def test_claude_asks_the_api_to_clear_old_tool_results():
    beta = Messages([_tool_use()])
    planner = ClaudePlanner(
        operations=("fs.read",), client=ClientWithBeta(Messages([]), Beta(beta))
    )
    planner.next_action("goal", [], SCOPES)

    call = beta.calls[0]
    assert call["betas"] == [CONTEXT_EDITING_BETA]
    edit = call["context_management"]["edits"][0]
    assert edit["type"] == "clear_tool_uses_20250919"
    assert edit["trigger"] == {"type": "input_tokens", "value": planner.clear_at_tokens}
    assert edit["keep"] == {"type": "tool_uses", "value": planner.keep_tool_uses}


def test_claude_history_is_append_only():
    """Editing an earlier turn invalidates every later thinking block."""
    beta = Messages([_tool_use() for _ in range(5)])
    planner = ClaudePlanner(
        operations=("fs.read",), client=ClientWithBeta(Messages([]), Beta(beta))
    )
    planner.next_action("goal", [], SCOPES)
    seen: list[str] = []
    for _ in range(4):
        planner.next_action("goal", [_observation(50_000)], SCOPES)
        current = [repr(m) for m in planner._messages]
        assert current[: len(seen)] == seen
        seen = current


def test_claude_caps_a_result_before_appending_it():
    beta = Messages([_tool_use(), _tool_use()])
    planner = ClaudePlanner(
        operations=("fs.read",),
        client=ClientWithBeta(Messages([]), Beta(beta)),
        max_result_chars=5000,
    )
    planner.next_action("goal", [], SCOPES)
    planner.next_action("goal", [_observation(200_000)], SCOPES)

    content = planner._messages[2]["content"][0]["content"]
    assert len(content) < 6000
    assert "cut to fit the context window" in content


def test_claude_without_a_beta_namespace_still_works():
    plain = Messages([_tool_use()])
    planner = ClaudePlanner(operations=("fs.read",), client=type("C", (), {"messages": plain})())
    assert isinstance(planner.next_action("goal", [], SCOPES), ActionRequest)
    assert "context_management" not in plain.calls[0]


class _TooLong(Exception):
    status_code = 400


def test_claude_prompt_too_long_is_an_answer_not_a_traceback():
    beta = Messages([_TooLong("prompt is too long: 1200000 tokens > 1000000 maximum")])
    planner = ClaudePlanner(
        operations=("fs.read",), client=ClientWithBeta(Messages([]), Beta(beta))
    )
    done = planner.next_action("goal", [], SCOPES)
    assert isinstance(done, Done) and not done.succeeded
    assert "larger than the model's context window" in done.summary


def test_claude_other_errors_still_raise():
    class Boom(Exception):
        status_code = 500

    beta = Messages([Boom("overloaded")])
    planner = ClaudePlanner(
        operations=("fs.read",), client=ClientWithBeta(Messages([]), Beta(beta))
    )
    with pytest.raises(Boom):
        planner.next_action("goal", [], SCOPES)


def test_claude_window_exceeded_mid_answer_is_reported():
    beta = Messages([Response([Block(type="text", text="...")], "model_context_window_exceeded")])
    planner = ClaudePlanner(
        operations=("fs.read",), client=ClientWithBeta(Messages([]), Beta(beta))
    )
    done = planner.next_action("goal", [], SCOPES)
    assert isinstance(done, Done) and "ran out of context window" in done.summary


def test_a_context_size_is_not_mistaken_for_a_secret(monkeypatch, tmp_path):
    from minos.config import settings

    monkeypatch.setenv("MINOS_CONTEXT_TOKENS", "8192")
    monkeypatch.setenv("MINOS_API_KEY", "sk-hidden")
    lines = "\n".join(settings(dotenv=tmp_path / "none.env").describe())
    assert "minos_context_tokens" not in lines
    assert "minos_api_key" in lines and "sk-hidden" not in lines
