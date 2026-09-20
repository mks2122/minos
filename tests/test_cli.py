"""The command line.

The entry point people actually reach for. Its job is to make the safe thing
the default: read-only scopes unless you typed otherwise, and prompts you have
to answer.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from minos.__main__ import build_parser, main


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    with open(ws / "sales.csv", "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows([["Quarter", "Revenue"], ["Q3", "41800"]])
    (ws / "notes.txt").write_text("meeting notes", encoding="utf-8")
    return ws


# -- parser ----------------------------------------------------------------


def test_every_subcommand_is_reachable():
    parser = build_parser()
    for command in ("demo", "run", "audit", "index", "recall", "skills", "eval"):
        assert parser.parse_args([command, *_min_args(command)]).command == command


def _min_args(command: str) -> list[str]:
    return {
        "run": ["a goal"],
        "index": ["."],
        "recall": ["a phrase"],
    }.get(command, [])


def test_a_subcommand_is_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_scopes_are_read_only_by_default():
    """Granting write should be a thing you typed."""
    args = build_parser().parse_args(["run", "goal"])
    assert not args.allow_write
    assert not args.allow_delete
    assert not args.yes
    assert not args.dry_run


def test_write_and_delete_are_opt_in():
    args = build_parser().parse_args(["run", "goal", "--allow-write", "--allow-delete"])
    assert args.allow_write
    assert args.allow_delete


# -- commands that need no model ------------------------------------------


def test_demo_runs(capsys):
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "byte-identical: True" in out
    assert "chain intact  : True" in out


def test_eval_runs(capsys):
    assert main(["eval", "--only", "read."]) == 0
    assert "success" in capsys.readouterr().out


def test_index_then_recall(workspace, tmp_path, capsys):
    state = str(tmp_path / "state")
    assert main(["index", str(workspace), "--state", state]) == 0
    assert "indexed 2 files" in capsys.readouterr().out

    assert main(["recall", "the spreadsheet", "--state", state]) == 0
    out = capsys.readouterr().out
    assert "sales.csv" in out
    assert "Because:" in out


def test_recall_without_an_index_explains_itself(tmp_path, capsys):
    assert main(["recall", "anything", "--state", str(tmp_path / "nope")]) == 2
    assert "Run `minos index" in capsys.readouterr().err


def test_audit_verifies_a_chain(workspace, tmp_path, capsys):
    from minos.audit import AuditLog
    from minos.broker import Broker
    from minos.checkpoint import FileCheckpointStore
    from minos.router import Router
    from minos.scopes import ScopeSet
    from minos.tiers.l1_system import FilesystemAdapter
    from minos.types import ActionRequest

    log = tmp_path / "audit.jsonl"
    broker = Broker(
        scopes=ScopeSet.parse([f"fs.read:{workspace}/**"]),
        audit=AuditLog(log),
        store=FileCheckpointStore(tmp_path / "cp"),
    )
    routed = Router(adapters=(FilesystemAdapter(),)).route(
        ActionRequest(
            goal_id="t",
            intent="read",
            operation="fs.read",
            params={"path": str(workspace / "notes.txt")},
        )
    )
    broker.submit(routed.invocation, routed.execute)

    assert main(["audit", str(log)]) == 0
    out = capsys.readouterr().out
    assert "chain intact : yes" in out
    assert "fs.read" in out


def test_audit_reports_a_broken_chain(tmp_path, capsys):
    log = tmp_path / "audit.jsonl"
    log.write_text(
        '{"prev_hash":"' + "0" * 64 + '","hash":"deadbeef","seq":1,"ts":"now",'
        '"status":"ok","invocation":{"tier":"L1","adapter":"x","tier_reason":"y",'
        '"request":{"goal_id":"g","intent":"i","operation":"fs.read","params":{},'
        '"tier_hint":null},"contract":{}},"decision":{"verdict":"allow",'
        '"rationale":"r","matched_scopes":[],"denied_by":null},'
        '"checkpoint_id":null,"observed":null,"reversal":null}\n',
        encoding="utf-8",
    )
    assert main(["audit", str(log)]) == 1
    assert "chain intact : NO" in capsys.readouterr().out


def test_audit_on_a_missing_log(tmp_path, capsys):
    assert main(["audit", str(tmp_path / "absent.jsonl")]) == 2
    assert "no audit log" in capsys.readouterr().err


def test_skills_on_an_empty_store(tmp_path, capsys):
    assert main(["skills", "--state", str(tmp_path / "state")]) == 0
    assert "no skills stored yet" in capsys.readouterr().out


# -- run ------------------------------------------------------------------


def test_run_rejects_a_bad_workspace(tmp_path, capsys):
    assert main(["run", "goal", "-w", str(tmp_path / "nonexistent")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_run_rejects_an_unknown_planner(workspace, capsys):
    """argparse rejects it and lists the valid choices, which beats a custom error."""
    with pytest.raises(SystemExit) as exit_info:
        main(["run", "goal", "-w", str(workspace), "--planner", "psychic"])
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err
    assert "claude" in err
    assert "local" in err


def test_local_planner_is_selectable(workspace):
    from minos.__main__ import _planner
    from minos.planner.local import LocalPlanner

    args = build_parser().parse_args(
        [
            "run",
            "goal",
            "--planner",
            "local",
            "--base-url",
            "http://localhost:1234/v1",
            "--model",
            "qwen3:8b",
        ]
    )
    planner = _planner(args, ("fs.read",))
    assert isinstance(planner, LocalPlanner)
    assert planner.base_url == "http://localhost:1234/v1"
    assert planner.model == "qwen3:8b"


def test_local_planner_does_not_inherit_the_claude_default_model():
    """--planner local without --model must not ask Ollama for claude-opus-5."""
    from minos.__main__ import _planner

    args = build_parser().parse_args(["run", "goal", "--planner", "local"])
    assert _planner(args, ("fs.read",)).model == "qwen3:8b"


def test_run_prints_the_scopes_it_will_use(workspace, tmp_path, capsys, monkeypatch):
    """You should be able to see what you granted before anything happens."""
    import minos.__main__ as cli

    class Refuses:
        def next_action(self, goal, observations, scopes):
            raise RuntimeError("no model here")

    monkeypatch.setattr(cli, "_planner", lambda args, ops: Refuses())

    code = main(
        [
            "run",
            "do a thing",
            "-w",
            str(workspace),
            "--allow-write",
            "--state",
            str(tmp_path / "state"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "fs.read:" in captured.out
    assert "fs.write:" in captured.out
    assert "fs.delete:" not in captured.out
    assert "the planner failed" in captured.err


def test_run_warns_loudly_about_yes(workspace, tmp_path, capsys, monkeypatch):
    import minos.__main__ as cli

    class Stops:
        def next_action(self, goal, observations, scopes):
            from minos.planner.base import Done

            return Done(summary="nothing to do", succeeded=True)

    monkeypatch.setattr(cli, "_planner", lambda args, ops: Stops())

    main(["run", "goal", "-w", str(workspace), "--yes", "--state", str(tmp_path / "s")])
    assert "approvals are auto-granted" in capsys.readouterr().out


def test_run_end_to_end_with_a_scripted_planner(workspace, tmp_path, capsys, monkeypatch):
    import minos.__main__ as cli
    from minos.planner.base import Done
    from minos.planner.scripted import ScriptedPlanner
    from minos.types import ActionRequest

    book = workspace / "sales.csv"
    monkeypatch.setattr(
        cli,
        "_planner",
        lambda args, ops: ScriptedPlanner(
            [
                ActionRequest(
                    goal_id="cli",
                    intent="set Q3",
                    operation="sheet.set_cell",
                    params={"path": str(book), "cell": "B2", "value": "48200"},
                ),
                Done(summary="updated Q3", succeeded=True),
            ]
        ),
    )

    code = main(
        [
            "run",
            "set Q3 to 48200",
            "-w",
            str(workspace),
            "--allow-write",
            "--state",
            str(tmp_path / "state"),
        ]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "SUCCEEDED" in out
    assert "[L2] sheet.set_cell" in out
    with open(book, newline="", encoding="utf-8") as fh:
        assert list(csv.reader(fh))[1][1] == "48200"


def test_run_dry_run_changes_nothing(workspace, tmp_path, capsys, monkeypatch):
    import minos.__main__ as cli
    from minos.planner.base import Done
    from minos.planner.scripted import ScriptedPlanner
    from minos.types import ActionRequest

    book = workspace / "sales.csv"
    before = book.read_bytes()
    monkeypatch.setattr(
        cli,
        "_planner",
        lambda args, ops: ScriptedPlanner(
            [
                ActionRequest(
                    goal_id="cli",
                    intent="set Q3",
                    operation="sheet.set_cell",
                    params={"path": str(book), "cell": "B2", "value": "48200"},
                ),
                Done(summary="would have updated Q3", succeeded=True),
            ]
        ),
    )

    main(
        [
            "run",
            "set Q3",
            "-w",
            str(workspace),
            "--allow-write",
            "--dry-run",
            "--state",
            str(tmp_path / "state"),
        ]
    )
    assert "DRY RUN" in capsys.readouterr().out
    assert book.read_bytes() == before


def test_run_denies_outside_the_workspace(workspace, tmp_path, capsys, monkeypatch):
    import minos.__main__ as cli
    from minos.planner.base import Done
    from minos.planner.scripted import ScriptedPlanner
    from minos.types import ActionRequest

    outside = tmp_path / "secrets.txt"
    outside.write_text("sensitive", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "_planner",
        lambda args, ops: ScriptedPlanner(
            [
                ActionRequest(
                    goal_id="cli",
                    intent="peek",
                    operation="fs.read",
                    params={"path": str(outside)},
                ),
                Done(summary="could not", succeeded=False),
            ]
        ),
    )

    code = main(["run", "peek", "-w", str(workspace), "--state", str(tmp_path / "state")])
    out = capsys.readouterr().out
    assert code == 1
    assert "DENY" in out
    assert "outside the granted scopes" in out
