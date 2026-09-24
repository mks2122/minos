# Changelog

All notable changes to minos are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[PEP 440](https://peps.python.org/pep-0440/) (`0.1.0a1` is the first alpha).

## [Unreleased]

### Added

- **Ghost cursor.** With `--allow-gui`, the agent draws its own orange pointer. It
  glides to each target and pauses there before clicking or typing, so you can see
  where it is about to act. It never takes focus, and clicks pass through it.
  `--no-ghost` turns it off.
- **`--gui-window TITLE`.** Limits mouse and keyboard input to the named windows. An
  action that names no window, or a different one, is refused before anything is sent.
- **Sandbox confinement chosen by who wrote the code.** Code the local planner writes
  runs in a confined subprocess: Landlock and seccomp on Linux, a seatbelt profile on
  macOS, a low-integrity token and a Job Object on Windows. Code from anywhere else
  runs in a container. Untrusted code with no container engine fails closed unless
  `MINOS_SANDBOX_ALLOW_DOWNGRADE=1`. Every run records what actually confined it.
- A live GUI harness, `tests/gui/live_check.py`. It drives disposable windows on a real
  desktop and checks what each window received.
- A second benchmark run of `qwen3:8b` with the 600s limit: 14/18, against 15/18
  before. With reruns of the tasks that moved, EVALUATION.md now reports
  per-task variance rather than a single number.
- Community files: CONTRIBUTING, a code of conduct, issue and pull request templates,
  this changelog, and private vulnerability reporting.

### Changed

- Every GUI tool takes a `window`. The window is brought to the front when the action
  runs, and input stops if something else takes focus.
- `ui.screenshot` lists the window's visible controls by name, so the model can click
  by name instead of guessing.
- After a click by coordinate, the mouse cursor goes back to where you left it.
- A screenshot's fingerprint of the screen is recorded but no longer decides whether
  a GUI action worked. SECURITY.md now says plainly that reads are not confined on
  Windows.

### Fixed

- GUI input went to whichever window was in front, whatever the grant named.
- A background window could not be brought to the front.
- Controls scrolled out of view matched by name and were clicked at (0, 0).
- Pressing a control that cannot be pressed by name crashed instead of falling back to
  a click.
- Most GUI clicks, and any screenshot that focused a window, stopped the task with
  `reconciliation_required`.
- `require_confinement` trusted what the platform offers, not what the run got. A
  script whose token failed to lower still ran. It is now stopped before it starts.
- `--sandbox subprocess` gave untrusted code a weaker jail than the automatic
  fallback.
- A sandboxed script could list directories outside its workspace.
- Test isolation: `.env` values loaded by one test leaked into later ones.
- **Grants did not match through symlinks.** A target was compared by its resolved
  path and the grant by the path as written. On macOS, where `/var` is
  `/private/var`, a grant for a workspace in the temp directory matched nothing in
  it. On Ubuntu, `proc.spawn:/bin/echo` never matched, because `/bin` is `/usr/bin`.
  A grant's fixed prefix is now resolved when it is made.
- **Two runs could hold the state lock at once.** A run that read the lock file
  while another was still writing its pid took it for a crashed one and deleted
  it. That broke the audit chain. On Windows, a failed delete on release could
  also leave the lock held until every other run timed out.
- **On Linux and macOS the sandbox broke numpy, pandas and openpyxl.** It gave
  them no read access to system libraries or the mime tables.
- CI type-checked only as Linux, so Windows-only code failed there unnoticed. It now
  checks all three platforms, and posts failing test names where they can be read
  without signing in.

## [0.1.0a1] - 2026-09-22

The first alpha. A local agent that works on your real files and desktop, with a
policy broker between the model and anything it touches.

### Added

- **Policy broker** with capability scopes, effect contracts, oracles, and a
  hash-chained audit log. The planner never executes (I1), every tier passes the same
  gate (I2), and falling back to a weaker tier is recorded (I3).
- **Effect classes**: `PURE`, `REVERSIBLE` (checkpointed before the change),
  `COMPENSABLE` (a declared inverse, which is executed) and `IRREVERSIBLE` (asks
  first).
- **Checkpoints and `minos undo`.** Every change is copied before it is written. An
  undo can itself be undone. Retention defaults to 7 days or 2 GB.
- **Tiers**: L1 system adapters (files, processes, apps), L2 typed adapters
  (spreadsheets), L2.5 a code sandbox for work no adapter covers (PDF to Word, for
  example), and L3 GUI with a real mouse and keyboard, a panic key (Ctrl+Alt+Esc) and
  clicking controls by name.
- **Local-first planning** over any OpenAI-compatible endpoint (Ollama by default),
  with an `--offline` guarantee and a Claude planner as an option.
- **Memory** of the computer's state, so vague references like "the excel from
  yesterday" resolve to a real path, or to a question when they are ambiguous.
- **`minos doctor`**, which reports what is installed and what the server is actually
  serving, including a context window the server silently ignored.
- **Live trace and session transcripts**, including the model's reasoning, plus
  context compaction for long tasks.
- **Eval suite**: 18 tasks, including long-horizon ones and tasks the agent should
  refuse, checked against 11 invariants. The reference planner scores 18/18.
  `qwen3:8b`, running fully offline, scores 15/18.
- Configuration through `.env`, and CI on Linux, macOS and Windows.

[Unreleased]: https://github.com/mks2122/minos/compare/v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/mks2122/minos/releases/tag/v0.1.0a1
