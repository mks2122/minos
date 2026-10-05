"""Spawning a confined child on Windows, which ``subprocess`` cannot do.

Two things Windows offers that the standard library does not expose, and both
are the answer to a limitation SECURITY.md has been carrying since the first
release:

**A Job Object** gives the memory and process-count limits that `RLIMIT_AS` and
`RLIMIT_NPROC` give on POSIX. ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` also means
the whole process tree dies when the handle closes, so a script that manages to
spawn something cannot leave it behind after a timeout.

**A low-integrity primary token** is the actual boundary. A process at Low
integrity cannot write to any object whose mandatory label is higher, which is
everything on the machine except what we explicitly relabel -- and we relabel
exactly one directory, the workspace. It is not a full AppContainer: reads are
still permitted, because Windows' default mandatory policy is no-write-up
rather than no-read-up. That limit is stated in SECURITY.md rather than papered
over, and the filesystem allow-list in the child covers ordinary reads.

The child is created **suspended**, assigned to the job, and only then resumed,
so there is no window in which it runs unconstrained.

Two shapes of child use the same launch. :func:`spawn_confined` runs a script
to completion with its output in files. :class:`ConfinedProcess` keeps a child
alive and talks to it over pipes -- the isolated planner, which must answer many
questions over one run.

Everything here degrades rather than raises: if the token cannot be lowered the
caller is told, and it decides whether an unconfined run is acceptable. A
security control that disappears without saying so is the failure mode this
project exists to avoid.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any

from .confine import ConfinementNotApplied

# getattr, not attribute access: these exist only on Windows, and CI
# typechecks every platform. The code that calls them runs only on Windows.
_WinDLL: Any = getattr(ctypes, "WinDLL", None)
_last_error: Callable[[], int] = getattr(ctypes, "get_last_error", lambda: 0)

__all__ = [
    "ConfinedProcess",
    "SpawnOutcome",
    "label_low_integrity",
    "spawn_confined",
    "supported",
]

# -- Win32 constants ---------------------------------------------------------

_TOKEN_DUPLICATE = 0x0002
_TOKEN_QUERY = 0x0008
_TOKEN_ASSIGN_PRIMARY = 0x0001
_TOKEN_ADJUST_DEFAULT = 0x0080
_TOKEN_ADJUST_SESSIONID = 0x0100
_SECURITY_IMPERSONATION = 2
_TOKEN_PRIMARY = 1
_TOKEN_INTEGRITY_LEVEL = 25
_SE_GROUP_INTEGRITY = 0x00000020
_LOW_INTEGRITY_SID = "S-1-16-4096"

_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400

_STARTF_USESTDHANDLES = 0x00000100
_GENERIC_WRITE = 0x40000000
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_CREATE_ALWAYS = 2
_OPEN_EXISTING = 3

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_LIMIT_JOB_MEMORY = 0x00000200

_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_INFINITE = 0xFFFFFFFF
_ERROR_PRIVILEGE_NOT_HELD = 1314
_MAXIMUM_ALLOWED = 0x02000000


def supported() -> bool:
    return os.name == "nt"


@dataclass(frozen=True, slots=True)
class SpawnOutcome:
    """What the confined run produced, plus what confinement actually held."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    integrity_lowered: bool
    job_object: bool
    detail: str = ""


