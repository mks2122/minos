"""Scoring more than once. One run is an anecdote."""

from __future__ import annotations

import json

import pytest

from minos.evals.__main__ import main as eval_main
from minos.evals.harness import EvalReport, TaskResult, VarianceReport, run_suite_repeatedly
from minos.evals.suite import SUITE, scripted_factory


def _result(task_id: str, ok: bool, violations: tuple[str, ...] = ()) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        category="c",
        kind="achieve",
        succeeded=ok,
        steps=1,
        duration_s=0.0,
        tier_counts={},
        fallback_rate=0.0,
        rollback_attempts=0,
        rollback_successes=0,
        unverified_effects=0,
        verified_effects=1,
        halted=False,
        audit_intact=True,
        summary="",
        violations=violations,
    )


def _report(*outcomes: bool, violations: tuple[str, ...] = ()) -> EvalReport:
    report = EvalReport(planner_name="local", model="m")
    report.results = [
        _result(f"t{i}", ok, violations if i == 0 else ()) for i, ok in enumerate(outcomes)
    ]
    return report


def test_the_spread_is_reported_not_just_the_mean():
    variance = VarianceReport(
        [_report(True, True, False), _report(True, False, False), _report(True, True, True)]
    )
    assert variance.scores == [2, 1, 3]
    assert variance.mean == pytest.approx(2.0)
    assert variance.stdev == pytest.approx(1.0)
    low, high = variance.interval
    assert low < 2.0 < high and high <= 3


def test_tasks_that_moved_are_named():
    variance = VarianceReport([_report(True, True, False), _report(True, False, False)])
    assert variance.unstable == ["t1"]
    assert variance.per_task() == {"t0": (2, 2), "t1": (1, 2), "t2": (0, 2)}


def test_a_violation_in_any_run_is_reported():
    variance = VarianceReport([_report(True), _report(True, violations=("I2: broke",))])
    assert variance.violations == ["run 2: t0: I2: broke"]
    assert "the runtime broke a promise" in variance.text()


def test_one_run_has_no_spread():
    variance = VarianceReport([_report(True, False)])
    assert variance.stdev == 0.0 and variance.interval == (1.0, 1.0)


def test_the_json_carries_every_run_and_the_summary():
    data = json.loads(VarianceReport([_report(True), _report(False)]).to_json())
    assert data["summary"]["scores"] == [1, 0]
    assert data["summary"]["unstable"] == ["t0"]
    assert len(data["runs"]) == 2


def test_the_reference_planner_is_perfectly_stable():
    tasks = [t for t in SUITE if t.id.startswith(("read.", "refuse."))]
    variance = run_suite_repeatedly(tasks, scripted_factory, 2)
    assert variance.scores == [len(tasks), len(tasks)]
    assert variance.unstable == [] and variance.violations == []


def test_the_cli_takes_runs(capsys, tmp_path):
    out = tmp_path / "x.json"
    assert eval_main(["--only", "read.", "--runs", "2", "--json", str(out)]) == 0
    assert "minos eval x2" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["runs"] == 2
