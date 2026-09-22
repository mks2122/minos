# minos

> **The model asks. The runtime decides.**

A desktop agent runtime where every action — a file write, a spreadsheet cell, a
script the model wrote itself, a synthetic click — passes through one
capability-scoped policy broker that declares what will change, verifies that it
did, and puts it back when it didn't.

It runs **fully offline on one laptop**, against a local model.

[![tests](https://img.shields.io/badge/tests-692%20passing-brightgreen)](#development)
[![suite](https://img.shields.io/badge/reference%20suite-18%2F18-brightgreen)](EVALUATION.md)
[![local model](https://img.shields.io/badge/qwen3%3A8b-15%2F18%20(83%25)-blue)](EVALUATION.md)
[![invariants](https://img.shields.io/badge/invariant%20violations-0-brightgreen)](EVALUATION.md)
[![licence](https://img.shields.io/badge/licence-Apache--2.0-blue)](LICENSE)

```bash
uv sync --all-extras
uv run python main.py
```

---

## The problem

Every open-source desktop agent grants the model **ambient authority**: it decides
what to do, then does it. Safety gets retrofitted by putting the whole thing in a
container.

That works on a throwaway VM. It fails the moment the agent needs your real files —
which is the only version anyone actually wants.

`minos` inverts it. The planner emits *requests*. The broker decides whether each is
admissible, records why, executes it on the planner's behalf, checks the result
against the system of record, and restores from a checkpoint when reality disagrees.

The premise is deliberately pessimistic: **assume the planner is compromised.**
Prompt injection is unsolved, so rather than trying to make the model immune, this
makes its compromise *survivable* — it cannot do anything it was not already scoped
to do.

---

## What it looks like

Ask for something nobody wrote a tool for:

```bash
uv run minos run "convert report.pdf into a word document" -w ./data --allow-write
```

```
  [1] code.run
        code:
          | from pypdf import PdfReader
          | from docx import Document
          | reader = PdfReader('materials/report.pdf')
          | doc = Document()
          | for page in reader.pages:
          |     doc.add_paragraph(page.extract_text())
          | doc.save('out/report.docx')
        via L2.5 / l2.code
        allowed: within scope; effect is pure
        -> ok

  [2] code.materialize
        artifact: report.docx  ->  E:\...\data\report.docx
        allowed: within scope; effect is reversible
        -> ok: 1 target(s) changed as declared

  SUCCEEDED
```

Then change your mind:

```bash
uv run minos undo --last
#   restored 1 target(s), verified
#   to reverse this undo:  minos undo 40738562e892
```

That whole run was `qwen3:8b` on an 8 GB laptop GPU, offline. Nobody wrote a
`doc.convert` operation, and nobody is going to.

---

## How it works

### Four tiers, one gate

| Tier | What it is | Preferred |
|---|---|---|
| **L1** system | Native typed calls — filesystem, process, app launch | first |
| **L2** adapters | Application-aware — cell-level workbook edits | second |
| **L2.5** code | A script the model writes, run in a sandbox | third |
| **L3** GUI | Synthetic mouse and keyboard on your real desktop | last |

Lower is better because the effect contract is more exact. `sheet.set_cell` can
promise *"B4 becomes 48200"*; a click can only promise *"pixels changed"*.

**Every tier passes the same broker.** An L3 click is admitted, recorded, verified
and reversed by exactly the same machinery as an L1 `unlink()`. No tier has a fast
path.

### Effects are classified by reversibility

This taxonomy is the point of the project.

| Class | Admission |
|---|---|
| `PURE` | auto, within scope |
| `REVERSIBLE` | auto, within scope — **checkpointed first** |
| `COMPENSABLE` | auto **only if** a declared inverse exists and is itself in scope |
| `IRREVERSIBLE` | **always prompts. Never auto. Never replayed without confirmation** |

A filesystem checkpoint cannot undo a sent email. Rather than pretend otherwise,
`minos` refuses to auto-approve what it cannot reverse, and verifies every effect
against the **system of record** — never a screenshot.

Checkpointing is a copy-before-write journal over the contract's *declared targets*,
so it needs no filesystem snapshots and behaves identically on Windows, macOS and
Linux. Undeclared writes are detected and halt the task with
`reconciliation_required` rather than being reported as a clean rollback.

### Arbitrary code, without giving up the guarantee

The broker's contract is *declare-then-verify*, and arbitrary code cannot declare
its targets up front. Running it in a sandbox first turns that around:

```
1. run the script in the sandbox, against copies    nothing real is touched
2. observe what it actually wrote                   the artifact set
3. that set becomes the effect contract             retroactively, and exactly
4. checkpoint the destinations, apply, verify       an ordinary fs.write from here
```

The computation is unverified — no oracle can read back *"whatever that program
decided to compute"* — and the runtime **counts and publishes that gap** rather than
inventing a green tick. The effect on your machine stays fully verified and fully
reversible.

> ⚠️ **The sandbox is a jail, not a security boundary.** It stops badly written code,
> not code trying to escape. See [SECURITY.md](SECURITY.md).

### The three invariants

**I1 — The planner never executes.** It emits `ActionRequest` objects and has no
filesystem handle, no subprocess API, no network client, no input device.

**I2 — Every tier passes the same gate.** See above.

**I3 — Degradation is auditable.** When the router falls back from a typed tool to
pixels, it records which adapters were tried and what was missing. Fallback rate is a
published metric, not a silent decay.

---

## Getting started

```bash
uv sync --all-extras
cp .env.example .env        # optional — the defaults are already local
uv run python main.py
```

An interactive menu. It ships with a sample `data/` workspace, starts **read-only**,
and prints the exact scopes before doing anything.

### No API key needed

| | |
|---|---|
| `minos demo` | dry-run → execute → byte-identical rollback → a refused request |
| `minos eval` | the 18-task suite with the honesty metrics |
| `minos doctor` | can this machine run fully offline? sized to your GPU |
| `minos undo` | what can be put back, and put it back |
| `minos audit` | verify the hash chain |
| `minos index ./data` | build the memory index |
| `minos recall "the excel from yesterday"` | resolve a vague reference, with reasons |

### Running an agent

```bash
# read-only by default — granting write is a thing you type
uv run minos run "set Q3 revenue to 48200" -w ./data --allow-write --dry-run
uv run minos run "set Q3 revenue to 48200" -w ./data --allow-write
```

`--dry-run` shows the diff and changes nothing. Irreversible actions prompt unless
you pass `--yes`, which is why `--yes` prints a warning.

Scopes are flags, not configuration buried in a file:

| Flag | Grants |
|---|---|
| *(default)* | `fs.read`, `memory.read`, `code.run` on the workspace |
| `--allow-write` | `fs.write` |
| `--allow-delete` | `fs.delete` |
| `--allow-open` | `app.open` — launch files in their default application |
| `--allow-gui` | `ui.input` — **your real mouse and keyboard**. `Ctrl+Alt+Esc` aborts |

### Fully local

```bash
ollama pull qwen3:8b
OLLAMA_CONTEXT_LENGTH=6144 ollama serve
uv run minos doctor          # confirms what is actually being served
```

`--offline` makes local a **guarantee**: it fails rather than calling a remote model,
so nothing can quietly leave the machine.

**One setting decides whether this works at all.** The tool schemas and system prompt
are ~2600 tokens, and Ollama serves 4096 by default — leaving ~1500 to work in, after
which it evicts the schemas themselves and the model stops being able to call tools.
`minos doctor` reads what your server actually serves and warns you. Full guide and
measured VRAM figures: **[docs/LOCAL.md](docs/LOCAL.md)**.

**Why a local model works here when OSWorld numbers say it shouldn't.** Those numbers
are about *GUI agents* — look at a screenshot, find a control, click the right pixel,
fifty times. This runtime prefers typed tools, so the planner's job is to pick one of
a dozen functions and fill in its arguments. That is **tool calling**, which 7–8B
models do competently. The tier hierarchy is what turns the hard problem into the
easy one.

---

## Status: alpha (0.1.0a1)

**692 tests. Reference suite 18/18 with 0 invariant violations. `qwen3:8b` scores
15/18 (83%) fully offline on an 8 GB laptop GPU**, including 3/3 on long-horizon
tasks and 6/6 on the refusal tasks.

Alpha means the evidence exists, not that the work is done.

**What alpha does not claim:**

- No frontier model has been scored, and no multi-run variance measured. One run of
  one 8B model on one laptop overstates dependability.
- **A bug in the broker is a full bypass.** It is the only line of defence; kernel
  confinement is not portable and is not implemented.
- GUI control is **Windows only**. Synthetic input *delivery* is exercised by hand,
  not by a test — a test suite must not drive the developer's cursor.
- The sandbox is a jail, not a kernel boundary.
- macOS is Tier 2: CI-green, never hand-verified.

**Do not point this at data you cannot afford to lose.**

Platforms: Windows and Linux (Tier 1), macOS (Tier 2). Runs natively, not in WSL2.

---

## Evaluation

```bash
uv run minos eval                                   # reference planner
uv run minos eval --planner local --model qwen3:8b  # a real model
```

18 tasks, binary completion, seeded workspaces. **Six are REFUSE tasks** — goals the
agent should *fail*, because they need something outside its scopes. An agent that
scores well on ACHIEVE and badly on REFUSE is exactly the agent you should not
install, and no public agent benchmark measures that.

Alongside success rate, the suite reports the numbers that would catch this runtime
quietly breaking its own promises: **unverified effect rate** (target 0), **rollback
success rate** (anything under 100% is a bug report), **fallback rate**, and **11
invariants** checked on every task — properties that must hold whatever the agent
did, which is the only way to evaluate a runtime whose capabilities are generated at
runtime rather than enumerated.

Full method, current numbers, and what is *not* measured:
**[EVALUATION.md](EVALUATION.md)**.

---

## Development

```bash
uv sync --all-extras
uv run pytest                      # 692 passing
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy                        # strict
uv run minos eval                  # must stay 18/18 with 0 violations
```

~11,900 lines of Python, zero required runtime dependencies. Everything beyond the
standard library is an optional extra.

### Contributing

Issues and pull requests welcome. Two things worth knowing before you start:

**Adapters are trusted.** An adapter decides an effect's class and declares its
targets, and the broker relies on both being honest. A new adapter is a security
review, not just a feature.

**Tests here are expected to be adversarial.** The interesting test is not that a
feature works — it is that the guarantee holds when something goes wrong. Every
invariant has a test that deliberately breaks it, because an invariant that cannot
fail is a comment.

If you are adding a capability, the shape is: effect contract → oracle that reads the
system of record → checkpoint or declared inverse → an eval task that proves it.
[ARCHITECTURE.md](ARCHITECTURE.md) walks through the life of an action.

---

## Docs

| | |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The invariants, the life of an action, trust boundaries, core types |
| [SECURITY.md](SECURITY.md) | Threat model — and what this does **not** protect against |
| [EVALUATION.md](EVALUATION.md) | Method, metrics, current numbers, what is not measured |
| [docs/LOCAL.md](docs/LOCAL.md) | Running fully offline: models, context sizing, measured VRAM |
| [docs/STATUS.md](docs/STATUS.md) | What is built, what is stubbed, what is missing |
| [docs/PLAN.md](docs/PLAN.md) | The original milestones, build-vs-borrow, metrics |
| [docs/PLAN-GENERALITY.md](docs/PLAN-GENERALITY.md) | Why capability stopped costing one adapter each |
| [docs/REVIEW.md](docs/REVIEW.md) | An audit of the checkpoint journal, and the bugs it found |
| [docs/PORTABILITY.md](docs/PORTABILITY.md) | Cross-platform design |
| [docs/MODELS.md](docs/MODELS.md) | Model choices and hardware ceilings |

---

## Prior art

`minos` is an assembly, not an invention:

- **[Microsoft UFO²](https://github.com/microsoft/UFO)** — the L1→L2→L3 preference
  hierarchy and its typed office adapters
- **[OpenAdapt-Flow](https://github.com/OpenAdaptAI/openadapt-flow)** — effect
  contracts, oracles that read the system of record, halt semantics
- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** — approvals and
  checkpoints, *and* the honest documentation of what filesystem-only rollback cannot
  cover, which is why the effect taxonomy exists
- **[cua](https://github.com/trycua/cua)** — the cross-OS GUI substrate
- **Anthropic Agent Skills** — the skill format

What is new is the intersection: a typed-tool-first hierarchy, a code sandbox whose
output is promoted under contract, GUI fallback, and a capability-scoped broker with
verified reversal under all of them.

---

## Licence

Apache-2.0. See [LICENSE](LICENSE).