def label_low_integrity(path: Path) -> bool:
    """Mark a directory writable by a Low-integrity process, and inheritably so.

    Without this the confined child cannot write its own output directory, so
    this is not hardening -- it is what makes the low-integrity token usable at
    all. ``icacls`` rather than ctypes because setting an inheritable mandatory
    label by hand means building a SACL, and a 60-year-old command-line tool
    that ships with the OS is the smaller thing to get wrong.
    """
    try:
        completed = subprocess.run(
            ["icacls", str(path), "/setintegritylevel", "(OI)(CI)L"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


# -- the Win32 surface, built once -------------------------------------------


@functools.lru_cache(maxsize=1)
def _api() -> SimpleNamespace:
    """Libraries, structures and prototypes, declared once per process."""
    from ctypes import wintypes

    kernel32 = _WinDLL("kernel32", use_last_error=True)
    advapi32 = _WinDLL("advapi32", use_last_error=True)

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = [("Label", SID_AND_ATTRIBUTES)]

    # Prototypes, and they are not optional. Without them ctypes passes every
    # argument as a C int: GetCurrentProcess() returns the pseudo-handle -1,
    # which becomes 0x00000000FFFFFFFF in a 64-bit register instead of
    # 0xFFFFFFFFFFFFFFFF, and OpenProcessToken fails with ERROR_INVALID_HANDLE.
    # That failure is *silent* -- the code falls back to an ordinary token and
    # the run proceeds unconfined -- which is why the outcome reports whether
    # the integrity level was actually lowered, and why a test asserts it.
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.DuplicateTokenEx.restype = wintypes.BOOL
    advapi32.DuplicateTokenEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi32.SetTokenInformation.restype = wintypes.BOOL
    advapi32.SetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.GetLengthSid.argtypes = [wintypes.LPVOID]

    return SimpleNamespace(
        wintypes=wintypes,
        kernel32=kernel32,
        advapi32=advapi32,
        STARTUPINFOW=STARTUPINFOW,
        PROCESS_INFORMATION=PROCESS_INFORMATION,
        SECURITY_ATTRIBUTES=SECURITY_ATTRIBUTES,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION=JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        TOKEN_MANDATORY_LABEL=TOKEN_MANDATORY_LABEL,
    )


@dataclass
class _Launched:
    """A started, resumed child and what confinement actually held."""

    process: int
    thread: int
    job: int
    pid: int
    integrity_lowered: bool
    job_applied: bool
    notes: list[str] = field(default_factory=list)


def _close(handle: int | None) -> None:
    if handle:
        api = _api()
        api.kernel32.CloseHandle(api.wintypes.HANDLE(handle))


def _launch(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdin: int,
    stdout: int,
    stderr: int,
    max_memory_bytes: int,
    max_processes: int,
    low_integrity: bool,
    require: bool,
) -> _Launched:
    """Create the job and the lowered token, start the child suspended, resume it.

    The three standard handles must already be inheritable. The caller owns the
    returned process, thread and job handles; closing the job kills the tree.
    """
    api = _api()
    kernel32, advapi32, wintypes = api.kernel32, api.advapi32, api.wintypes

    notes: list[str] = []
    job = token = duplicate = 0
    integrity_lowered = job_applied = False

    try:
        # -- the job object --------------------------------------------------
        job = int(kernel32.CreateJobObjectW(None, None) or 0)
        if job:
            limits = api.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            flags = _JOB_LIMIT_KILL_ON_JOB_CLOSE | _JOB_LIMIT_ACTIVE_PROCESS
            limits.BasicLimitInformation.ActiveProcessLimit = max_processes
            if max_memory_bytes > 0:
                flags |= _JOB_LIMIT_PROCESS_MEMORY | _JOB_LIMIT_JOB_MEMORY
                limits.ProcessMemoryLimit = max_memory_bytes
                limits.JobMemoryLimit = max_memory_bytes
            limits.BasicLimitInformation.LimitFlags = flags
            job_applied = bool(
                kernel32.SetInformationJobObject(
                    wintypes.HANDLE(job),
                    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(limits),
                    ctypes.sizeof(limits),
                )
            )
            if not job_applied:
                notes.append(f"job limits refused (error {_last_error()})")
        else:
            notes.append("job object could not be created")

        # -- the lowered token -----------------------------------------------
        if low_integrity:
            current = wintypes.HANDLE()
            wanted = (
                _TOKEN_DUPLICATE
                | _TOKEN_QUERY
                | _TOKEN_ASSIGN_PRIMARY
                | _TOKEN_ADJUST_DEFAULT
                | _TOKEN_ADJUST_SESSIONID
            )
            if advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(), wanted, ctypes.byref(current)
            ):
                token = current.value or 0
                dup = wintypes.HANDLE()
                if advapi32.DuplicateTokenEx(
                    wintypes.HANDLE(token),
                    _MAXIMUM_ALLOWED,
                    None,
                    _SECURITY_IMPERSONATION,
                    _TOKEN_PRIMARY,
                    ctypes.byref(dup),
                ):
                    duplicate = dup.value or 0
                    sid = wintypes.LPVOID()
                    if advapi32.ConvertStringSidToSidW(_LOW_INTEGRITY_SID, ctypes.byref(sid)):
                        label = api.TOKEN_MANDATORY_LABEL()
                        label.Label.Sid = sid
                        label.Label.Attributes = _SE_GROUP_INTEGRITY
                        size = ctypes.sizeof(label) + advapi32.GetLengthSid(sid)
                        if advapi32.SetTokenInformation(
                            wintypes.HANDLE(duplicate),
                            _TOKEN_INTEGRITY_LEVEL,
                            ctypes.byref(label),
                            size,
                        ):
                            integrity_lowered = True
                        else:
                            notes.append(f"integrity label refused (error {_last_error()})")
                        kernel32.LocalFree(sid)
                    else:
                        notes.append("low integrity SID could not be built")
                else:
                    notes.append(f"token duplication failed (error {_last_error()})")
            else:
                notes.append(f"process token not available (error {_last_error()})")

        # -- the process -----------------------------------------------------
        startup = api.STARTUPINFOW()
        startup.cb = ctypes.sizeof(api.STARTUPINFOW)
        startup.dwFlags = _STARTF_USESTDHANDLES
        startup.hStdInput = stdin
        startup.hStdOutput = stdout
        startup.hStdError = stderr

        block = "".join(f"{name}={value}\0" for name, value in env.items()) + "\0"
        environment = ctypes.create_unicode_buffer(block)
        command = subprocess.list2cmdline(argv)
        info = api.PROCESS_INFORMATION()
        creation = _CREATE_SUSPENDED | _CREATE_NO_WINDOW | _CREATE_UNICODE_ENVIRONMENT

        created = False
        if integrity_lowered:
            created = bool(
                advapi32.CreateProcessAsUserW(
                    wintypes.HANDLE(duplicate),
                    None,
                    ctypes.create_unicode_buffer(command),
                    None,
                    None,
                    True,
                    creation,
                    environment,
                    str(cwd),
                    ctypes.byref(startup),
                    ctypes.byref(info),
                )
            )
            if not created:
                error = _last_error()
                integrity_lowered = False
                notes.append(
                    "low integrity spawn refused"
                    + (
                        " (no SeAssignPrimaryToken privilege)"
                        if error == _ERROR_PRIVILEGE_NOT_HELD
                        else f" (error {error})"
                    )
                )
        if not created:
            created = bool(
                kernel32.CreateProcessW(
                    None,
                    ctypes.create_unicode_buffer(command),
                    None,
                    None,
                    True,
                    creation,
                    environment,
                    str(cwd),
                    ctypes.byref(startup),
                    ctypes.byref(info),
                )
            )
        if not created:
            raise OSError(_last_error(), f"cannot start {argv[0]}")

        process, thread = int(info.hProcess or 0), int(info.hThread or 0)

        # Suspended until now, so there is no instant in which the child runs
        # outside the job. This is the race that assigning after spawn has.
        if job and not kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(job), wintypes.HANDLE(process)
        ):
            job_applied = False
            notes.append(f"job assignment refused (error {_last_error()})")

        if require and not (integrity_lowered and job_applied):
            # Still suspended: not one instruction of the child has run. A run
            # that demanded the kernel boundary must not quietly get the
            # in-process one instead, so it does not run at all.
            kernel32.TerminateProcess(wintypes.HANDLE(process), 1)
            kernel32.WaitForSingleObject(wintypes.HANDLE(process), 5000)
            _close(thread)
            _close(process)
            raise ConfinementNotApplied(
                "the kernel boundary could not be applied ("
                + ("; ".join(notes) or "unknown reason")
                + "), and this run requires it; the child was not started"
            )

        kernel32.ResumeThread(wintypes.HANDLE(thread))
        launched = _Launched(
            process=process,
            thread=thread,
            job=job,
            pid=int(info.dwProcessId),
            integrity_lowered=integrity_lowered,
            job_applied=job_applied,
            notes=notes,
        )
        job = 0  # owned by the caller now
        return launched
    finally:
        for handle in (duplicate, token, job):
            _close(handle)


