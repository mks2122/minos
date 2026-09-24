"""What confines the sandbox, and what it admits it cannot confine.

The tests that matter here are the ones that fail when a control quietly stops
working. A jail whose escape attempts are untested is a jail nobody has checked,
and the history of this file is the reason: the previous suite asserted that
process spawning was *not* blocked, which was honest, and left the hole open for
sixteen milestones.

Three kinds of test below:

- **Escapes.** A script tries to leave the workspace; the run must fail and say
  why. These run on every platform, because the in-process layer is everywhere.
- **Reporting.** What the runtime claims about its own confinement has to match
  what it applied, on the machine it is running on rather than in general.
- **Policy.** Untrusted code does not get the weak backend by accident.

Platform-specific mechanisms (Landlock, seccomp, seatbelt, integrity tokens) are
tested where they exist and skipped where they do not. A skip is a statement
that this machine could not check it, which is different from a pass.
"""

from __future__ import annotations

import os
import sys

import pytest

from minos.sandbox import (
    CodeOrigin,
    ContainerSandbox,
    ContainerUnavailable,
    SubprocessSandbox,
    Unconfined,
    Workspace,
    detect_confinement,
    select_backend,
)
from minos.sandbox import _child_confine as child
from minos.sandbox.confine import NoConfinement, container_engine


@pytest.fixture
def workspace(tmp_path):
    return Workspace(tmp_path / "ws")


@pytest.fixture
def sandbox():
    return SubprocessSandbox(default_timeout=60.0)


# -- escapes ---------------------------------------------------------------


def test_a_script_cannot_read_outside_the_workspace(sandbox, workspace, tmp_path):
    """The line SECURITY.md used to carry: "the cwd is a convention, not a jail"."""
    secret = tmp_path / "not-a-material.txt"
    secret.write_text("private")

    result = sandbox.run(workspace, f"print(open({str(secret)!r}).read())")

    assert not result.ok
    assert "private" not in result.stdout
    assert "outside the workspace" in result.stderr


def test_a_script_cannot_list_outside_the_workspace(sandbox, workspace, tmp_path):
    """Opening ~/.aws/credentials was refused; learning it exists was not."""
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "credentials").write_text("x")
    here = tmp_path / "private"

    for listing in (f"os.listdir({str(here)!r})", f"list(os.scandir({str(here)!r}))"):
        result = sandbox.run(workspace, f"import os; print({listing})")
        assert not result.ok, listing
        assert "credentials" not in result.stdout
        assert "cannot be listed" in result.stderr


def test_a_script_can_still_list_its_own_workspace(sandbox, workspace):
    result = sandbox.run(
        workspace, "import os; open('made.txt', 'w').write('x'); print(sorted(os.listdir('.')))"
    )
    assert result.ok, result.stderr
    assert "made.txt" in result.stdout


def test_a_script_cannot_write_outside_the_workspace(sandbox, workspace, tmp_path):
    target = tmp_path / "escaped.txt"

    result = sandbox.run(workspace, f"open({str(target)!r}, 'w').write('escaped')")

    assert not result.ok
    assert not target.exists()


def test_traversal_out_of_the_workspace_is_refused(sandbox, workspace, tmp_path):
    """Resolved real paths, so ``..`` is not a way around the allow-list."""
    (tmp_path / "sibling.txt").write_text("private")

    result = sandbox.run(workspace, "print(open('../sibling.txt').read())")

    assert not result.ok
    assert "private" not in result.stdout


def test_a_script_can_still_read_its_materials_and_write_its_output(sandbox, workspace, tmp_path):
    """The allow-list has to leave the sandbox usable, or it is just a break."""
    source = tmp_path / "data.csv"
    source.write_text("a,b\n1,2\n")
    workspace.add_material(source)

    result = sandbox.run(
        workspace,
        """
        from pathlib import Path
        rows = Path('materials/data.csv').read_text().splitlines()
        Path('out/rows.txt').write_text(str(len(rows)))
        """,
    )

    assert result.ok, result.stderr
    assert result.artifacts[0].path.read_text() == "2"


