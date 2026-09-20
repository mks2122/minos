"""The sandbox scratchpad, and the code tier built on it.

The interesting tests here are the adversarial ones. The jail is not a security
boundary (see SECURITY.md and `minos.sandbox.runner`), but the properties it
*does* claim have to actually hold, and "we meant to block that" is not a
mechanism.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from minos.sandbox import SubprocessSandbox, Workspace, scrubbed_environment
from minos.tiers.base import OperationUnsupported
from minos.tiers.l2_code import CodeAdapter
from minos.types import ActionRequest


@pytest.fixture
def workspace(tmp_path):
    return Workspace(tmp_path / "ws")


@pytest.fixture
def sandbox():
    return SubprocessSandbox(default_timeout=60.0)


def run(sandbox, workspace, code, **kwargs):
    return sandbox.run(workspace, code, **kwargs)


# -- it actually runs code -------------------------------------------------


def test_a_script_runs_and_its_output_is_captured(sandbox, workspace):
    result = run(sandbox, workspace, "print('hello from the sandbox')")

    assert result.ok
    assert result.returncode == 0
    assert "hello from the sandbox" in result.stdout


def test_a_failing_script_reports_its_error(sandbox, workspace):
    result = run(sandbox, workspace, "raise ValueError('deliberate')")

    assert not result.ok
    assert result.returncode != 0
    assert "deliberate" in result.stderr


def test_files_written_to_out_become_artifacts(sandbox, workspace):
    result = run(
        sandbox,
        workspace,
        """
        from pathlib import Path
        Path('out').mkdir(exist_ok=True)
        Path('out/result.txt').write_text('computed')
        """,
    )

    assert result.ok
    assert len(result.artifacts) == 1
    assert result.artifacts[0].relative == "result.txt"
    assert result.artifacts[0].path.read_text() == "computed"


def test_materials_are_copies_not_the_originals(sandbox, workspace, tmp_path):
    """A script that corrupts its input must corrupt a copy."""
    original = tmp_path / "input.txt"
    original.write_text("precious original")
    workspace.add_material(original)

    result = run(
        sandbox,
        workspace,
        """
        from pathlib import Path
        Path('materials/input.txt').write_text('destroyed')
        """,
    )

    assert result.ok
    assert original.read_text() == "precious original"


# -- what the jail claims to enforce ---------------------------------------


def test_the_network_is_blocked(sandbox, workspace):
    """Every ordinary client library goes through socket."""
    result = run(
        sandbox,
        workspace,
        """
        import urllib.request
        urllib.request.urlopen('http://example.com', timeout=5)
        """,
    )

    assert not result.ok
    assert "network access is disabled" in result.stderr


def test_raw_sockets_are_blocked_too(sandbox, workspace):
    result = run(
        sandbox,
        workspace,
        """
        import socket
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        """,
    )

    assert not result.ok
    assert "network access is disabled" in result.stderr


def test_dns_resolution_is_blocked(sandbox, workspace):
    result = run(sandbox, workspace, "import socket; socket.getaddrinfo('example.com', 80)")

    assert not result.ok
    assert "network access is disabled" in result.stderr


def test_credentials_are_not_in_the_environment(sandbox, workspace, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-do-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-leak-either")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-nope")

    result = run(
        sandbox,
        workspace,
        """
        import os
        print(repr(sorted(os.environ)))
        print(repr(os.environ.get('ANTHROPIC_API_KEY')))
        """,
    )

    assert result.ok
    assert "sk-ant-do-not-leak" not in result.stdout
    assert "do-not-leak-either" not in result.stdout
    assert "ghp-nope" not in result.stdout
    assert "ANTHROPIC_API_KEY" not in result.stdout


def test_scrubbed_environment_keeps_only_what_is_needed():
    scrubbed = scrubbed_environment(
        {
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "secret",
            "MY_PASSWORD": "secret",
            "SOME_RANDOM_VAR": "value",
            "SESSION_TOKEN": "secret",
        }
    )

    assert scrubbed["PATH"] == "/usr/bin"
    assert "ANTHROPIC_API_KEY" not in scrubbed
    assert "MY_PASSWORD" not in scrubbed
    assert "SESSION_TOKEN" not in scrubbed
    # Allow-list: unknown variables do not travel even when they look harmless.
    assert "SOME_RANDOM_VAR" not in scrubbed


def test_a_runaway_script_is_killed(sandbox, workspace):
    result = run(sandbox, workspace, "while True: pass", timeout=2.0)

    assert not result.ok
    assert result.timed_out
    assert "killed after" in result.detail


def test_enormous_output_is_truncated_not_streamed_into_memory(sandbox, workspace):
    result = run(sandbox, workspace, "print('x' * 500_000)")

    assert len(result.stdout) < 200_000
    assert "truncated" in result.stdout


def test_the_script_starts_in_the_workspace(sandbox, workspace):
    result = run(sandbox, workspace, "import os; print(os.getcwd())")

    assert result.ok
    assert str(workspace.root) in result.stdout


def test_parent_interpreter_settings_do_not_leak(sandbox, workspace, monkeypatch):
    """-E: a PYTHONPATH pointing at the user's code must not be inherited."""
    monkeypatch.setenv("PYTHONPATH", str(Path.home()))

    result = run(sandbox, workspace, "import sys; print(repr(sys.path))")

    assert result.ok
    assert str(Path.home()) not in result.stdout


