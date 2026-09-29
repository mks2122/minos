#!/usr/bin/env python
"""Interactive menu for minos.

    uv run python main.py

Pick a number, type your own goal. Everything the CLI does, without having to
remember flags.

Option 1 is a conversation, not a single shot: goals follow on from each other,
``/undo`` puts back what the last one changed, and a step refused for want of
a permission can be granted -- for that one file -- and the goal run again.
Grants join the *next* run. A run in progress never gains authority.

Deliberately plain: ASCII only (a Windows console is cp1252 and raises on box
drawing), no dependencies, and what is permitted is on screen before every
goal so you can always see what you granted.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from minos.approval import LastThinking, SessionApprover, grant_offers
from minos.planner.local import SUGGESTED_MODELS, server_available
from minos.planner.providers import PROVIDERS, key_status

LOCAL_URL = "http://localhost:11434/v1"


@dataclass
class Settings:
    workspace: Path = field(default_factory=lambda: Path("./data").resolve())
    planner: str = "auto"
    model: str = ""
    base_url: str = LOCAL_URL
    allow_write: bool = False
    allow_delete: bool = False
    dry_run: bool = False
    offline: bool = False
    allow_open: bool = False
    state: Path = field(default_factory=lambda: Path(".minos").resolve())
    max_steps: int = 20
    grants: list[str] = field(default_factory=list)
    """One-file scopes granted during this session, after a denial. Gone on exit."""

    @property
    def effective_planner(self) -> str:
        if self.planner != "auto":
            return self.planner
        return "local" if server_available(self.base_url) else "claude"

    @property
    def effective_model(self) -> str:
        if self.model:
            return self.model
        planner = self.effective_planner
        if planner == "local":
            return "qwen3:8b"
        if planner in PROVIDERS:
            return PROVIDERS[planner].default_model or "(set a model)"
        return "claude-opus-5"

    def scopes(self) -> list[str]:
        scopes = [f"fs.read:{self.workspace}/**"]
        if self.allow_write:
            scopes.append(f"fs.write:{self.workspace}/**")
        if self.allow_delete:
            scopes.append(f"fs.delete:{self.workspace}/**")
        if self.allow_open:
            scopes.append(f"app.open:{self.workspace}/**")
        return scopes + self.grants

    def cli_args(self, goal: str) -> list[str]:
        args = [
            "run",
            goal,
            "-w",
            str(self.workspace),
            "--planner",
            self.planner,
            "--base-url",
            self.base_url,
            "--state",
            str(self.state),
            "--max-steps",
            str(self.max_steps),
        ]
        if self.model:
            args += ["--model", self.model]
        if self.allow_write:
            args.append("--allow-write")
        if self.allow_delete:
            args.append("--allow-delete")
        if self.allow_open:
            args.append("--allow-open")
        if self.dry_run:
            args.append("--dry-run")
        if self.offline:
            args.append("--offline")
        return args


def rule(title: str = "") -> None:
    print("\n" + (f"-- {title} " + "-" * max(0, 58 - len(title)) if title else "-" * 62))


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    return answer or default


def yes_no(prompt: str, default: bool = False) -> bool:
    answer = ask(f"{prompt} (y/n)", "y" if default else "n").lower()
    return answer.startswith("y")


def banner(settings: Settings) -> None:
    local_up = server_available(settings.base_url)
    print("\n" + "=" * 62)
    print("  minos -- the model asks, the runtime decides")
    print("=" * 62)
    print(f"  workspace : {settings.workspace}")
    print(f"  planner   : {settings.effective_planner}  ({settings.effective_model})")
    print(f"  local llm : {'running' if local_up else 'not detected at ' + settings.base_url}")
    if settings.offline:
        print("  offline   : ON -- a remote model will never be called")
    print(f"  toggles   : {toggles(settings)}")


# key, attribute, label. One keystroke in the menu; the same with a slash in a
# conversation, where a bare letter would be a goal.
TOGGLES = (
    ("w", "allow_write", "write"),
    ("d", "allow_delete", "delete"),
    ("a", "allow_open", "apps"),
    ("x", "dry_run", "dry-run"),
    ("o", "offline", "offline"),
)


def toggles(settings: Settings, prefix: str = "") -> str:
    parts = [
        f"{prefix}{key}:{label} {'ON' if getattr(settings, attr) else 'off'}"
        for key, attr, label in TOGGLES
    ]
    if settings.grants:
        parts.append(f"+{len(settings.grants)} granted")
    return "  ".join(parts)


def toggle(settings: Settings, key: str) -> bool:
    """Flip the setting ``key`` names. False when it names none."""
    for name, attr, label in TOGGLES:
        if key == name:
            setattr(settings, attr, not getattr(settings, attr))
            print(f"  {label}: {'ON' if getattr(settings, attr) else 'off'}")
            return True
    return False


MENU = """
  1. Talk to the agent      goals, follow-ups, /undo -- /help once inside
  2. Demo                   dry-run -> execute -> rollback, no model needed
  3. Eval suite             15 tasks, including containment
  4. Index a folder         build the memory index
  5. Recall                 "the excel from yesterday"
  6. Audit log              verify the hash chain
  7. Skills                 list what has been promoted
  8. Settings               workspace, planner, model, permissions
  9. Doctor                 can this machine run fully offline?
  0. Quit

  w d a x o  flip a toggle above, no need to open Settings