def test_the_interpreter_can_still_import_the_packages_it_ships_with(sandbox, workspace):
    """The allow-list must not refuse the stdlib.

    It nearly did: the interpreter prefix is reached through a directory
    junction under uv, and comparing a resolved path against an unresolved root
    refused every import. Worth a test, because the failure looked like the
    sandbox being broken rather than the allow-list being wrong.
    """
    result = sandbox.run(
        workspace, "import json, csv, sqlite3, zipfile; print('stdlib', json.dumps([1]))"
    )

    assert result.ok, result.stderr
    assert "stdlib" in result.stdout


def test_spawning_a_process_is_refused(sandbox, workspace):
    result = sandbox.run(workspace, "import subprocess; subprocess.run(['echo', 'pwned'])")

    assert not result.ok
    assert "not permitted in the minos sandbox" in result.stderr


def test_loading_a_shared_library_is_refused_when_asked_for(workspace):
    """ctypes is the bypass for everything patched in-process."""
    strict = SubprocessSandbox(default_timeout=60.0, confine_ctypes=True)

    result = strict.run(workspace, "import ctypes; ctypes.CDLL(None)")

    assert not result.ok
    assert "not permitted in the minos sandbox" in result.stderr


def test_ctypes_is_not_refused_by_default_and_the_report_says_so(sandbox, workspace):
    """The deliberate half of the trade, recorded so it cannot be lost quietly.

    Refusing ``ctypes.dlopen`` would be tidier and would break numpy, pandas and
    openpyxl -- and on Windows ``import ctypes`` itself, which binds kernel32 at
    import time. What contains ctypes is the kernel layer, and where that is not
    enough the answer is a container rather than a longer denylist.
    """
    result = sandbox.run(workspace, "import ctypes; print('imported', ctypes.sizeof(ctypes.c_int))")

    assert result.ok, result.stderr
    assert "imported" in result.stdout
    assert "ctypes refusal" in result.confinement


@pytest.mark.parametrize("module", ["numpy", "pandas", "openpyxl", "pypdf", "PIL"])
def test_the_sandbox_package_set_still_imports(sandbox, workspace, module):
    """The measurement that decided the trade above, kept as a test.

    Each of these loads a compiled extension or imports ``subprocess`` at module
    scope. A hardening change that breaks them has taken the sandbox's actual
    usefulness away, and should have to say so out loud.
    """
    pytest.importorskip(module)

    result = sandbox.run(workspace, f"import {module}; print('imported {module}')")

    assert result.ok, result.stderr
    assert f"imported {module}" in result.stdout


def test_strict_imports_are_available_for_callers_who_want_them(workspace, tmp_path):
    strict = SubprocessSandbox(
        default_timeout=60.0, blocked_imports=tuple(sorted(child.STRICT_IMPORTS))
    )

    result = strict.run(workspace, "import ctypes")

    assert not result.ok
    assert "not importable in the minos sandbox" in result.stderr


# -- reporting -------------------------------------------------------------


def test_every_run_records_what_actually_confined_it(sandbox, workspace):
    """Not what the platform could do -- what this run got."""
    result = sandbox.run(workspace, "print('x')")

    assert result.confinement
    assert detect_confinement().name in result.confinement
    assert "enforced:" in result.confinement


def test_the_report_names_what_was_unavailable_rather_than_omitting_it(sandbox, workspace):
    """A control that is absent and silent is the failure mode, not the absence."""
    result = sandbox.run(workspace, "print('x')")

    assert "enforced:" in result.confinement
    if not sys.platform.startswith("linux"):
        # Landlock and seccomp cannot apply here, and the report has to say so.
        assert "unavailable:" in result.confinement


def test_describe_names_the_platform_mechanism_and_the_in_process_layers(sandbox):
    described = sandbox.describe()

    assert detect_confinement().name in described
    for layer in sandbox.in_process_layers():
        assert layer in described


def test_requiring_confinement_refuses_rather_than_running_unconfined(workspace, monkeypatch):
    """The only honest response to "I cannot confine this here" is to stop."""
    monkeypatch.setattr(
        "minos.sandbox.runner.detect", lambda: NoConfinement(detail="nothing on this box")
    )
    strict = SubprocessSandbox(require_confinement=True)

    with pytest.raises(Unconfined, match="requires kernel-enforced confinement"):
        strict.run(workspace, "print('x')")


