# writ

> **The model asks. The runtime decides.**

A desktop agent runtime where every action — a syscall, a typed spreadsheet write, or a
synthetic click — passes through one capability-scoped policy broker, with declared
effects, full provenance, and a rollback you can actually trust.

**Status: pre-alpha.** The broker, capability scopes, checkpointing, oracles, audit log,
tier router and L1 filesystem/process adapters work and are tested. There is no planner and
no agent yet. Do not point this at data you cannot afford to lose.

**Platforms:** Windows and Linux (Tier 1), macOS (Tier 2 — CI-green, not hand-verified).
Runs natively. Not in WSL2.

---

## Why

Every desktop agent in open source grants the model **ambient authority**: it decides what
to do, then does it. Safety is retrofitted by putting the whole thing in a container.

That works on a throwaway VM. It fails the moment the agent needs your real files — which
is the only version anyone actually wants.

`writ` inverts it. The planner emits *requests*. The broker decides whether each one is
admissible, records why, executes it on the planner's behalf, verifies that what happened
is what was promised, and puts things back when it wasn't.

The design premise is deliberately pessimistic: **assume the planner is compromised.**
Prompt injection is unsolved, so rather than trying to make the model immune, `writ` makes
its compromise survivable — it cannot do anything it was not already scoped to do.

---

## Try it

```bash
uv sync
uv run python examples/dry_run_then_rollback.py
```

Dry-run the edit, apply it for real, roll it back byte-identical, then watch an
out-of-scope request get refused before the executor is ever called:

```
1. DRY RUN - what would happen, without doing it
      would_touch: ['.../q3-report.txt']
           expect: Q3 revenue becomes 48,200
       reversible: True
  file on disk  : 'Q3 revenue: 41,800'  (unchanged)

2. EXECUTE - for real, verified against the system of record
  status        : ok
  verified      : True (1 target(s) changed as declared)
  file on disk  : 'Q3 revenue: 48,200'

3. ROLLBACK - restore and prove it
  byte-identical: True

4. OUT OF SCOPE - the planner asks for something it may not have
  status        : denied
  executor ran  : False

5. AUDIT - every decision, hash-chained
  chain intact  : True
```

---

## The three invariants

**I1 — The planner never executes.** It emits `ActionRequest` objects and has no
filesystem handle, no subprocess API, no network client, no input device. Only the broker
acts.

**I2 — Every tier passes the same gate.** An L3 synthetic click is admitted, recorded,
verified and reversed by exactly the same machinery as an L1 `unlink()`. No tier has a
fast path.

**I3 — Degradation is auditable.** When the router falls back from a typed tool to
pixels, it records why — which adapters were tried, what was missing, what was unavailable
on this platform. Fallback rate is a published metric, not a silent decay.

```python
router.stats.summary()
# {'total': 12, 'L1': 11, 'L2': 1, 'L3': 0, 'fallback_rate': 0.0833, 'gui_rate': 0.0}
```

---

## Effects are classified by reversibility

This taxonomy is the point of the project.

| Class | Admission |
|---|---|
| `PURE` | auto, within scope |
| `REVERSIBLE` | auto, within scope — checkpointed first |
| `COMPENSABLE` | auto **only if** a declared inverse exists and is itself in scope |
| `IRREVERSIBLE` | **always prompts. Never auto. Never replayed without confirmation** |

Filesystem checkpoints cannot undo a sent email or a database write. Rather than pretend
otherwise, `writ` refuses to auto-approve the effects it cannot reverse, and verifies every
effect against the **system of record** — never a screenshot.

Checkpointing is a copy-before-write journal over the contract's *declared targets*, so it
needs no filesystem snapshots and behaves identically on all three platforms. Undeclared
writes are detected and halt the task with `reconciliation_required` rather than being
reported as a clean rollback.

Two classifications in the L1 adapters are worth arguing about, and both are deliberate.
`fs.delete` is **REVERSIBLE** — the checkpoint holds the bytes, so restoring genuinely
brings the file back, and calling it irreversible would be theatre. `proc.spawn` is
**IRREVERSIBLE** and always prompts — once control passes to an external binary the runtime
has no model of what it did, and no oracle can read that back. Getting routine work out of
the prompt loop means writing a typed L2 adapter, which is exactly the incentive the tier
hierarchy is meant to create.

---

## Development

```bash
uv sync --all-extras
uv run pytest          # 94 passing
uv run ruff check src tests examples
uv run mypy            # strict
```

---

## Docs

| | |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The invariants, the life of an action, trust boundaries, core types |
| [SECURITY.md](SECURITY.md) | Threat model — and what this does **not** protect against |
| [docs/PLAN.md](docs/PLAN.md) | Milestones, build-vs-borrow, metrics |
| [docs/PORTABILITY.md](docs/PORTABILITY.md) | Cross-platform design |
| [docs/MODELS.md](docs/MODELS.md) | Model choices and hardware ceilings |
| [docs/research/](docs/research/) | The survey this design came out of |

---

## Prior art

`writ` is an assembly, not an invention. It builds on and borrows from:

- **[Microsoft UFO²](https://github.com/microsoft/UFO)** — the L1→L2→L3 preference
  hierarchy, and its typed office adapters
- **[OpenAdapt-Flow](https://github.com/OpenAdaptAI/openadapt-flow)** — effect contracts,
  oracles that read the system of record, `RECONCILIATION_REQUIRED` halt semantics
- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** — approvals and
  checkpoints, *and* the honest documentation of what filesystem-only rollback cannot
  cover, which is why the effect taxonomy exists
- **[cua](https://github.com/trycua/cua)** — the cross-OS GUI substrate that L3 will wrap
- **Anthropic Agent Skills / agentskills.io** — the skill format, when skills land

What is new here is the intersection: no existing project combines a typed-tool-first
hierarchy, GUI fallback, and a capability-scoped broker with verified reversal under all
three tiers.

---

## Licence

Apache-2.0. Personal project, no support promise, use at your own risk.
