"""L1 filesystem adapter.

Native, typed, cross-platform. This is the tier the router prefers, because the
effect contract it produces is *exact*: we know precisely which paths change,
which is what makes the checkpoint sound.

Two classification notes worth reading, since they are where this tier stops
being boring:

``fs.delete`` is **REVERSIBLE**, not irreversible. The checkpoint copies the
file's bytes before the action, so restoring genuinely brings it back. Marking
it irreversible would be theatre.

``fs.mkdir`` is **COMPENSABLE**, because the checkpoint store is file-shaped and
cannot restore a directory that did not exist. Its declared inverse is
``fs.delete`` on the same path, which the broker admits only when that inverse
is itself in scope.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ...oracles import FileHashOracle, PathExistsOracle
from ...types import (
    ActionRequest,
    EffectClass,
    EffectContract,
    Grant,
    Invocation,
    Oracle,
    Tier,
)
from ..base import CapabilityManifest, OperationUnsupported, Preparation

__all__ = ["FilesystemAdapter"]

_OPERATIONS = (
    "fs.read",
    "fs.list",
    "fs.stat",
    "fs.write",
    "fs.append",
    "fs.copy",
    "fs.move",
    "fs.mkdir",
    "fs.delete",
)


def _path(params: dict[str, Any], key: str) -> Path:
    value = params.get(key)
    if value is None:
        raise OperationUnsupported(f"missing required param {key!r}")
    return Path(str(value)).expanduser().resolve()


class FilesystemAdapter:
    """Filesystem operations, all of them behind the broker."""

    manifest = CapabilityManifest(
        adapter="l1.fs",
        tier=Tier.L1_SYSTEM,
        operations=_OPERATIONS,
        summary="Native filesystem operations with exact effect contracts",
    )

    def prepare(self, request: ActionRequest) -> Preparation:
        op = request.operation
        if op not in _OPERATIONS:
            raise OperationUnsupported(op)
        handler = getattr(self, f"_prepare_{op.split('.', 1)[1]}")
        return handler(request)  # type: ignore[no-any-return]

    # -- reads (PURE) ------------------------------------------------------

    def _prepare_read(self, request: ActionRequest) -> Preparation:
        path = _path(request.params, "path")
        encoding = request.params.get("encoding", "utf-8")

        def execute(_: Invocation) -> str:
            return path.read_text(encoding=encoding)

        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.PURE,
                targets=(path,),
                oracle=FileHashOracle((path,)),
                expect=f"read {path.name}; nothing changes",
            ),
            execute=execute,
            grants=(Grant("fs.read", str(path)),),
        )

    def _prepare_list(self, request: ActionRequest) -> Preparation:
        path = _path(request.params, "path")

        def execute(_: Invocation) -> list[str]:
            return sorted(p.name for p in path.iterdir())

        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.PURE,
                targets=(),
                oracle=PathExistsOracle((path,)),
                expect=f"list {path}; nothing changes",
            ),
            execute=execute,
            grants=(Grant("fs.read", str(path)),),
        )

    def _prepare_stat(self, request: ActionRequest) -> Preparation:
        path = _path(request.params, "path")

        def execute(_: Invocation) -> dict[str, Any]:
            st = path.stat()
            return {"size": st.st_size, "mtime": st.st_mtime, "is_dir": path.is_dir()}

        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.PURE,
                targets=(),
                oracle=PathExistsOracle((path,)),
                expect=f"stat {path}; nothing changes",
            ),
            execute=execute,
            grants=(Grant("fs.read", str(path)),),
        )

    # -- writes (REVERSIBLE) -----------------------------------------------

    def _prepare_write(self, request: ActionRequest) -> Preparation:
        path = _path(request.params, "path")
        content = request.params.get("content")
        if content is None:
            raise OperationUnsupported("fs.write requires 'content'")
        encoding = request.params.get("encoding", "utf-8")

        def execute(_: Invocation) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(content), encoding=encoding)

        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.REVERSIBLE,
                targets=(path,),
                oracle=FileHashOracle((path,)),
                expect=f"{path.name} contains the supplied content ({len(str(content))} chars)",
            ),
            execute=execute,
            grants=(Grant("fs.write", str(path)),),
        )

    def _prepare_append(self, request: ActionRequest) -> Preparation:
        path = _path(request.params, "path")
        content = request.params.get("content")
        if content is None:
            raise OperationUnsupported("fs.append requires 'content'")
        encoding = request.params.get("encoding", "utf-8")

        def execute(_: Invocation) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding=encoding) as fh:
                fh.write(str(content))

        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.REVERSIBLE,
                targets=(path,),
                oracle=FileHashOracle((path,)),
                expect=f"{len(str(content))} chars appended to {path.name}",
            ),
            execute=execute,
            grants=(Grant("fs.write", str(path)),),
        )

    def _prepare_copy(self, request: ActionRequest) -> Preparation:
        source = _path(request.params, "source")
        dest = _path(request.params, "path")

        def execute(_: Invocation) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)

        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.REVERSIBLE,
                targets=(dest,),
                oracle=FileHashOracle((dest,)),
                expect=f"{dest.name} becomes a copy of {source.name}",
            ),
            execute=execute,
            # Two capabilities, two subjects. This is why grants are explicit:
            # the operation's own capability alone would miss the read.
            grants=(Grant("fs.read", str(source)), Grant("fs.write", str(dest))),
        )

    def _prepare_move(self, request: ActionRequest) -> Preparation:
        source = _path(request.params, "source")
        dest = _path(request.params, "path")

        def execute(_: Invocation) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest))

        return Preparation(
            contract=EffectContract(
                effect_class=EffectClass.REVERSIBLE,
                # Both ends are checkpointed: the source must come back and the
                # destination must go away.
                targets=(source, dest),
                oracle=FileHashOracle((source, dest)),
                expect=f"{source.name} moves to {dest}",
            ),
            execute=execute,
            grants=(Grant("fs.delete", str(source)), Grant("fs.write", str(dest))),
        )

    def _prepare_delete(self, request: ActionRequest) -> Preparation:
        path = _path(request.params, "path")

        def execute(_: Invocation) -> None:
            if path.is_dir():
                path.rmdir()  # empty directories only; recursive delete is not a primitive
            else:
                path.unlink()

        # A directory hashes to None whether it exists or not, so a hash oracle
        # cannot tell a deleted directory from a present one and reports every
        # rmdir as a no-op. Existence is the right question for a directory --
        # it is the case PathExistsOracle was written for.
        oracle: Oracle = PathExistsOracle((path,)) if path.is_dir() else FileHashOracle((path,))

        return Preparation(
            contract=EffectContract(
                # Genuinely reversible: the checkpoint holds the bytes.
                effect_class=EffectClass.REVERSIBLE,
                targets=(path,),
                oracle=oracle,
                expect=f"{path} no longer exists",
            ),
            execute=execute,
            grants=(Grant("fs.delete", str(path)),),
        )

    # -- mkdir (COMPENSABLE) -----------------------------------------------

    def _prepare_mkdir(self, request: ActionRequest) -> Preparation:
        path = _path(request.params, "path")

        def execute(_: Invocation) -> None:
            path.mkdir(parents=True, exist_ok=False)

        return Preparation(
            contract=EffectContract(
                # The checkpoint store is file-shaped and cannot restore a
                # directory into non-existence, so this is compensated rather
                # than reversed.
                effect_class=EffectClass.COMPENSABLE,
                targets=(),
                oracle=PathExistsOracle((path,)),
                expect=f"directory {path} exists",
                compensation=ActionRequest(
                    goal_id=request.goal_id,
                    intent=f"remove the directory {path} created by this task",
                    operation="fs.delete",
                    params={"path": str(path)},
                ),
            ),
            execute=execute,
            grants=(Grant("fs.write", str(path)),),
        )