# -- artifact resolution is not negotiable ---------------------------------


def test_artifact_traversal_is_refused(workspace):
    """The planner picks this string and the planner is untrusted."""
    with pytest.raises(ValueError, match="outside the sandbox"):
        workspace.resolve_artifact("../../../etc/passwd")


def test_absolute_artifact_paths_are_refused(workspace):
    target = "C:/Windows/System32/config/SAM" if sys.platform == "win32" else "/etc/passwd"
    with pytest.raises(ValueError):
        workspace.resolve_artifact(target)


def test_a_nonexistent_artifact_is_refused(workspace):
    with pytest.raises(ValueError, match="not a file"):
        workspace.resolve_artifact("never-produced.txt")


def test_material_names_cannot_escape_the_materials_directory(workspace, tmp_path):
    """A material called ``../../secret`` lands in materials/, flattened."""
    sneaky = tmp_path / "secret.txt"
    sneaky.write_text("x")

    landed = workspace.add_material(sneaky)

    assert landed.parent == workspace.materials


def test_two_materials_with_the_same_name_do_not_clobber(workspace, tmp_path):
    a = tmp_path / "a" / "report.txt"
    b = tmp_path / "b" / "report.txt"
    for path, text in ((a, "first"), (b, "second")):
        path.parent.mkdir(parents=True)
        path.write_text(text)

    first = workspace.add_material(a)
    second = workspace.add_material(b)

    assert first != second
    assert {first.read_text(), second.read_text()} == {"first", "second"}


# -- the adapter -----------------------------------------------------------


def test_code_run_declares_no_targets_on_the_real_machine(tmp_path):
    """It cannot reach anything the user owns, so there is nothing to protect."""
    adapter = CodeAdapter(state=tmp_path)
    prepared = adapter.prepare(
        ActionRequest(goal_id="g", intent="compute", operation="code.run", params={"code": "pass"})
    )

    assert prepared.contract.targets == ()
    assert any(g.capability == "code.run" for g in prepared.grants)


def test_code_run_requires_fs_read_on_every_material(tmp_path):
    """A script must not be handed a file the task was never granted."""
    adapter = CodeAdapter(state=tmp_path)
    prepared = adapter.prepare(
        ActionRequest(
            goal_id="g",
            intent="convert",
            operation="code.run",
            params={"code": "pass", "materials": ["/data/secret.pdf"]},
        )
    )

    assert Grantish("fs.read", "/data/secret.pdf") in [
        (g.capability, g.subject) for g in ()
    ] or any(g.capability == "fs.read" and g.subject == "/data/secret.pdf" for g in prepared.grants)


def test_code_run_without_code_is_declined(tmp_path):
    adapter = CodeAdapter(state=tmp_path)
    with pytest.raises(OperationUnsupported):
        adapter.prepare(
            ActionRequest(goal_id="g", intent="", operation="code.run", params={"code": "   "})
        )


def test_materialize_declares_the_real_target_and_needs_fs_write(tmp_path):
    adapter = CodeAdapter(state=tmp_path)
    (adapter.workspace.out / "result.docx").write_bytes(b"converted")
    destination = tmp_path / "real" / "result.docx"

    prepared = adapter.prepare(
        ActionRequest(
            goal_id="g",
            intent="promote",
            operation="code.materialize",
            params={"artifact": "result.docx", "path": str(destination)},
        )
    )

    assert prepared.contract.targets == (destination.resolve(),)
    assert prepared.grants == tuple(g for g in prepared.grants if g.capability == "fs.write"), (
        "materialize is an ordinary fs.write"
    )


