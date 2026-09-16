"""Storing skills on disk.

Two files per skill, deliberately:

``SKILL.md``    YAML frontmatter plus prose, in the agentskills.io / Anthropic
                Agent Skills shape. Readable by a human, and by the ecosystem
                Hermes and OpenClaw already feed. Inventing a format here would
                forfeit that for nothing.
``skill.json``  the machine-readable steps, preconditions, oracles and scopes.

The split is honest about what each is for. The Markdown is documentation and
interchange; the JSON is what replay actually executes. A reader should never
have to wonder whether the prose and the behaviour agree.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..types import EffectClass
from .skill import Skill, SkillStep

__all__ = ["SkillStore"]

_SAFE_NAME = re.compile(r"[^a-z0-9._-]+")


def _slug(name: str) -> str:
    return _SAFE_NAME.sub("-", name.strip().lower()).strip("-") or "skill"


class SkillStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, name: str) -> Path:
        return self.root / _slug(name)

    # -- write -------------------------------------------------------------

    def save(self, skill: Skill) -> Path:
        directory = self.path_for(skill.name)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(_markdown(skill), encoding="utf-8")
        (directory / "skill.json").write_text(
            json.dumps(_to_json(skill), indent=2), encoding="utf-8"
        )
        return directory

    # -- read --------------------------------------------------------------

    def load(self, name: str) -> Skill:
        path = self.path_for(name) / "skill.json"
        if not path.exists():
            raise KeyError(f"no skill {name!r} in {self.root}")
        return _from_json(json.loads(path.read_text(encoding="utf-8")))

    def list_skills(self) -> list[str]:
        return sorted(p.parent.name for p in self.root.glob("*/skill.json") if p.is_file())

    def delete(self, name: str) -> None:
        directory = self.path_for(name)
        for child in directory.glob("*"):
            child.unlink()
        if directory.exists():
            directory.rmdir()


# -- serialisation ---------------------------------------------------------


def _to_json(skill: Skill) -> dict[str, Any]:
    data = asdict(skill)
    data["steps"] = [
        {**asdict(step), "effect_class": str(step.effect_class)} for step in skill.steps
    ]
    return data


def _from_json(data: dict[str, Any]) -> Skill:
    steps = tuple(
        SkillStep(
            operation=str(step["operation"]),
            params=dict(step.get("params", {})),
            effect_class=EffectClass(str(step["effect_class"])),
            expect=str(step.get("expect", "")),
            precondition=dict(step.get("precondition", {})),
            oracle_kind=str(step.get("oracle_kind", "")),
        )
        for step in data.get("steps", [])
    )
    return Skill(
        name=str(data["name"]),
        description=str(data.get("description", "")),
        steps=steps,
        parameters=tuple(data.get("parameters", ())),
        scopes=tuple(data.get("scopes", ())),
        source_goal=str(data.get("source_goal", "")),
        verified_at=str(data.get("verified_at", "")),
        runs=int(data.get("runs", 0)),
        failures=int(data.get("failures", 0)),
    )


def _markdown(skill: Skill) -> str:
    approval = "yes" if skill.requires_approval else "no"
    lines = [
        "---",
        f"name: {skill.name}",
        f"description: {skill.description}",
        "---",
        "",
        f"# {skill.name}",
        "",
        skill.description,
        "",
        "## Provenance",
        "",
        f"- Promoted from a verified run of: {skill.source_goal!r}",
        f"- Verified at: {skill.verified_at}",
        "- Every step was confirmed against a system of record at promotion time.",
        "",
        "## Parameters",
        "",
    ]
    lines.extend(f"- `{p}`" for p in skill.parameters or ("(none)",))
    lines += ["", "## Capability scopes", "", "Replay narrows to these; it cannot widen.", ""]
    lines.extend(f"- `{s}`" for s in skill.scopes or ("(none)",))
    lines += [
        "",
        "## Steps",
        "",
        f"Requires human approval on replay: **{approval}**",
        "",
    ]
    for index, step in enumerate(skill.steps, start=1):
        marker = "  **(prompts every replay)**" if step.needs_approval else ""
        lines.append(f"{index}. `{step.operation}` -- {step.effect_class}{marker}")
        if step.expect:
            lines.append(f"   - expects: {step.expect}")
        if step.oracle_kind:
            lines.append(f"   - verified by: `{step.oracle_kind}` oracle")
    lines += [
        "",
        "## Replay semantics",
        "",
        "- Runs under the scopes above, verified to be a subset of the caller's.",
        "- Every step is re-verified. Past success is not evidence of present success.",
        "- If the world no longer matches the recorded preconditions, replay is",
        "  abandoned and control returns to a real planner.",
        "- Irreversible steps re-prompt every time. Caching skips the thinking,",
        "  not the consent.",
        "",
    ]
    return "\n".join(lines)
