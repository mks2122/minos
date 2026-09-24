"""Restrictions the sandboxed child applies to *itself*, before user code runs.

This module is copied into the workspace and imported by the runner, so it must
stand alone: no imports from ``minos``, no third-party imports, standard library
only. It is also imported directly by the tests, which is why it is a real
module rather than another string constant.

**Why in-process rather than in ``preexec_fn``.** Landlock and seccomp are
inherited across ``exec`` and cannot be undone, so applying them at the top of
the child is exactly as binding as applying them after ``fork`` -- and it runs
as ordinary Python rather than in the async-signal-unsafe window between fork
and exec. The only things that genuinely cannot be done from inside the child
are the ones Windows needs (an integrity-lowered token, a Job Object); those
live in :mod:`minos.sandbox.confine` and happen in the parent.

Three layers, and they are not of equal strength. Read them in this order:

1. **Kernel confinement** (``_landlock``, ``_seccomp``) -- Linux only, and the
   only layer here that a hostile script cannot argue with. Landlock decides
   which paths exist as far as this process is concerned; seccomp makes
   ``socket(2)`` fail at the syscall boundary, which is what finally closes the
   ``ctypes`` hole the rest of this module can only inconvenience.
2. **Resource limits** (``_limit_resources``) -- POSIX. Self-imposed, but
   ``RLIMIT_AS`` is not raisable again once lowered by an unprivileged process,
   so it holds.
3. **Interpreter-level hardening** (``_block_network``, ``_block_modules``,
   ``_restrict_filesystem``) -- every platform, and **bypassable in principle**
   by anything that reaches libc directly. It is there to turn a script that
   wanders off into a clear error instead of a surprise, and on Linux it sits
   behind layer 1 rather than instead of it.

Where a layer is unavailable it says so in the report rather than failing
quietly: :func:`apply` returns what it actually managed to enforce, the runner
prints it under ``MINOS_CONFINEMENT`` when asked, and the parent reads it back.
A control that is absent and silent is worse than one that is absent.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import NoReturn, cast

__all__ = ["Applied", "apply", "seccomp_program"]


class Applied:
    """What was actually enforced, as opposed to what was requested.

    A plain class with a ``list`` inside rather than a dataclass, because this
    module is read by an interpreter started with ``-E -s`` in a directory that
    is not a package and the fewer moving parts the better.
    """

    def __init__(self) -> None:
        self.enforced: list[str] = []
        self.skipped: list[str] = []

    def ok(self, name: str) -> None:
        self.enforced.append(name)

    def no(self, name: str, why: str) -> None:
        self.skipped.append(f"{name} ({why})")

    def render(self) -> str:
        parts = []
        if self.enforced:
            parts.append("enforced: " + ", ".join(self.enforced))
        if self.skipped:
            parts.append("unavailable: " + ", ".join(self.skipped))
        return "; ".join(parts) or "nothing enforced"


# -- layer 1: Linux kernel confinement --------------------------------------

_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1

# Access bits, in the order the ABI grew them. Handling a bit the running kernel
# does not know about makes landlock_create_ruleset fail with EINVAL, so the set
# is masked by the ABI version the kernel reports.
_FS_BITS_ABI1 = (1 << 13) - 1  # EXECUTE .. MAKE_SYM
_FS_REFER = 1 << 13  # ABI 2
_FS_TRUNCATE = 1 << 14  # ABI 3
_FS_IOCTL_DEV = 1 << 15  # ABI 5

_FS_EXECUTE = 1 << 0
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3

_NET_BIND_TCP = 1 << 0  # ABI 4
_NET_CONNECT_TCP = 1 << 1


def landlock_abi() -> int:
    """The Landlock ABI version this kernel supports, or 0 for none.

    A read-only query -- it creates no ruleset and changes nothing -- so the
    parent can call it to decide what to promise before any child exists.
    """
    if not sys.platform.startswith("linux"):
        return 0
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        version = libc.syscall(
            ctypes.c_long(_LANDLOCK_CREATE_RULESET),
            None,
            ctypes.c_size_t(0),
            ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
        )
    except Exception:
        return 0
    return int(version) if version and version > 0 else 0


def _landlock(read_write: list[str], read_only: list[str], report: Applied) -> None:
    """Confine this process to a path allow-list, for the rest of its life.

    Everything outside the two lists stops existing: not unreadable, *absent*.
    That is the property the subprocess jail never had, and the reason the
    "cwd is a convention, not a jail" line in SECURITY.md can finally come down
    on Linux.
    """
    abi = landlock_abi()
    if abi <= 0:
        report.no("landlock", "kernel too old or landlock disabled")
        return

    import ctypes

    handled_fs = _FS_BITS_ABI1
    if abi >= 2:
        handled_fs |= _FS_REFER
    if abi >= 3:
        handled_fs |= _FS_TRUNCATE
    if abi >= 5:
        handled_fs |= _FS_IOCTL_DEV
    handled_net = (_NET_BIND_TCP | _NET_CONNECT_TCP) if abi >= 4 else 0

    class RulesetAttr(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]

    class PathBeneathAttr(ctypes.Structure):
        # Packed: the kernel struct is __attribute__((packed)) and ctypes will
        # otherwise pad parent_fd to an 8-byte boundary and pass a struct the
        # kernel reads as garbage.
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    libc = ctypes.CDLL(None, use_errno=True)

    attr = RulesetAttr(handled_fs, handled_net)
    size = ctypes.sizeof(RulesetAttr) if abi >= 4 else 8
    ruleset = libc.syscall(
        ctypes.c_long(_LANDLOCK_CREATE_RULESET),
        ctypes.byref(attr),
        ctypes.c_size_t(size),
        ctypes.c_uint32(0),
    )
    if ruleset < 0:
        report.no("landlock", f"create_ruleset errno {ctypes.get_errno()}")
        return

    readable = handled_fs & (_FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR | _FS_IOCTL_DEV)
    try:
        for paths, access in ((read_write, handled_fs), (read_only, readable)):
            for path in paths:
                try:
                    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)  # type: ignore[attr-defined,unused-ignore]
                except OSError:
                    # A path that is not there needs no rule. Granting access to
                    # a directory that does not exist is not a thing anyway.
                    continue
                try:
                    rule = PathBeneathAttr(access, fd)
                    if (
                        libc.syscall(
                            ctypes.c_long(_LANDLOCK_ADD_RULE),
                            ctypes.c_int(ruleset),
                            ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
                            ctypes.byref(rule),
                            ctypes.c_uint32(0),
                        )
                        < 0
                    ):
                        report.no("landlock", f"add_rule {path} errno {ctypes.get_errno()}")
                        return
                finally:
                    os.close(fd)

        # No new privileges first: the kernel refuses restrict_self without it,
        # and it is independently worth having -- it makes setuid binaries stop
        # being an escape hatch for everything below.
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            report.no("landlock", "PR_SET_NO_NEW_PRIVS refused")
            return
        if (
            libc.syscall(
                ctypes.c_long(_LANDLOCK_RESTRICT_SELF), ctypes.c_int(ruleset), ctypes.c_uint32(0)
            )
            < 0
        ):
            report.no("landlock", f"restrict_self errno {ctypes.get_errno()}")
            return
    finally:
        os.close(ruleset)

    report.ok(f"landlock abi{abi}" + (" +net" if handled_net else ""))


# Blocked syscalls, per architecture. socket(2) alone would do -- everything
# else needs a socket first -- but a filter that also refuses connect and the
# send/recv family fails closer to the mistake and reads as intent.
_SECCOMP_BLOCKED = {
    "x86_64": (41, 42, 49, 50, 43, 288, 44, 45, 46, 47, 53, 101),
    "aarch64": (198, 203, 200, 201, 202, 242, 206, 207, 211, 212, 199, 117),
}
_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}


def seccomp_arch() -> str:
    """The machine name this module has a syscall table for, or ``""``."""
    if not sys.platform.startswith("linux"):
        return ""
    machine = os.uname().machine
    machine = "x86_64" if machine in ("x86_64", "amd64") else machine
    machine = "aarch64" if machine in ("aarch64", "arm64") else machine
    return machine if machine in _SECCOMP_BLOCKED else ""


# BPF opcodes and seccomp return values, named so the program below reads.
_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_RET_K = 0x06
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO_EPERM = 0x00050000 | 1
_SECCOMP_RET_KILL_PROCESS = 0x00000000

Instruction = tuple[int, int, int, int]
"""One ``struct sock_filter``: (code, jt, jf, k)."""


def seccomp_program(arch: str) -> list[Instruction]:
    """Build the BPF filter as plain tuples, so it can be checked anywhere.

    Deliberately separated from the syscall that installs it. A filter with a
    wrong jump offset does not crash -- it **allows everything**, silently, on
    the one platform where this is the load-bearing layer. Keeping the arithmetic
    in a pure function means the offsets are tested on every machine that runs
    the suite, not only on a Linux box with the right kernel.

    Layout::

        0   load  arch
        1   jeq   AUDIT_ARCH   -> fall through, else jump to KILL
        2   load  nr
        3.. jeq   <blocked>    -> jump to EPERM, else fall through
        n   ret   ALLOW
        n+1 ret   ERRNO(EPERM)
        n+2 ret   KILL_PROCESS
    """
    blocked = _SECCOMP_BLOCKED[arch]
    deny_at = 3 + len(blocked)  # the ALLOW sits here, EPERM one after
    kill_at = deny_at + 2
    program: list[Instruction] = [
        (_BPF_LD_W_ABS, 0, 0, 4),  # seccomp_data.arch
        # A BPF jump is relative to the *next* instruction, so reaching index
        # kill_at from index 1 is an offset of kill_at - 2. Getting this wrong
        # does not crash: it points past the end, and a filter the kernel
        # rejects is a filter that never applies.
        (_BPF_JEQ_K, 0, kill_at - 2, _AUDIT_ARCH[arch]),  # wrong arch -> kill
        (_BPF_LD_W_ABS, 0, 0, 0),  # seccomp_data.nr
    ]
    for offset, number in enumerate(blocked):
        index = 3 + offset
        program.append((_BPF_JEQ_K, deny_at + 1 - index - 1, 0, number))
    program.append((_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW))
    program.append((_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO_EPERM))
    program.append((_BPF_RET_K, 0, 0, _SECCOMP_RET_KILL_PROCESS))
    return program


def _seccomp(report: Applied) -> None:
    """Refuse the socket syscalls in the kernel, not in the ``socket`` module.

    This is the layer that ``ctypes`` cannot walk around, and therefore the only
    reason the network claim in SECURITY.md is a claim rather than a hope. The
    filter returns EPERM rather than killing the process, so a script that tries
    gets a normal Python exception naming the sandbox instead of a mystery
    SIGSYS with no traceback.
    """
    arch = seccomp_arch()
    if not arch:
        report.no("seccomp", "no syscall table for this architecture")
        return

    import ctypes

    class SockFilter(ctypes.Structure):
        _fields_ = [
            ("code", ctypes.c_uint16),
            ("jt", ctypes.c_uint8),
            ("jf", ctypes.c_uint8),
            ("k", ctypes.c_uint32),
        ]

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(SockFilter))]

    program = seccomp_program(arch)
    array = (SockFilter * len(program))(*(SockFilter(*line) for line in program))
    fprog = SockFprog(len(program), array)

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS, required first
        report.no("seccomp", "PR_SET_NO_NEW_PRIVS refused")
        return
    if libc.prctl(22, 2, ctypes.byref(fprog), 0, 0) != 0:  # PR_SET_SECCOMP, FILTER
        report.no("seccomp", f"PR_SET_SECCOMP errno {ctypes.get_errno()}")
        return
    report.ok(f"seccomp ({len(_SECCOMP_BLOCKED[arch])} syscalls, {arch})")


# -- layer 2: resource limits ------------------------------------------------


def _limit_resources(max_memory_bytes: int, report: Applied) -> None:
    """POSIX only. Windows gets the equivalent from a Job Object in the parent."""
    try:
        import resource
    except ImportError:
        report.no("rlimits", "no resource module on this platform")
        return

    done = []
    if max_memory_bytes > 0:
        try:
            resource.setrlimit(  # type: ignore[attr-defined,unused-ignore]
                resource.RLIMIT_AS,  # type: ignore[attr-defined,unused-ignore]
                (max_memory_bytes, max_memory_bytes),
            )
            done.append("RLIMIT_AS")
        except (ValueError, OSError):
            pass
    for name, limit in (("RLIMIT_NPROC", 64), ("RLIMIT_NOFILE", 256), ("RLIMIT_CORE", 0)):
        which = getattr(resource, name, None)
        if which is None:
            continue
        try:
            resource.setrlimit(which, (limit, limit))  # type: ignore[attr-defined,unused-ignore]
            done.append(name)
        except (ValueError, OSError):
            pass
    if done:
        report.ok("rlimits: " + "+".join(done))
    else:
        report.no("rlimits", "all refused")


# -- layer 3: interpreter-level hardening ------------------------------------

_NETWORK_MESSAGE = (
    "network access is disabled in the minos sandbox. "
    "Declare the data you need as a material instead."
)

STRICT_IMPORTS = frozenset(
    {
        # Reaches libc directly, which is how everything else here gets reached
        # once it is gone.
        "ctypes",
        "_ctypes",
        # Spawning is the whole escape: a child process inherits none of the
        # in-process patching below.
        "subprocess",
        "multiprocessing",
        "_posixsubprocess",
        "pty",
        # The C accelerator behind socket; patching the Python wrapper while
        # leaving this importable would be theatre.
        "_socket",
    }
)
"""A ready-made denylist for a caller who wants imports refused outright.

