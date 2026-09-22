"""``minos`` -- the command line.

    minos doctor                                 can this machine run fully offline?
    minos demo                                   see it work, no API key needed
    minos run "set Q3 revenue to 48200" -w ./data --allow-write
    minos eval                                   run the task suite
    minos audit .minos/audit.jsonl                verify the chain
    minos undo                                   what can be put back
    minos undo --last                            put the last action back
    minos index ./data                           build the memory index
    minos recall "the excel from yesterday"      resolve a vague reference
    minos skills                                 list stored skills

Scopes are flags, not configuration buried in a file, and the default is
read-only. Granting write access should be a thing you typed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import settings

DEFAULT_STATE = Path(".minos")

_CREDENTIALS_HINT = """
This looks like missing credentials. Either:
    export ANTHROPIC_API_KEY=sk-ant-...
or:
    ant auth login

To try the runtime without a model or an API key:
    minos demo
    minos eval"""


def _looks_like_missing_credentials(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "authentication" in text or "api_key" in text or "api key" in text


# -- minos run --------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    from .agent import Agent, AgentLimits
    from .audit import AuditLog
    from .broker import Broker, cli_approver
    from .checkpoint import FileCheckpointStore
    from .memory import FileWatcher, MemoryStore
    from .router import Router
    from .scopes import ScopeSet
    from .tiers.base import Adapter
    from .tiers.l1_system import (
        AppAdapter,
        FilesystemAdapter,
        MemoryAdapter,
        ProcessAdapter,
    )
    from .tiers.l2_adapters import TabularAdapter
    from .tiers.l2_code import CodeAdapter
    from .tiers.l3_gui import GuiAdapter
    from .trace import ConsolePrinter, SessionRecorder, fan_out

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"error: {workspace} is not a directory", file=sys.stderr)
        return 2

    state = Path(args.state).expanduser().resolve()
    # code.run is granted by default: the sandbox reaches nothing the user
    # owns, and its output still needs fs.write to go anywhere.
    scopes = [
        f"fs.read:{workspace}/**",
        f"memory.read:{workspace}/**",
        f"code.run:{state}/**",
    ]
    if args.allow_write:
        scopes.append(f"fs.write:{workspace}/**")
    if args.allow_delete:
        scopes.append(f"fs.delete:{workspace}/**")
    if args.allow_open:
        scopes.append(f"app.open:{workspace}/**")
    if args.allow_gui:
        # The virtual device drives the real desktop. Never implicit.
        scopes.append("ui.input:*")

    operations: tuple[str, ...] = (
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
        "app.open",
        "code.run",
        "code.materialize",
    )
    if args.allow_gui:
        operations += ("ui.click", "ui.type", "ui.key", "ui.screenshot")

    # Index the workspace so "the excel from yesterday" has something to
    # resolve against. Scoped to the workspace, so memory never learns about
    # files this task could not have listed anyway.
    memory = MemoryStore(state / "memory.db")
    memory.index_tree(workspace)
    memory.start_session("cli", args.goal)

    # Notice edits made outside this runtime while it works -- someone saving
    # in Excel mid-task, say. Without it memory only ever sees the world as it
    # was when the run started.
    watcher = FileWatcher(memory, roots=(workspace,), interval=args.watch_interval)
    if args.watch:
        watcher.start()

    try:
        planner = _planner(args, operations)
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    gui_adapters: tuple[Adapter, ...] = ()
    panic: Any = None
    if args.allow_gui:
        from .tiers.l3_gui import PanicAbort, WindowsDriver, panic_watcher  # noqa: F401

        try:
            panic = panic_watcher()
            gui_adapters = (GuiAdapter(driver=WindowsDriver(panic=panic)),)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    broker = Broker(
        scopes=ScopeSet.parse(scopes),
        audit=AuditLog(state / "audit.jsonl"),
        store=FileCheckpointStore(state / "checkpoints"),
        approver=(lambda inv, dec: True) if args.yes else cli_approver,
        dry_run=args.dry_run,
    )
    # Live trace to the terminal, and a transcript kept for afterwards. The
    # transcript is a debugging record, deliberately separate from the audit
    # chain -- see minos.trace.
    recorder = SessionRecorder(state=state, goal=args.goal) if args.trace else None
    printer = ConsolePrinter(show_thinking=not args.quiet, show_code=not args.quiet)
    observer = fan_out(printer if not args.quiet else None, recorder)

    agent = Agent(
        observer=observer,
        planner=planner,
        router=Router(
            adapters=(
                FilesystemAdapter(),
                AppAdapter(),
                MemoryAdapter(memory, roots=(workspace,)),
                ProcessAdapter(),
                TabularAdapter(),
                CodeAdapter(state=state),
                *gui_adapters,
            )
        ),
        broker=broker,
        limits=AgentLimits(max_steps=args.max_steps),
    )

    # A context too small to hold the tool schemas is invisible at runtime: the
    # server truncates silently and the model simply gets worse at its job. Say
    # it before the run rather than leaving someone to conclude the model is bad.
    warning = _context_warning(planner, args.base_url)
    if warning:
        print(f"\n  !! {warning}")

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
        if panic is not None:
            print("  !! the agent will use your real mouse and keyboard.")
            print("     press Ctrl+Alt+Esc to abort and release input.")
            print()
            with panic:
                trajectory = agent.run(args.goal, goal_id="cli")
        else:
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

    print()
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

    watcher.stop()
    external = watcher.drain()

    # Record what this run touched, so the next one can resolve "yesterday's".
    for outcome in trajectory.outcomes:
        memory.record_outcome(outcome, session_id="cli", goal=args.goal)
        if outcome.status == "ok" and outcome.invocation.request.operation == "app.open":
            result = outcome.result if isinstance(outcome.result, dict) else {}
            memory.record_open(
                result.get("opened", ""),
                str(result.get("handler", "")),
                session_id="cli",
                goal=args.goal,
            )
    memory.end_session("cli")
    memory.close()
    print(f"  memory: {state / 'memory.db'}")
    if recorder is not None:
        print(f"  trace : {recorder.path}")
        recorder.prune(keep=20)
    if external:
        # Changes nobody asked this runtime to make. Worth saying out loud.
        print(f"  watcher: {len(external)} file change(s) seen outside this run")
    for problem in watcher.errors:
        # A quiet watcher is indistinguishable from one that found nothing.
        print(f"  watcher: {problem}", file=sys.stderr)

    return 0 if trajectory.succeeded else 1


def _context_warning(planner: Any, base_url: str) -> str:
    """Compare the planner's fixed overhead against what the server really serves."""
    check = getattr(planner, "context_warning", None)
    if check is None:
        return ""
    from .doctor import _served_context

    try:
        served = _served_context(base_url, getattr(planner, "model", ""))
        return str(check(served))
    except Exception:
        # A diagnostic that breaks the run it is diagnosing is worse than none.
        return ""