def test_switching_kernel_confinement_off_is_visible_in_the_description():
    unconfined = SubprocessSandbox(confine_kernel=False)

    assert not unconfined.confinement.kernel_enforced
    assert "switched off by the caller" in unconfined.describe()


# -- platform mechanisms ---------------------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Landlock is Linux only")
def test_landlock_is_detected_when_the_kernel_has_it():
    abi = child.landlock_abi()

    assert abi >= 0
    if abi:
        assert "landlock" in detect_confinement().name


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="seccomp is Linux only")
def test_seccomp_blocks_the_socket_syscall_under_ctypes(sandbox, workspace):
    """The one escape the in-process layer cannot close, closed by the kernel."""
    if not child.seccomp_arch():
        pytest.skip("no seccomp syscall table for this architecture")

    result = sandbox.run(
        workspace,
        """
        import ctypes
        libc = ctypes.CDLL('libc.so.6', use_errno=True)
        print('fd', libc.socket(2, 1, 0))
        """,
    )

    # Either the audit hook refused the dlopen, or seccomp refused the syscall.
    # Both are correct; running is not.
    assert not result.ok or "fd -1" in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="seatbelt is macOS only")
def test_the_seatbelt_profile_is_probed_not_assumed():
    confinement = detect_confinement()

    assert confinement.name in {"sandbox-exec", "none"}
    if confinement.name == "sandbox-exec":
        assert "(deny default)" in confinement.profile(__import__("pathlib").Path.cwd())


@pytest.mark.skipif(sys.platform != "win32", reason="integrity levels are Windows only")
def test_windows_uses_a_low_integrity_token_and_a_job_object():
    confinement = detect_confinement()

    assert confinement.name == "low-integrity+job"
    assert confinement.kernel_enforced
    # Stated rather than glossed: Windows' mandatory policy is no-write-up.
    assert "reads are not confined" in confinement.detail


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows only")
def test_a_runaway_script_is_killed_with_its_children(workspace):
    """The Job Object is what makes the timeout cover the whole tree."""
    result = SubprocessSandbox(default_timeout=2.0).run(workspace, "while True: pass", timeout=2.0)

    assert result.timed_out
    assert "killed after" in result.detail


# -- the origin policy -----------------------------------------------------


def test_local_code_gets_the_subprocess_backend():
    choice = select_backend(CodeOrigin.LOCAL_PLANNER)

    assert choice.backend.name == "subprocess"
    assert not choice.downgraded


@pytest.mark.parametrize(
    "origin",
    [CodeOrigin.REMOTE_PLANNER, CodeOrigin.SHARED_SKILL, CodeOrigin.DOWNLOADED],
)
def test_untrusted_code_never_silently_gets_the_weak_backend(origin):
    """Either a container, or a refusal. Never a quiet downgrade."""
    if container_engine():
        choice = select_backend(origin)
        assert choice.backend.name == "container"
        assert not choice.downgraded
    else:
        with pytest.raises(ContainerUnavailable, match="needs a container"):
            select_backend(origin)


def test_a_downgrade_has_to_be_asked_for_and_is_recorded_as_one():
    if container_engine():
        pytest.skip("a container is available, so nothing downgrades")

    choice = select_backend(CodeOrigin.DOWNLOADED, allow_downgrade=True)

    assert choice.downgraded
    assert "DOWNGRADED" in choice.describe()
    assert choice.backend.require_confinement


def test_choosing_the_subprocess_backend_for_untrusted_code_is_still_a_downgrade():
    """Naming a backend is not the same as arguing the threat away."""
    choice = select_backend(CodeOrigin.DOWNLOADED, prefer="subprocess")

    assert choice.downgraded
    # And it gets the same floor as the automatic downgrade, not a weaker one.
    assert choice.backend.require_confinement


def test_local_code_on_the_named_subprocess_backend_is_not_forced():
    choice = select_backend(CodeOrigin.LOCAL_PLANNER, prefer="subprocess")

    assert not choice.backend.require_confinement


def test_only_the_local_planner_is_trusted():
    trusted = [origin for origin in CodeOrigin if origin.trusted]

    assert trusted == [CodeOrigin.LOCAL_PLANNER]


def test_the_origin_survives_a_round_trip_through_a_string():
    """It is written to the audit log and read back from a config file."""
    assert CodeOrigin("downloaded") is CodeOrigin.DOWNLOADED
    assert CodeOrigin.DOWNLOADED.value == "downloaded"


