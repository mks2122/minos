"""Hosted endpoints fail in ways a local server does not.

Found by the first real run against a free OpenRouter model: an upstream 503
arrived as an error object inside an HTTP 200, and the planner died with
``KeyError: 'choices'``. Transient failures are now retried with backoff, and
everything else becomes a reason rather than a traceback.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

import minos.planner.local as local_module
from minos.planner.base import Done
from minos.planner.local import LocalPlanner
from minos.scopes import ScopeSet
from minos.types import ActionRequest

SCOPES = ScopeSet.parse(["fs.read:/ws/**"])

OK = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c", "function": {"name": "fs_read", "arguments": '{"path": "/ws/a"}'}}
                ],
            }
        }
    ]
}
OVERLOADED = {
    "id": "gen-1",
    "error": {"message": "Upstream error from Nvidia: Service temporarily overloaded", "code": 503},
}


class _Server:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply

        class Response:
            def read(self_inner):
                return json.dumps(reply).encode()

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        return Response()


@pytest.fixture
def slept(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(local_module.time, "sleep", waits.append)
    return waits


def _planner() -> LocalPlanner:
    return LocalPlanner(
        operations=("fs.read",), local_server=False, label="OpenRouter", context_window=65536
    )


def test_an_overloaded_answer_inside_a_200_is_retried(monkeypatch, slept):
    server = _Server([OVERLOADED, OVERLOADED, OK])
    monkeypatch.setattr(urllib.request, "urlopen", server)

    step = _planner().next_action("goal", [], SCOPES)

    assert isinstance(step, ActionRequest)
    assert server.calls == 3
    assert slept == [2.0, 4.0]


def test_an_overload_that_lasts_is_reported_not_raised(monkeypatch, slept):
    monkeypatch.setattr(urllib.request, "urlopen", _Server([OVERLOADED] * 4))

    done = _planner().next_action("goal", [], SCOPES)

    assert isinstance(done, Done) and not done.succeeded
    assert "still unavailable after 4 attempts" in done.summary
    assert "overloaded" in done.summary


def test_a_permanent_error_in_the_body_is_not_retried(monkeypatch, slept):
    bad = {"error": {"message": "No endpoints found that support tool use", "code": 404}}
    server = _Server([bad])
    monkeypatch.setattr(urllib.request, "urlopen", server)

    done = _planner().next_action("goal", [], SCOPES)

    assert isinstance(done, Done) and "No endpoints found" in done.summary
    assert server.calls == 1 and slept == []


def test_no_choices_and_no_error_is_a_reason(monkeypatch, slept):
    monkeypatch.setattr(urllib.request, "urlopen", _Server([{"id": "x"}]))
    done = _planner().next_action("goal", [], SCOPES)
    assert isinstance(done, Done) and "no choices" in done.summary


def test_a_rate_limit_waits_as_long_as_it_is_told(monkeypatch, slept):
    limited = urllib.error.HTTPError(
        "u", 429, "Too Many Requests", {"Retry-After": "7"}, io.BytesIO(b'{"error": {}}')
    )
    monkeypatch.setattr(urllib.request, "urlopen", _Server([limited, OK]))

    assert isinstance(_planner().next_action("goal", [], SCOPES), ActionRequest)
    assert slept == [7.0]


def test_a_refused_key_is_not_retried(monkeypatch, slept):
    refused = urllib.error.HTTPError(
        "u", 401, "Unauthorized", {}, io.BytesIO(b'{"error": {"message": "bad key"}}')
    )
    server = _Server([refused])
    monkeypatch.setattr(urllib.request, "urlopen", server)

    done = _planner().next_action("goal", [], SCOPES)

    assert isinstance(done, Done) and "refused the API key" in done.summary
    assert server.calls == 1 and slept == []


# -- an unreachable model is not a result ----------------------------------------


def test_a_lasting_overload_is_marked_unavailable(monkeypatch, slept):
    monkeypatch.setattr(urllib.request, "urlopen", _Server([OVERLOADED] * 4))
    done = _planner().next_action("goal", [], SCOPES)
    assert isinstance(done, Done) and done.unavailable


def test_a_permanent_error_is_not_unavailability(monkeypatch, slept):
    bad = {"error": {"message": "No endpoints found that support tool use", "code": 404}}
    monkeypatch.setattr(urllib.request, "urlopen", _Server([bad]))
    done = _planner().next_action("goal", [], SCOPES)
    assert isinstance(done, Done) and not done.unavailable


def test_a_refuse_task_is_not_won_by_a_model_that_never_answered():
    """Found by scoring a free model: four REFUSE 'passes' were 503s."""
    from minos.evals.harness import run_task
    from minos.evals.suite import SUITE
    from minos.planner.scripted import ScriptedPlanner

    task = next(t for t in SUITE if t.id == "refuse.write_outside_scope")
    gone = Done(summary="unavailable after 4 attempts", succeeded=False, unavailable=True)
    result = run_task(task, lambda task, ws: ScriptedPlanner([gone]))

    assert result.unavailable
    assert not result.succeeded
    assert result.row().lstrip().startswith("N/A")


def test_unavailable_survives_the_process_boundary():
    from minos.planner.isolated import _decode_step, _encode_step

    back = _decode_step(_encode_step(Done(summary="x", succeeded=False, unavailable=True)))
    assert isinstance(back, Done) and back.unavailable
