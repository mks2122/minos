"""``python -m writ.evals`` -- run the suite and print the numbers.

    uv run python -m writ.evals                      # scripted reference
    uv run python -m writ.evals --json out.json      # machine-readable
    uv run python -m writ.evals --baseline out.json  # regression check
    uv run python -m writ.evals --planner claude     # needs an API key

The scripted run is the one to start from: it establishes that every task in
the suite is achievable, which is what makes a later model score mean anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..planner.base import Planner
from .harness import PlannerFactory, run_suite
from .suite import SUITE, scripted_factory
from .task import Task

_OPERATIONS = (
    "fs.read",
    "fs.list",
    "fs.stat",
    "fs.write",
    "fs.copy",
    "fs.move",
    "fs.delete",
    "sheet.list",
    "sheet.read_cell",
    "sheet.read_range",
    "sheet.find_row",
    "sheet.set_cell",
    "proc.spawn",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="writ.evals")
    parser.add_argument("--planner", default="scripted", choices=["scripted", "claude", "local"])
    parser.add_argument(
        "--base-url",
        default="http://localhost:11434/v1",
        help="OpenAI-compatible endpoint for --planner local",
    )
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--json", type=Path, help="write the full report here")
    parser.add_argument("--baseline", type=Path, help="compare against a previous run")
    parser.add_argument("--only", help="run tasks whose id contains this substring")
    args = parser.parse_args(argv)

    tasks = [t for t in SUITE if not args.only or args.only in t.id]
    if not tasks:
        print(f"no tasks match {args.only!r}", file=sys.stderr)
        return 2

    if args.planner == "local":
        factory = _local_factory(args.base_url, args.model)
        name, model = "local", args.model
    elif args.planner == "claude":
        factory = _claude_factory(args.model)
        name, model = "claude", args.model
    else:
        factory = scripted_factory
        name, model = "scripted", "n/a"

    report = run_suite(tasks, factory, planner_name=name, model=model)
    print(report.text())

    if args.json:
        args.json.write_text(report.to_json(), encoding="utf-8")
        print(f"  wrote {args.json}\n")

    exit_code = 0 if report.passed == report.total else 1

    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        regressions = report.regressions(baseline)
        if regressions:
            print("  REGRESSIONS (passed before, fail now):")
            for task_id in regressions:
                print(f"    {task_id}")
            print("")
            exit_code = 1
        else:
            print("  no regressions against the baseline\n")

    return exit_code


def _local_factory(base_url: str, model: str) -> PlannerFactory:
    """Point the suite at a local model and get your own number."""
    from ..planner.local import LocalPlanner

    resolved = "qwen3:8b" if model == "claude-opus-5" else model

    def factory(task: Task, workspace: Path) -> Planner:
        return LocalPlanner(operations=_OPERATIONS, base_url=base_url, model=resolved)

    return factory


def _claude_factory(model: str) -> PlannerFactory:
    from ..planner.claude import ClaudePlanner

    def factory(task: Task, workspace: Path) -> Planner:
        return ClaudePlanner(operations=_OPERATIONS, model=model)

    return factory


if __name__ == "__main__":
    raise SystemExit(main())
