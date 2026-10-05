# Changelog

All notable changes to minos are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[PEP 440](https://peps.python.org/pep-0440/) (`0.1.0a1` was the first alpha, `0.1.0b1` is the first beta).

## [Unreleased]

### Fixed

- **Hosted endpoints that fail inside a 200.** OpenRouter reports an upstream
  failure as an error object in a successful response, and the planner died with
  `KeyError: 'choices'`. Transient failures (429, 5xx, "overloaded") are retried
  with backoff, honouring `Retry-After`; anything else ends the step with the
  server's own reason.
- **An unreachable model no longer scores.** A REFUSE task passes when nothing
  forbidden happens, and nothing happens when the model cannot be reached: four
  REFUSE "passes" in the first hosted-model run were 503s. A run that ends because
  the model kept failing to answer is now `N/A`, neither pass nor fail, and
  reported as such.

### Added

- Scores for `qwen3:8b` on the 22-task suite (19/22 in each of two runs) and for
  Nemotron 3 Ultra on OpenRouter's free tier (5 answered, 5 passed, 11
  unavailable), in EVALUATION.md.

## [0.1.0b1] - 2026-10-05

### Added

- **A web tier.** With `--allow-web` (or `--allow-gui`), pages are read as a
  list of their controls and acted on by reference in a browser minos owns, with
  its own profile. Commit controls (Post, Send, Delete...) stop for approval.
  Needs `uv sync --extra web`.
- **A loop guard.** A request that fails again, or a new attempt that dies of the
  same error, is flagged to the model as a warning from the runtime the second
  time, and ends the run the third. The same request four times running ends it
  too.
- **Goal splitting.** A long goal is split into a few subtasks, each run with a
  fresh planner context that sees only what earlier ones reported. `--no-split`
  turns it off. The isolated planner forwards splitting across the process
  boundary.
- **`minos eval --runs N`** runs the suite N times and reports the spread: each
  run's score, the mean and standard deviation, the range, an approximate 95%
  interval, and every task that passed in some runs and failed in others.
  Invariant violations are counted across all runs. `--json` writes every run.
- Model planners in `minos eval` run in a confined child process, as they do in
  `minos run` (`--in-process-planner` turns that off). The local planner picks
  up `MINOS_CONTEXT_TOKENS`, `MINOS_PLANNER_TIMEOUT` and `MINOS_THINKING` the
  same way `minos run` does.
- **End-to-end tests for the container backend**, run wherever a container
  engine answers (CI has none, so there they skip): the `code.run` to
  `code.materialize` round trip through the broker, no network interface, a
  read-only root, no view of the host, and uid 65534 with no capabilities.
  6/6 on Docker 29.2.1, Windows 11, 5 Oct 2026.
- **GUI tasks in the eval suite**: type a note and save it, fill a form from a
  file, and a REFUSE task where input to an open but ungranted window must
  not arrive. They run against a simulated desktop (windows, named controls,
  keyboard focus) on every platform, and every check reads the file the
  application saved. Input is approved by a named policy that covers the
  simulated windows only, so the audit log never says a human said yes.
  `ui.click` by name asks a driver that can locate its own controls before
  falling back to UI Automation.
- The eval report splits the unverified-effect rate: L3 input has no system of
  record until something is saved, and the report now says so, while the
  target stays 0 everywhere else.
- **The planner runs in its own confined process.** `minos run` starts the
  planner (the code that talks to the model and parses what it says) as a
  child process that cannot write files or start programs. On Windows that is
  a low-integrity token and a one-process Job Object; on Linux, Landlock with
  the whole filesystem read-only; on macOS, a seatbelt profile. The child can
  reach the network, which it needs for its model, and nothing it sends is
  trusted. Replies are size-capped JSON, checked field by field, and every
  action still goes through the broker. `--in-process-planner` or
  `MINOS_ISOLATE_PLANNER=0` turns it off. `minos doctor` reports which
  confinement applies.
- **`minos run --resume [SESSION]`** continues a recorded run that was
  interrupted, crashed or ended early. It starts a fresh conversation primed
  with what the earlier run did, step by step, rather than replaying the old
  one: thinking blocks are bound to the conversation that produced them, and a
  summary is far cheaper. Scopes always come from the flags given now, never
  from the old session. `minos sessions` lists recorded runs and whether each
  finished.
- A 19th eval task, `long.file_the_inbox`, at 25 steps. It is long enough that
  a small window has to compact mid-task.
- **Context management that measures tokens.** OpenAI-compatible planners (local
  and hosted) fit each request to the model's window before sending it. The
  system prompt and goal are always kept, the newest steps are kept while they
  fit, and the steps dropped are named in one line. An oversized latest result
  is shrunk at both ends rather than dropped. The token estimate is corrected
  by the count the server reports. If the server still says the request is too
  long, it is retried once at half the budget. A request that cannot fit at all
  fails with a reason instead of being silently truncated by the server.
- The window is now the one Ollama actually serves (its OpenAI endpoint ignores
  `num_ctx`), or for a hosted model the one its `/models` reports, falling back
  to 64k.
- **The Claude planner clears old tool results server-side** (context editing,
  `clear_tool_uses_20250919`) and caps each result before appending it. Its
  history stays append-only, because editing earlier turns invalidates thinking
  blocks on current models. `prompt is too long` and
  `model_context_window_exceeded` end the run with a reason instead of a
  traceback.
- **Hosted providers.** `--planner openrouter` (or `openai`, `groq`, `together`,
  `deepseek`, `mistral`, `fireworks`, `gemini`, `xai`, `cerebras`) uses any hosted
  OpenAI-compatible API, and `--planner custom` uses any other one. Local presets
  exist for `ollama`, `lmstudio`, `llamacpp` and `vllm`. `minos providers` lists them
  and shows which keys are set. The same names work for `minos eval`.
- Hosted requests leave out Ollama's `options` and `chat_template_kwargs` fields,
  because OpenAI rejects parameters it does not know. They also cap `max_tokens`. A
  refused key, a 402 (out of credit) and a 429 (rate limited) each get their own
  error message.
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

- Code written by a hosted model, including `--planner claude`, is now treated as
  `remote-planner` and runs in a container, as SECURITY.md's origin policy always
  said it should. Before this, the default origin was `local-planner` whatever the
  planner was. `--code-origin` or `MINOS_SANDBOX_ORIGIN` overrides it.
- `--offline` refuses any provider whose endpoint is not on this machine. A server
  on the LAN counts as remote.

- Every GUI tool takes a `window`. The window is brought to the front when the action
  runs, and input stops if something else takes focus.
- `ui.screenshot` lists the window's visible controls by name, so the model can click
  by name instead of guessing.
- After a click by coordinate, the mouse cursor goes back to where you left it.
- A screenshot's fingerprint of the screen is recorded but no longer decides whether
  a GUI action worked. SECURITY.md now says plainly that reads are not confined on
  Windows.

### Fixed

- The container backend now records what confined each run
  (`CodeResult.confinement`), as the subprocess backend always did. Until now
  the field was empty, which SECURITY.md said could not happen. Found by the
  first end-to-end run against a real engine.
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

[Unreleased]: https://github.com/mks2122/minos/compare/v0.1.0b1...HEAD
[0.1.0b1]: https://github.com/mks2122/minos/compare/v0.1.0a1...v0.1.0b1
[0.1.0a1]: https://github.com/mks2122/minos/releases/tag/v0.1.0a1