"""


def main() -> int:
    from minos.__main__ import main as cli

    settings = Settings()
    session = Session()
    while True:
        banner(settings)
        print(MENU)
        choice = ask("choose", "1")

        if choice in ("0", "q", "quit", "exit", ""):
            print("\nbye\n")
            return 0
        if toggle(settings, choice.lower()):
            continue

        try:
            if choice == "1":
                _conversation(settings, session)
            elif choice == "2":
                cli(["demo"])
            elif choice == "3":
                _eval(settings, cli)
            elif choice == "4":
                _index(settings, cli)
            elif choice == "5":
                _recall(settings, cli)
            elif choice == "6":
                cli(["audit", str(settings.state / "audit.jsonl")])
            elif choice == "7":
                cli(["skills", "--state", str(settings.state)])
            elif choice == "8":
                _settings(settings)
            elif choice == "9":
                cli(["doctor", "--base-url", settings.base_url])
            else:
                print(f"\n  '{choice}' is not on the menu")
        except KeyboardInterrupt:
            print("\n\n  interrupted -- nothing further was run")
        except Exception as exc:
            print(f"\n  that failed: {type(exc).__name__}: {exc}")

        ask("\npress enter to continue")


# -- actions ---------------------------------------------------------------


@dataclass
class Turn:
    goal: str
    summary: str
    succeeded: bool


@dataclass
class Session:
    """What a conversation remembers between goals. Nothing of it outlives the menu."""

    thinking: LastThinking = field(default_factory=LastThinking)
    approver: SessionApprover = field(init=False)
    history: list[Turn] = field(default_factory=list)
    last: object | None = None
    """The last run's RunReport, while it is still undoable from here."""
    last_goal: str = ""

    def __post_init__(self) -> None:
        # One approver for the whole session, so "[a]lways" survives a retry.
        self.approver = SessionApprover(why=self.thinking.get)

    def context(self, keep: int = 3) -> str:
        return "\n".join(
            f"- asked: {turn.goal}\n  result: {'done' if turn.succeeded else 'not done'}"
            f" -- {turn.summary}"
            for turn in self.history[-keep:]
        )


HELP = """
  Type a goal and press enter. Follow-ups can refer to what came before.

    /undo          put back what the last goal changed
    /retry         run the last goal again
    /new           forget the conversation so far (grants stay)
    /grants        what has been granted this session
    /revoke        drop session grants and "always" approvals
    /w /d /a /x /o flip write, delete, apps, dry-run, offline
    /menu          back to the menu (so does an empty line)
"""