**Not the default, and the reason is a measurement rather than a preference.**
Refusing these at import breaks the sandbox's own package set: ``pypdf`` imports
``subprocess`` at module scope, ``typing_extensions`` imports ``_socket``, and
``numpy`` wants ``ctypes``. None of them *use* those modules to leave the
workspace -- they import them for a code path that will not be taken -- so a
denylist trades a large amount of real capability for a nicer error message.

What blocks the escape instead is the audit hook below, which refuses the
**calls**: ``subprocess.Popen``, ``os.system``, ``ctypes.dlopen``. That is the
stronger layer anyway, because an audit hook cannot be removed once installed
and an import guard can simply be stepped around with ``importlib``.

Pass it as ``blocked_imports`` when the extra brittleness is worth it -- an
untrusted origin with no container engine available, say. The honest answer
there is a container; see :mod:`minos.sandbox.origin`.
"""


class _Blocked(OSError):
    """Raised by every refusal here, so one except clause catches the sandbox."""


class _ModuleGuard:
    """A meta-path finder that refuses a denylist before the real finders run.

    Deliberately not an ``import`` audit hook: this produces an ImportError
    naming the sandbox at the import statement, which is a readable failure for
    a model rewriting its own script, where an audit refusal deep inside the
    import machinery is not.

    Empty unless a caller asks for :data:`STRICT_IMPORTS`. See its docstring for
    why the default is to let the import through and refuse the call.
    """

    def __init__(self, blocked: frozenset[str]) -> None:
        self.blocked = blocked

    def find_module(  # pragma: no cover - legacy protocol
        self, fullname: str, path: object = None
    ) -> None:
        return None

    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        root = fullname.partition(".")[0]
        if root in self.blocked or fullname in self.blocked:
            raise _Blocked(
                f"{fullname!r} is not importable in the minos sandbox: it can reach "
                "outside the workspace. See SECURITY.md."
            )
        return None


def _block_modules(blocked: frozenset[str], report: Applied) -> None:
    if not blocked:
        return
    for name in list(sys.modules):
        root = name.partition(".")[0]
        if root in blocked:
            # Already imported by the interpreter's own startup: drop the
            # reference so `import x` goes through the guard rather than finding
            # a cached module object.
            del sys.modules[name]
    sys.meta_path.insert(0, _ModuleGuard(blocked))
    report.ok(f"import denylist ({len(blocked)} modules)")


def _block_network(report: Applied) -> None:
    """Make network access fail loudly, before the script's first line.

    Defeats every ordinary client library. On Linux it is redundant behind
    seccomp and kept anyway, because the error it produces names the sandbox and
    EPERM from a syscall filter does not.
    """
    import socket

    def refuse(*args: object, **kwargs: object) -> NoReturn:
        raise _Blocked(_NETWORK_MESSAGE)

    # A subclass, not a function: ssl does `class SSLSocket(socket.socket)` at
    # import time, and replacing the name with a function turns a clear refusal
    # into a baffling TypeError from deep inside the stdlib.
    class BlockedSocket(socket.socket):
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise _Blocked(_NETWORK_MESSAGE)

    socket.socket = BlockedSocket  # type: ignore[misc]
    socket.create_connection = refuse
    socket.create_server = refuse
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "socketpair"):
        if hasattr(socket, name):
            setattr(socket, name, refuse)
    report.ok("socket module patched")


def _audit_hook(blocked_events: tuple[str, ...]) -> Callable[[str, tuple[object, ...]], None]:
    def hook(event: str, args: tuple[object, ...]) -> None:  # pragma: no cover - in the child
        if event in blocked_events:
            raise _Blocked(f"{event} is not permitted in the minos sandbox. See SECURITY.md.")

    return hook


_BLOCKED_EVENTS = (
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "os.fork",
    "os.forkpty",
    "subprocess.Popen",
    "socket.connect",
    "socket.bind",
    "socket.getaddrinfo",
)
"""Calls refused on every run. Cheap, and nothing legitimate in the sandbox's
package set makes them."""

_CTYPES_EVENTS = (
    # dlopen and dlsym are where a function pointer into libc is obtained; cdata
    # is where one is forged from an integer. Refusing all three is the only way
    # to stop `import ctypes` being equivalent to having no in-process sandbox.
    "ctypes.dlopen",
    "ctypes.dlsym",
    "ctypes.dlsym/handle",
    "ctypes.cdata",
    "ctypes.call_function",
)
"""Refused only when the caller asks, and the reason is a measurement.

