"""The sandbox workspace: a scratchpad, not a machine.

Three directories, and the separation between them is the whole design:

``materials/``
    Read-only copies of the real files a task declared. Copies, so a script that
    corrupts its input corrupts a copy.

``out/``
    Everything the script produced. This is the artifact set, and it is what
    :mod:`minos.tiers.l2_code` turns into a retroactive effect contract.

``run/``
    The script, the runner, and the captured output. Never promoted.

A workspace is ephemeral and holds no credentials. It is explicitly *not* a
mirror of the user's computer: that design needs a file-sync bridge, a
credential bridge and a display bridge, and those bridges are harder and more
dangerous than the agent. See docs/PLAN-GENERALITY.md.
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Artifact", "Workspace"]


@dataclass(frozen=True, slots=True)
class Artifact:
    """One file the script produced, and where it came from."""

    relative: str
    """Path within ``out/``. This is what a materialize request names."""

    path: Path
    size: int
    digest: str

    @property
    def name(self) -> str:
        return Path(self.relative).name


@dataclass
class Workspace:
    """One sandbox run's scratch space.

    Created under the state directory so it is cleaned by the same retention
    policy as everything else, and so a user who deletes ``.minos/`` deletes all
    of it.
    """

    root: Path
    session: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        for directory in (self.materials, self.out, self.run):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def materials(self) -> Path:
        return self.root / "materials"

    @property
    def out(self) -> Path:
        return self.root / "out"

    @property
    def run(self) -> Path:
        return self.root / "run"

    # -- inputs ------------------------------------------------------------

    def add_material(self, source: Path) -> Path:
        """Copy one real file in. Returns its path inside the workspace.

        Flattened to a basename on purpose: a script should not be able to infer
        the user's directory layout from the paths it is handed, and a material
        named ``../../etc/passwd`` must not describe anything outside
        ``materials/``.
        """
        source = Path(source).expanduser().resolve()
        destination = self.materials / Path(source.name).name
        counter = 1
        while destination.exists():
            destination = self.materials / f"{source.stem}-{counter}{source.suffix}"
            counter += 1
        shutil.copyfile(source, destination)
        return destination

    # -- outputs -----------------------------------------------------------

    def artifacts(self) -> tuple[Artifact, ...]:
        """Everything under ``out/``, sorted, with digests.

        Sorted so that two runs producing the same files produce the same
        contract; an effect contract whose target order wobbles is not one
        anybody can review.
        """
        from ..checkpoint import file_digest

        found: list[Artifact] = []
        for path in sorted(self.out.rglob("*")):
            if not path.is_file():
                continue
            digest = file_digest(path)
            if digest is None:
                continue
            found.append(
                Artifact(
                    relative=path.relative_to(self.out).as_posix(),
                    path=path,
                    size=path.stat().st_size,
                    digest=digest,
                )
            )
        return tuple(found)

    def resolve_artifact(self, relative: str) -> Path:
        """Resolve a name within ``out/``, refusing anything that escapes it.

        The planner chooses this string, and the planner is untrusted (I1). A
        materialize request naming ``../../../.ssh/id_rsa`` must fail here
        rather than reaching the broker with a plausible-looking target.
        """
        candidate = (self.out / relative).resolve()
        out = self.out.resolve()
        if candidate != out and out not in candidate.parents:
            raise ValueError(f"{relative!r} is outside the sandbox output directory")
        if not candidate.is_file():
            raise ValueError(f"{relative!r} is not a file the sandbox produced")
        return candidate

    # -- lifecycle ---------------------------------------------------------

    def dispose(self) -> None:
        """Delete the workspace. Safe to call twice."""
        shutil.rmtree(self.root, ignore_errors=True)

    def usage_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