def _conversation(settings: Settings, session: Session) -> None:
    rule("talk to the agent")
    print("  Examples:")
    print("    read notes.txt and write a summary into summary.txt")
    print("    now make it shorter")
    print("    make a backup copy of every csv in this folder")
    print("\n  /help for commands. An empty line goes back to the menu.")

    while True:
        print(f"\n  {toggles(settings, '/')}")
        line = ask("minos>")
        if not line:
            print("  nothing to do" if not session.history else "  back to the menu")
            return
        if line.startswith("/"):
            if not _command(line[1:].strip().lower(), settings, session):
                return
            continue
        _goal(line, settings, session)


def _command(command: str, settings: Settings, session: Session) -> bool:
    """Handle one slash command. False means leave the conversation."""
    if command in ("menu", "back", "q", "quit", "exit"):
        return False
    if command in ("help", "h", "?"):
        print(HELP)
    elif command == "undo":
        _undo_last(settings, session)
    elif command == "retry":
        if session.last_goal:
            _goal(session.last_goal, settings, session)
        else:
            print("  nothing to retry yet")
    elif command == "new":
        session.history.clear()
        print("  conversation cleared; the next goal starts fresh")
    elif command == "grants":
        _show_grants(settings, session)
    elif command == "revoke":
        settings.grants.clear()
        session.approver.always.clear()
        print("  session grants and 'always' approvals dropped")
    elif not toggle(settings, command[:1] if len(command) == 1 else _toggle_key(command)):
        print(f"  unknown command '/{command}' -- /help lists them")
    return True


def _toggle_key(word: str) -> str:
    return {label: key for key, _attr, label in TOGGLES}.get(word, "")


def _show_grants(settings: Settings, session: Session) -> None:
    if not settings.grants and not session.approver.always:
        print("  nothing beyond the toggles has been granted")
        return
    for scope in settings.grants:
        print(f"    {scope}")
    for operation, targets in sorted(session.approver.always):
        print(f"    always approve {operation} on {', '.join(targets)}")


def _goal(goal: str, settings: Settings, session: Session) -> None:
    from minos.__main__ import build_parser, execute_run, locked

    if not settings.workspace.is_dir():
        print(f"\n  {settings.workspace} does not exist. Set a workspace in Settings (8).")
        return

    session.last_goal = goal
    args = build_parser().parse_args(settings.cli_args(goal))
    first_seq = _next_seq(settings)
    try:
        report = locked(
            args,
            lambda: execute_run(
                args,
                approver=session.approver,
                extra_scopes=settings.grants,
                context=session.context(),
                observer=session.thinking,
            ),
        )
    except KeyboardInterrupt:
        # Whatever it managed before the interrupt is still this goal's to undo.
        from minos.__main__ import RunReport

        print("\n\n  interrupted -- the run stopped")
        session.last = RunReport(code=130, first_seq=first_seq, end_seq=_next_seq(settings))
        _offer_undo(settings, session)
        return
    if report is None:
        return  # the state lock was busy, and locked() has said so

    finished = report.trajectory.finished if report.trajectory else None
    session.history.append(
        Turn(goal, finished.summary if finished else "the run failed", report.code == 0)
    )
    session.last = report
    _offer_undo(settings, session)
    _offer_grants(goal, report, settings, session)


def _next_seq(settings: Settings) -> int:
    from minos.audit import AuditLog

    return AuditLog(settings.state / "audit.jsonl").next_seq()


def _undoable(settings: Settings, report) -> list:  # type: ignore[no-untyped-def,type-arg]
    from minos.audit import AuditLog
    from minos.checkpoint import FileCheckpointStore
    from minos.undo import undoable_between

    audit_path = settings.state / "audit.jsonl"
    if report is None or not audit_path.exists():
        return []
    return undoable_between(
        AuditLog(audit_path),
        FileCheckpointStore(settings.state / "checkpoints"),
        report.first_seq,
        report.end_seq,
    )


