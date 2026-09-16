"""Copy-before-write checkpointing.

Portable by construction. Because :class:`~writ.types.EffectContract` declares
its targets *before* the action runs, a checkpoint copies those named files
rather than snapshotting a volume — so there is no dependency on btrfs, APFS or
VSS, and Windows gets the same correctness as Linux.

Platform fast paths (``clonefile`` on APFS, ``FICLONE`` on btrfs/XFS, ReFS block
cloning) are optimisations behind :class:`CheckpointStore`. The baseline is a
plain copy and is always correct.

**Limit, by design:** only *declared* targets are protected. Undeclared writes
are detected by :class:`~writ.oracles.FileTreeOracle`, and when they occur the
broker halts rather than claiming a clean rollback.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .types import CheckpointId

__all__ = ["CheckpointStore", "FileCheckpointStore", "RestoreResult", "file_digest"]

_CHUNK = 1024 * 1024


def file_digest(path: Path) -> str | None:
    """SHA-256 of a file, or ``None`` if it does not exist."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while chunk := fh.read(_CHUNK):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        return None
    except PermissionError:
        return None


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


class FileCheckpointStore:
    """Content-addressed copy-before-write store.

    Layout::

        root/
          objects/<sha256>         file contents, deduplicated
          manifests/<id>.json      {path: sha256 | null}

    A ``null`` digest records "this file did not exist", so restore can delete a
    file the action created. That case is easy to forget and is why the manifest
    stores absence explicitly rather than omitting the entry.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.manifests = self.root / "manifests"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)

    def checkpoint(self, targets: tuple[Path, ...]) -> CheckpointId:
        checkpoint_id = uuid.uuid4().hex
        manifest: dict[str, str | None] = {}
        for target in targets:
            resolved = Path(target).expanduser().resolve()
            digest = file_digest(resolved)
            manifest[str(resolved)] = digest
            if digest is not None:
                self._store_object(resolved, digest)
        self._manifest_path(checkpoint_id).write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        return checkpoint_id

    def restore(self, checkpoint_id: CheckpointId) -> RestoreResult:
        manifest = self._read_manifest(checkpoint_id)
        restored: list[Path] = []
        failed: list[tuple[Path, str]] = []

        for raw_path, digest in manifest.items():
            path = Path(raw_path)
            try:
                if digest is None:
                    # The file did not exist at checkpoint time; remove it again.
                    if path.exists():
                        path.unlink()
                    restored.append(path)
                    continue
                obj = self.objects / digest
                if not obj.exists():
                    failed.append((path, f"missing object {digest[:12]}"))
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_copy(obj, path)
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

    def verify(self, checkpoint_id: CheckpointId) -> bool:
        """True when on-disk state matches the checkpoint exactly.

        Called *after* a restore. A restore that reports success but does not
        verify is the failure mode this whole component exists to prevent.
        """
        manifest = self._read_manifest(checkpoint_id)
        return all(file_digest(Path(raw_path)) == digest for raw_path, digest in manifest.items())

    # -- internals ---------------------------------------------------------

    def _manifest_path(self, checkpoint_id: CheckpointId) -> Path:
        return self.manifests / f"{checkpoint_id}.json"

    def _read_manifest(self, checkpoint_id: CheckpointId) -> dict[str, str | None]:
        path = self._manifest_path(checkpoint_id)
        if not path.exists():
            raise KeyError(f"unknown checkpoint {checkpoint_id!r}")
        data: dict[str, str | None] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def _store_object(self, source: Path, digest: str) -> None:
        obj = self.objects / digest
        if obj.exists():
            return  # content-addressed: identical bytes are stored once
        if _try_clone(source, obj):
            return
        _atomic_copy(source, obj)


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
        import sys

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
