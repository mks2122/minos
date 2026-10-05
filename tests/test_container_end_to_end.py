"""The container backend, run for real.

Skipped unless a container engine answers -- CI has none, so these run on a
developer machine with Docker or Podman started. They are the end-to-end
evidence for the one case the origin policy sends to a container: code whose
author is not on this machine.

Each test runs a real container. The image is ``python:3.12-slim`` unless
``MINOS_SANDBOX_IMAGE`` says otherwise; the first run pulls it.
"""

from __future__ import annotations

import json
import os

import pytest

from minos.audit import AuditLog
from minos.broker import Broker
from minos.checkpoint import FileCheckpointStore
from minos.router import Router
from minos.sandbox import CodeOrigin
from minos.sandbox.confine import container_engine
from minos.scopes import ScopeSet
from minos.tiers.l2_code import CodeAdapter
from minos.types import ActionRequest


def _engine_running() -> bool:
    if not container_engine():
        return False
    from minos.sandbox import ContainerSandbox

    try:
        return ContainerSandbox().available()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _engine_running(), reason="no running container engine")


@pytest.fixture
def rig(tmp_path):
    state = tmp_path / ".minos"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "input.csv").write_text("name,qty\nwidget,3\ngadget,5\n", encoding="utf-8")
    # Code from somewhere else: the origin policy's container case.
    adapter = CodeAdapter(state=state, origin=CodeOrigin.DOWNLOADED, timeout=120)
    broker = Broker(
        scopes=ScopeSet.parse(
            [f"fs.read:{workspace}/**", f"fs.write:{workspace}/**", f"code.run:{state}/**"]
        ),
        audit=AuditLog(state / "audit.jsonl"),
        store=FileCheckpointStore(state / "checkpoints"),
    )
    return {"workspace": workspace, "broker": broker, "router": Router(adapters=(adapter,))}


def act(rig, operation, **params):
    request = ActionRequest(goal_id="e2e", intent=operation, operation=operation, params=params)
    routed = rig["router"].route(request)
    return rig["broker"].submit(routed.invocation, routed.execute)


def _run(rig, code: str, **params):
    outcome = act(rig, "code.run", code=code, **params)
    assert outcome.status == "ok", outcome.error
    return outcome.result


def test_untrusted_code_runs_in_a_container_and_says_so(rig):
    result = _run(rig, "print('hello from inside')")
    assert result.ok, result.stderr
    assert "hello from inside" in result.stdout
    assert "no network" in result.confinement and "read-only root" in result.confinement


def test_an_artifact_comes_out_only_through_the_broker(rig):
    convert = (
        "import csv, json\nfrom pathlib import Path\n"
        "rows = list(csv.DictReader(Path('materials/input.csv').open()))\n"
        "Path('out/converted.json').write_text(json.dumps(rows))\n"
    )
    result = _run(rig, convert, materials=[str(rig["workspace"] / "input.csv")])
    assert result.ok, result.stderr

    destination = rig["workspace"] / "converted.json"
    assert not destination.exists()  # nothing leaves the container by itself
    promoted = act(rig, "code.materialize", artifact="converted.json", path=str(destination))
    assert promoted.status == "ok", promoted.error
    assert json.loads(destination.read_text()) == [
        {"name": "widget", "qty": "3"},
        {"name": "gadget", "qty": "5"},
    ]
    assert promoted.observed is not None and promoted.observed.verifiable


def test_there_is_no_network_interface(rig):
    probe = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    print('CONNECTED')\n"
        "except OSError as exc:\n"
        "    print('no network:', type(exc).__name__)\n"
    )
    result = _run(rig, probe)
    assert "CONNECTED" not in result.stdout
    assert "no network" in result.stdout


def test_the_root_filesystem_is_read_only(rig):
    probe = (
        "import os\n"
        "for path in ('/etc/minos_probe', '/usr/minos_probe', '/minos_probe'):\n"
        "    try:\n"
        "        open(path, 'w').write('x')\n"
        "        print('WROTE', path)\n"
        "    except OSError:\n"
        "        print('refused', path)\n"
    )
    result = _run(rig, probe)
    assert "WROTE" not in result.stdout
    assert result.stdout.count("refused") == 3


def test_the_host_is_not_visible(rig):
    home = os.path.expanduser("~")
    probe = (
        "import os\n"
        f"print('HOME VISIBLE' if os.path.exists({home!r}) else 'host home absent')\n"
        "print(sorted(os.listdir('/workspace')))\n"
    )
    result = _run(rig, probe)
    assert "HOME VISIBLE" not in result.stdout
    assert "host home absent" in result.stdout


def test_it_runs_as_nobody_with_no_capabilities(rig):
    probe = (
        "import os\n"
        "print('uid', os.getuid())\n"
        "status = open('/proc/self/status').read()\n"
        "print([l for l in status.splitlines() if l.startswith('CapEff')][0])\n"
    )
    result = _run(rig, probe)
    assert "uid 65534" in result.stdout
    assert "CapEff:\t0000000000000000" in result.stdout
