"""Copy-before-write checkpointing.

Portable by construction. Because :class:`~minos.types.EffectContract` declares
its targets *before* the action runs, a checkpoint copies those named files
rather than snapshotting a volume — so there is no dependency on btrfs, APFS or
VSS, and Windows gets the same correctness as Linux.

Platform fast paths (``clonefile`` on APFS, ``FICLONE`` on btrfs/XFS, ReFS block
cloning) are optimisations behind :class:`CheckpointStore`. The baseline is a
plain copy and is always correct.

**Limit, by design:** only *declared* targets are protected. Undeclared writes
are detected by :class:`~minos.oracles.FileTreeOracle`, and when they occur the
broker halts rather than claiming a clean rollback.

**A target we cannot copy is a target we cannot protect.** Probing raises
:class:`UnprotectableTarget` rather than guessing, and the broker turns that
into a refusal. Silently treating an unreadable file as an absent one is how a
rollback deletes the thing it was asked to defend.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from .types import CheckpointId

__all__ = [
    "UNREADABLE",
    "CheckpointError",
    "CheckpointStore",
    "FileCheckpointStore",
    "RestoreResult",
    "TargetState",
    "UnprotectableTarget",
    "file_digest",
    "probe",
]

_CHUNK = 1024 * 1024
_MANIFEST_VERSION = 2

_DEFAULT_MAX_TARGET_BYTES = 1 << 30
"""1 GiB. A target larger than this is refused rather than silently copied."""

_FREE_SPACE_MARGIN = 64 << 20
"""Never fill the volume to the last byte; leave room to write the manifest."""

UNREADABLE = "<unreadable>"
"""Oracle marker for a path that exists but could not be read.

