"""Watching a run, and keeping what you watched.

Two properties matter more than the formatting: an observer must never be able
to change the outcome of a run, and a session transcript must never be mistaken
for the audit log.
"""

from __future__ import annotations

import io
import json

import pytest

from minos.trace import ConsolePrinter, Event, SessionRecorder, fan_out


def event(kind="plan", step=1, text="fs.write", **data):
    return Event(kind=kind, step=step, text=text, data=data)


# -- the observer must not be able to break a run --------------------------


def test_a_failing_observer_does_not_propagate():
    """Watching is not supposed to change the outcome."""

    def broken(_event):
        raise RuntimeError("the printer is broken")

    fan_out(broken)(event())  # must not raise


def test_a_failing_observer_does_not_stop_the_others():
    seen = []
    fan_out(lambda e: (_ for _ in ()).throw(ValueError("boom")), seen.append)(event())

    assert len(seen) == 1


def test_fan_out_skips_missing_observers():
    seen = []
    fan_out(None, seen.append, None)(event())

    assert len(seen) == 1


def test_the_agent_survives_a_broken_observer(broker, workspace):
    """End to end: a bad observer must not abort a task that is going fine."""
    from minos.agent import Agent, AgentLimits
    from minos.planner.scripted import ScriptedPlanner
    from minos.router import Router
    from minos.tiers.l1_system import FilesystemAdapter
    from minos.types import ActionRequest

    target = workspace / "a.txt"
    agent = Agent(
        planner=ScriptedPlanner(
            [
                ActionRequest(
                    goal_id="g",
                    intent="write",
                    operation="fs.write",
                    params={"path": str(target), "content": "hello"},
                )
            ]
        ),
        router=Router(adapters=(FilesystemAdapter(),)),
        broker=broker,
        limits=AgentLimits(max_steps=3),
        observer=lambda e: (_ for _ in ()).throw(RuntimeError("observer is broken")),
    )

    trajectory = agent.run("write a file")

    assert target.read_text() == "hello"
    assert trajectory.outcomes[0].status == "ok"


# -- what the console shows ------------------------------------------------


def printed(events, **kwargs):
    stream = io.StringIO()
    printer = ConsolePrinter(stream=stream, **kwargs)
    for item in events:
        printer(item)
    return stream.getvalue()


def test_a_plan_shows_its_parameters():
    text = printed([event(params={"path": "/data/a.txt"})])

    assert "fs.write" in text
    assert "/data/a.txt" in text


def test_code_is_shown_line_by_line():
    """The script a model wrote is the most useful thing on the screen."""
    text = printed([event(params={"code": "import os\nprint('hi')"})])

    assert "| import os" in text
    assert "| print('hi')" in text


def test_code_can_be_hidden():
    text = printed([event(params={"code": "secret_source()"})], show_code=False)

    assert "secret_source" in text  # still summarised
    assert "| secret_source()" not in text


def test_thinking_is_shown_and_can_be_hidden():
    thinking = [event(kind="thinking", text="I should read the file first")]

    assert "read the file first" in printed(thinking)
    assert "read the file first" not in printed(thinking, show_thinking=False)


def test_a_denial_is_obvious():
    text = printed([event(kind="verdict", text="outside scope", verdict="deny")])

    assert "DENIED" in text


def test_long_thinking_is_truncated():
    """A hostile model must not be able to flood the terminal."""
    text = printed([event(kind="thinking", text="x" * 50_000)])

    assert len(text) < 5_000
    assert "truncated" in text or "+" in text


def test_non_ascii_does_not_crash_the_trace():
    """A plain Windows console is cp1252 and raises on anything else."""
    text = printed([event(kind="thinking", text="思考中 — naïve café ✓")])

    assert text  # replaced, not raised


# -- the transcript --------------------------------------------------------


def test_a_session_is_written_as_jsonl(tmp_path):
    recorder = SessionRecorder(state=tmp_path, goal="do a thing")
    recorder(event(kind="plan", text="fs.write"))
    recorder(event(kind="result", text="", status="ok"))

    lines = [json.loads(line) for line in recorder.path.read_text().splitlines()]

    assert lines[0]["goal"] == "do a thing"
    assert [entry.get("kind") for entry in lines] == ["session", "plan", "result"]


def test_the_transcript_is_not_the_audit_log(tmp_path):
    """Different guarantees, different files. Conflating them weakens the chain."""
    recorder = SessionRecorder(state=tmp_path, goal="g")
    recorder(event())

    assert "sessions" in str(recorder.path)
    assert recorder.path.suffix == ".jsonl"
    assert not (tmp_path / "audit.jsonl").exists()


def test_transcripts_are_pruned(tmp_path):
    """Unbounded debugging output on a laptop is a slow leak."""
    directory = tmp_path / "sessions"
    directory.mkdir()
    for index in range(25):
        (directory / f"old-{index:02d}.jsonl").write_text("{}\n")

    removed = SessionRecorder(state=tmp_path).prune(keep=10)

    assert removed == 15
    assert len(list(directory.glob("*.jsonl"))) == 10


def test_pruning_an_empty_directory_is_fine(tmp_path):
    assert SessionRecorder(state=tmp_path).prune() == 0


def test_events_serialise_without_exploding_on_odd_values(tmp_path):
    """Params come from a model. Anything can be in there."""
    recorder = SessionRecorder(state=tmp_path)
    recorder(event(params={"path": object()}))

    assert recorder.path.read_text().count("\n") == 2


# -- the agent emits the right shape --------------------------------------


def test_the_agent_emits_a_full_step(broker, workspace):
    from minos.agent import Agent, AgentLimits
    from minos.planner.scripted import ScriptedPlanner
    from minos.router import Router
    from minos.tiers.l1_system import FilesystemAdapter
    from minos.types import ActionRequest

    seen: list[Event] = []
    target = workspace / "a.txt"
    Agent(
        planner=ScriptedPlanner(
            [
                ActionRequest(
                    goal_id="g",
                    intent="write",
                    operation="fs.write",
                    params={"path": str(target), "content": "x"},
                )
            ]
        ),
        router=Router(adapters=(FilesystemAdapter(),)),
        broker=broker,
        limits=AgentLimits(max_steps=3),
        observer=seen.append,
    ).run("write it")

    kinds = [e.kind for e in seen]
    assert kinds[:4] == ["plan", "route", "verdict", "result"]
    assert kinds[-1] == "finish"


def test_an_unroutable_step_is_reported(broker, workspace):
    from minos.agent import Agent, AgentLimits
    from minos.planner.scripted import ScriptedPlanner
    from minos.router import Router
    from minos.types import ActionRequest

    seen: list[Event] = []
    Agent(
        planner=ScriptedPlanner(
            [ActionRequest(goal_id="g", intent="?", operation="fs.write", params={"path": "/x"})]
        ),
        router=Router(adapters=()),
        broker=broker,
        limits=AgentLimits(max_steps=2),
        observer=seen.append,
    ).run("go")

    assert any(e.data.get("status") == "unroutable" for e in seen)


@pytest.mark.parametrize("kind", ["thinking", "plan", "route", "verdict", "result", "finish"])
def test_every_kind_has_a_printer(kind):
    """An event nobody renders is an event nobody sees."""
    assert hasattr(ConsolePrinter(), f"_on_{kind}")