def spawn_confined(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    max_memory_bytes: int = 0,
    max_processes: int = 32,
    low_integrity: bool = True,
    output_dir: Path | None = None,
    require: bool = False,
) -> SpawnOutcome:
    """Run ``argv`` inside a Job Object, at Low integrity where possible.

    Output goes to files rather than pipes. Pipes would need two reader threads
    to avoid the classic fill-the-buffer deadlock, and files cost nothing here:
    the run is already bounded by a timeout, and the caller already truncates.
    """
    api = _api()
    kernel32, wintypes = api.kernel32, api.wintypes

    output_dir = Path(output_dir or cwd)
    out_path = output_dir / "_minos_stdout.txt"
    err_path = output_dir / "_minos_stderr.txt"

    inheritable = api.SECURITY_ATTRIBUTES()
    inheritable.nLength = ctypes.sizeof(api.SECURITY_ATTRIBUTES)
    inheritable.lpSecurityDescriptor = None
    inheritable.bInheritHandle = True

    def open_for_write(path: Path) -> int:
        handle = kernel32.CreateFileW(
            str(path),
            _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            ctypes.byref(inheritable),
            _CREATE_ALWAYS,
            0,
            None,
        )
        if handle == wintypes.HANDLE(-1).value:
            raise OSError(_last_error(), f"cannot create {path}")
        return int(handle)

    handles: list[int] = []
    launched: _Launched | None = None
    timed_out = False
    returncode = -1
    try:
        stdout_handle = open_for_write(out_path)
        handles.append(stdout_handle)
        stderr_handle = open_for_write(err_path)
        handles.append(stderr_handle)
        stdin_handle = int(
            kernel32.CreateFileW(
                "NUL",
                _GENERIC_READ,
                _FILE_SHARE_READ,
                ctypes.byref(inheritable),
                _OPEN_EXISTING,
                0,
                None,
            )
            or 0
        )
        handles.append(stdin_handle)

        launched = _launch(
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin_handle,
            stdout=stdout_handle,
            stderr=stderr_handle,
            max_memory_bytes=max_memory_bytes,
            max_processes=max_processes,
            low_integrity=low_integrity,
            require=require,
        )
        process = wintypes.HANDLE(launched.process)

        milliseconds = _INFINITE if timeout <= 0 else int(timeout * 1000)
        timed_out = kernel32.WaitForSingleObject(process, milliseconds) == _WAIT_TIMEOUT
        if timed_out:
            # The job, not the process: a script that spawned children takes
            # them with it.
            if launched.job:
                kernel32.TerminateJobObject(wintypes.HANDLE(launched.job), 1)
            else:
                kernel32.TerminateProcess(process, 1)
            kernel32.WaitForSingleObject(process, 5000)

        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(process, ctypes.byref(code))
        returncode = -1 if timed_out else int(code.value)
    finally:
        if launched is not None:
            for handle in (launched.thread, launched.process, launched.job):
                _close(handle)
        for handle in handles:
            _close(handle)

    def read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    stdout, stderr = read(out_path), read(err_path)
    for path in (out_path, err_path):
        with contextlib.suppress(OSError):
            path.unlink()

    return SpawnOutcome(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        integrity_lowered=launched.integrity_lowered,
        job_object=launched.job_applied,
        detail="; ".join(launched.notes),
    )


