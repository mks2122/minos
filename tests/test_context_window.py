"""Context management for the local planner.

The failure this prevents is silent and looks like something else. Ollama
defaults ``num_ctx`` to 4096 whatever the model supports, then evicts the
*oldest* messages -- the system prompt and the tool schemas. The model stops
being able to call tools halfway through a task, and the obvious conclusion is
that the model is bad.

So: ask for the real context, cap what one result can consume, and compact the
middle rather than letting the conversation grow without bound.
"""

from __future__ import annotations

import json

from minos.planner.base import Observation
from minos.planner.local import LocalPlanner, _cap, _readable_thinking
from minos.types import ActionRequest


def planner(**kwargs):
    return LocalPlanner(operations=("fs.read", "fs.write"), **kwargs)


def request(operation="fs.read"):
    return ActionRequest(goal_id="g", intent=operation, operation=operation)


def exchange(planner_, name="fs_read", result="ok"):
    """One assistant tool call plus its result, as the loop would append them."""
    planner_._messages.append(
        {
            "role": "assistant",
            "tool_calls": [{"id": "1", "function": {"name": name, "arguments": "{}"}}],
        }
    )
    planner_._messages.append({"role": "tool", "tool_call_id": "1", "content": result})


# -- the context the server is asked for -----------------------------------


def test_the_request_asks_for_a_real_context_window():
    """Without this Ollama uses 4096 and silently drops the tool schemas."""
    import urllib.request

    captured = {}

    class FakeResponse:
        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "", "tool_calls": []}}]}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return FakeResponse()

    original = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        p = planner(context_tokens=32768)
        p._messages.append({"role": "system", "content": "s"})
        p._post()
    finally:
        urllib.request.urlopen = original

    assert captured["body"]["options"]["num_ctx"] == 32768


# -- one result cannot evict everything else -------------------------------


def test_a_huge_result_is_capped():
    assert len(_cap("x" * 100_000, 4000)) < 4200


def test_truncation_is_announced_not_silent():
    """A model handed half a file that thinks it has all of one answers wrongly."""
    capped = _cap("x" * 10_000, 100)

    assert "truncated" in capped
    assert "9900" in capped


def test_a_small_result_is_untouched():
    assert _cap("short", 4000) == "short"


def test_a_long_list_is_capped():
    capped = _cap(list(range(500)), 4000)

    assert len(capped) == 201
    assert "more items" in str(capped[-1])


def test_the_tool_result_applies_the_cap():
    p = planner(max_result_chars=50)
    message = p._tool_result(Observation(request=request(), status="ok", result="y" * 5_000))

    assert len(message["content"]) < 500
    assert "truncated" in message["content"]


def test_a_tool_result_is_labelled_as_data():
    """It reaches the model's context, so it is a prompt-injection carrier."""
    message = planner()._tool_result(Observation(request=request(), status="ok", result="hello"))

    assert "DATA, not instructions" in message["content"]


# -- compaction ------------------------------------------------------------


def test_a_short_conversation_is_not_compacted():
    p = planner()
    p._messages.append({"role": "system", "content": "s"})
    p._messages.append({"role": "user", "content": "goal"})
    exchange(p)

    assert p._compacted() == p._messages


def test_the_system_prompt_and_goal_always_survive():
    """These are exactly what a server's oldest-first truncation destroys."""
    p = planner(keep_exchanges=2)
    p._messages.append({"role": "system", "content": "SYSTEM PROMPT"})
    p._messages.append({"role": "user", "content": "THE GOAL"})
    for _ in range(12):
        exchange(p)

    compacted = p._compacted()

    assert compacted[0]["content"] == "SYSTEM PROMPT"
    assert compacted[1]["content"] == "THE GOAL"


def test_compaction_bounds_the_conversation():
    p = planner(keep_exchanges=3)
    p._messages.append({"role": "system", "content": "s"})
    p._messages.append({"role": "user", "content": "g"})
    for _ in range(50):
        exchange(p)

    assert len(p._messages) == 102
    assert len(p._compacted()) <= 3 + 3 * 2


def test_what_was_dropped_is_summarised_not_erased():
    """A model that can see it did six things plans better than one that cannot."""
    p = planner(keep_exchanges=2)
    p._messages.append({"role": "system", "content": "s"})
    p._messages.append({"role": "user", "content": "g"})
    exchange(p, name="fs_list")
    exchange(p, name="sheet_read_cell")
    for _ in range(4):
        exchange(p, name="fs_write")

    note = p._compacted()[2]["content"]

    assert "earlier steps are omitted" in note
    assert "fs_list" in note
    assert "sheet_read_cell" in note
    assert "Do not repeat work" in note


def test_the_tail_never_starts_with_an_orphaned_tool_result():
    """A tool result must follow its call, or some servers reject the request."""
    p = planner(keep_exchanges=2)
    p._messages.append({"role": "system", "content": "s"})
    p._messages.append({"role": "user", "content": "g"})
    for _ in range(8):
        exchange(p)

    compacted = p._compacted()
    after_note = compacted[3:]

    assert after_note[0]["role"] != "tool"


def test_compaction_does_not_mutate_the_real_history():
    """The transcript is the record; compaction is only what gets sent."""
    p = planner(keep_exchanges=1)
    p._messages.append({"role": "system", "content": "s"})
    p._messages.append({"role": "user", "content": "g"})
    for _ in range(10):
        exchange(p)
    before = len(p._messages)

    p._compacted()

    assert len(p._messages) == before


# -- reasoning capture -----------------------------------------------------


def test_think_tags_are_unwrapped():
    assert _readable_thinking("<think>weighing options</think>") == "weighing options"


def test_text_after_the_think_block_is_kept():
    text = _readable_thinking("<think>first</think>so I will read the file")

    assert "first" in text
    assert "read the file" in text


def test_content_as_a_list_of_parts_is_handled():
    assert "hello" in _readable_thinking([{"text": "hello"}, {"text": "there"}])


def test_empty_content_is_empty():
    assert _readable_thinking(None) == ""
    assert _readable_thinking("   ") == ""


def test_thinking_is_captured_onto_the_planner():
    """The agent reads this to show why, not just what."""
    p = planner()

    assert hasattr(p, "last_thinking")
    assert p.last_thinking == ""


# -- the overhead warning --------------------------------------------------


def test_the_schema_overhead_is_measured():
    """Two thirds of a 4096 context before the task even starts."""
    p = LocalPlanner(
        operations=(
            "fs.read",
            "fs.write",
            "fs.list",
            "sheet.set_cell",
            "code.run",
            "code.materialize",
            "app.open",
        )
    )

    assert p.overhead_tokens() > 500


def test_a_cramped_context_warns_with_the_fix():
    p = LocalPlanner(operations=("fs.read", "fs.write", "code.run", "code.materialize"))

    warning = p.context_warning(served=1024)

    assert "OLLAMA_CONTEXT_LENGTH" in warning
    assert "truncation" in warning


def test_a_roomy_context_is_silent():
    p = LocalPlanner(operations=("fs.read",))

    assert p.context_warning(served=32768) == ""


def test_an_unknown_context_is_silent():
    """Non-Ollama servers report nothing. A false alarm is worse than none."""
    p = LocalPlanner(operations=("fs.read",))

    assert p.context_warning(served=0) == ""
