"""The planner in its own process.

What these pin down: steps cross the boundary as validated JSON and nothing
else; the child really is confined where the platform allows it; a child that
dies, hangs or talks nonsense becomes an error rather than an action; and a
real planner still works through the pipe, network included.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from minos.planner.base import Done, Observation
from minos.planner.isolated import (
    IsolatedPlanner,
    PlannerProcessError,
    _decode_observation,
    _decode_step,
    _encode_observation,
    _spec_of,
)
from minos.planner.local import LocalPlanner
from minos.planner.scripted import CallablePlanner, ScriptedPlanner
from minos.scopes import ScopeSet
from minos.types import ActionRequest

SCOPES = ScopeSet.parse(["fs.read:/ws/**"])


# -- the wire format -----------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "not an object",
        {"kind": "action", "operation": "fs.read", "params": "path=/etc/passwd"},
        {"kind": "action", "operation": "__import__('os')", "params": {}},
        {"kind": "action", "operation": "FS.READ", "params": {}},
        {"kind": "action", "operation": "fs.read", "params": {1: "x"}},
        {"kind": "done", "summary": "ok", "succeeded": "yes"},
        {"kind": "execute", "code": "rm -rf /"},
    ],
)
def test_anything_but_a_well_formed_step_is_refused(bad):
    with pytest.raises(PlannerProcessError):
        _decode_step(bad)


def test_a_well_formed_action_decodes():
    step = _decode_step(
        {"kind": "action", "operation": "sheet.set_cell", "params": {"cell": "B4"}, "intent": "x"}
    )
    assert isinstance(step, ActionRequest) and step.params == {"cell": "B4"}


def test_overlong_text_is_cut_not_trusted():
    step = _decode_step({"kind": "done", "summary": "s" * 50_000, "succeeded": True})
    assert isinstance(step, Done) and len(step.summary) == 10_000


def test_observations_survive_the_trip():
    observation = Observation(
        request=ActionRequest(goal_id="g", intent="i", operation="fs.read", params={"path": "/a"}),
        status="ok",
        result={"text": "hello", "lines": [1, 2]},
        detail="verified",
    )
    back = _decode_observation(json.loads(json.dumps(_encode_observation(observation))))
    assert back.request.operation == "fs.read"
    assert back.result == {"text": "hello", "lines": [1, 2]}
    assert back.detail == "verified"


def test_only_known_planners_may_be_isolated():
    with pytest.raises(TypeError):
        _spec_of(CallablePlanner(lambda *a: Done(summary="", succeeded=True)))


def test_a_live_client_never_travels():
    spec = _spec_of(LocalPlanner(operations=("fs.read",), api_key="sk-secret"))
    assert "client" not in spec["kwargs"]
    assert spec["target"] == "minos.planner.local:LocalPlanner"


# -- the child, end to end -----------------------------------------------------


def test_a_scripted_planner_runs_in_the_child():
    inner = ScriptedPlanner(
        [
            ActionRequest(goal_id="g", intent="look", operation="fs.list", params={"path": "/ws"}),
            Done(summary="all done", succeeded=True),
        ]
    )
    with IsolatedPlanner.of(inner, timeout=60) as planner:
        assert planner.pid and planner.pid != os.getpid()
        first = planner.next_action("goal", [], SCOPES)
        second = planner.next_action("goal", [], SCOPES)
    assert isinstance(first, ActionRequest) and first.operation == "fs.list"
    assert isinstance(second, Done) and second.summary == "all done"


def test_a_dead_child_is_an_error_not_a_step():
    inner = ScriptedPlanner([Done(summary="x", succeeded=True)])
    planner = IsolatedPlanner.of(inner, timeout=30)
    planner._process.kill()
    planner._process.wait(5)
    with pytest.raises(PlannerProcessError):
        planner.next_action("goal", [], SCOPES)
    planner.close()


def test_a_garbled_reply_is_an_error():
    inner = ScriptedPlanner([Done(summary="x", succeeded=True)])
    with IsolatedPlanner.of(inner, timeout=30) as planner:
        # Swallow the real reply and hand the parent nonsense in its place.
        planner._process.stdin.write(
            b'{"op": "next", "goal": "g", "scopes": [], "observations": []}\n'
        )
        planner._process.stdin.flush()
        planner._lines.get(timeout=30)
        planner._lines.put(b"not json at all\n")
        with pytest.raises(PlannerProcessError, match="not JSON"):
            planner._ask({"op": "get", "name": "context_window"})


class _ChatServer(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict]] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append(body)
        reply = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "<think>list it first</think>",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "fs_list", "arguments": '{"path": "/ws"}'},
                            }
                        ],
                    }
                }
            ]
        }
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def test_a_real_planner_works_through_the_pipe_and_the_network():
    """Confined, and still able to reach its model -- the one thing it needs."""
    _ChatServer.seen = []
    server = HTTPServer(("127.0.0.1", 0), _ChatServer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        inner = LocalPlanner(
            operations=("fs.list",),
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            context_tokens=8192,
        )
        with IsolatedPlanner.of(inner, timeout=60) as planner:
            step = planner.next_action("list /ws", [], SCOPES)
            assert isinstance(step, ActionRequest) and step.operation == "fs.list"
            assert planner.last_thinking == "list it first"

            planner.context_window = 4096
            assert planner.context_window == 4096
            assert isinstance(planner.context_warning(4096), str)

            observation = Observation(request=step, status="ok", result=["a.txt", "b.txt"])
            planner.next_action("list /ws", [observation], SCOPES)
    finally:
        server.shutdown()

    second = _ChatServer.seen[1]["messages"]
    assert second[-1]["role"] == "tool" and "a.txt" in second[-1]["content"]


# -- confinement, where the platform has it ------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="Windows token and job")
def test_windows_child_cannot_write_or_spawn():
    from minos.sandbox.winspawn import ConfinedProcess

    probe = Path.home() / "minos_planner_probe.txt"
    probe.unlink(missing_ok=True)
    child = (
        "import json, os, subprocess, sys\n"
        "out = {}\n"
        f"try:\n    open({str(probe)!r}, 'w').write('x'); out['write'] = 'allowed'\n"
        "except OSError:\n    out['write'] = 'refused'\n"
        "try:\n    subprocess.run(['cmd', '/c', 'echo'], capture_output=True)\n"
        "    out['spawn'] = 'allowed'\n"
        "except OSError:\n    out['spawn'] = 'refused'\n"
        "sys.stdin.buffer.readline()\n"
        "sys.stdout.write(json.dumps(out) + '\\n'); sys.stdout.flush()\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    python = getattr(sys, "_base_executable", "") or sys.executable
    process = ConfinedProcess([python, "-c", child], cwd=Path.cwd(), env=env)
    try:
        assert process.integrity_lowered and process.job_object
        process.stdin.write(b"go\n")
        result = json.loads(process.stdout.readline())
    finally:
        process.close()
    assert result == {"write": "refused", "spawn": "refused"}
    assert not probe.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows token and job")
def test_windows_planner_reports_its_confinement():
    with IsolatedPlanner.of(ScriptedPlanner([]), timeout=60) as planner:
        assert "low-integrity" in planner.describe()
        assert "cannot start programs" in planner.describe()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Landlock is Linux")
def test_linux_child_makes_the_filesystem_read_only(tmp_path):
    from minos.sandbox._child_confine import landlock_abi

    if not landlock_abi():
        pytest.skip("this kernel has no Landlock")
    script = (
        "import os\n"
        "os.environ['MINOS_PLANNER_CONFINE'] = '1'\n"
        "from minos.planner.isolated import _confine_self\n"
        "print(_confine_self())\n"
        "try:\n"
        f"    open({str(tmp_path / 'x')!r}, 'w')\n"
        "    print('WRITE ALLOWED')\n"
        "except OSError:\n"
        "    print('write refused')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    ).stdout
    assert "read-only" in out and "write refused" in out


# -- the CLI ---------------------------------------------------------------------


def test_run_isolates_a_model_planner_by_default(monkeypatch):
    import minos.__main__ as cli
    from minos.planner.isolated import IsolatedPlanner as Isolated

    wrapped = cli._isolate(LocalPlanner(operations=("fs.read",)))
    try:
        assert isinstance(wrapped, Isolated)
    finally:
        wrapped.close()


def test_a_scripted_planner_is_left_alone():
    import minos.__main__ as cli

    planner = ScriptedPlanner([])
    assert cli._isolate(planner) is planner


def test_in_process_planner_turns_it_off():
    from minos.__main__ import build_parser

    assert build_parser().parse_args(["run", "g"]).isolate_planner is True
    assert build_parser().parse_args(["run", "g", "--in-process-planner"]).isolate_planner is False


def test_an_isolated_planner_still_splits_goals():
    """The agent splits a goal only for a Decomposer; isolation must not hide that."""
    from minos.planner.base import Decomposer

    with IsolatedPlanner.of(ScriptedPlanner([]), timeout=60) as planner:
        assert isinstance(planner, Decomposer)
        # A scripted planner cannot split; the answer is "do not split", not an error.
        assert planner.decompose("a goal", SCOPES) == []