# -- the container backend -------------------------------------------------


def test_the_container_backend_refuses_to_exist_without_an_engine(monkeypatch):
    monkeypatch.setattr("minos.sandbox.container.container_engine", lambda: "")

    with pytest.raises(ContainerUnavailable, match="no container engine"):
        ContainerSandbox(engine="")


@pytest.mark.skipif(not container_engine(), reason="no container engine installed")
def test_the_container_command_drops_everything_that_matters():
    """Asserted on the command rather than a run, so it holds without a daemon."""
    sandbox = ContainerSandbox()
    argv = " ".join(sandbox._argv(_FakeWorkspace(), 60))

    assert "--network none" in argv
    assert "--cap-drop ALL" in argv
    assert "--read-only" in argv
    assert "no-new-privileges" in argv
    assert "--user 65534:65534" in argv
    assert "--pids-limit" in argv


@pytest.mark.skipif(not container_engine(), reason="no container engine installed")
def test_an_engine_that_is_not_running_is_reported_as_such(workspace):
    sandbox = ContainerSandbox()
    if sandbox.available():
        pytest.skip("the engine is running, so there is no failure to report")

    result = sandbox.run(workspace, "print('x')", timeout=60)

    assert not result.ok
    assert "installed but not running" in result.detail


class _FakeWorkspace:
    """Just enough workspace to build a command line from."""

    root = "/tmp/ws"


# -- the seccomp filter, checked without a kernel --------------------------


def _run_bpf(program, arch: int, nr: int) -> int:
    """Interpret the filter the way the kernel would, and return its verdict.

    Only the four opcodes the filter uses. This exists because a seccomp program
    with a wrong jump offset does not fail loudly -- the kernel rejects it and
    the process runs with **no filter at all**, on the one platform where this is
    the layer that closes the ctypes hole. An interpreter here means the
    arithmetic is checked on every machine that runs the suite, including the
    ones that have no seccomp.
    """
    data = {0: nr, 4: arch}
    accumulator = 0
    index = 0
    for _ in range(len(program) + 1):
        code, jt, jf, k = program[index]
        if code == child._BPF_LD_W_ABS:
            accumulator = data[k]
            index += 1
        elif code == child._BPF_JEQ_K:
            index += 1 + (jt if accumulator == k else jf)
        elif code == child._BPF_RET_K:
            return k
        else:  # pragma: no cover - the filter uses no other opcode
            raise AssertionError(f"unknown opcode {code:#x}")
        assert 0 <= index < len(program), f"jump out of range: {index}"
    raise AssertionError("filter did not terminate")


@pytest.mark.parametrize("arch", sorted(child._SECCOMP_BLOCKED))
def test_every_blocked_syscall_returns_eperm(arch):
    program = child.seccomp_program(arch)
    audit = child._AUDIT_ARCH[arch]

    for number in child._SECCOMP_BLOCKED[arch]:
        assert _run_bpf(program, audit, number) == child._SECCOMP_RET_ERRNO_EPERM


@pytest.mark.parametrize("arch", sorted(child._SECCOMP_BLOCKED))
def test_ordinary_syscalls_are_allowed(arch):
    """A filter that blocks read or write does not run Python at all."""
    program = child.seccomp_program(arch)
    audit = child._AUDIT_ARCH[arch]

    for number in (0, 1, 2, 3, 9, 60, 257):
        if number in child._SECCOMP_BLOCKED[arch]:
            continue
        assert _run_bpf(program, audit, number) == child._SECCOMP_RET_ALLOW


@pytest.mark.parametrize("arch", sorted(child._SECCOMP_BLOCKED))
def test_a_foreign_architecture_is_killed_rather_than_allowed(arch):
    """The x32 trick: same syscall numbers, different ABI.

    This jump was off by one when it was written -- it pointed one past the end
    of the program, which the kernel rejects, which means no filter at all. That
    is the exact failure this test exists to catch, and it caught it.
    """
    program = child.seccomp_program(arch)

    assert _run_bpf(program, 0xDEADBEEF, 41) == child._SECCOMP_RET_KILL_PROCESS