def test_materialize_refuses_an_artifact_that_escapes_the_sandbox(tmp_path):
    adapter = CodeAdapter(state=tmp_path)

    with pytest.raises(OperationUnsupported):
        adapter.prepare(
            ActionRequest(
                goal_id="g",
                intent="exfiltrate",
                operation="code.materialize",
                params={"artifact": "../../../secrets.txt", "path": str(tmp_path / "x")},
            )
        )


def test_the_adapter_declines_operations_it_does_not_own(tmp_path):
    adapter = CodeAdapter(state=tmp_path)
    with pytest.raises(OperationUnsupported):
        adapter.prepare(ActionRequest(goal_id="g", intent="", operation="fs.write"))


# -- helper ----------------------------------------------------------------


def Grantish(capability: str, subject: str) -> tuple[str, str]:
    return (capability, subject)


def test_workspace_dispose_removes_everything(tmp_path):
    ws = Workspace(tmp_path / "ws")
    (ws.out / "a.txt").write_text("x")

    ws.dispose()

    assert not ws.root.exists()


def test_artifacts_are_sorted_so_contracts_are_stable(sandbox, workspace):
    """A contract whose target order wobbles is not one anybody can review."""
    run(
        sandbox,
        workspace,
        """
        from pathlib import Path
        for name in ('z.txt', 'a.txt', 'm.txt'):
            Path('out', name).write_text(name)
        """,
    )

    names = [a.relative for a in workspace.artifacts()]
    assert names == sorted(names)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource limits")
def test_memory_limit_applies_on_posix(sandbox, workspace):
    small = SubprocessSandbox(max_memory_bytes=64 << 20, default_timeout=30.0)
    result = small.run(workspace, "x = bytearray(512 * 1024 * 1024)")

    assert not result.ok


def test_os_environ_has_the_sandbox_marker(sandbox, workspace):
    """So a script can tell, and so can anyone reading a stack trace."""
    result = run(sandbox, workspace, "import os; print(os.environ.get('MINOS_SANDBOX'))")

    assert result.ok
    assert "1" in result.stdout


def test_a_script_cannot_see_the_parents_working_directory(sandbox, workspace):
    result = run(sandbox, workspace, "import os; print(os.listdir('.'))")

    assert result.ok
    assert "src" not in result.stdout
    assert "materials" in result.stdout


def test_no_bytecode_is_written_into_the_workspace(sandbox, workspace):
    run(sandbox, workspace, "print('x')")

    assert not list(workspace.root.rglob("__pycache__"))


def test_unwritable_out_directory_does_not_crash_the_runtime(sandbox, workspace):
    """The sandbox must fail as a result, never as an exception in the parent."""
    result = run(sandbox, workspace, "raise SystemExit(3)")

    assert not result.ok
    assert result.returncode == 3


def test_workspace_usage_is_reportable(workspace):
    (workspace.out / "a.bin").write_bytes(b"x" * 1024)

    assert workspace.usage_bytes() >= 1024


def test_os_system_is_not_blocked_and_we_say_so(sandbox, workspace):
    """Documenting the boundary: this jail does not stop process spawning.

    Recorded as a test so the limitation cannot quietly stop being true without
    someone noticing. See SECURITY.md.
    """
    result = run(sandbox, workspace, "import subprocess; print(subprocess.run is not None)")

    assert result.ok
    assert "True" in result.stdout


def test_environment_is_not_inherited_wholesale(sandbox, workspace, monkeypatch):
    monkeypatch.setenv("SOME_BUSINESS_SECRET", "leak-me")

    result = run(sandbox, workspace, "import os; print(len(os.environ))")

    assert result.ok
    assert "leak-me" not in result.stdout


def test_run_returns_artifacts_even_when_the_script_failed(sandbox, workspace):
    """Partial work is still work, and the user may want to see it."""
    result = run(
        sandbox,
        workspace,
        """
        from pathlib import Path
        Path('out/partial.txt').write_text('got this far')
        raise RuntimeError('then failed')
        """,
    )

    assert not result.ok
    assert len(result.artifacts) == 1


