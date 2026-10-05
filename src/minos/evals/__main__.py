"""``python -m minos.evals`` -- run the suite and print the numbers.

    uv run python -m minos.evals                      # scripted reference
    uv run python -m minos.evals --json out.json      # machine-readable
    uv run python -m minos.evals --baseline out.json  # regression check
    uv run python -m minos.evals --planner claude     # needs an API key
    uv run python -m minos.evals --planner openrouter --model openai/gpt-4.1

The scripted run is the one to start from: it establishes that every task in
the suite is achievable, which is what makes a later model score mean anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..planner.base import Planner
from ..planner.providers import PROVIDERS, MissingKey, build_planner, resolve
from .harness import PlannerFactory, run_suite, run_suite_repeatedly
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
    parser = argparse.ArgumentParser(prog="minos.evals")
    parser.add_argument(
        "--planner",
        default="scripted",
        choices=["scripted", "claude", "local", *PROVIDERS, "custom"],
        help="any name from `minos providers` scores that OpenAI-compatible provider",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:11434/v1",
        help="OpenAI-compatible endpoint for --planner local",
    )
    parser.add_argument(
        "--model",
        default="",
        help="default: claude-opus-5 for claude, qwen3:8b for local, the preset's for a provider",
    )
    parser.add_argument("--json", type=Path, help="write the full report here")
    parser.add_argument("--baseline", type=Path, help="compare against a previous run")
    parser.add_argument("--only", help="run tasks whose id contains this substring")
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="run the suite this many times and report the spread, not one score",
    )
    parser.add_argument(
        "--in-process-planner",
        dest="isolate",
        action="store_false",
        help="keep a model planner in this process instead of a confined child",
    )
    args = parser.parse_args(argv)

    tasks = [t for t in SUITE if not args.only or args.only in t.id]
    if not tasks:
        print(f"no tasks match {args.only!r}", file=sys.stderr)
        return 2

    if args.planner == "local":
        model = args.model or "qwen3:8b"
        factory = _local_factory(args.base_url, model)
        name = "local"
    elif args.planner == "claude":
        model = args.model or "claude-opus-5"
        factory = _claude_factory(model)
        name = "claude"
    elif args.planner != "scripted":
        from ..config import settings

        settings()  # a key kept in .env counts
        try:
            provider = resolve(
                args.planner,
                model=args.model,
                base_url=args.base_url if args.planner == "custom" else "",
            )
        except (MissingKey, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        factory = _provider_factory(provider)
        name, model = provider.name, provider.model
    else:
        factory = scripted_factory
        name, model = "scripted", "n/a"

    if args.isolate and args.planner != "scripted":
        factory = _isolated(factory)

    if args.runs > 1:
        variance = run_suite_repeatedly(
            tasks,
            factory,
            args.runs,
            planner_name=name,
            model=model,
            progress=lambda i, r: print(f"  run {i}/{args.runs}: {r.passed}/{r.total}", flush=True),
        )
        print(variance.text())
        if args.json:
            args.json.write_text(variance.to_json(), encoding="utf-8")
            print(f"  wrote {args.json}\n")
        perfect = all(r.passed == r.total for r in variance.runs)
        return 0 if perfect and not variance.violations else 1

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


def _isolated(factory: PlannerFactory) -> PlannerFactory:
    """Each task's planner in its own confined process, as `minos run` does it."""
    from ..planner.isolated import IsolatedPlanner

    def isolated(task: Task, workspace: Path) -> Planner:
        planner = factory(task, workspace)
        timeout = float(getattr(planner, "timeout", 600.0)) + 120.0
        return IsolatedPlanner.of(planner, timeout=timeout)

    return isolated


def _local_factory(base_url: str, model: str) -> PlannerFactory:
    """Point the suite at a local model and get your own number."""
    from ..config import settings
    from ..planner.local import LocalPlanner

    # The same knobs `minos run` honours. Above all the context size: budgeting
    # for 16k against a server that serves 6k is the silent truncation the
    # context management exists to prevent.
    cfg = settings()

    def factory(task: Task, workspace: Path) -> Planner:
        return LocalPlanner(
            operations=task.operations or _OPERATIONS,
            base_url=base_url,
            model=model,
            context_tokens=cfg.context_tokens,
            timeout=cfg.planner_timeout,
            thinking=cfg.thinking,
        )

    return factory


def _provider_factory(provider: object) -> PlannerFactory:
    """A fresh planner per task, so no conversation leaks between tasks."""

    def factory(task: Task, workspace: Path) -> Planner:
        return build_planner(provider, task.operations or _OPERATIONS)  # type: ignore[arg-type]

    return factory


def _claude_factory(model: str) -> PlannerFactory:
    from ..planner.claude import ClaudePlanner

    def factory(task: Task, workspace: Path) -> Planner:
        return ClaudePlanner(operations=task.operations or _OPERATIONS, model=model)

    return factory


if __name__ == "__main__":
    raise SystemExit(main())
