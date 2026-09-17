"""``writ`` -- the command line.

    writ doctor                                 can this machine run fully offline?
    writ demo                                   see it work, no API key needed
    writ run "set Q3 revenue to 48200" -w ./data --allow-write
    writ eval                                   run the task suite
    writ audit .writ/audit.jsonl                verify the chain
    writ index ./data                           build the memory index
    writ recall "the excel from yesterday"      resolve a vague reference
    writ skills                                 list stored skills

Scopes are flags, not configuration buried in a file, and the default is
read-only. Granting write access should be a thing you typed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__

DEFAULT_STATE = Path(".writ")

_CREDENTIALS_HINT = """
This looks like missing credentials. Either:
    export ANTHROPIC_API_KEY=sk-ant-...
or:
    ant auth login

To try the runtime without a model or an API key:
    writ demo
    writ eval"""


def _looks_like_missing_credentials(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "authentication" in text or "api_key" in text or "api key" in text


# -- writ run --------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    from .agent import Agent, AgentLimits
    from .audit import AuditLog
    from .broker import Broker, cli_approver
    from .checkpoint import FileCheckpointStore
    from .memory import MemoryStore
    from .router import Router
    from .scopes import ScopeSet
    from .tiers.l1_system import FilesystemAdapter, MemoryAdapter, ProcessAdapter
    from .tiers.l2_adapters import TabularAdapter

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"error: {workspace} is not a directory", file=sys.stderr)
        return 2

    scopes = [f"fs.read:{workspace}/**", f"memory.read:{workspace}/**"]
    if args.allow_write:
        scopes.append(f"fs.write:{workspace}/**")
    if args.allow_delete:
        scopes.append(f"fs.delete:{workspace}/**")

    state = Path(args.state).expanduser().resolve()
    operations = (
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
        "memory.recall",
        "memory.recent",
    )

    # Index the workspace so "the excel from yesterday" has something to
    # resolve against. Scoped to the workspace, so memory never learns about
    # files this task could not have listed anyway.
    memory = MemoryStore(state / "memory.db")
    memory.index_tree(workspace)
    memory.start_session("cli", args.goal)

    try:
        planner = _planner(args, operations)
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    broker = Broker(
        scopes=ScopeSet.parse(scopes),
        audit=AuditLog(state / "audit.jsonl"),
        store=FileCheckpointStore(state / "checkpoints"),
        approver=(lambda inv, dec: True) if args.yes else cli_approver,
        dry_run=args.dry_run,
    )
    agent = Agent(
        planner=planner,
        router=Router(
            adapters=(
                FilesystemAdapter(),
                MemoryAdapter(memory, roots=(workspace,)),
                ProcessAdapter(),
                TabularAdapter(),
            )
        ),
        broker=broker,
        limits=AgentLimits(max_steps=args.max_steps),
    )

    print(f"\ngoal      : {args.goal}")
    print(f"workspace : {workspace}")
    print("scopes    :")
    for scope in scopes:
        print(f"    {scope}")
    if args.dry_run:
        print("\nDRY RUN -- nothing will be changed.")
    if args.yes:
        print("\n!! --yes: approvals are auto-granted, including irreversible ones.")
    print()

    try:
        trajectory = agent.run(args.goal, goal_id="cli")
    except Exception as exc:
        print(f"\nthe planner failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if _looks_like_missing_credentials(exc):
            print(_CREDENTIALS_HINT, file=sys.stderr)
        print(
            "\nnothing was changed beyond what the audit log records "
            f"({broker.audit.path}, {broker.audit.count} entries).",
            file=sys.stderr,
        )
        return 2

    print("-" * 64)
    for step, outcome in zip(trajectory.steps, trajectory.outcomes, strict=False):
        mark = {"ok": "ok  ", "denied": "DENY", "dry_run": "dry "}.get(outcome.status, "FAIL")
        print(f"  {mark}  [{outcome.invocation.tier}] {step.operation}")
        if outcome.status != "ok":
            print(f"        {outcome.error or outcome.decision.rationale}")

    finished = trajectory.finished
    print()
    print(f"  {'SUCCEEDED' if trajectory.succeeded else 'DID NOT SUCCEED'}")
    if finished:
        print(f"  {finished.summary}")
    print(f"\n  audit: {broker.audit.path}  ({broker.audit.count} entries)")

    # Record what this run touched, so the next one can resolve "yesterday's".
    for outcome in trajectory.outcomes:
        memory.record_outcome(outcome, session_id="cli", goal=args.goal)
    memory.end_session("cli")
    memory.close()
    print(f"  memory: {state / 'memory.db'}")

    return 0 if trajectory.succeeded else 1


def _planner(args: argparse.Namespace, operations: tuple[str, ...]):  # type: ignore[no-untyped-def]
    choice = args.planner
    offline = getattr(args, "offline", False)

    if offline and choice == "claude":
        raise ImportError(
            "--offline was given but --planner claude would call a remote API. "
            "Drop --offline, or start a local server (see: writ doctor)."
        )

    if choice == "auto":
        from .planner.local import server_available

        if server_available(args.base_url):
            choice = "local"
        elif offline:
            raise ImportError(
                "--offline was given but no local server is reachable at "
                f"{args.base_url}.\nRun `writ doctor` to see what is missing."
            )
        else:
            choice = "claude"
        print(f"planner   : {choice} (auto-detected)")

    if choice == "local":
        from .planner.local import LocalPlanner

        return LocalPlanner(
            operations=operations,
            base_url=args.base_url,
            model=args.model or "qwen3:8b",
        )
    if choice != "claude":
        raise ImportError(f"unknown planner {choice!r}")
    try:
        from .planner.claude import ClaudePlanner
    except ImportError as exc:
        raise ImportError(
            "the Claude planner needs the anthropic SDK:\n"
            "    uv sync --extra claude\n"
            "and credentials in ANTHROPIC_API_KEY (or `ant auth login`)."
        ) from exc
    return ClaudePlanner(operations=operations, model=args.model or "claude-opus-5")


# -- writ demo -------------------------------------------------------------


def cmd_demo(args: argparse.Namespace) -> int:
    """The 30-second demo. No API key, no network, no planner."""
    import runpy

    script = Path(__file__).resolve().parents[2] / "examples" / "dry_run_then_rollback.py"
    if not script.exists():
        print("error: examples/dry_run_then_rollback.py not found", file=sys.stderr)
        return 2
    runpy.run_path(str(script), run_name="__main__")
    return 0


# -- writ doctor -----------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import diagnose, render

    report = diagnose(args.base_url)
    print(render(report))
    return 0 if report.can_run_offline else 1


# -- writ audit ------------------------------------------------------------


def cmd_audit(args: argparse.Namespace) -> int:
    from .audit import AuditLog

    path = Path(args.path).expanduser().resolve()
    if not path.exists():
        print(f"error: no audit log at {path}", file=sys.stderr)
        return 2

    log = AuditLog(path)
    entries = log.entries()
    breaks = log.verify()

    print(f"\n{path}\n{'-' * 64}")
    for entry in entries[-args.tail :]:
        invocation = entry["invocation"]
        print(
            f"  #{entry['seq']:<4} {entry['status']:<24} "
            f"[{invocation['tier']}] {invocation['request']['operation']}"
        )
        if args.verbose:
            print(f"        why tier : {invocation['tier_reason']}")
            print(f"        decision : {entry['decision']['rationale']}")

    print(f"\n  entries      : {len(entries)}")
    print(f"  chain intact : {'yes' if not breaks else 'NO'}")
    for chain_break in breaks:
        print(f"    broken at seq {chain_break.seq}")
    print()
    return 0 if not breaks else 1


# -- writ index / recall ---------------------------------------------------


def cmd_index(args: argparse.Namespace) -> int:
    from .memory import MemoryStore

    with MemoryStore(Path(args.state).expanduser().resolve() / "memory.db") as memory:
        count = memory.index_tree(Path(args.directory).expanduser().resolve())
    print(f"indexed {count} files")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    from .memory import MemoryStore, resolve

    db = Path(args.state).expanduser().resolve() / "memory.db"
    if not db.exists():
        print(f"error: no memory index at {db}. Run `writ index <dir>` first.", file=sys.stderr)
        return 2
    with MemoryStore(db) as memory:
        result = resolve(args.phrase, memory)
    print()
    print(result.explain())
    print()
    return 0 if result.best else 1


# -- writ skills -----------------------------------------------------------


def cmd_skills(args: argparse.Namespace) -> int:
    from .skills import SkillStore

    store = SkillStore(Path(args.state).expanduser().resolve() / "skills")
    names = store.list_skills()
    if not names:
        print("no skills stored yet")
        return 0

    if args.name:
        skill = store.load(args.name)
        print(f"\n{skill.name} -- {skill.description}")
        print(f"  promoted from : {skill.source_goal!r}")
        print(f"  verified at   : {skill.verified_at}")
        print(f"  parameters    : {', '.join(skill.parameters) or '(none)'}")
        print("  scopes        :")
        for scope in skill.scopes:
            print(f"      {scope}")
        print("  steps         :")
        for index, step in enumerate(skill.steps, start=1):
            flag = "  (prompts every replay)" if step.needs_approval else ""
            print(f"      {index}. {step.operation} [{step.effect_class}]{flag}")
        print()
        return 0

    for name in names:
        print(f"  {name}")
    return 0


# -- writ eval -------------------------------------------------------------


def cmd_eval(args: argparse.Namespace) -> int:
    from .evals.__main__ import main as eval_main

    return eval_main(list(args.rest))


# -- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="writ",
        description="The model asks. The runtime decides.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"writ {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run the dry-run/execute/rollback demo")
    demo.set_defaults(func=cmd_demo)

    run = sub.add_parser("run", help="run the agent on a goal")
    run.add_argument("goal")
    run.add_argument("-w", "--workspace", default=".", help="the only directory in scope")
    run.add_argument("--allow-write", action="store_true", help="grant fs.write")
    run.add_argument("--allow-delete", action="store_true", help="grant fs.delete")
    run.add_argument("--dry-run", action="store_true", help="predict, change nothing")
    run.add_argument(
        "--yes",
        action="store_true",
        help="auto-approve prompts, INCLUDING irreversible effects",
    )
    run.add_argument(
        "--planner",
        default="auto",
        choices=["auto", "local", "claude"],
        help="auto prefers a local server when one is listening",
    )
    run.add_argument(
        "--base-url",
        default="http://localhost:11434/v1",
        help="OpenAI-compatible endpoint for --planner local (Ollama, LM Studio, ...)",
    )
    run.add_argument(
        "--model",
        default=None,
        help="defaults to qwen3:8b for local, claude-opus-5 for claude",
    )
    run.add_argument("--max-steps", type=int, default=20)
    run.add_argument(
        "--offline",
        action="store_true",
        help="refuse to use a remote model; fail instead of reaching the network",
    )
    run.add_argument("--state", default=str(DEFAULT_STATE))
    run.set_defaults(func=cmd_run)

    doctor = sub.add_parser("doctor", help="can this machine run fully offline?")
    doctor.add_argument("--base-url", default="http://localhost:11434/v1")
    doctor.set_defaults(func=cmd_doctor)

    audit = sub.add_parser("audit", help="verify and print an audit chain")
    audit.add_argument("path", nargs="?", default=str(DEFAULT_STATE / "audit.jsonl"))
    audit.add_argument("-n", "--tail", type=int, default=20)
    audit.add_argument("-v", "--verbose", action="store_true")
    audit.set_defaults(func=cmd_audit)

    index = sub.add_parser("index", help="index a directory into memory")
    index.add_argument("directory")
    index.add_argument("--state", default=str(DEFAULT_STATE))
    index.set_defaults(func=cmd_index)

    recall = sub.add_parser("recall", help="resolve a vague reference, with reasons")
    recall.add_argument("phrase")
    recall.add_argument("--state", default=str(DEFAULT_STATE))
    recall.set_defaults(func=cmd_recall)

    skills = sub.add_parser("skills", help="list or show stored skills")
    skills.add_argument("name", nargs="?")
    skills.add_argument("--state", default=str(DEFAULT_STATE))
    skills.set_defaults(func=cmd_skills)

    # Listed for `writ --help`; main() delegates before this parser sees it.
    evaluate = sub.add_parser(
        "eval", help="run the eval suite (accepts the eval module's own flags)"
    )
    evaluate.add_argument("rest", nargs=argparse.REMAINDER)
    evaluate.set_defaults(func=cmd_eval)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `writ eval --only read.` must reach the eval parser intact. argparse's
    # REMAINDER does not capture a leading flag -- the top-level parser claims
    # it first -- so delegate before parsing rather than fighting that.
    if argv and argv[0] == "eval":
        from .evals.__main__ import main as eval_main

        return eval_main(argv[1:])

    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