def _planner(args: argparse.Namespace, operations: tuple[str, ...]):  # type: ignore[no-untyped-def]
    choice = args.planner
    cfg = settings()
    # MINOS_OFFLINE makes local a guarantee without having to remember the flag
    # on every invocation; --offline still forces it on.
    offline = getattr(args, "offline", False) or cfg.offline

    if offline and choice == "claude":
        raise ImportError(
            "--offline was given but --planner claude would call a remote API. "
            "Drop --offline, or start a local server (see: minos doctor)."
        )

    if choice == "auto":
        from .planner.local import server_available

        if server_available(args.base_url):
            choice = "local"
        elif offline:
            raise ImportError(
                "--offline was given but no local server is reachable at "
                f"{args.base_url}.\nRun `minos doctor` to see what is missing."
            )
        else:
            choice = "claude"
        print(f"planner   : {choice} (auto-detected)")

    if choice == "local":
        from .planner.local import LocalPlanner

        return LocalPlanner(
            operations=operations,
            base_url=args.base_url,
            model=args.model or cfg.model,
            context_tokens=cfg.context_tokens,
            timeout=cfg.planner_timeout,
            thinking=cfg.thinking,
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
    return ClaudePlanner(operations=operations, model=args.model or cfg.remote_model)


# -- minos demo -------------------------------------------------------------


def cmd_demo(args: argparse.Namespace) -> int:
    """The 30-second demo. No API key, no network, no planner."""
    import runpy

    script = Path(__file__).resolve().parents[2] / "examples" / "dry_run_then_rollback.py"
    if not script.exists():
        print("error: examples/dry_run_then_rollback.py not found", file=sys.stderr)
        return 2
    runpy.run_path(str(script), run_name="__main__")
    return 0


# -- minos doctor -----------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import diagnose, render

    report = diagnose(args.base_url)
    print(render(report))
    return 0 if report.can_run_offline else 1


# -- minos audit ------------------------------------------------------------


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


# -- minos undo -------------------------------------------------------------


def cmd_undo(args: argparse.Namespace) -> int:
    from .audit import AuditLog
    from .checkpoint import FileCheckpointStore, UnprotectableTarget
    from .undo import UndoError, find, perform_undo, undoable_actions

    state = Path(args.state).expanduser().resolve()
    audit_path = state / "audit.jsonl"
    if not audit_path.exists():
        print(f"error: no audit log at {audit_path}", file=sys.stderr)
        return 2

    audit = AuditLog(audit_path)
    store = FileCheckpointStore(state / "checkpoints")

    selector = "last" if args.last else args.which
    if selector is None:
        actions = undoable_actions(audit, store)
        if not actions:
            print("\n  nothing to undo: no action has a restorable checkpoint\n")
            return 0
        print(f"\n  undoable actions ({len(actions)})\n  {'-' * 62}")
        for action in actions[: args.tail]:
            flag = "" if action.clean else "  [partial: had collateral changes]"
            print(f"  #{action.seq:<4} {action.when}  {action.operation:<16}{flag}")
            if action.intent:
                print(f"        {action.intent}")
            for target in action.targets[:3]:
                print(f"        target: {target}")
        print(f"\n  minos undo --last      undo #{actions[0].seq}")
        print("  minos undo <seq>       undo a specific one\n")
        return 0

    try:
        action = find(audit, store, selector)
    except UndoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"\n  undo #{action.seq}  {action.operation}  ({action.when})")
    if action.intent:
        print(f"    {action.intent}")
    for target in action.targets:
        print(f"    restores: {target}")
    if not action.clean:
        print(
            f"    WARNING: this action also changed {len(action.collateral)} undeclared "
            "path(s).\n             Those were never checkpointed and will NOT be restored."
        )

    # Restoring overwrites whatever is there now. That is a write, and writes in
    # this runtime are things you typed.
    if not args.yes and input("\n  proceed? [y/N] ").strip().lower() not in {"y", "yes"}:
        print("  cancelled\n")
        return 1

    try:
        result, redo_id = perform_undo(audit, store, action)
    except UnprotectableTarget as exc:
        print(f"\n  refused: {exc}\n", file=sys.stderr)
        return 1

    if result.succeeded:
        print(f"\n  restored {len(result.restored)} target(s), verified")
        if redo_id:
            print(f"  to reverse this undo:  minos undo {redo_id}")
        print()
        return 0

    print(f"\n  FAILED: {result.detail}")
    for path, reason in result.failed:
        print(f"    {path}: {reason}")
    print()
    return 1