Distinct from ``None`` (absent) on purpose: a file locked by an application is
not a deleted file, and an oracle that conflates them reports a deletion that
never happened.
"""

TargetKind = Literal["file", "absent", "directory", "symlink"]


class CheckpointError(Exception):
    """Base for checkpointing failures."""


class UnprotectableTarget(CheckpointError):
    """This target cannot be copied, so the action must not proceed.

    Raised for unreadable files, sockets and devices, targets over the size
    limit, and insufficient free space. The broker records a ``failed`` outcome
    instead of running the action unprotected.
    """


def file_digest(path: Path) -> str | None:
    """SHA-256 of a file's contents.

    ``None`` means *absent or a directory* — :class:`~minos.oracles.PathExistsOracle`
    covers the directory case. :data:`UNREADABLE` means the path exists and could
    not be read; it is deliberately not ``None`` so that a locked file never
    reads as a deleted one.

    Oracles use this. Checkpointing uses :func:`probe`, which refuses rather
    than encoding uncertainty in a return value.
    """
    p = Path(path)
    try:
        if p.is_dir():
            return None
    except OSError:  # pragma: no cover - a path we cannot even stat
        return UNREADABLE
    try:
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            while chunk := fh.read(_CHUNK):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return None
    except OSError:
        # PermissionError (locked by another process on Windows), EIO, and
        # friends. It exists; we just cannot read it.
        return UNREADABLE


@dataclass(frozen=True, slots=True)
class TargetState:
    """What a target was at checkpoint time.

    ``kind`` is explicit because restoring depends on it: an absent target is
    removed, a directory is recreated, a symlink is relinked rather than being
    replaced by a copy of whatever it pointed at.
    """

    kind: TargetKind
    digest: str | None = None
    size: int = 0
    mode: int | None = None
    mtime: float | None = None
    link_target: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "digest": self.digest,
            "size": self.size,
            "mode": self.mode,
            "mtime": self.mtime,
            "link_target": self.link_target,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> TargetState:
        kind: TargetKind = raw.get("kind", "absent")
        return cls(
            kind=kind,
            digest=raw.get("digest"),
            size=int(raw.get("size") or 0),
            mode=raw.get("mode"),
            mtime=raw.get("mtime"),
            link_target=raw.get("link_target"),
        )


def probe(path: Path) -> TargetState:
    """Classify a target, or refuse it.

    Raises :class:`UnprotectableTarget` when the path exists but cannot be
    copied. That is the whole point: the caller must not be able to mistake
    "I could not read this" for "this was not there".
    """
    p = Path(path)
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return TargetState(kind="absent")
    except OSError as exc:
        raise UnprotectableTarget(f"cannot stat {p}: {exc}") from exc

    if stat.S_ISLNK(st.st_mode):
        try:
            return TargetState(
                kind="symlink",
                link_target=os.readlink(p),
                mode=st.st_mode,
                mtime=st.st_mtime,
            )
        except OSError as exc:
            raise UnprotectableTarget(f"cannot read symlink {p}: {exc}") from exc

    if stat.S_ISDIR(st.st_mode):
        return TargetState(kind="directory", mode=st.st_mode, mtime=st.st_mtime)

    if not stat.S_ISREG(st.st_mode):
        raise UnprotectableTarget(
            f"{p} is not a regular file (device, socket or FIFO); it cannot be checkpointed"
        )

    digest = _digest_or_raise(p)
    return TargetState(
        kind="file",
        digest=digest,
        size=st.st_size,
        mode=st.st_mode,
        mtime=st.st_mtime,
    )


def _digest_or_raise(path: Path) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while chunk := fh.read(_CHUNK):
                h.update(chunk)
        return h.hexdigest()
    except OSError as exc:
        raise UnprotectableTarget(
            f"cannot read {path}: {exc}. It may be open in another application. "
            "Refusing to act on a target that cannot be checkpointed."
        ) from exc


@dataclass(frozen=True, slots=True)
class RestoreResult:
    succeeded: bool
    restored: tuple[Path, ...] = ()
    failed: tuple[tuple[Path, str], ...] = ()
    detail: str = ""


class CheckpointStore(Protocol):
    def checkpoint(self, targets: tuple[Path, ...]) -> CheckpointId: ...
    def restore(self, checkpoint_id: CheckpointId) -> RestoreResult: ...
    def verify(self, checkpoint_id: CheckpointId) -> bool: ...
    def ensure_intact(self, checkpoint_id: CheckpointId) -> tuple[bool, tuple[str, ...]]: ...
    def unchanged_since(self, checkpoint_id: CheckpointId) -> tuple[bool, tuple[str, ...]]: ...


class FileCheckpointStore:
    """Content-addressed copy-before-write store.

    Layout::

        root/
          objects/<sha256>         file contents, deduplicated
          manifests/<id>.json      {"version": 2, "entries": {path: TargetState}}

    The manifest records *what the target was*, not merely its digest, so that
    restore can tell an absent file from a directory from a symlink. Version 1
    manifests (a flat ``{path: digest | null}``) are still readable.

    **The store holds plaintext copies of your files.** It is chmod 0700 on
    POSIX and inherits directory ACLs on Windows; it is not encrypted. See
    SECURITY.md.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        max_target_bytes: int = _DEFAULT_MAX_TARGET_BYTES,
        free_space_margin: int = _FREE_SPACE_MARGIN,
    ) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.manifests = self.root / "manifests"
        self.max_target_bytes = max_target_bytes
        self.free_space_margin = free_space_margin
        self.objects.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                self.root.chmod(0o700)

    # -- capture -----------------------------------------------------------

    def checkpoint(self, targets: tuple[Path, ...]) -> CheckpointId:
        """Copy every target, or raise and copy none.

        Probing happens for all targets before any bytes are written, so a
        refusal leaves the store untouched rather than half-populated.
        """
        probed: list[tuple[Path, TargetState]] = []
        for target in targets:
            resolved = _normalise(target)
            state = probe(resolved)
            if state.kind == "file" and state.size > self.max_target_bytes:
                raise UnprotectableTarget(
                    f"{resolved} is {state.size} bytes, over the "
                    f"{self.max_target_bytes}-byte checkpoint limit. "
                    "Raise max_target_bytes to protect it, or narrow the action."
                )
            probed.append((resolved, state))

        self._require_free_space(probed)

        entries: dict[str, dict[str, Any]] = {}
        for resolved, state in probed:
            if state.kind == "file" and state.digest is not None:
                self._store_object(resolved, state.digest)
            entries[str(resolved)] = state.to_json()

        checkpoint_id = uuid.uuid4().hex
        payload = json.dumps({"version": _MANIFEST_VERSION, "entries": entries}, indent=2)
        _atomic_write_text(self._manifest_path(checkpoint_id), payload)
        return checkpoint_id

    def _require_free_space(self, probed: Iterable[tuple[Path, TargetState]]) -> None:
        needed = sum(
            state.size
            for _, state in probed
            if state.kind == "file"
            and state.digest is not None
            and not (self.objects / state.digest).exists()
        )
        if needed == 0:
            return
        try:
            free = shutil.disk_usage(self.root).free
        except OSError:  # pragma: no cover - unusual filesystems
            return
        if free < needed + self.free_space_margin:
            raise UnprotectableTarget(
                f"checkpoint needs {needed} bytes but only {free} are free on "
                f"{self.root}. Free up space rather than acting unprotected."
            )

    # -- restore -----------------------------------------------------------

    def restore(self, checkpoint_id: CheckpointId) -> RestoreResult:
        """Put the declared targets back, or report exactly what did not go back.

        Object availability is checked for every entry *before* the first write,
        so the common partial-restore case — a pruned or corrupted object found
        halfway through — leaves the filesystem untouched instead of mixed.
        """
        entries = self._entries(checkpoint_id)

        intact, missing = self.ensure_intact(checkpoint_id)
        if not intact:
            return RestoreResult(
                succeeded=False,
                failed=tuple((Path(p), "missing checkpoint object") for p in missing),
                detail=(
                    f"{len(missing)} checkpoint object(s) are missing; refusing a "
                    "partial restore. Nothing was changed."
                ),
            )

        restored: list[Path] = []
        failed: list[tuple[Path, str]] = []
        for raw_path, state in entries.items():
            path = Path(raw_path)
            try:
                self._restore_one(path, state)
                restored.append(path)
            except OSError as exc:
                failed.append((path, str(exc)))

        ok = not failed
        return RestoreResult(
            succeeded=ok,
            restored=tuple(restored),
            failed=tuple(failed),
            detail="" if ok else f"{len(failed)} target(s) could not be restored",
        )

    def _restore_one(self, path: Path, state: TargetState) -> None:
        if state.kind == "absent":
            _remove(path)
            return

        if state.kind == "symlink":
            _remove(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(state.link_target or "", path)
            return

        if state.kind == "directory":
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                _remove(path)
            path.mkdir(parents=True, exist_ok=True)
            _restore_metadata(path, state)
            return

        # A regular file.
        if path.is_symlink() or path.is_dir():
            _remove(path)
        obj = self.objects / (state.digest or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_copy(obj, path)
        _restore_metadata(path, state)

    # -- inspection --------------------------------------------------------

    def verify(self, checkpoint_id: CheckpointId) -> bool:
        """True when on-disk state matches the checkpoint exactly.

        Called *after* a restore. A restore that reports success but does not
        verify is the failure mode this whole component exists to prevent.
        """
        return not self._drift(checkpoint_id)

    def ensure_intact(self, checkpoint_id: CheckpointId) -> tuple[bool, tuple[str, ...]]:
        """Are the stored objects still present? Check this *before* acting.

        Discovering a pruned object at rollback time is discovering it at the
        one moment nothing can be done about it.
        """
        missing = [
            raw_path
            for raw_path, state in self._entries(checkpoint_id).items()
            if state.kind == "file"
            and state.digest is not None
            and not (self.objects / state.digest).exists()
        ]
        return (not missing, tuple(missing))

    def unchanged_since(self, checkpoint_id: CheckpointId) -> tuple[bool, tuple[str, ...]]:
        """Have the targets drifted since the checkpoint was taken?

        Closes the window between "we copied it" and "we acted on it". A target
        someone else edited in that gap would otherwise be silently rolled back
        to a state that was never the user's.
        """
        drift = self._drift(checkpoint_id)
        return (not drift, drift)

    def _drift(self, checkpoint_id: CheckpointId) -> tuple[str, ...]:
        drifted: list[str] = []
        for raw_path, state in self._entries(checkpoint_id).items():
            try:
                current = probe(Path(raw_path))
            except UnprotectableTarget:
                drifted.append(raw_path)
                continue
            if not _same_state(current, state):
                drifted.append(raw_path)
        return tuple(drifted)

    # -- lifecycle ---------------------------------------------------------

    def forget(self, checkpoint_id: CheckpointId) -> None:
        """Drop a checkpoint. Objects survive until :meth:`gc`."""
        with contextlib.suppress(FileNotFoundError):
            self._manifest_path(checkpoint_id).unlink()

    def gc(self) -> int:
        """Delete objects no live manifest references. Returns how many went.

        Without this the store grows without bound, which on a laptop is a
        failure mode as real as any bug.
        """
        referenced: set[str] = set()
        for manifest in self.manifests.glob("*.json"):
            with contextlib.suppress(OSError, ValueError):
                for state in _parse_manifest(manifest.read_text(encoding="utf-8")).values():
                    if state.digest is not None:
                        referenced.add(state.digest)
        removed = 0
        for obj in self.objects.iterdir():
            if obj.name not in referenced:
                with contextlib.suppress(OSError):
                    obj.unlink()
                    removed += 1
        return removed

    def prune(self, *, max_age_days: float = 7.0, max_bytes: int = 2 << 30) -> tuple[int, int]:
        """Apply the retention policy: age first, then size. Returns (manifests, objects).

        Checkpoints are what ``minos undo`` offers, so pruning is deliberately
        conservative and deliberately explicit — an unbounded store is a real
        failure mode on a laptop, and silently keeping everything is not the
        safer option it appears to be.

        Age wins first because a week-old checkpoint is rarely what anyone wants
        back. If the store is still over budget after that, the oldest survivors
        go until it fits.
        """
        now = time.time()
        cutoff = now - max_age_days * 86_400
        manifests = sorted(self.manifests.glob("*.json"), key=_mtime)

        dropped = 0
        survivors: list[Path] = []
        for manifest in manifests:
            if _mtime(manifest) < cutoff:
                with contextlib.suppress(OSError):
                    manifest.unlink()
                    dropped += 1
            else:
                survivors.append(manifest)

        objects = self.gc()

        while survivors and self.usage_bytes() > max_bytes:
            oldest = survivors.pop(0)
            with contextlib.suppress(OSError):
                oldest.unlink()
                dropped += 1
            objects += self.gc()

        return dropped, objects

    def usage_bytes(self) -> int:
        """Total size of the object store, for reporting and retention policy."""
        total = 0
        for obj in self.objects.iterdir():
            with contextlib.suppress(OSError):
                total += obj.stat().st_size
        return total

    # -- internals ---------------------------------------------------------

    def _manifest_path(self, checkpoint_id: CheckpointId) -> Path:
        return self.manifests / f"{checkpoint_id}.json"

    def _entries(self, checkpoint_id: CheckpointId) -> dict[str, TargetState]:
        path = self._manifest_path(checkpoint_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise KeyError(f"unknown checkpoint {checkpoint_id!r}") from exc
        return _parse_manifest(raw)

    def _store_object(self, source: Path, digest: str) -> None:
        obj = self.objects / digest
        if obj.exists():
            return  # content-addressed: identical bytes are stored once
        if _try_clone(source, obj):
            return
        _atomic_copy(source, obj)


def _parse_manifest(raw: str) -> dict[str, TargetState]:
    data: dict[str, Any] = json.loads(raw)
    if data.get("version") == _MANIFEST_VERSION:
        entries: dict[str, Any] = data.get("entries", {})
        return {path: TargetState.from_json(state) for path, state in entries.items()}
    # Version 1: {path: digest | null}, where null meant "absent" and could not
    # express a directory, a symlink, or a file we failed to read.
    return {
        path: TargetState(kind="absent") if digest is None else TargetState("file", digest=digest)
        for path, digest in data.items()
    }


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:  # pragma: no cover - raced with another prune
        return 0.0


def _same_state(current: TargetState, recorded: TargetState) -> bool:
    """Is the target still what the checkpoint recorded?

    Kind first: a path that was a file and is now a directory has drifted even
    if no digest is involved. Metadata is deliberately *not* compared — a mode
    or mtime change is not a content change, and treating it as drift would
    refuse actions for no reason.
    """
    if current.kind != recorded.kind:
        return False
    if current.kind == "file":
        return current.digest == recorded.digest
    if current.kind == "symlink":
        return current.link_target == recorded.link_target
    return True


def _normalise(target: Path | str) -> Path:
    """Absolute path with symlinks resolved in the *parent* chain only.

    Resolving the final component would make a symlink indistinguishable from
    the file it points at, and restore would replace the link with a copy.
    """
    p = Path(target).expanduser()
    if not p.name:
        return p.absolute()
    try:
        parent = p.parent.resolve()
    except OSError:  # pragma: no cover - unreachable parents
        parent = p.parent.absolute()
    return parent / p.name


def _remove(path: Path) -> None:
    """Remove whatever is at ``path``, if anything."""
    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    if path.is_dir():
        os.rmdir(path)  # deliberately not rmtree: only declared targets may go
        return
    path.unlink()


def _restore_metadata(path: Path, state: TargetState) -> None:
    """Put back permissions and mtime.

    mtime matters beyond tidiness: the memory index keys on it, so a restore
    that left a fresh mtime would make every rollback look like an edit.
    """
    if state.mode is not None:
        with contextlib.suppress(OSError, NotImplementedError):
            path.chmod(stat.S_IMODE(state.mode))
    if state.mtime is not None:
        with contextlib.suppress(OSError, NotImplementedError):
            os.utime(path, (state.mtime, state.mtime))


def _atomic_write_text(dest: Path, text: str) -> None:
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def _atomic_copy(source: Path, dest: Path) -> None:
    """Copy via a temp file in the destination directory, then replace.

    ``os.replace`` is atomic on POSIX and on Windows, so a crash mid-restore
    cannot leave a half-written target.
    """
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def _try_clone(source: Path, dest: Path) -> bool:
    """Best-effort copy-on-write clone. Returns False to fall back to a copy.

    macOS/APFS: ``clonefile``. Linux btrfs/XFS: ``FICLONE``. Both make the
    checkpoint nearly free in time and space; neither is required for
    correctness.
    """
    try:
        if sys.platform == "darwin":
            import ctypes
            import ctypes.util

            libc_name = ctypes.util.find_library("c")
            if not libc_name:
                return False
            libc = ctypes.CDLL(libc_name, use_errno=True)
            if not hasattr(libc, "clonefile"):
                return False
            rc: int = libc.clonefile(str(source).encode(), str(dest).encode(), ctypes.c_int(0))
            return rc == 0

        if sys.platform.startswith("linux"):
            import fcntl

            FICLONE = 0x40049409
            with open(source, "rb") as src, open(dest, "wb") as dst:
                fcntl.ioctl(dst.fileno(), FICLONE, src.fileno())
            return True
    except (OSError, AttributeError, ImportError, ValueError):
        # Cross-filesystem, unsupported fs, or no permission. Clean up any
        # partial destination and fall back to a plain copy.
        with contextlib.suppress(OSError):
            if dest.exists():
                dest.unlink()
        return False
    return False