Blocking ``ctypes.dlopen`` breaks **numpy, pandas and openpyxl**, which load
their own extension libraries through it, and on Windows it breaks ``import
ctypes`` itself -- the module binds ``kernel32`` at import. That is most of the
sandbox's usefulness traded for a layer that a determined script walks around
anyway by other means.

So the honest arrangement is the one the module docstring describes: ctypes is
contained by the **kernel** layer, not by this one. On Linux seccomp refuses the
socket syscalls whatever ctypes does, and Landlock refuses the paths. On Windows
the low-integrity token refuses the writes. Where that is not enough -- code
whose origin you do not trust -- the answer is a container, not a longer denylist.

Turn it on with ``SubprocessSandbox(confine_ctypes=True)`` for a script you know
needs no compiled packages.
"""


def _install_audit_hook(ctypes_too: bool, report: Applied) -> None:
    """Refuse the calls, not just the imports.

    An audit hook cannot be removed once added, which makes this the most
    durable of the three interpreter-level layers -- a script that re-imports a
    blocked module through a path the guard missed still cannot *call* it.
    """
    events = _BLOCKED_EVENTS + (_CTYPES_EVENTS if ctypes_too else ())
    sys.addaudithook(_audit_hook(events))
    report.ok(f"audit hook ({len(events)} events)")
    if not ctypes_too:
        report.no("ctypes refusal", "off by default; it breaks numpy and pandas")


def _restrict_filesystem(write_roots: list[str], read_roots: list[str], report: Applied) -> None:
    """Refuse opens outside the workspace, via the ``open`` audit event.

    On Linux this duplicates Landlock and is kept for the error message. On
    Windows and macOS it is the only thing standing between a script and the
    user's home directory, and it is **advisory**: anything that reaches the
    platform's file API without going through CPython walks around it. That is
    the honest description, and it is why ``ctypes`` is on the denylist above.
    """

    # realpath, not abspath, on both sides. The interpreter's own prefix is very
    # often reached through a symlink or a directory junction -- uv's Python
    # installs are, and so is /tmp on macOS -- and comparing a resolved path
    # against an unresolved root refuses the stdlib. This is the same rule the
    # capability matcher uses, for the same reason.
    def normalise(paths: list[str]) -> tuple[str, ...]:
        return tuple(os.path.normcase(os.path.realpath(p)) for p in paths if p)

    writable = normalise(write_roots)
    readable = writable + normalise(read_roots)

    def under(path: str, roots: tuple[str, ...]) -> bool:
        try:
            resolved = os.path.normcase(os.path.realpath(path))
        except (OSError, ValueError):
            return False
        return any(resolved == root or resolved.startswith(root + os.sep) for root in roots)

    def hook(event: str, args: tuple[object, ...]) -> None:  # pragma: no cover - in the child
        if event in _LISTING_EVENTS:
            # Listing a directory is a read of its contents. Without this a
            # script could not open ~/.aws/credentials but could learn that it
            # exists, and walk the whole home tree to find out what else does.
            target = args[0] if args else None
            if target is None or isinstance(target, int):
                target = "."
            listed = os.fsdecode(os.fspath(cast("str | bytes | os.PathLike[str]", target)))
            if not under(listed, readable):
                raise _Blocked(
                    f"{listed!r} cannot be listed from the minos sandbox: it is outside "
                    "the workspace. See SECURITY.md."
                )
            return
        if event != "open":
            return
        raw, mode, raw_flags = args
        if raw is None or isinstance(raw, int):
            return
        path = os.fsdecode(os.fspath(cast("str | bytes | os.PathLike[str]", raw)))
        flags = int(cast(int, raw_flags))
        wants_write = bool(mode and any(c in str(mode) for c in "wxa+")) or bool(
            flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC)
        )
        roots = writable if wants_write else readable
        if not under(path, roots):
            verb = "written" if wants_write else "read"
            raise _Blocked(
                f"{path!r} cannot be {verb} from the minos sandbox: it is outside the "
                "workspace. Declare it as a material instead. See SECURITY.md."
            )

    sys.addaudithook(hook)
    report.ok("filesystem allow-list")


_LISTING_EVENTS = frozenset({"os.listdir", "os.scandir", "glob.glob"})
"""Audit events that reveal what a directory holds. ``os.stat`` raises none, so
a script can still ask whether one named path exists; SECURITY.md says so."""


# -- entry point -------------------------------------------------------------


def read_roots(workspace_root: str) -> list[str]:
    """Paths the interpreter genuinely needs in order to keep working.

    Anything not on this list or the write list does not exist, so it is worth
    being precise: the interpreter's own prefixes, whatever is already on
    ``sys.path`` (the stdlib and site-packages, chosen by the parent and not by
    the script), and nothing else. Notably absent: the user's home directory.
    """
    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}
    roots.update(p for p in sys.path if p)
    roots.discard("")
    return sorted(os.path.abspath(p) for p in roots if p)


def apply(
    workspace_root: str,
    max_memory_bytes: int = 0,
    *,
    kernel: bool = True,
    network: bool = True,
    modules: bool = True,
    filesystem: bool = True,
    ctypes_calls: bool = False,
    blocked_imports: frozenset[str] | tuple[str, ...] = (),
) -> Applied:
    """Apply every layer this platform supports and report what actually held.

    Order matters. The filesystem allow-list is installed last because the
    layers above it open files -- the interpreter's own, and the ones Landlock
    needs a descriptor for -- and installing it first would refuse them.
    """
    report = Applied()
    workspace_root = os.path.abspath(workspace_root)
    tmp = os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or ""
    writable = [workspace_root] + ([os.path.abspath(tmp)] if tmp else [])
    readable = read_roots(workspace_root)

    if max_memory_bytes:
        _limit_resources(max_memory_bytes, report)
    if kernel and sys.platform.startswith("linux"):
        _landlock(writable, readable, report)
        if network:
            _seccomp(report)
    elif kernel:
        report.no("kernel confinement", f"not implemented in-process on {sys.platform}")
    if network:
        _block_network(report)
    if modules:
        _block_modules(frozenset(blocked_imports), report)
        _install_audit_hook(ctypes_calls, report)
    if filesystem:
        _restrict_filesystem(writable, readable, report)
    return report
