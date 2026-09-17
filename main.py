#!/usr/bin/env python
"""Interactive menu for writ.

    uv run python main.py

Pick a number, type your own goal. Everything the CLI does, without having to
remember flags.

Deliberately plain: ASCII only (a Windows console is cp1252 and raises on box
drawing), no dependencies, and the current scopes printed before anything runs
so you can always see what you granted.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from writ.planner.local import SUGGESTED_MODELS, server_available

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
    state: Path = field(default_factory=lambda: Path(".writ").resolve())
    max_steps: int = 20

    @property
    def effective_planner(self) -> str:
        if self.planner != "auto":
            return self.planner
        return "local" if server_available(self.base_url) else "claude"

    @property
    def effective_model(self) -> str:
        if self.model:
            return self.model
        return "qwen3:8b" if self.effective_planner == "local" else "claude-opus-5"

    def scopes(self) -> list[str]:
        scopes = [f"fs.read:{self.workspace}/**"]
        if self.allow_write:
            scopes.append(f"fs.write:{self.workspace}/**")
        if self.allow_delete:
            scopes.append(f"fs.delete:{self.workspace}/**")
        return scopes

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
    print("  writ -- the model asks, the runtime decides")
    print("=" * 62)
    print(f"  workspace : {settings.workspace}")
    print(f"  planner   : {settings.effective_planner}  ({settings.effective_model})")
    print(f"  local llm : {'running' if local_up else 'not detected at ' + settings.base_url}")
    if settings.offline:
        print("  offline   : ON -- a remote model will never be called")

    permissions = ["read"]
    if settings.allow_write:
        permissions.append("write")
    if settings.allow_delete:
        permissions.append("delete")
    flags = ", ".join(permissions) + ("  [DRY RUN]" if settings.dry_run else "")
    print(f"  permitted : {flags}")


MENU = """
  1. Run the agent          (type your own goal)
  2. Demo                   dry-run -> execute -> rollback, no model needed
  3. Eval suite             15 tasks, including containment
  4. Index a folder         build the memory index
  5. Recall                 "the excel from yesterday"
  6. Audit log              verify the hash chain
  7. Skills                 list what has been promoted
  8. Settings               workspace, planner, model, permissions
  9. Doctor                 can this machine run fully offline?
  0. Quit
"""


def main() -> int:
    from writ.__main__ import main as cli

    settings = Settings()
    while True:
        banner(settings)
        print(MENU)
        choice = ask("choose", "1")

        if choice in ("0", "q", "quit", "exit", ""):
            print("\nbye\n")
            return 0

        try:
            if choice == "1":
                _run(settings, cli)
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


def _run(settings: Settings, cli) -> None:  # type: ignore[no-untyped-def]
    rule("run the agent")
    print("  Examples:")
    print("    set Q3 revenue in sales_2025.csv to 48200")
    print("    read notes.txt and write a summary into summary.txt")
    print("    make a backup copy of every csv in this folder")

    goal = ask("\n  your goal")
    if not goal:
        print("  nothing to do")
        return

    if not settings.workspace.is_dir():
        print(f"\n  {settings.workspace} does not exist. Set a workspace in Settings (8).")
        return

    print(f"\n  workspace : {settings.workspace}")
    print("  scopes    :")
    for scope in settings.scopes():
        print(f"      {scope}")
    if not settings.allow_write:
        print("\n  NOTE: read-only. Turn on write in Settings (8) if the goal needs it.")
    if settings.dry_run:
        print("  NOTE: dry run -- nothing will actually change.")

    if not yes_no("\n  go", True):
        print("  cancelled")
        return

    cli(settings.cli_args(goal))


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
            settings.planner = {"1": "auto", "2": "local", "3": "claude"}.get(
                ask("  choose", "1"), settings.planner
            )
        elif choice == "3":
            if settings.effective_planner == "local":
                print("\n  Models that fit a consumer GPU and call tools well:")
                for name, note in SUGGESTED_MODELS.items():
                    print(f"    {name:<24} {note}")
                print("\n  Pull one first, e.g.:  ollama pull qwen3:8b")
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


if __name__ == "__main__":
    raise SystemExit(main())
