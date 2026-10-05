"""``minos`` -- the command line.

    minos doctor                                 can this machine run fully offline?
    minos providers                              hosted and local model providers
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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from . import __version__
from .config import settings

if TYPE_CHECKING:
    from .broker import Approver
    from .planner.base import Planner, Trajectory
    from .trace import Observer
    from .types import Grant

_T = TypeVar("_T")

DEFAULT_STATE = Path(".minos")


def _planner_choices() -> tuple[str, ...]:
    from .planner.providers import PROVIDERS

    return ("auto", "local", "claude", *PROVIDERS, "custom")


PLANNER_CHOICES = _planner_choices()

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


@dataclass
class RunReport:
    """What one run did, for a caller that keeps going afterwards.

    ``first_seq`` to ``end_seq`` (exclusive) are the audit entries this run
    wrote, which is exactly the set "undo what that just did" should reach --
    no further back, and nothing a later run did.
    """

    code: int
    trajectory: Trajectory | None = None
    first_seq: int = 0
    end_seq: int = 0
    missing: tuple[Grant, ...] = ()
    """Grants that denied steps needed and no scope covered. Never applied here:
    scopes are fixed for the length of a run."""


def cmd_run(args: argparse.Namespace) -> int:
    return execute_run(args).code


def execute_run(
    args: argparse.Namespace,
    *,
    approver: Approver | None = None,
    extra_scopes: Sequence[str] = (),
    context: str = "",
    observer: Observer | None = None,
    planner: Planner | None = None,
) -> RunReport:
    """``minos run``, for a caller that wants the result and not just an exit code.

    ``extra_scopes`` join the granted set before the run starts; they cannot
    arrive during it. ``context`` is what earlier goals in a conversation did,
    handed to the planner after the goal itself. ``planner`` replaces the one
    the flags would build, which is how the tests drive a real run.
    """
    from .agent import Agent, AgentLimits
    from .approval import LastThinking, SessionApprover, terminal_ask
    from .audit import AuditLog
    from .broker import Broker
    from .checkpoint import FileCheckpointStore
    from .memory import FileWatcher, MemoryStore
    from .router import Router
    from .sandbox import CodeOrigin
    from .scopes import ScopeSet
    from .tiers.base import Adapter
    from .tiers.l1_system import (
        AppAdapter,
        BrowserAdapter,
        FilesystemAdapter,
        MemoryAdapter,
        ProcessAdapter,
        UserAdapter,
    )
    from .tiers.l2_adapters import TabularAdapter, WebAdapter, playwright_available
    from .tiers.l2_code import CodeAdapter
    from .tiers.l3_gui import GuiAdapter
    from .trace import ConsolePrinter, SessionRecorder, fan_out

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"error: {workspace} is not a directory", file=sys.stderr)
        return RunReport(code=2)

    state = Path(args.state).expanduser().resolve()

    # --resume continues a recorded run: its goal (unless a new one was typed)
    # and a summary of what it did. Never its scopes -- those are the flags
    # typed now, because a grant must come from the person, before the task.
    resumed_from = ""
    if getattr(args, "resume", None) is not None:
        from .trace import find_session, resume_context

        try:
            session = find_session(state, args.resume)
        except LookupError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return RunReport(code=2)
        args.goal = args.goal or session.goal
        resumed_from = session.session_id
        context = resume_context(session) + (f"\n\n{context}" if context.strip() else "")
        print(
            f"resuming  : session {session.session_id}, {len(session.steps)} step(s) recorded"
            + ("" if session.interrupted else " (it had finished)")
        )
    if not getattr(args, "goal", ""):
        print("error: give a goal, or --resume to continue a recorded run", file=sys.stderr)
        return RunReport(code=2)

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
    if args.gui_window:
        # Input only for the named windows. An action that names no window, or
        # another one, is outside the grant and is refused before it is sent.
        args.allow_gui = True
        scopes.extend(f"ui.input:{title}" for title in args.gui_window)
    elif args.allow_gui:
        # The virtual device drives the real desktop. Never implicit.
        scopes.append("ui.input:*")
    allow_browser = bool(getattr(args, "allow_browser", False) or args.allow_gui)
    # The web tier acts in pages without the mouse, so --allow-gui implies it:
    # it is strictly less reach than the real input device.
    want_web = bool(getattr(args, "allow_web", False) or args.allow_gui)
    web = want_web and playwright_available()
    if allow_browser or web:
        # Opening a page is a read; acting in one is web.input or ui.input.
        scopes.append("browser.open:*")
    if web:
        scopes.append("web.input:*")
    # Someone is at the terminal to answer. Not under --yes, which means
    # "unattended", and not through a pipe, where input() would read the pipe.
    can_ask = not args.yes and sys.stdin.isatty()
    if can_ask:
        scopes.append("user.ask:*")
    # Granted by a person between runs, one exact path each. See minos.approval.
    scopes.extend(extra_scopes)

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
    if web:
        # One way to a website, not two. Offered both, a small model opens the
        # page in the person's browser and is back to clicking coordinates.
        operations += ("web.open", "web.snapshot", "web.read", "web.click", "web.fill", "web.press")
    elif allow_browser:
        operations += ("browser.open",)
    if args.allow_gui:
        operations += ("ui.click", "ui.type", "ui.key", "ui.screenshot")
    if can_ask:
        operations += ("user.ask",)

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

    isolated: list[Any] = []
    web_adapter = WebAdapter.launching() if web else None

    def release() -> None:
        # A one-shot CLI could leave these to process exit. A conversation runs
        # many goals in one process, and a leaked watcher keeps scanning.
        watcher.stop()
        memory.close()
        for child in isolated:
            child.close()
        if web_adapter is not None:
            web_adapter.close()

    if planner is None:
        try:
            planner = _planner(args, operations)
        except ImportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            release()
            return RunReport(code=2)
        if getattr(args, "isolate_planner", False):
            try:
                planner = _isolate(planner)
            except Exception as exc:
                # Refusing to run is the safe failure: the person asked for an
                # isolated planner, and an in-process one is not a substitute.
                print(f"error: could not start the planner process: {exc}", file=sys.stderr)
                print("       --in-process-planner runs it unisolated.", file=sys.stderr)
                release()
                return RunReport(code=2)
            describe = getattr(planner, "describe", None)
            if describe is not None:
                isolated.append(planner)
                print(f"isolation : planner {describe()}")
    code_origin = _code_origin(args)

    gui_adapters: tuple[Adapter, ...] = ()
    panic: Any = None
    ghost: Any = None
    if args.allow_gui:
        from .tiers.l3_gui import PanicAbort, WindowsDriver, panic_watcher  # noqa: F401

        try:
            panic = panic_watcher()
            if args.ghost:
                from .tiers.l3_gui.ghost import GhostCursor

                # Its own pointer, so the person can see where it is about to
                # act. Losing it is not worth failing the run over.
                try:
                    ghost = GhostCursor()
                except RuntimeError as exc:
                    print(f"  (no ghost cursor: {exc})", file=sys.stderr)
            gui_adapters = (GuiAdapter(driver=WindowsDriver(panic=panic, ghost=ghost)),)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            release()
            return RunReport(code=2)

    thinking = LastThinking()
    if approver is None:
        approver = (
            (lambda inv, dec: True)
            if args.yes
            else SessionApprover(why=thinking.get, gui=getattr(args, "gui_confirm", "all"))
        )
    ask = terminal_ask if can_ask else None
    broker = Broker(
        scopes=ScopeSet.parse(scopes),
        audit=AuditLog(state / "audit.jsonl"),
        store=FileCheckpointStore(state / "checkpoints"),
        approver=approver,
        dry_run=args.dry_run,
    )
    # Live trace to the terminal, and a transcript kept for afterwards. The
    # transcript is a debugging record, deliberately separate from the audit
    # chain -- see minos.trace.
    recorder = (
        SessionRecorder(state=state, goal=args.goal, resumed_from=resumed_from)
        if args.trace
        else None
    )
    printer = ConsolePrinter(show_thinking=not args.quiet, show_code=not args.quiet)

    agent = Agent(
        observer=fan_out(printer if not args.quiet else None, recorder, thinking, observer),
        planner=planner,
        router=Router(
            adapters=(
                FilesystemAdapter(),
                AppAdapter(),
                BrowserAdapter(preferences=memory, ask=ask),
                UserAdapter(ask=ask),
                MemoryAdapter(memory, roots=(workspace,)),
                ProcessAdapter(),
                TabularAdapter(),
                *((web_adapter,) if web_adapter is not None else ()),
                CodeAdapter(
                    state=state,
                    origin=CodeOrigin(code_origin),
                    prefer=args.sandbox,
                    allow_downgrade=args.allow_unconfined,
                    timeout=args.sandbox_timeout,
                ),
                *gui_adapters,
            )
        ),
        broker=broker,
        limits=AgentLimits(max_steps=args.max_steps, split=getattr(args, "split", True)),
    )

    # A context too small to hold the tool schemas is invisible at runtime: the
    # server truncates silently and the model simply gets worse at its job. Say
    # it before the run rather than leaving someone to conclude the model is bad.
    warning = _context_warning(planner, args.base_url)
    if warning:
        print(f"\n  !! {warning}")

    if want_web and not web:
        print(
            "\n  (no web tier: Playwright is not installed -- uv sync --extra web. "
            "Websites fall back to the GUI tier.)"
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

    first_seq = broker.audit.next_seq()
    task = _with_context(args.goal, context)
    try:
        if panic is not None:
            print("  !! the agent will use your real mouse and keyboard.")
            print("     press Ctrl+Alt+Esc to abort and release input.")
            if ghost is not None:
                print("     the orange pointer shows where it will act, before it does.")
            print()
            try:
                with panic:
                    trajectory = agent.run_task(task, goal_id="cli")
            finally:
                if ghost is not None:
                    ghost.close()
        else:
            trajectory = agent.run_task(task, goal_id="cli")
    except Exception as exc:
        printer.stop_spinner()
        print(f"\nthe planner failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if _looks_like_missing_credentials(exc):
            print(_CREDENTIALS_HINT, file=sys.stderr)
        print(
            "\nnothing was changed beyond what the audit log records "
            f"({broker.audit.path}, {broker.audit.count} entries).",
            file=sys.stderr,
        )
        release()
        # Steps before the failure may have changed files, and those are still
        # this run's to undo.
        return RunReport(code=2, first_seq=first_seq, end_seq=broker.audit.next_seq())
    except BaseException:
        printer.stop_spinner()
        release()
        raise

    print()
    print("-" * 64)
    for line in summary_lines(trajectory):
        print(line)

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
    if web_adapter is not None:
        web_adapter.close()
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

    for child in isolated:
        child.close()

    missing: list[Grant] = []
    for outcome in trajectory.outcomes:
        if outcome.status == "denied":
            missing += [g for g in broker.missing_grants(outcome.invocation) if g not in missing]
    return RunReport(
        code=0 if trajectory.succeeded else 1,
        trajectory=trajectory,
        first_seq=first_seq,
        end_seq=broker.audit.next_seq(),
        missing=tuple(missing),
    )


def summary_lines(trajectory: Any) -> list[str]:
    """One line per step, from the observations.

    Not from zipping steps against outcomes: a step no adapter could route has
    no outcome, and pairing the two lists by position shifts every line after
    it onto the wrong step.
    """
    tiers = {id(o.invocation.request): o.invocation.tier for o in trajectory.outcomes}
    lines: list[str] = []
    for observation in trajectory.observations:
        status = observation.status
        mark = {"ok": "ok  ", "denied": "DENY", "dry_run": "dry ", "unroutable": "SKIP"}.get(
            status, "FAIL"
        )
        tier = tiers.get(id(observation.request))
        where = f"[{tier}] " if tier is not None else ""
        lines.append(f"  {mark}  {where}{observation.request.operation}")
        if status not in ("ok", "dry_run"):
            lines.append(f"        {observation.error or observation.detail}")
    return lines


def _with_context(goal: str, context: str) -> str:
    """The goal first, then what earlier goals in the conversation did.

    The goal leads so it stays the instruction; the history is background the
    planner can use to resolve "that file" or "now do the same for B".
    """
    if not context.strip():
        return goal
    return (
        f"{goal}\n\n"
        "Earlier in this session (already done; do not redo it unless asked):\n"
        f"{context.strip()}"
    )


def _code_origin(args: argparse.Namespace) -> str:
    """Who wrote the code ``code.run`` will execute, for the sandbox policy.

    A typed flag wins, then MINOS_SANDBOX_ORIGIN if it was actually set. With
    neither, a hosted model's code is ``remote-planner`` and goes to a
    container: the task is yours, but the context that produced the script
    passed through someone else's servers.
    """
    if getattr(args, "code_origin", None):
        return str(args.code_origin)
    cfg = settings()
    if cfg.source.get("MINOS_SANDBOX_ORIGIN") == "environment":
        return cfg.sandbox_origin
    return "remote-planner" if getattr(args, "planner_hosted", False) else cfg.sandbox_origin


def _isolate(planner: Any) -> Any:
    """Move a model planner into its own confined process.

    Only the planners that talk to a model are moved: they are the ones that
    parse untrusted output with third-party code. Anything else -- a scripted
    planner in a test -- stays where it is.
    """
    from .planner.claude import ClaudePlanner
    from .planner.isolated import IsolatedPlanner
    from .planner.local import LocalPlanner

    if not isinstance(planner, LocalPlanner | ClaudePlanner):
        return planner
    timeout = float(getattr(planner, "timeout", 600.0)) + 120.0
    return IsolatedPlanner.of(planner, timeout=timeout)


def _context_warning(planner: Any, base_url: str) -> str:
    """Compare the planner's fixed overhead against what the server really serves."""
    check = getattr(planner, "context_warning", None)
    if check is None or not getattr(planner, "local_server", True):
        # Only a local server can be asked what context it really serves.
        return ""
    from .doctor import _served_context

    try:
        served = _served_context(base_url, getattr(planner, "model", ""))
        if served > 0 and hasattr(planner, "context_window"):
            # Fit to what Ollama really serves, not to what was asked for: its
            # OpenAI endpoint ignores num_ctx, and a planner budgeting for 16k
            # against a 4k window is exactly the silent truncation this avoids.
            planner.context_window = served
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

    args.planner_hosted = False

    if offline and choice == "claude":
        raise ImportError(
            "--offline was given but --planner claude would call a remote API. "
            "Drop --offline, or start a local server (see: minos doctor)."
        )

    if choice not in ("auto", "local", "claude"):
        from .planner.providers import MissingKey, build_planner, resolve

        try:
            provider = resolve(
                choice,
                model=args.model or cfg.hosted_model,
                base_url=args.base_url if choice == "custom" else "",
            )
        except (MissingKey, ValueError) as exc:
            raise ImportError(str(exc)) from exc
        if offline and provider.hosted:
            raise ImportError(
                f"--offline was given but --planner {choice} is {provider.label} at "
                f"{provider.base_url}, which is not on this machine."
            )
        args.planner_hosted = provider.hosted
        print(f"planner   : {provider.name} ({provider.label}), model {provider.model}")
        return build_planner(
            provider,
            operations,
            context_tokens=cfg.context_tokens,
            timeout=cfg.planner_timeout,
            thinking=cfg.thinking,
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
    args.planner_hosted = True
    try:
        from .planner.claude import ClaudePlanner
    except ImportError as exc:
        raise ImportError(
            "the Claude planner needs the anthropic SDK:\n"
            "    uv sync --extra claude\n"
            "and credentials in ANTHROPIC_API_KEY (or `ant auth login`)."
        ) from exc
    return ClaudePlanner(operations=operations, model=args.model or cfg.remote_model)


# -- minos providers --------------------------------------------------------


def cmd_providers(args: argparse.Namespace) -> int:
    """Every preset, where it points, and whether its key is set."""
    from .planner.providers import key_status

    settings()  # load .env, so a key kept there counts
    print("\n  provider     where    key                         default model")
    print("  " + "-" * 76)
    for provider, ready in key_status():
        where = "local" if provider.local else "hosted"
        if not provider.key_env:
            key = "none needed"
        else:
            key = f"{provider.key_env} {'set' if ready else 'MISSING'}"
        print(f"  {provider.name:<12} {where:<8} {key:<27} {provider.default_model or '-'}")
    print(
        '\n  minos run "..." --planner openrouter --model openai/gpt-4.1'
        "\n  custom: MINOS_BASE_URL + MINOS_API_KEY + --model, any OpenAI-compatible server"
        "\n  claude: Anthropic direct, ANTHROPIC_API_KEY\n"
    )
    return 0


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


def cmd_sessions(args: argparse.Namespace) -> int:
    """Recorded runs, newest first, and whether each one finished."""
    from .trace import list_sessions

    sessions = list_sessions(Path(args.state))
    if not sessions:
        print("no recorded sessions")
        return 0
    print(f"\n  {'id':<14} {'steps':>5}  {'outcome':<12} goal")
    print("  " + "-" * 72)
    for session in sessions[: args.tail]:
        if session.interrupted:
            outcome = "interrupted"
        else:
            outcome = "succeeded" if session.succeeded else "did not"
        goal = session.goal if len(session.goal) <= 44 else session.goal[:41] + "..."
        print(f"  {session.session_id:<14} {len(session.steps):>5}  {outcome:<12} {goal}")
    print("\n  minos run --resume [ID] continues one; scopes come from the flags you give.\n")
    return 0


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
    from .sandbox import CodeOrigin

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
    run.add_argument("goal", nargs="?", default="")
    run.add_argument(
        "--resume",
        nargs="?",
        const="",
        default=None,
        metavar="SESSION",
        help=(
            "continue a recorded run: the newest, or the session whose id starts "
            "with SESSION (see `minos sessions`). Scopes still come from the flags"
        ),
    )
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
        choices=PLANNER_CHOICES,
        help=(
            "auto prefers a local server when one is listening; claude is Anthropic "
            "direct; any other name is an OpenAI-compatible provider, see "
            f"`minos providers` (MINOS_PLANNER={cfg.planner})"
        ),
    )
    run.add_argument(
        "--base-url",
        default=cfg.base_url,
        help=f"OpenAI-compatible endpoint: Ollama, LM Studio, ... (MINOS_BASE_URL={cfg.base_url})",
    )
    run.add_argument(
        "--model",
        default=None,
        help=(
            f"MINOS_MODEL={cfg.model} for local, MINOS_REMOTE_MODEL={cfg.remote_model} "
            "for claude, MINOS_HOSTED_MODEL or the preset default for a provider"
        ),
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
        "--no-ghost",
        dest="ghost",
        action="store_false",
        help=(
            "do not draw the agent's own orange pointer, which shows where it "
            "is about to click or type before it does"
        ),
    )
    run.add_argument(
        "--allow-browser",
        action="store_true",
        help=(
            "let the agent open web pages in your browser (implied by --allow-gui). "
            "It asks which browser profile to use once per site and remembers."
        ),
    )
    run.add_argument(
        "--allow-web",
        action="store_true",
        help=(
            "let the agent act in web pages -- click, fill, press -- in a browser "
            "it controls, by the page's controls rather than the mouse (implied by "
            "--allow-gui; needs: uv sync --extra web). Sign in once in that "
            "window; it keeps its own profile in ~/.minos/browser."
        ),
    )
    run.add_argument(
        "--no-split",
        dest="split",
        action="store_false",
        help="run the goal as one task, without splitting a large one into subtasks",
    )
    run.add_argument(
        "--gui-confirm",
        choices=("commit", "all"),
        default="commit",
        help=(
            "which GUI inputs stop for approval: 'commit' (default) only clicks on "
            "Post/Send/Delete/Pay..., Ctrl+Enter, multi-line typing and clicks by "
            "coordinate; 'all' asks for every input"
        ),
    )
    run.add_argument(
        "--gui-window",
        action="append",
        metavar="TITLE",
        help=(
            "like --allow-gui, but input may only go to the window with this "
            "title. Repeat for more than one. Safer: nothing can be typed into "
            "whatever else happens to be in front."
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
    run.add_argument(
        "--sandbox",
        choices=("auto", "subprocess", "container"),
        default=cfg.sandbox_backend,
        help="what runs the model's code (default: auto, decided by --code-origin)",
    )
    run.add_argument(
        "--code-origin",
        choices=tuple(o.value for o in CodeOrigin),
        default=None,
        help=(
            "who wrote the code. Only local-planner is trusted with the "
            "subprocess jail; everything else needs a container. Default: "
            "remote-planner when the model is hosted, else "
            f"MINOS_SANDBOX_ORIGIN={cfg.sandbox_origin}"
        ),
    )
    run.add_argument(
        "--sandbox-timeout",
        type=float,
        default=cfg.sandbox_timeout,
        help="seconds a single sandboxed script may run (default: %(default)s)",
    )
    run.add_argument(
        "--allow-unconfined",
        action="store_true",
        default=cfg.sandbox_allow_downgrade,
        help="let untrusted code run without a container when none is available",
    )
    run.add_argument(
        "--in-process-planner",
        dest="isolate_planner",
        action="store_false",
        default=cfg.isolate_planner,
        help=(
            "run the planner inside this process instead of a confined child that "
            "cannot write files or start programs (MINOS_ISOLATE_PLANNER)"
        ),
    )
    run.add_argument("--state", default=cfg.state)
    run.add_argument(
        "--wait",
        type=float,
        default=0.0,
        help="seconds to wait for another minos process to finish (default: fail)",
    )
    run.set_defaults(func=cmd_run)

    providers = sub.add_parser("providers", help="list model providers and which keys are set")
    providers.set_defaults(func=cmd_providers)

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

    sessions = sub.add_parser("sessions", help="list recorded runs, to --resume one")
    sessions.add_argument("-n", "--tail", type=int, default=20)
    sessions.add_argument("--state", default=cfg.state)
    sessions.set_defaults(func=cmd_sessions)

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
    code = locked(args, lambda: int(args.func(args)))
    return 2 if code is None else code


def locked(args: argparse.Namespace, body: Callable[[], _T]) -> _T | None:
    """Run ``body`` holding the state lock that ``args.state`` names.

    Returns None, having said why, when another minos process holds it.
    Read-only commands (audit, doctor, recall) take no state directory and need
    no lock. Anything that drives a run or an undo from outside this module
    goes through here too, so two of them never write one audit chain at once.
    """
    state = getattr(args, "state", None)
    if state is None:
        return body()

    from .locking import LockBusy, lock_state

    try:
        lock = lock_state(state, timeout=getattr(args, "wait", 0.0))
        lock.acquire()
    except LockBusy as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("       `minos <command> --wait 60` waits instead of failing.", file=sys.stderr)
        return None

    try:
        return body()
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
