"""Reading recorded runs back, and resuming one.

A resumed run is a fresh conversation primed with what the earlier one did --
never the old conversation replayed, and never the old scopes. These pin both.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minos.__main__ import main
from minos.trace import find_session, list_sessions, load_session, resume_context


def _write(state: Path, name: str, events: list[dict], torn: bool = False) -> Path:
    directory = state / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.jsonl"
    text = "\n".join(json.dumps(e) for e in events) + "\n"
    if torn:
        text += '{"kind": "plan", "text": "fs.wr'  # died mid-write
    path.write_text(text, encoding="utf-8")
    return path


def _events(session_id: str, goal: str, *, finished: bool) -> list[dict]:
    events = [
        {"kind": "session", "goal": goal, "id": session_id},
        {"kind": "plan", "step": 1, "text": "fs.list", "data": {"params": {"path": "/ws"}}},
        {"kind": "result", "step": 1, "text": "verified", "data": {"status": "ok"}},
        {"kind": "plan", "step": 2, "text": "fs.write", "data": {"params": {"path": "/ws/a"}}},
        {
            "kind": "result",
            "step": 2,
            "text": "outside the granted scopes",
            "data": {"status": "denied"},
        },
    ]
    if finished:
        events.append({"kind": "finish", "step": 3, "text": "done", "data": {"succeeded": True}})
    return events


def test_a_transcript_reads_back_as_steps(tmp_path):
    path = _write(tmp_path, "20260101-000000-aaa111", _events("aaa111", "tidy up", finished=True))
    session = load_session(path)

    assert session.goal == "tidy up"
    assert [s.operation for s in session.steps] == ["fs.list", "fs.write"]
    assert [s.status for s in session.steps] == ["ok", "denied"]
    assert session.finished and session.succeeded


def test_a_torn_transcript_still_reads(tmp_path):
    path = _write(
        tmp_path, "20260101-000000-bbb222", _events("bbb222", "g", finished=False), torn=True
    )
    session = load_session(path)
    assert session.interrupted
    assert len(session.steps) == 2


def test_newest_first_and_prefix_lookup(tmp_path):
    _write(tmp_path, "20260101-000000-old111", _events("old111", "first", finished=True))
    _write(tmp_path, "20260102-000000-new222", _events("new222", "second", finished=False))

    assert [s.session_id for s in list_sessions(tmp_path)] == ["new222", "old111"]
    assert find_session(tmp_path).goal == "second"
    assert find_session(tmp_path, "old").goal == "first"


def test_an_ambiguous_prefix_is_refused(tmp_path):
    _write(tmp_path, "20260101-000000-abc111", _events("abc111", "a", finished=True))
    _write(tmp_path, "20260102-000000-abc222", _events("abc222", "b", finished=True))
    with pytest.raises(LookupError, match="matches 2 sessions"):
        find_session(tmp_path, "abc")


def test_no_sessions_is_said(tmp_path):
    with pytest.raises(LookupError, match="no recorded sessions"):
        find_session(tmp_path)


def test_the_resume_summary_says_what_happened_and_warns(tmp_path):
    path = _write(tmp_path, "20260101-000000-ccc333", _events("ccc333", "g", finished=False))
    text = resume_context(load_session(path))

    assert "was interrupted" in text
    assert "fs.list" in text and "-> ok" in text
    assert "-> denied -- outside the granted scopes" in text
    assert "read before you rely on anything" in text
    assert "do not redo a step that already succeeded" in text


def test_a_long_session_is_summarised_to_its_tail(tmp_path):
    events = [{"kind": "session", "goal": "g", "id": "ddd444"}]
    for i in range(100):
        events.append({"kind": "plan", "text": f"op.{i}", "data": {"params": {}}})
        events.append({"kind": "result", "text": "", "data": {"status": "ok"}})
    text = resume_context(load_session(_write(tmp_path, "20260101-000000-ddd444", events)))
    assert "60 earlier steps not shown" in text
    assert "op.99" in text and "op.10 " not in text


# -- the CLI -----------------------------------------------------------------


def _scripted(monkeypatch, seen: dict) -> None:
    import minos.__main__ as cli
    from minos.planner.base import Done

    class Recording:
        def next_action(self, goal, observations, scopes):
            seen["goal"] = goal
            seen["scopes"] = [str(s) for s in scopes]
            return Done(summary="nothing left", succeeded=True)

    monkeypatch.setattr(cli, "_planner", lambda args, ops: Recording())


def test_resume_continues_the_goal_with_its_history(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write(state, "20260101-000000-eee555", _events("eee555", "tidy the folder", finished=False))
    seen: dict = {}
    _scripted(monkeypatch, seen)

    code = main(["run", "--resume", "-w", str(workspace), "--state", str(state)])

    assert code == 0
    assert seen["goal"].startswith("tidy the folder")
    assert "continues an earlier run (session eee555)" in seen["goal"]
    assert "resuming  : session eee555" in capsys.readouterr().out


def test_resume_never_inherits_scopes(tmp_path, monkeypatch):
    """The old run may have had --allow-write; this one was not given it."""
    state = tmp_path / "state"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write(state, "20260101-000000-fff666", _events("fff666", "g", finished=False))
    seen: dict = {}
    _scripted(monkeypatch, seen)

    main(["run", "--resume", "fff", "-w", str(workspace), "--state", str(state)])

    assert not any(s.startswith("fs.write") for s in seen["scopes"])


def test_a_new_goal_with_resume_keeps_the_history(tmp_path, monkeypatch):
    state = tmp_path / "state"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write(state, "20260101-000000-ggg777", _events("ggg777", "old goal", finished=True))
    seen: dict = {}
    _scripted(monkeypatch, seen)

    main(["run", "now do B", "--resume", "-w", str(workspace), "--state", str(state)])

    assert seen["goal"].startswith("now do B")
    assert "session ggg777" in seen["goal"]


def test_the_resumed_run_records_where_it_came_from(tmp_path, monkeypatch):
    state = tmp_path / "state"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write(state, "20260101-000000-hhh888", _events("hhh888", "g", finished=False))
    _scripted(monkeypatch, {})

    main(["run", "--resume", "-w", str(workspace), "--state", str(state)])

    newest = list_sessions(state)[0]
    header = json.loads(newest.path.read_text(encoding="utf-8").splitlines()[0])
    assert header["resumed_from"] == "hhh888"


def test_run_with_neither_goal_nor_resume_is_an_error(tmp_path, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert main(["run", "-w", str(workspace), "--state", str(tmp_path / "s")]) == 2
    assert "give a goal, or --resume" in capsys.readouterr().err


def test_resume_with_nothing_recorded_is_an_error(tmp_path, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert main(["run", "--resume", "-w", str(workspace), "--state", str(tmp_path / "s")]) == 2
    assert "no recorded sessions" in capsys.readouterr().err


def test_sessions_lists_outcomes(tmp_path, capsys):
    _write(tmp_path, "20260101-000000-iii999", _events("iii999", "the goal", finished=False))
    assert main(["sessions", "--state", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "iii999" in out and "interrupted" in out and "the goal" in out
