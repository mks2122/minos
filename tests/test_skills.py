"""Skill promotion, storage and replay.

The tests that matter most here are the refusals. Published work found that
naive experience accumulation can make an agent *worse*, so what this system
declines to promote is more important than what it promotes.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from writ.agent import Agent
from writ.audit import AuditLog
from writ.broker import Broker
from writ.checkpoint import FileCheckpointStore
from writ.planner.base import Done, Trajectory
from writ.planner.scripted import ScriptedPlanner
from writ.router import Router
from writ.scopes import ScopeSet, ScopeViolation
from writ.skills import (
    PromotionRefused,
    Skill,
    SkillPlanner,
    SkillStore,
    promote,
    why_not_promotable,
)
from writ.skills.replay import check_drift, replay_is_safe
from writ.tiers.l1_system import FilesystemAdapter, ProcessAdapter
from writ.tiers.l2_adapters import TabularAdapter
from writ.types import ActionRequest, EffectClass

SALES = [
    ["Quarter", "Revenue", "Units"],
    ["Q1", "38100", "412"],
    ["Q3", "41800", "455"],
]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    with open(ws / "sales.csv", "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(SALES)
    return ws


def req(operation: str, **params) -> ActionRequest:
    return ActionRequest(goal_id="t", intent=operation, operation=operation, params=params)


def build(workspace: Path, planner, *, scopes=None, approve=False) -> Agent:
    root = workspace.parent
    return Agent(
        planner=planner,
        router=Router(adapters=(FilesystemAdapter(), ProcessAdapter(), TabularAdapter())),
        broker=Broker(
            scopes=ScopeSet.parse(
                scopes or [f"fs.read:{workspace}/**", f"fs.write:{workspace}/**"]
            ),
            audit=AuditLog(root / "audit.jsonl"),
            store=FileCheckpointStore(root / "cp"),
            approver=lambda inv, dec: approve,
        ),
    )


def successful_run(workspace: Path) -> Trajectory:
    book = workspace / "sales.csv"
    return build(
        workspace,
        ScriptedPlanner(
            [
                req("sheet.read_cell", path=str(book), cell="B3"),
                req("sheet.set_cell", path=str(book), cell="B3", value="48200"),
                Done(summary="updated", succeeded=True),
            ]
        ),
    ).run("update Q3 revenue")


# -- refusals --------------------------------------------------------------


def test_refuses_an_unsuccessful_trajectory(workspace):
    trajectory = build(workspace, ScriptedPlanner([Done(summary="gave up", succeeded=False)])).run(
        "do a thing"
    )

    assert why_not_promotable(trajectory) == "the run did not succeed"
    with pytest.raises(PromotionRefused):
        promote(trajectory, name="nope")


def test_refuses_a_trajectory_with_no_actions(workspace):
    trajectory = build(
        workspace, ScriptedPlanner([Done(summary="nothing to do", succeeded=True)])
    ).run("do nothing")

    assert "nothing to replay" in (why_not_promotable(trajectory) or "")


def test_refuses_an_unverified_effect(workspace):
    """The central guard: replaying an unconfirmed effect is a liability.

    proc.spawn carries a NullOracle, because this runtime cannot observe what a
    subprocess did. That makes the run unpromotable however well it went.
    """
    import sys

    agent = build(
        workspace,
        ScriptedPlanner(
            [
                req("proc.spawn", command=sys.executable, args=["-c", "print(1)"]),
                Done(summary="ran it", succeeded=True),
            ]
        ),
        scopes=[f"fs.read:{workspace}/**", f"proc.spawn:{sys.executable}"],
        approve=True,
    )
    trajectory = agent.run("run a program")

    reason = why_not_promotable(trajectory) or ""
    assert "not verified against a system of record" in reason
    assert "liability" in reason
    with pytest.raises(PromotionRefused):
        promote(trajectory, name="spawn-thing")


def test_refuses_a_halted_trajectory(workspace):
    """reconciliation_required means the runtime lost track of state."""
    trajectory = Trajectory(goal="g", goal_id="g")
    real = successful_run(workspace)
    halted = real.outcomes[0]
    trajectory.outcomes = [type(halted)(**{**vars_of(halted), "status": "reconciliation_required"})]
    trajectory.finished = Done(summary="halted", succeeded=False)

    reason = why_not_promotable(trajectory) or ""
    assert "lost track of state" in reason


def vars_of(outcome):
    return {
        "status": outcome.status,
        "invocation": outcome.invocation,
        "decision": outcome.decision,
        "observed": outcome.observed,
        "reversal": outcome.reversal,
        "checkpoint_id": outcome.checkpoint_id,
        "predicted": outcome.predicted,
        "result": outcome.result,
        "error": outcome.error,
    }


# -- promotion -------------------------------------------------------------


def test_promotes_a_verified_run(workspace):
    skill = promote(
        successful_run(workspace),
        name="update-q3",
        description="Set Q3 revenue in the sales workbook",
        workspace=workspace,
    )
    assert skill.name == "update-q3"
    assert len(skill.steps) == 2
    assert skill.parameters == ("workspace",)
    assert skill.deterministic


def test_promotion_parameterises_the_workspace(workspace):
    skill = promote(successful_run(workspace), name="update-q3", workspace=workspace)
    for step in skill.steps:
        assert "${workspace}" in step.params["path"]
        assert str(workspace) not in step.params["path"]


def test_promotion_carries_scopes(workspace):
    """A skill is a scoped procedure, not a blob of remembered steps."""
    skill = promote(successful_run(workspace), name="update-q3", workspace=workspace)
    assert skill.scopes
    assert all("${workspace}" in s for s in skill.scopes)
    assert any(s.startswith("fs.write:") for s in skill.scopes)
    assert any(s.startswith("fs.read:") for s in skill.scopes)


def test_promotion_records_preconditions(workspace):
    """What the world looked like, so drift can be measured later."""
    skill = promote(successful_run(workspace), name="update-q3", workspace=workspace)
    write_step = skill.steps[1]
    assert write_step.precondition == {"B3": "41800"}
    assert write_step.oracle_kind == "cell"


def test_promotion_records_effect_classes(workspace):
    skill = promote(successful_run(workspace), name="update-q3", workspace=workspace)
    assert skill.steps[0].effect_class is EffectClass.PURE
    assert skill.steps[1].effect_class is EffectClass.REVERSIBLE
    assert not skill.requires_approval


def test_explicit_parameters(workspace):
    skill = promote(
        successful_run(workspace),
        name="update-any",
        workspace=workspace,
        parameters={"amount": "48200"},
    )
    assert "amount" in skill.parameters
    assert skill.steps[1].params["value"] == "${amount}"


# -- replay ----------------------------------------------------------------


def test_replay_makes_no_model_call(workspace, tmp_path):
    skill = promote(successful_run(workspace), name="update-q3", workspace=workspace)

    fresh = tmp_path / "ws2"
    fresh.mkdir()
    with open(fresh / "sales.csv", "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(SALES)

    planner = SkillPlanner(skill, {"workspace": str(fresh)})
    trajectory = build(fresh, planner).run("update Q3 again")

    assert trajectory.succeeded
    assert "no model call" in trajectory.finished.summary
    with open(fresh / "sales.csv", newline="", encoding="utf-8") as fh:
        assert list(csv.reader(fh))[2][1] == "48200"


def test_replay_is_verified_like_any_other_action(workspace, tmp_path):
    """I2 applied to time: a cached step is not privileged."""
    skill = promote(successful_run(workspace), name="update-q3", workspace=workspace)
    fresh = tmp_path / "ws3"
    fresh.mkdir()
    with open(fresh / "sales.csv", "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(SALES)

    agent = build(fresh, SkillPlanner(skill, {"workspace": str(fresh)}))
    trajectory = agent.run("replay")

    assert all(o.observed is not None and o.observed.verifiable for o in trajectory.outcomes)
    assert agent.broker.audit.verify() == []

    # This run shares an audit log with the promotion run above, which is the
    # point: the chain is append-only and stays intact across sessions.
    replayed = agent.broker.audit.entries()[-2:]
    assert [e["status"] for e in replayed] == ["ok", "ok"]
    assert [e["invocation"]["request"]["operation"] for e in replayed] == [
        "sheet.read_cell",
        "sheet.set_cell",
    ]


def test_missing_binding_is_refused_up_front(workspace):
    skill = promote(successful_run(workspace), name="update-q3", workspace=workspace)
    with pytest.raises(ValueError, match="needs bindings"):
        SkillPlanner(skill, {})


def test_replay_cannot_widen_the_callers_scopes(workspace):
    """A cached procedure must not reach where the live agent could not."""
    skill = promote(successful_run(workspace), name="update-q3", workspace=workspace)
    planner = SkillPlanner(skill, {"workspace": str(workspace)})

    narrow = ScopeSet.parse([f"fs.read:{workspace}/**"])  # no write
    with pytest.raises(ScopeViolation):
        planner.narrowed_scopes(narrow)

    assert "cannot widen" in (replay_is_safe(skill, narrow, {"workspace": str(workspace)}) or "")


def test_replay_narrows_successfully_when_subset(workspace):
    skill = promote(successful_run(workspace), name="update-q3", workspace=workspace)
    planner = SkillPlanner(skill, {"workspace": str(workspace)})
    wide = ScopeSet.parse([f"fs.read:{workspace.parent}/**", f"fs.write:{workspace.parent}/**"])
    assert len(planner.narrowed_scopes(wide)) == len(skill.scopes)
    assert replay_is_safe(skill, wide, {"workspace": str(workspace)}) is None


def test_failed_step_abandons_replay(workspace, tmp_path):
    """A failure means the recording no longer describes reality."""
    skill = promote(successful_run(workspace), name="update-q3", workspace=workspace)
    empty = tmp_path / "empty"
    empty.mkdir()  # no sales.csv at all

    planner = SkillPlanner(skill, {"workspace": str(empty)})
    trajectory = build(empty, planner).run("replay into an empty directory")

    assert not trajectory.succeeded
    assert "abandoned" in trajectory.finished.summary


def test_replay_falls_back_to_a_real_planner(workspace, tmp_path):
    """A stale cache should hand control back, not press on."""
    skill = promote(successful_run(workspace), name="update-q3", workspace=workspace)
    empty = tmp_path / "empty2"
    empty.mkdir()

    fallback = ScriptedPlanner([Done(summary="planned it properly instead", succeeded=True)])
    planner = SkillPlanner(skill, {"workspace": str(empty)}, fallback=fallback)
    trajectory = build(empty, planner).run("replay with a fallback")

    assert trajectory.succeeded
    assert "properly instead" in trajectory.finished.summary


# -- drift -----------------------------------------------------------------


def test_no_drift_when_the_world_matches():
    assert check_drift({"B3": "41800"}, {"B3": "41800"}) == (False, "")


def test_drift_detected_when_a_value_moved():
    drifted, detail = check_drift({"B3": "41800"}, {"B3": "50000"})
    assert drifted
    assert "expected '41800'" in detail


def test_extra_keys_are_not_drift():
    """The world having more in it than last time is normal."""
    assert not check_drift({"B3": "41800"}, {"B3": "41800", "C3": "455"})[0]


def test_empty_precondition_is_never_drift():
    assert check_drift({}, {"anything": "at all"}) == (False, "")


def test_drift_check_renders_parameters(workspace, tmp_path):
    skill = promote(successful_run(workspace), name="update-q3", workspace=workspace)
    planner = SkillPlanner(skill, {"workspace": str(tmp_path)})
    planner._index = 1  # the write step, whose precondition is {"B3": "41800"}
    assert planner.drift_check({"B3": "41800"}) == (False, "")
    assert planner.drift_check({"B3": "99999"})[0]


# -- storage ---------------------------------------------------------------


def test_round_trips_through_disk(workspace, tmp_path):
    skill = promote(successful_run(workspace), name="Update Q3", workspace=workspace)
    store = SkillStore(tmp_path / "skills")
    store.save(skill)

    loaded = store.load("Update Q3")
    assert loaded.name == skill.name
    assert loaded.steps == skill.steps
    assert loaded.scopes == skill.scopes
    assert loaded.parameters == skill.parameters


def test_writes_an_agentskills_compatible_markdown(workspace, tmp_path):
    skill = promote(successful_run(workspace), name="update-q3", workspace=workspace)
    directory = SkillStore(tmp_path / "skills").save(skill)

    text = (directory / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: update-q3\n")
    assert "description:" in text
    assert "## Capability scopes" in text
    assert "Replay narrows to these; it cannot widen." in text
    assert "## Replay semantics" in text


def test_markdown_flags_steps_that_reprompt(tmp_path):
    """Caching skips the thinking, not the consent."""
    skill = Skill(
        name="risky",
        description="sends something",
        steps=(type(Skill.__dataclass_fields__["steps"].type) if False else _irreversible_step(),),
    )
    directory = SkillStore(tmp_path / "skills").save(skill)
    text = (directory / "SKILL.md").read_text(encoding="utf-8")
    assert "Requires human approval on replay: **yes**" in text
    assert "(prompts every replay)" in text


def _irreversible_step():
    from writ.skills.skill import SkillStep

    return SkillStep(
        operation="proc.spawn",
        params={"command": "/bin/mail"},
        effect_class=EffectClass.IRREVERSIBLE,
        expect="sends mail",
    )


def test_listing_and_deleting(workspace, tmp_path):
    store = SkillStore(tmp_path / "skills")
    store.save(promote(successful_run(workspace), name="a", workspace=workspace))
    assert store.list_skills() == ["a"]
    store.delete("a")
    assert store.list_skills() == []


def test_unknown_skill_raises(tmp_path):
    with pytest.raises(KeyError):
        SkillStore(tmp_path / "skills").load("missing")