def _offer_undo(settings: Settings, session: Session) -> None:
    changes = _undoable(settings, session.last)
    if changes:
        print(f"\n  {len(changes)} change(s) from this goal can be put back:  /undo")


def _offer_grants(goal: str, report, settings: Settings, session: Session) -> None:  # type: ignore[no-untyped-def]
    """A step was refused for want of a permission: offer exactly that, then retry.

    The grant is one file, inside the workspace, and joins the next run. Scopes
    never change while a run is going, so a yes here cannot reach the run that
    asked -- only a fresh one, which starts with the grant on screen.
    """
    offers = grant_offers(report.missing, settings.workspace)
    offered = {scope.split(":", 1)[0] for scope in offers}
    others = [g for g in report.missing if not grant_offers([g], settings.workspace)]
    if not offers:
        if others:
            print("\n  refused for lack of a permission this menu does not hand out:")
            for grant in others[:3]:
                print(f"    {grant}")
        return

    print("\n  refused because it was not permitted. It asked for:")
    for scope in offers:
        print(f"    {scope}")
    print("  (that file only, this session only)")
    if not yes_no("  grant and run the goal again", False):
        if "fs.write" in offered and not settings.allow_write:
            print("  not granted. /w allows writing anywhere in the workspace.")
        return
    settings.grants += [scope for scope in offers if scope not in settings.grants]
    _goal(goal, settings, session)


def _undo_last(settings: Settings, session: Session) -> None:
    from minos.__main__ import locked
    from minos.audit import AuditLog
    from minos.checkpoint import FileCheckpointStore, UnprotectableTarget
    from minos.undo import perform_undo

    if session.last is None:
        print("  nothing from this session to undo")
        return

    def body() -> None:
        changes = _undoable(settings, session.last)
        if not changes:
            print("  the last goal changed nothing that can be put back")
            session.last = None
            return
        print(f"\n  put back {len(changes)} change(s), newest first:")
        for action in changes:
            print(f"    #{action.seq:<4} {action.operation:<14} {', '.join(action.targets[:2])}")
            if not action.clean:
                print(
                    f"          also changed {len(action.collateral)} undeclared path(s), "
                    "which will NOT be restored"
                )
        if not yes_no("  go ahead", False):
            print("  left as it is")
            return
        audit = AuditLog(settings.state / "audit.jsonl")
        store = FileCheckpointStore(settings.state / "checkpoints")
        for action in changes:
            try:
                result, _redo = perform_undo(audit, store, action)
            except UnprotectableTarget as exc:
                print(f"    #{action.seq}: refused: {exc}")
                continue
            print(
                f"    #{action.seq}: {'restored, verified' if result.succeeded else result.detail}"
            )
        # Each undo is itself undoable from the CLI: `minos undo` lists it.
        print("  done. `minos undo` can reverse these if that was a mistake.")
        session.last = None

    locked(argparse.Namespace(state=str(settings.state), wait=0.0), body)


def _eval(settings: Settings, cli) -> None:  # type: ignore[no-untyped-def]
    rule("eval suite")
    print("  1. reference planner  (fast, no model, proves the runtime works)")
    print(f"  2. your planner       ({settings.effective_planner}: {settings.effective_model})")
    which = ask("\n  choose", "1")

    if which == "2":
        args = ["eval", "--planner", settings.effective_planner]
        if settings.effective_planner == "local":
            args += ["--base-url", settings.base_url]
        args += ["--model", settings.effective_model]
        print("\n  This calls a model once per step. It will be slower, and if the")
        print("  planner is remote it will cost money.")
        if not yes_no("  continue", False):
            return
        cli(args)
    else:
        cli(["eval"])


def _index(settings: Settings, cli) -> None:  # type: ignore[no-untyped-def]
    rule("index a folder")
    folder = ask("  folder", str(settings.workspace))
    if folder:
        cli(["index", folder, "--state", str(settings.state)])


def _recall(settings: Settings, cli) -> None:  # type: ignore[no-untyped-def]
    rule("recall")
    print("  Try: the excel from yesterday / the budget spreadsheet / the notes")
    phrase = ask("\n  phrase")
    if phrase:
        cli(["recall", phrase, "--state", str(settings.state)])