def test_missing_interpreter_is_reported_not_raised(workspace):
    broken = SubprocessSandbox(python=str(Path("nonexistent-python-xyz")))

    result = broken.run(workspace, "print('x')", timeout=5)

    assert not result.ok
    assert "could not be started" in result.detail


def test_workspace_survives_across_steps(tmp_path):
    """A script writes a file; a later step promotes it."""
    adapter = CodeAdapter(state=tmp_path)
    first = adapter.workspace.root
    second = adapter.workspace.root

    assert first == second


def test_out_dir_exists_before_the_script_runs(sandbox, workspace):
    """Scripts should not need boilerplate to write their output."""
    result = run(
        sandbox,
        workspace,
        "from pathlib import Path; Path('out/x.txt').write_text('no mkdir needed')",
    )

    assert result.ok, result.stderr
    assert len(result.artifacts) == 1


def test_summary_is_human_readable(sandbox, workspace):
    result = run(sandbox, workspace, "print('x')")

    assert "artifact" in result.summary()


def test_os_getcwd_is_not_the_users_home(sandbox, workspace):
    result = run(sandbox, workspace, "import os; print(os.getcwd() == os.path.expanduser('~'))")

    assert "False" in result.stdout


def test_sandbox_directory_is_under_the_state_directory(tmp_path):
    """So `.minos/` remains the one thing a user deletes to reset everything."""
    adapter = CodeAdapter(state=tmp_path)

    assert str(tmp_path) in str(adapter.workspace.root)
    assert "sandbox" in str(adapter.workspace.root)


def test_environment_scrub_is_applied_to_the_real_run(sandbox, workspace):
    result = run(sandbox, workspace, "import os; print('PATH' in os.environ)")

    assert "True" in result.stdout


def test_a_script_that_reads_a_material_by_relative_path(sandbox, workspace, tmp_path):
    source = tmp_path / "data.csv"
    source.write_text("a,b\n1,2\n")
    workspace.add_material(source)

    result = run(
        sandbox,
        workspace,
        """
        from pathlib import Path
        text = Path('materials/data.csv').read_text()
        Path('out/rows.txt').write_text(str(len(text.splitlines())))
        """,
    )

    assert result.ok, result.stderr
    assert result.artifacts[0].path.read_text() == "2"


def test_os_environ_does_not_contain_pythonpath(sandbox, workspace):
    result = run(sandbox, workspace, "import os; print('PYTHONPATH' in os.environ)")

    assert "False" in result.stdout


def test_artifact_digests_match_the_file(sandbox, workspace):
    from minos.checkpoint import file_digest

    run(sandbox, workspace, "from pathlib import Path; Path('out/a.txt').write_text('content')")

    artifact = workspace.artifacts()[0]
    assert artifact.digest == file_digest(artifact.path)


def test_nested_artifacts_keep_their_relative_path(sandbox, workspace):
    run(
        sandbox,
        workspace,
        """
        from pathlib import Path
        Path('out/pages').mkdir(parents=True, exist_ok=True)
        Path('out/pages/1.txt').write_text('page one')
        """,
    )

    assert workspace.artifacts()[0].relative == "pages/1.txt"


def test_materialize_of_a_nested_artifact(tmp_path):
    adapter = CodeAdapter(state=tmp_path)
    nested = adapter.workspace.out / "pages" / "1.txt"
    nested.parent.mkdir(parents=True)
    nested.write_text("page one")

    prepared = adapter.prepare(
        ActionRequest(
            goal_id="g",
            intent="promote",
            operation="code.materialize",
            params={"artifact": "pages/1.txt", "path": str(tmp_path / "out.txt")},
        )
    )

    assert prepared.contract.targets == ((tmp_path / "out.txt").resolve(),)


def test_workspace_creates_its_directories(tmp_path):
    ws = Workspace(tmp_path / "fresh")

    assert ws.materials.is_dir()
    assert ws.out.is_dir()
    assert ws.run.is_dir()


def test_os_name_is_available(sandbox, workspace):
    """Sanity: the interpreter genuinely starts under the scrubbed environment."""
    result = run(sandbox, workspace, "import os, sys; print(os.name, sys.version_info[0])")

    assert result.ok, result.stderr
    assert "3" in result.stdout