class ConfinedProcess:
    """A long-lived confined child, spoken to over its standard streams.

    The same launch as :func:`spawn_confined` -- suspended, assigned to a job,
    lowered to Low integrity, then resumed -- but with pipes rather than files,
    because the child answers many questions over one run. ``max_processes=1``
    by default: the child may not start anything, so whatever it is talked
    into, it cannot launch a program to do it.

    Only the child's ends of the pipes are made inheritable; the parent's ends
    never are, and the child's are closed here as soon as it has them.
    """

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        max_memory_bytes: int = 0,
        max_processes: int = 1,
        low_integrity: bool = True,
        require: bool = False,
    ) -> None:
        import msvcrt

        child_in, parent_in = os.pipe()
        parent_out, child_out = os.pipe()
        parent_err, child_err = os.pipe()
        child_fds = (child_in, child_out, child_err)
        get_handle: Callable[[int], int] = getattr(msvcrt, "get_osfhandle")  # noqa: B009
        set_inheritable: Callable[[int, bool], None] = getattr(os, "set_handle_inheritable")  # noqa: B009
        child_handles = [get_handle(fd) for fd in child_fds]
        try:
            for handle in child_handles:
                set_inheritable(handle, True)
            self._launched = _launch(
                argv,
                cwd=cwd,
                env=env,
                stdin=child_handles[0],
                stdout=child_handles[1],
                stderr=child_handles[2],
                max_memory_bytes=max_memory_bytes,
                max_processes=max_processes,
                low_integrity=low_integrity,
                require=require,
            )
        except BaseException:
            for fd in (*child_fds, parent_in, parent_out, parent_err):
                with contextlib.suppress(OSError):
                    os.close(fd)
            raise
        for fd in child_fds:
            os.close(fd)

        self.pid = self._launched.pid
        self.integrity_lowered = self._launched.integrity_lowered
        self.job_object = self._launched.job_applied
        self.detail = "; ".join(self._launched.notes)
        self.stdin: IO[bytes] = os.fdopen(parent_in, "wb", buffering=0)
        self.stdout: IO[bytes] = os.fdopen(parent_out, "rb")
        self.stderr: IO[bytes] = os.fdopen(parent_err, "rb")
        self._closed = False

    def poll(self) -> int | None:
        api = _api()
        process = api.wintypes.HANDLE(self._launched.process)
        if api.kernel32.WaitForSingleObject(process, 0) != _WAIT_OBJECT_0:
            return None
        code = api.wintypes.DWORD()
        api.kernel32.GetExitCodeProcess(process, ctypes.byref(code))
        return int(code.value)

    def wait(self, timeout: float = 5.0) -> int | None:
        api = _api()
        api.kernel32.WaitForSingleObject(
            api.wintypes.HANDLE(self._launched.process), int(timeout * 1000)
        )
        return self.poll()

    def kill(self) -> None:
        api = _api()
        if self._launched.job:
            api.kernel32.TerminateJobObject(api.wintypes.HANDLE(self._launched.job), 1)
        else:
            api.kernel32.TerminateProcess(api.wintypes.HANDLE(self._launched.process), 1)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(OSError):
            self.stdin.close()
        if self.wait(2.0) is None:
            self.kill()
            self.wait(5.0)
        for stream in (self.stdout, self.stderr):
            with contextlib.suppress(OSError):
                stream.close()
        # Closing the job is what kills anything the child left behind.
        for handle in (self._launched.thread, self._launched.process, self._launched.job):
            _close(handle)