# -- minos index / recall ---------------------------------------------------


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
        print(f"error: no memory index at {db}. Run `minos index <dir>` first.", file=sys.stderr)
        return 2
    with MemoryStore(db) as memory:
        result = resolve(args.phrase, memory)
    print()
    print(result.explain())
    print()
    return 0 if result.best else 1


# -- minos skills -----------------------------------------------------------


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


# -- minos eval -------------------------------------------------------------


def cmd_eval(args: argparse.Namespace) -> int:
    from .evals.__main__ import main as eval_main

    return eval_main(list(args.rest))


# -- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # Environment and .env supply the defaults; a typed flag overrides them,
    # because a flag someone typed is the most specific intent available.
    cfg = settings()

    parser = argparse.ArgumentParser(
        prog="minos",
        description="The model asks. The runtime decides.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"minos {__version__}")
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
        default=cfg.planner,
        choices=["auto", "local", "claude"],
        help=f"auto prefers a local server when one is listening (MINOS_PLANNER={cfg.planner})",
    )
    run.add_argument(
        "--base-url",
        default=cfg.base_url,
        help=f"OpenAI-compatible endpoint: Ollama, LM Studio, ... (MINOS_BASE_URL={cfg.base_url})",
    )
    run.add_argument(
        "--model",
        default=None,
        help=f"MINOS_MODEL={cfg.model} for local, MINOS_REMOTE_MODEL={cfg.remote_model} for claude",
    )
    run.add_argument("--max-steps", type=int, default=cfg.max_steps)
    run.add_argument(
        "--allow-open",
        action="store_true",
        help="grant app.open -- launch files in their default application",
    )
    run.add_argument(
        "--no-watch",
        dest="watch",
        action="store_false",
        help="do not watch the workspace for edits made outside this run",
    )
    run.add_argument("--watch-interval", type=float, default=2.0)
    run.add_argument(
        "--offline",
        action="store_true",
        default=cfg.offline,
        help="refuse to use a remote model; fail instead of reaching the network"
        + (" (MINOS_OFFLINE is set)" if cfg.offline else ""),
    )
    run.add_argument(
        "--allow-gui",
        action="store_true",
        help=(
            "let the agent use your real mouse and keyboard. It shares your "
            "desktop and takes the cursor; Ctrl+Alt+Esc aborts."
        ),
    )
    run.add_argument(
        "--quiet",
        action="store_true",
        help="hide the live trace; print only the final summary",
    )
    run.add_argument(
        "--no-trace",
        dest="trace",
        action="store_false",
        help="do not write a session transcript to .minos/sessions/",
    )
    run.add_argument("--state", default=cfg.state)
    run.add_argument(
        "--wait",
        type=float,
        default=0.0,
        help="seconds to wait for another minos process to finish (default: fail)",
    )
    run.set_defaults(func=cmd_run)

    doctor = sub.add_parser("doctor", help="can this machine run fully offline?")
    doctor.add_argument("--base-url", default=cfg.base_url)
    doctor.set_defaults(func=cmd_doctor)

    audit = sub.add_parser("audit", help="verify and print an audit chain")
    audit.add_argument("path", nargs="?", default=str(Path(cfg.state) / "audit.jsonl"))
    audit.add_argument("-n", "--tail", type=int, default=20)
    audit.add_argument("-v", "--verbose", action="store_true")
    audit.set_defaults(func=cmd_audit)

    undo = sub.add_parser("undo", help="put a past action's files back")
    undo.add_argument(
        "which",
        nargs="?",
        default=None,
        help="audit sequence number or checkpoint id; omit to list what is undoable",
    )
    undo.add_argument("--last", action="store_true", help="undo the most recent action")
    undo.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    undo.add_argument("-n", "--tail", type=int, default=20)
    undo.add_argument("--state", default=cfg.state)
    undo.add_argument("--wait", type=float, default=0.0)
    undo.set_defaults(func=cmd_undo)

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

    # Listed for `minos --help`; main() delegates before this parser sees it.
    evaluate = sub.add_parser(
        "eval", help="run the eval suite (accepts the eval module's own flags)"
    )
    evaluate.add_argument("rest", nargs=argparse.REMAINDER)
    evaluate.set_defaults(func=cmd_eval)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `minos eval --only read.` must reach the eval parser intact. argparse's
    # REMAINDER does not capture a leading flag -- the top-level parser claims
    # it first -- so delegate before parsing rather than fighting that.
    if argv and argv[0] == "eval":
        from .evals.__main__ import main as eval_main

        return eval_main(argv[1:])

    args = build_parser().parse_args(argv)
    state = getattr(args, "state", None)
    if state is None:
        # Read-only commands (audit, doctor, recall) take no state directory and
        # need no lock.
        return int(args.func(args))

    from .locking import LockBusy, lock_state

    try:
        lock = lock_state(state, timeout=getattr(args, "wait", 0.0))
        lock.acquire()
    except LockBusy as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("       `minos <command> --wait 60` waits instead of failing.", file=sys.stderr)
        return 2

    try:
        return int(args.func(args))
    finally:
        _apply_retention(state)
        lock.release()


def _apply_retention(state: str | None) -> None:
    """Apply the retention policy from MINOS_CHECKPOINT_DAYS / _GB.

    Runs on the way out so it never delays the command, and never raises: a
    failure to tidy up must not turn a successful action into a failed one.
    """
    if not state:
        return
    checkpoints = Path(state).expanduser() / "checkpoints"
    if not checkpoints.exists():
        return
    try:
        from .checkpoint import FileCheckpointStore

        cfg = settings()
        FileCheckpointStore(checkpoints).prune(
            max_age_days=cfg.checkpoint_days,
            max_bytes=int(cfg.checkpoint_gb * (1 << 30)),
        )
    except Exception:
        # Tidying must never turn a successful command into a failed one.
        pass


if __name__ == "__main__":
    raise SystemExit(main())
