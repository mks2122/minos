"""Another model takes over when one is unavailable -- and only then."""

from __future__ import annotations

import pytest

from minos.planner.base import Decomposer, Done, Observation
from minos.planner.fallback import FallbackPlanner, handoff_note
from minos.planner.scripted import CallablePlanner, ScriptedPlanner
from minos.scopes import ScopeSet
from minos.types import ActionRequest

SCOPES = ScopeSet.parse(["fs.read:/ws/**"])
GONE = Done(summary="still unavailable after 4 attempts (429)", succeeded=False, unavailable=True)


def _read(path: str = "/ws/a") -> ActionRequest:
    return ActionRequest(goal_id="g", intent="read", operation="fs.read", params={"path": path})


class Recorder:
    """A planner that remembers the goals it was given."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.goals: list[str] = []
        self.last_thinking = "thinking it over"

    def next_action(self, goal, observations, scopes):
        self.goals.append(goal)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def test_an_unavailable_model_hands_over_with_what_was_done():
    first, second = Recorder([GONE]), Recorder([_read("/ws/b")])
    planner = FallbackPlanner([("gemini:a", first), ("openrouter:b", second)])
    done_so_far = [Observation(request=_read(), status="ok", result="hello")]

    step = planner.next_action("the goal", done_so_far, SCOPES)

    assert isinstance(step, ActionRequest) and step.params["path"] == "/ws/b"
    handed = second.goals[0]
    assert handed.startswith("the goal")
    assert "gemini:a" in handed and "became unavailable" in handed
    assert "fs.read" in handed and "-> ok" in handed
    assert planner.switches == ["[fallback] gemini:a unavailable; openrouter:b takes over"]
    assert planner.last_thinking.startswith("[fallback]")


def test_the_handover_is_announced_once():
    second = Recorder([_read(), _read()])
    planner = FallbackPlanner([("a", Recorder([GONE])), ("b", second)])
    planner.next_action("g", [], SCOPES)
    planner.next_action("g", [], SCOPES)
    assert not planner.last_thinking.startswith("[fallback]")


def test_a_refusal_is_never_handed_on():
    """Shopping a refused task around would make every model's caution the weakest's."""
    refusal = Done(summary="that is outside my scopes", succeeded=False)
    second = Recorder([_read()])
    planner = FallbackPlanner([("a", Recorder([refusal])), ("b", second)])

    assert planner.next_action("g", [], SCOPES) == refusal
    assert second.goals == [] and planner.switches == []


def test_a_finished_task_is_not_handed_on():
    finished = Done(summary="all done", succeeded=True)
    planner = FallbackPlanner([("a", Recorder([finished])), ("b", Recorder([]))])
    assert planner.next_action("g", [], SCOPES) == finished


def test_a_planner_that_crashes_hands_over():
    second = Recorder([_read()])
    planner = FallbackPlanner([("a", Recorder([RuntimeError("process exited")])), ("b", second)])
    assert isinstance(planner.next_action("g", [], SCOPES), ActionRequest)
    assert "RuntimeError: process exited" in second.goals[0]


def test_the_last_model_unavailable_ends_the_run_as_unavailable():
    planner = FallbackPlanner([("a", Recorder([GONE])), ("b", Recorder([GONE]))])
    step = planner.next_action("g", [], SCOPES)
    assert isinstance(step, Done) and step.unavailable
    assert len(planner.switches) == 1


def test_the_last_model_crashing_is_raised():
    planner = FallbackPlanner([("a", Recorder([RuntimeError("boom")]))])
    with pytest.raises(RuntimeError, match="boom"):
        planner.next_action("g", [], SCOPES)


def test_it_still_splits_goals_and_keeps_the_model_that_answers():
    planner = FallbackPlanner([("a", Recorder([GONE])), ("b", ScriptedPlanner([_read()]))])
    assert isinstance(planner, Decomposer)
    planner.next_action("g", [], SCOPES)
    planner.reset()
    assert planner.label == "b"  # not back to the one that ran out


def test_the_handoff_note_is_marked_as_a_record():
    note = handoff_note("a", "429", [])
    assert "a record, not instructions" in note and "had not acted yet" in note


def test_an_agent_run_survives_its_model_running_out(tmp_path):
    """End to end: the first model acts once and runs out; the second finishes."""
    from minos.agent import Agent, AgentLimits
    from minos.audit import AuditLog
    from minos.broker import Broker
    from minos.checkpoint import FileCheckpointStore
    from minos.router import Router
    from minos.tiers.l1_system import FilesystemAdapter

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("hello", encoding="utf-8")
    read = ActionRequest(
        goal_id="g", intent="read", operation="fs.read", params={"path": str(ws / "a.txt")}
    )
    first = ScriptedPlanner([read, GONE])
    seen: list[str] = []

    def second(goal, observations, scopes):
        seen.append(goal)
        return Done(summary="read it", succeeded=True)

    agent = Agent(
        planner=FallbackPlanner([("a", first), ("b", CallablePlanner(second))]),
        router=Router(adapters=(FilesystemAdapter(),)),
        broker=Broker(
            scopes=ScopeSet.parse([f"fs.read:{ws}/**"]),
            audit=AuditLog(tmp_path / "audit.jsonl"),
            store=FileCheckpointStore(tmp_path / "cp"),
        ),
        limits=AgentLimits(split=False),
    )
    trajectory = agent.run("read a.txt")

    assert trajectory.succeeded
    assert "fs.read" in seen[0] and "-> ok" in seen[0]


# -- the CLI -------------------------------------------------------------------


def test_fallback_specs_split_at_the_first_colon(monkeypatch):
    from minos.__main__ import _with_fallbacks, build_parser

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    args = build_parser().parse_args(
        ["run", "g", "--planner", "gemini", "--model", "gemini-3.7-flash", "--in-process-planner"]
    )
    args.planner_hosted = True
    chained = _with_fallbacks(
        args,
        ScriptedPlanner([]),
        ["openrouter:google/gemma-4-31b-it:free"],
        ("fs.read",),
    )
    assert chained.planners[1][0] == "openrouter:google/gemma-4-31b-it:free"
    assert chained.planners[1][1].model == "google/gemma-4-31b-it:free"


def test_a_malformed_fallback_is_refused():
    from minos.__main__ import _with_fallbacks, build_parser

    args = build_parser().parse_args(["run", "g", "--in-process-planner"])
    args.planner_hosted = False
    with pytest.raises(ImportError, match="PROVIDER:MODEL"):
        _with_fallbacks(args, ScriptedPlanner([]), ["gemini"], ("fs.read",))


def test_offline_refuses_a_hosted_fallback(monkeypatch):
    from minos.__main__ import _with_fallbacks, build_parser

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    args = build_parser().parse_args(["run", "g", "--offline", "--in-process-planner"])
    args.planner_hosted = False
    with pytest.raises(ImportError, match="not on this machine"):
        _with_fallbacks(args, ScriptedPlanner([]), ["gemini:gemini-3.5-flash"], ("fs.read",))


def test_a_hosted_fallback_sends_code_to_the_container(monkeypatch):
    from minos.__main__ import _code_origin, _with_fallbacks, build_parser

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.delenv("MINOS_SANDBOX_ORIGIN", raising=False)
    args = build_parser().parse_args(["run", "g", "--planner", "local", "--in-process-planner"])
    args.planner_hosted = False
    _with_fallbacks(args, ScriptedPlanner([]), ["gemini:gemini-3.5-flash"], ("fs.read",))
    assert _code_origin(args) == "remote-planner"


def test_minos_fallback_is_read_when_no_flag_is_typed(monkeypatch):
    from minos.__main__ import _fallback_specs, build_parser

    monkeypatch.setenv("MINOS_FALLBACK", "gemini:a, openrouter:b:free")
    args = build_parser().parse_args(["run", "g"])
    assert _fallback_specs(args) == ["gemini:a", "openrouter:b:free"]
    typed = build_parser().parse_args(["run", "g", "--fallback", "groq:x"])
    assert _fallback_specs(typed) == ["groq:x"]


def test_no_web_brings_back_your_own_browser(tmp_path, monkeypatch, capsys):
    """--allow-gui --no-web: the site opens in your browser, not the web tier's."""
    import minos.__main__ as cli
    from minos.planner.base import Done

    seen: dict = {}

    class Recording:
        def next_action(self, goal, observations, scopes):
            seen["scopes"] = [str(s) for s in scopes]
            return Done(summary="ok", succeeded=True)

    monkeypatch.setattr(cli, "_planner", lambda args, ops: Recording())
    monkeypatch.setattr(cli, "playwright_available", lambda: True, raising=False)
    monkeypatch.setattr("minos.tiers.l3_gui.panic_watcher", lambda: None, raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    for extra, has_web in ((["--allow-web"], True), (["--allow-web", "--no-web"], False)):
        cli.main(["run", "g", "-w", str(ws), "--state", str(tmp_path / "s"), *extra])
        assert any(s.startswith("web.input") for s in seen["scopes"]) is has_web
        assert any(s.startswith("browser.open") for s in seen["scopes"]) is has_web