def test_the_socket_syscall_is_blocked_on_every_architecture_we_claim():
    """Names, not numbers: the tables are hand-written and must agree."""
    assert _run_bpf(child.seccomp_program("x86_64"), child._AUDIT_ARCH["x86_64"], 41) == (
        child._SECCOMP_RET_ERRNO_EPERM
    )
    assert _run_bpf(child.seccomp_program("aarch64"), child._AUDIT_ARCH["aarch64"], 198) == (
        child._SECCOMP_RET_ERRNO_EPERM
    )


# -- Windows: the kernel layer, checked without the in-process one ----------


@pytest.mark.skipif(sys.platform != "win32", reason="mandatory integrity is Windows only")
def test_the_low_integrity_token_actually_applies(tmp_path):
    """The control that was silently absent, pinned so it cannot go again.

    It failed for a while and nothing noticed: ctypes had no prototypes, so the
    pseudo-handle from ``GetCurrentProcess()`` was passed as a 32-bit int,
    ``OpenProcessToken`` returned ERROR_INVALID_HANDLE, and the code fell back
    to an ordinary token. Every escape test still passed -- the *in-process*
    allow-list was catching them -- which is exactly how a kernel boundary
    disappears without anybody finding out.
    """
    from minos.sandbox import winspawn

    workspace = Workspace(tmp_path / "ws")
    assert winspawn.label_low_integrity(workspace.root)

    outcome = winspawn.spawn_confined(
        [sys.executable, "-I", "-c", "print('ran')"],
        cwd=workspace.root,
        env={"PATH": "", "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows")},
        timeout=60,
        max_memory_bytes=1 << 30,
        output_dir=workspace.run,
    )

    assert outcome.returncode == 0, outcome.stderr
    assert outcome.integrity_lowered, outcome.detail
    assert outcome.job_object, outcome.detail


@pytest.mark.skipif(sys.platform != "win32", reason="mandatory integrity is Windows only")
def test_the_kernel_refuses_a_write_with_every_in_process_layer_off(tmp_path):
    """Whatever refuses this is the kernel, because nothing else is left.

    The in-process allow-list would refuse it too, which is why it is switched
    off here: a test that passes under both layers cannot tell you which one is
    working, and the one that matters is the one a hostile script cannot remove.
    """
    naked = SubprocessSandbox(
        default_timeout=60.0,
        confine_network=False,
        confine_modules=False,
        confine_filesystem=False,
    )
    target = tmp_path / "escaped.txt"

    result = naked.run(Workspace(tmp_path / "ws"), f"open({str(target)!r}, 'w').write('escaped')")

    assert not result.ok
    assert not target.exists()
    assert "PermissionError" in result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="mandatory integrity is Windows only")
def test_the_workspace_stays_writable_under_the_lowered_token(tmp_path):
    """The other half: confinement that also breaks the sandbox is not a win."""
    naked = SubprocessSandbox(
        default_timeout=60.0,
        confine_network=False,
        confine_modules=False,
        confine_filesystem=False,
    )

    result = naked.run(
        Workspace(tmp_path / "ws"),
        "from pathlib import Path; Path('out/a.txt').write_text('ok'); print('wrote')",
    )

    assert result.ok, result.stderr
    assert "wrote" in result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="mandatory integrity is Windows only")
def test_the_report_says_whether_the_token_was_really_lowered(sandbox, workspace):
    result = sandbox.run(workspace, "print('x')")

    assert "integrity lowered" in result.confinement
    assert "job object applied" in result.confinement
    assert "INTEGRITY NOT LOWERED" not in result.confinement


@pytest.mark.skipif(sys.platform != "win32", reason="integrity levels are Windows only")
def test_a_required_boundary_that_cannot_be_applied_means_the_script_never_runs(tmp_path):
    """Found in review: a token that failed to lower used to fall back to an
    ordinary spawn and report success. With require=True the child is killed
    while still suspended, so not one line of it runs."""
    from minos.sandbox import winspawn
    from minos.sandbox.confine import ConfinementNotApplied

    marker = tmp_path / "ran.txt"
    with pytest.raises(ConfinementNotApplied, match="was not started"):
        winspawn.spawn_confined(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"],
            cwd=tmp_path,
            env=dict(os.environ),
            timeout=30,
            low_integrity=False,  # stands in for a token that could not be lowered
            require=True,
        )
    assert not marker.exists()