def _settings(settings: Settings) -> None:
    while True:
        rule("settings")
        print(f"  1. workspace      {settings.workspace}")
        print(f"  2. planner        {settings.planner}  (using: {settings.effective_planner})")
        print(
            f"  3. model          {settings.model or '(default: ' + settings.effective_model + ')'}"
        )
        print(f"  4. local url      {settings.base_url}")
        print(f"  5. allow write    {'yes' if settings.allow_write else 'no'}")
        print(f"  6. allow delete   {'yes' if settings.allow_delete else 'no'}")
        print(f"  7. dry run        {'yes' if settings.dry_run else 'no'}")
        print(f"  8. max steps      {settings.max_steps}")
        print(f"  9. offline only   {'yes' if settings.offline else 'no'}")
        print(f" 10. allow open    {'yes' if settings.allow_open else 'no'}")
        print("  0. back")

        choice = ask("\n  change", "0")
        if choice in ("0", "", "b", "back"):
            return

        if choice == "1":
            path = Path(ask("  workspace path", str(settings.workspace))).expanduser()
            if path.is_dir():
                settings.workspace = path.resolve()
            else:
                print(f"  {path} is not a directory")
        elif choice == "2":
            print("\n  1. auto    prefer a local server when one is listening")
            print("  2. local   an OpenAI-compatible server (Ollama, LM Studio, ...)")
            print("  3. claude  the Anthropic API (needs ANTHROPIC_API_KEY)")
            print("  4. hosted  OpenRouter, OpenAI, Groq, ... (any OpenAI-compatible API)")
            picked = ask("  choose", "1")
            if picked == "4":
                hosted = [(p, ok) for p, ok in key_status() if not p.local]
                print()
                for number, (provider, ready) in enumerate(hosted, 1):
                    mark = "key set" if ready else f"needs {provider.key_env}"
                    print(f"    {number:>2}. {provider.name:<11} {mark}")
                index = ask("  provider", "1")
                if index.isdigit() and 1 <= int(index) <= len(hosted):
                    settings.planner = hosted[int(index) - 1][0].name
                    settings.model = ""
            else:
                settings.planner = {"1": "auto", "2": "local", "3": "claude"}.get(
                    picked, settings.planner
                )
        elif choice == "3":
            if settings.effective_planner == "local":
                print("\n  Models that fit a consumer GPU and call tools well:")
                for name, note in SUGGESTED_MODELS.items():
                    print(f"    {name:<24} {note}")
                print("\n  Pull one first, e.g.:  ollama pull qwen3:8b")
            elif settings.effective_planner in PROVIDERS:
                provider = PROVIDERS[settings.effective_planner]
                print(f"\n  {provider.label} catalogue: {provider.docs}")
                print(f"  default: {provider.default_model or '(none)'}")
            else:
                print("\n    claude-opus-5    most capable")
                print("    claude-sonnet-5  cheaper")
                print("    claude-haiku-4-5 cheapest")
            settings.model = ask("\n  model (blank for the default)", settings.model)
        elif choice == "4":
            settings.base_url = ask("  base url", settings.base_url)
        elif choice == "5":
            settings.allow_write = yes_no("  allow writing files", settings.allow_write)
        elif choice == "6":
            settings.allow_delete = yes_no("  allow deleting files", settings.allow_delete)
        elif choice == "7":
            settings.dry_run = yes_no("  dry run", settings.dry_run)
        elif choice == "8":
            raw = ask("  max steps", str(settings.max_steps))
            if raw.isdigit() and int(raw) > 0:
                settings.max_steps = int(raw)
        elif choice == "9":
            settings.offline = yes_no("  refuse to call any remote model", settings.offline)
        elif choice == "10":
            settings.allow_open = yes_no(
                "  allow opening files in their default app", settings.allow_open
            )


if __name__ == "__main__":
    raise SystemExit(main())
