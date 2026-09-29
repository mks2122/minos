"""The interactive surface: approvals, grants offered after a denial, undo of
the last goal, the conversation loop, and the live trace.

What is worth pinning is less the wording than the limits: ``[a]lways`` never
covers an irreversible effect, an offered grant is one file inside the
workspace, a grant never reaches the run that asked for it, and nothing a model
wrote can put a control character on the terminal.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import sys
import time
from pathlib import Path

import pytest

from conftest import make_invocation
from minos.approval import LastThinking, SessionApprover, describe, grant_offers, preview
from minos.scopes import ScopeSet
from minos.trace import ConsolePrinter, Event, printable
from minos.types import AdmissionDecision, EffectClass, Grant

ROOT = Path(__file__).resolve().parent.parent

PROMPT = AdmissionDecision(verdict="prompt", rationale="needs a person")


def answers(*replies: str):
    """An ``ask`` that replies in order, then behaves like a closed stdin."""
    queue = list(replies)

    def ask(_prompt: str = "") -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    return ask


# -- the approver ------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "admitted"), [("y", True), ("yes", True), ("n", False), ("", False)]
)
def test_yes_admits_and_anything_else_refuses(reply, admitted, capsys):
    assert SessionApprover(ask=answers(reply))(make_invocation(), PROMPT) is admitted


def test_no_one_to_answer_is_a_refusal(capsys):
    assert SessionApprover(ask=answers())(make_invocation(), PROMPT) is False


def test_always_is_remembered_for_the_same_operation_and_targets(tmp_path, capsys):
    def mkdir(name: str):
        return make_invocation(
            operation="fs.mkdir", targets=(tmp_path / name,), effect_class=EffectClass.REVERSIBLE
        )

    approver = SessionApprover(ask=answers("a"))
    assert approver(mkdir("a"), PROMPT)
    # No reply left: a second prompt would read EOF and refuse.
    assert approver(mkdir("a"), PROMPT)
    assert "approved (always" in capsys.readouterr().out
    assert approver(mkdir("b"), PROMPT) is False


def test_always_is_never_offered_for_an_irreversible_effect(tmp_path, capsys):
    invocation = make_invocation(
        operation="proc.spawn", targets=(tmp_path / "x",), effect_class=EffectClass.IRREVERSIBLE
    )
    approver = SessionApprover(ask=answers("a", "n"))

    assert approver(invocation, PROMPT) is False
    assert not approver.always
    out = capsys.readouterr().out
    assert "[a]lways" not in out
    assert "CANNOT BE UNDONE" in out


def test_show_diffs_against_the_file_as_it_is_now(tmp_path, capsys):
    target = tmp_path / "notes.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")
    invocation = make_invocation(
        targets=(target,), params={"path": str(target), "content": "one\nTWO\n"}
    )

    SessionApprover(ask=answers("s", "n"))(invocation, PROMPT)

    out = capsys.readouterr().out
    assert "-two" in out
    assert "+TWO" in out


def test_show_marks_a_new_file_as_new(tmp_path):
    lines = preview(make_invocation(targets=(tmp_path / "new.txt",), params={"content": "hello"}))
    assert any("new file" in line for line in lines)
    assert any("+ hello" in line for line in lines)


def test_why_shows_the_models_reasoning_and_is_hidden_without_any(capsys):
    SessionApprover(ask=answers("w", "n"), why=lambda: "because the goal said so")(
        make_invocation(), PROMPT
    )
    assert "because the goal said so" in capsys.readouterr().out

    SessionApprover(ask=answers("n"))(make_invocation(), PROMPT)
    assert "[w]hy" not in capsys.readouterr().out


def test_last_thinking_forgets_the_previous_step():
    memo = LastThinking()
    memo(Event(kind="thinking", step=1, text="step one reasoning"))
    assert memo.get() == "step one reasoning"
    memo(Event(kind="waiting", step=2))
    assert memo.get() == ""


def test_a_model_cannot_put_escape_codes_in_an_approval():
    invocation = make_invocation()
    hostile = dataclasses.replace(invocation.request, intent="fine\x1b[2K\x1b[1Ahidden")
    lines = describe(dataclasses.replace(invocation, request=hostile), PROMPT)
    assert not any("\x1b" in line for line in lines)


# -- grants offered after a denial -------------------------------------------


def test_an_offer_is_one_exact_file_inside_the_workspace(workspace):
    offers = grant_offers([Grant("fs.write", str(workspace / "summary.txt"))], workspace)
    assert len(offers) == 1
    assert offers[0].startswith("fs.write:")
    assert "*" not in offers[0]

    scopes = ScopeSet.parse(offers)
    assert scopes.check("fs.write", workspace / "summary.txt")[0]
    assert not scopes.check("fs.write", workspace / "other.txt")[0]


@pytest.mark.parametrize(
    "grant",
    [Grant("proc.spawn", "notepad.exe"), Grant("ui.input", "*"), Grant("code.run", "anything")],
)
def test_authority_that_is_typed_is_never_offered(grant, workspace):
    assert grant_offers([grant], workspace) == []


def test_nothing_outside_the_workspace_is_offered(workspace, tmp_path):
    assert grant_offers([Grant("fs.write", str(tmp_path / "elsewhere.txt"))], workspace) == []
    assert grant_offers([Grant("fs.write", str(workspace / ".." / "x"))], workspace) == []
    assert grant_offers([Grant("fs.delete", str(workspace))], workspace) == []


def test_a_filename_with_glob_characters_is_granted_as_itself(workspace):
    scopes = ScopeSet.parse(
        grant_offers([Grant("fs.write", str(workspace / "q[1].csv"))], workspace)
    )
    assert scopes.check("fs.write", workspace / "q[1].csv")[0]
    assert not scopes.check("fs.write", workspace / "q1.csv")[0]


def test_missing_grants_names_what_no_scope_covers(broker, workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    assert broker.missing_grants(make_invocation(targets=(outside,))) == (
        Grant("fs.write", str(outside)),
    )
    assert broker.missing_grants(make_invocation(targets=(workspace / "a",))) == ()


def test_missing_grants_leaves_out_what_a_deny_rule_refused(broker, workspace):
    from minos.broker import Broker

    secret = workspace / "secret.txt"
    guarded = Broker(
        scopes=ScopeSet.parse([f"fs.write:{workspace}/**", f"!fs.write:{secret}"]),
        audit=broker.audit,
        store=broker.store,
    )
    assert guarded.missing_grants(make_invocation(targets=(secret,))) == ()


# -- the live trace ------------------------------------------------------------


def test_printable_strips_control_characters():
    assert "\x1b" not in printable("a\x1b[31mred\x07")
    assert printable("tab\tstays") == "tab\tstays"


def test_the_trace_never_writes_an_escape_code():
    stream = io.StringIO()
    ConsolePrinter(stream=stream)(Event(kind="thinking", step=1, text="x\x1b]0;pwned\x07y"))
    assert "\x1b" not in stream.getvalue()


def test_no_spinner_when_the_output_is_not_a_terminal():
    stream = io.StringIO()
    printer = ConsolePrinter(stream=stream)
    printer(Event(kind="waiting", step=1))
    assert stream.getvalue() == ""
    assert printer._spin_thread is None


def test_the_spinner_runs_while_waiting_and_stops_on_the_next_event():
    stream = io.StringIO()
    printer = ConsolePrinter(stream=stream, spinner=True)
    printer(Event(kind="waiting", step=3))
    deadline = time.monotonic() + 2
    while "waiting for the model" not in stream.getvalue() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "[3] waiting for the model" in stream.getvalue()

    printer(Event(kind="plan", step=3, text="fs.read"))
    assert printer._spin_thread is None
    assert stream.getvalue().rstrip().endswith("fs.read")


# -- a whole run, driven the way the menu drives it ---------------------------


def _run_args(workspace: Path, state: Path, *extra: str) -> argparse.Namespace:
    from minos.__main__ import build_parser

    return build_parser().parse_args(
        ["run", "write it", "-w", str(workspace), "--state", str(state), "--no-watch", *extra]
    )


def _write_step(target: Path):
    from minos.planner.scripted import ScriptedPlanner
    from minos.types import ActionRequest

    return ScriptedPlanner(
        [
            ActionRequest(
                goal_id="g",
                intent="write the file",
                operation="fs.write",
                params={"path": str(target), "content": "written"},
            )
        ]
    )


def test_a_denied_run_names_the_grant_it_lacked_and_granting_fixes_the_next(tmp_path, workspace):
    from minos.__main__ import execute_run

    state = tmp_path / ".minos"
    target = workspace / "out.txt"

    denied = execute_run(_run_args(workspace, state), planner=_write_step(target))
    assert not target.exists()
    assert [g.capability for g in denied.missing] == ["fs.write"]

    offers = grant_offers(denied.missing, workspace)
    granted = execute_run(
        _run_args(workspace, state), planner=_write_step(target), extra_scopes=offers
    )
    assert target.read_text(encoding="utf-8") == "written"
    assert granted.missing == ()
    assert granted.first_seq >= denied.end_seq


def test_undo_reaches_exactly_what_one_run_did(tmp_path, workspace):
    from minos.__main__ import execute_run
    from minos.audit import AuditLog
    from minos.checkpoint import FileCheckpointStore
    from minos.undo import perform_undo, undoable_between

    state = tmp_path / ".minos"
    first, second = workspace / "first.txt", workspace / "second.txt"
    first.write_text("before", encoding="utf-8")
    second.write_text("before", encoding="utf-8")

    execute_run(_run_args(workspace, state, "--allow-write"), planner=_write_step(first))
    report = execute_run(_run_args(workspace, state, "--allow-write"), planner=_write_step(second))

    audit = AuditLog(state / "audit.jsonl")
    store = FileCheckpointStore(state / "checkpoints")
    actions = undoable_between(audit, store, report.first_seq, report.end_seq)
    assert len(actions) == 1
    assert Path(actions[0].targets[0]).name == "second.txt"

    perform_undo(audit, store, actions[0])
    assert second.read_text(encoding="utf-8") == "before"
    assert first.read_text(encoding="utf-8") == "written"


def test_context_follows_the_goal_to_the_planner(tmp_path, workspace):
    from minos.__main__ import execute_run
    from minos.planner.base import Done
    from minos.planner.scripted import CallablePlanner

    seen: list[str] = []

    def decide(goal, _observations, _scopes):
        seen.append(goal)
        return Done(summary="ok")

    execute_run(
        _run_args(workspace, tmp_path / ".minos"),
        planner=CallablePlanner(decide),
        context="- asked: summarise notes.txt",
    )
    assert seen[0].startswith("write it")
    assert "summarise notes.txt" in seen[0]


# -- the menu and the conversation --------------------------------------------


def _load_menu():
    import importlib.util

    spec = importlib.util.spec_from_file_location("minos_menu_interactive", ROOT / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["minos_menu_interactive"] = module
    spec.loader.exec_module(module)
    return module


menu = _load_menu()


def feed(monkeypatch, *replies: str) -> None:
    queue = list(replies)
    monkeypatch.setattr("builtins.input", lambda _prompt="": queue.pop(0) if queue else "")


def test_a_menu_letter_flips_a_toggle_without_a_detour(monkeypatch, capsys):
    monkeypatch.setattr(menu, "server_available", lambda _url=None: False)
    feed(monkeypatch, "w", "0")
    assert menu.main() == 0
    out = capsys.readouterr().out
    assert "write: ON" in out
    assert "w:write ON" in out  # the banner, redrawn with it


def test_slash_commands_toggle_help_and_leave(monkeypatch, capsys, tmp_path):
    settings = menu.Settings(workspace=tmp_path, state=tmp_path / ".minos")
    feed(monkeypatch, "/help", "/write", "/x", "/nonsense", "/menu")
    menu._conversation(settings, menu.Session())
    out = capsys.readouterr().out
    assert "/undo" in out
    assert settings.allow_write and settings.dry_run
    assert "unknown command '/nonsense'" in out


def test_a_conversation_grants_retries_and_undoes(monkeypatch, capsys, tmp_path, workspace):
    import minos.__main__ as cli

    target = workspace / "summary.txt"
    monkeypatch.setattr(cli, "_planner", lambda _args, _ops: _write_step(target))
    settings = menu.Settings(workspace=workspace, state=tmp_path / ".minos")
    session = menu.Session()

    # goal -> refused, read-only -> grant that one file and retry -> undo it.
    feed(monkeypatch, "write a summary", "y", "/undo", "y", "")
    menu._conversation(settings, session)

    out = capsys.readouterr().out
    assert "It asked for:" in out
    assert len(settings.grants) == 1 and "summary.txt" in settings.grants[0]
    assert "can be put back:  /undo" in out
    assert "restored, verified" in out
    assert not target.exists()


def test_the_conversation_carries_context_forward(monkeypatch, tmp_path, workspace):
    import minos.__main__ as cli
    from minos.planner.base import Done
    from minos.planner.scripted import CallablePlanner

    goals: list[str] = []

    def planner(_args, _ops):
        def decide(goal, _observations, _scopes):
            goals.append(goal)
            return Done(summary=f"did {len(goals)}")

        return CallablePlanner(decide)

    monkeypatch.setattr(cli, "_planner", planner)
    settings = menu.Settings(workspace=workspace, state=tmp_path / ".minos")
    feed(monkeypatch, "first goal", "second goal", "")
    menu._conversation(settings, menu.Session())

    assert "Earlier in this session" not in goals[0]
    assert goals[1].startswith("second goal")
    assert "first goal" in goals[1] and "did 1" in goals[1]


def test_the_new_files_are_cp1252_safe():
    for path in (ROOT / "main.py", ROOT / "src" / "minos" / "approval.py"):
        path.read_text(encoding="utf-8").encode("cp1252")
