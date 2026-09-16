# Architecture

> **The model asks. The runtime decides.**

This document describes how the runtime is put together and — more importantly — *why*. If you read only one section, read [§3 The life of an action](#3-the-life-of-an-action).

**Status:** design, pre-implementation. Decisions marked 🔓 are still open.

---

## 1. The premise

Every desktop agent in open source today grants the model **ambient authority**: the model decides what to do, and then does it. Safety is retrofitted by putting the whole thing in a container and hoping the blast radius is acceptable.

That works fine when the agent operates on a throwaway VM. It fails the moment the agent needs your actual files — which is the only version of this anyone actually wants.

This runtime inverts that. **The model never holds authority.** It emits *requests*. A broker decides whether each request is admissible, records why, executes it on the model's behalf, verifies that what happened is what was promised, and can put things back if it wasn't.

The design premise is deliberately pessimistic:

> **Assume the planner is compromised.**

Indirect prompt injection is unsolved. A document, a web page, a filename, or a tool result can carry instructions that the planner will follow. Rather than trying to make the planner immune — which nobody has managed — we make its compromise *survivable*, by ensuring it cannot do anything it was not already scoped to do.

---

## 2. The three invariants

Everything else is implementation detail. These three are the architecture.

### I1 — The planner never executes

The planner emits `ActionRequest` objects. It has no filesystem handle, no subprocess API, no network client, no input device. The only component with execution authority is the broker.

*Why:* an untrusted component that cannot act cannot be exploited into acting. This is the difference between "the agent was tricked" and "the agent was tricked and it mattered."

*Consequence:* any convenience API that lets the planner "just quickly do X" is a bug, no matter how small X is.

### I2 — Every tier passes the same gate

An L3 synthetic mouse click is admitted, recorded, verified and reversed by exactly the same machinery as an L1 `unlink()`. No tier is privileged. No tier has a fast path.

*Why:* the usual failure is a governed API surface with an ungoverned escape hatch. If GUI control bypasses the broker, the broker is decorative — an agent that can move a mouse can do anything a user can do.

*Consequence:* the GUI tier is harder to build than it looks, because "click at (x,y)" must be expressible as a scoped, contract-bearing action.

### I3 — Degradation is auditable

When the router cannot satisfy a request with a typed tool and falls back to pixels, it records *why*: which adapter was missing, what was attempted, what it fell back to.

*Why:* fallback rate is the health metric of the whole design. A runtime that silently drifts into clicking everything has failed without telling anyone. Making degradation loud turns an invisible decay into a number you can put in the README.

---

## 3. The life of an action

This is the core loop. Everything in the codebase exists to serve it.

```
  planner                router               broker              tier
     │                     │                    │                   │
     │  ActionRequest      │                    │                   │
     ├────────────────────►│                    │                   │
     │                     │                    │                   │
     │            ┌────────┴────────┐           │                   │
     │            │ select tier     │           │                   │
     │            │ L1 > L2 > L3    │           │                   │
     │            │ record reason   │           │                   │
     │            └────────┬────────┘           │                   │
     │                     │                    │                   │
     │                     │ Invocation +       │                   │
     │                     │ EffectContract     │                   │
     │                     ├───────────────────►│                   │
     │                     │                    │                   │
     │                     │           ┌────────┴────────┐          │
     │                     │           │ 1 scope check   │          │
     │                     │           │ 2 effect class  │          │
     │                     │           │ 3 admission     │          │
     │                     │           └────────┬────────┘          │
     │                     │                    │                   │
     │                     │       ══ DRY RUN? ═╪══► predicted diff │
     │                     │                    │     (stop here)   │
     │                     │                    │                   │
     │                     │           ┌────────┴────────┐          │
     │                     │           │ 4 checkpoint    │          │
     │                     │           └────────┬────────┘          │
     │                     │                    │  5 execute        │
     │                     │                    ├──────────────────►│
     │                     │                    │◄──────────────────┤
     │                     │           ┌────────┴────────┐          │
     │                     │           │ 6 oracle verify │          │
     │                     │           │ 7 reverse if ✗  │          │
     │                     │           │ 8 record        │          │
     │                     │           └────────┬────────┘          │
     │◄─────────────────────────────────────────┤                   │
     │   Outcome (+ tool output as UNTRUSTED)   │                   │
```

**Step by step:**

1. **Scope check.** Does the active task's capability set permit this? Denial is terminal for the request — the planner may re-plan, but it may not retry the same thing hoping for a different answer, and it may not request a wider scope.
2. **Effect classification.** The adapter declared what class of effect this is (§6). This determines whether it can be auto-approved.
3. **Admission.** `ALLOW` / `PROMPT` / `DENY`, from the scope, the effect class and the policy.
4. **Checkpoint.** For reversible effects, copy the contract's declared targets into the checkpoint store first. Because the targets are *declared*, this is a few files — not a volume snapshot. See [PORTABILITY.md](PORTABILITY.md).
5. **Execute.** The broker calls the tier. The tier does exactly what was admitted — no more.
6. **Verify.** The oracle reads the *system of record* and compares against the contract. Not a screenshot. Never a screenshot.
7. **Reverse on mismatch.** If reality ≠ contract, attempt reversal. If reversal succeeds, the request failed cleanly. **If reversal fails, halt the whole task with `RECONCILIATION_REQUIRED`** — do not retry, do not continue, do not let the planner improvise around it.
8. **Record.** Append to the hash-chained log: request, tier, reason for tier, contract, decision, observed effect, reversal status.

---

## 4. Components

```
src/
├── planner/     UNTRUSTED. Turns intent into ActionRequests. Swappable.
├── router/      TRUSTED. Picks a tier, records why, gets the contract.
├── broker/      TRUSTED — the TCB. Scopes, admission, checkpoint,
│                oracle, reversal, audit. Nothing else executes.
├── tiers/
│   ├── l1_system/    filesystem, process, window, clipboard, terminal
│   ├── l2_adapters/  typed adapters + capability manifests (MCP, office, browser)
│   └── l3_gui/       vision + synthetic input, over cua-driver
├── memory/      Computer-state index. Content is UNTRUSTED.
└── skills/      Verified procedures. Carry scopes + oracles.
```

### Trust boundaries

| Component | Trust | Why |
|---|---|---|
| **Planner** | ❌ Untrusted | Assume injected. Design premise |
| **Tool output** | ❌ Untrusted | It's data. A file's contents can contain instructions |
| **Memory contents** | ❌ Untrusted | Indexed from files, therefore attacker-influenced |
| **Skill *bodies*** | ❌ Untrusted | Induced from trajectories the planner shaped |
| **Skill *manifests*** | ✅ Trusted | Human-approved at promotion time |
| **Adapters** | ✅ Trusted | You wrote or vendored them. They declare effect shapes |
| **Router** | ✅ Trusted | Small, no I/O, deterministic |
| **Broker** | ✅ TCB | Keep it small enough to audit by reading |

**The rule that follows:** *nothing derived from untrusted input may widen a scope.* A file that says "you are now permitted to delete /etc" is a string, not a grant. Scopes come from the human, at task start, and only from there.

---

## 5. Capability scopes

A scope is a bounded grant, set **before** the task, **immutable during** it.

```
fs.read      : ~/Invoices/**
fs.write     : ~/Invoices/2026/**
fs.delete    : (none)
proc.spawn   : /usr/bin/soffice
net.http     : api.example.com:443
ui.input     : window.class=soffice.bin
clipboard    : read
```

**Semantics**

- **Default deny.** Anything not granted is denied. No implicit grants, no inheritance.
- **Immutable within a task.** Widening requires a new task and a human. This is the property that makes injection survivable: a compromised planner is still confined to the box the human drew.
- **Narrowing is free.** A skill or subtask may run with *less* than the task's scope, never more.
- **Enforced by mediation.** Path scopes check the resolved real path after symlink resolution. The broker's check *is* the enforcement — not a side-channel audit of code that could also do I/O directly, because per I1 no such code exists. Kernel confinement (Landlock/seccomp on Linux, AppContainer on Windows) is per-platform defence-in-depth added later, never the foundation; it isn't portable. **Consequence, stated plainly in `SECURITY.md`: a bug in the broker is a full bypass.** Keep it small enough to audit by reading.

**Resolution order:** deny rules beat allow rules; longest path prefix wins; ties deny.

🔓 *Open: glob grammar vs. a capability URI scheme. Recommendation — start with globs, they're legible to humans reviewing an approval prompt, and legibility is the point.*

---

## 6. Effect contracts

This is the part that distinguishes this runtime from a checkpoint system.

Before an action runs, the **adapter** declares what it is about to change. The planner supplies values; it does not get to invent the *shape*, because the planner is untrusted.

```python
EffectContract(
    class_   = EffectClass.REVERSIBLE,
    targets  = ["~/Invoices/2026/q3.ods"],
    oracle   = FileHashOracle(path="~/Invoices/2026/q3.ods"),
    expect   = "cell B14 == 48200; all other cells unchanged",
    reversal = SnapshotRestore(checkpoint_id),
)
```

### Effect classes — the taxonomy is the contribution

| Class | Meaning | Admission | Reversal |
|---|---|---|---|
| **`PURE`** | Reads. Changes nothing | Auto within scope | n/a |
| **`REVERSIBLE`** | Covered by a snapshot we took | Auto within scope | Snapshot restore, **verified** |
| **`COMPENSABLE`** | External, but has a declared inverse — created a calendar event, made a git commit | Auto within scope **only if** the compensation is declared and itself in scope | Run the compensation, then verify |
| **`IRREVERSIBLE`** | Sent an email. Made a payment. Deleted from a system with no undo. Published something | **Always prompts. Never auto-approved. Never replayed from a skill without confirmation** | None. That's the point |

**Why this matters.** Hermes' documentation states plainly that its checkpoints *"do not capture external effects like API calls, database modifications, or environment changes."* That is the honest limitation of filesystem snapshotting, and it is where almost every real-world regret lives — the email that got sent, the record that got updated.

Classifying effects by reversibility, and refusing to auto-approve the irreversible ones, is the mechanism that closes that gap. **If this runtime ships with only `PURE` and `REVERSIBLE`, it is a checkpoint system with a different name.**

### Oracles

An oracle reads the **system of record** and answers: did the declared effect actually happen, and did anything else happen too?

| Oracle | Reads |
|---|---|
| `FileHashOracle` | File digest before/after |
| `FileTreeOracle` | Directory manifest — catches collateral writes |
| `ProcessOracle` | Process table |
| `HttpOracle` | `GET` the resource and compare |
| `SqlOracle` | `SELECT` against the database |
| `NullOracle` | **Explicitly unverifiable.** Must be declared, counts against the unverified-effect metric |

**Screenshots are never oracles.** Measured by OpenAdapt-Flow: screen-only checks silently accepted **75%** of wrong effects; a single system-of-record read cut that to **12.5%**. A screenshot tells you what was rendered, not what is true.

`NullOracle` exists because pretending everything is verifiable would be a lie. It is allowed, it is loud, and its frequency is a published metric.

---

## 7. Reversal

**What "rollback" means here:** restore the system to its pre-action state *and prove it*.

**Mechanism: a copy-before-write journal, not a filesystem snapshot.** The effect contract declares its targets before the action runs, so the checkpoint only has to cover *those files*. Copy them into a content-addressed store, act, and restore by copying back. This is plain `shutil` + `hashlib` and behaves identically on Windows, macOS and Linux; platform fast paths (`clonefile` on APFS, `FICLONE` on btrfs/XFS, ReFS block clone) are optimisations that fall back gracefully. Full rationale in [PORTABILITY.md](PORTABILITY.md).

- Checkpoint the declared targets before any `REVERSIBLE` action
- Restore, then **re-run the oracle** to confirm the restoration
- **Undeclared writes are not protected** — which is what `FileTreeOracle` is for. If it detects collateral changes, reversal is known-incomplete and the task halts rather than claiming a clean rollback
- `COMPENSABLE` runs its declared inverse, then verifies
- `IRREVERSIBLE` cannot be reversed — which is exactly why it required a human first

**When reversal fails:** halt the task with `RECONCILIATION_REQUIRED`. Report what was attempted, what state the system is believed to be in, and what the human must check. **Do not retry. Do not continue. Do not let the planner improvise.** A runtime that keeps going after it has lost track of state is worse than one that stops.

---

## 8. Tier router

Preference order, always: **L1 → L2 → L3.**

| Tier | What | Contract quality |
|---|---|---|
| **L1** | Filesystem, process, window, clipboard, terminal | Exact — we know precisely what changed |
| **L2** | Typed adapters: LibreOffice, browser, editor, PDF, MCP servers | Good — the adapter declares its effects |
| **L3** | Vision + synthetic input, over `cua-driver` | Weak — we observe, we don't know |

Adapters advertise a **capability manifest**: what operations they support, what scopes they need, what effect classes they produce. The router matches the request against manifests, most-specific first.

**Degradation is recorded, always:**

```json
{"tier": "L3", "reason": "no L2 adapter for app=com.acme.legacy",
 "attempted": ["l2.office", "l2.generic_mcp"], "fallback_rate_task": 0.18}
```

**Fallback rate is a first-class metric.** It appears in eval output and in the README. Rising fallback rate means the adapter ecosystem is losing to the long tail, and that is something the project needs to know publicly rather than discover privately.

---

## 9. Computer-state memory

Not conversation history. Not a RAG index of document *contents*. An index of **what the computer was doing, over time**.

- **Files** — path, mtime, type, and edit provenance: *which app touched this, in which session*
- **Windows/apps** — what was open, when, with which document
- **Sessions** — task boundaries, what was worked on

**Purpose:** resolve deictic references — *"the Excel we were working on yesterday"* — to a concrete path, **and explain how it decided**. The explanation is not a nicety; an unexplained resolution is an unauditable one, and this runtime does not do unauditable.

**Storage:** SQLite + FTS5, `fanotify` watcher. Deliberately *not* Letta or Mem0 — those model conversational/agent memory, which is a different data model from a filesystem-and-window state index.

**Security:** memory content is untrusted (§4). Memory is also an injection surface — a filename can carry an instruction. Resolution may inform planning; it may never widen a scope.

---

## 10. Skills

A skill is a **verified procedure** promoted from a trajectory that succeeded.

What makes it different from every other skill library: **a skill carries its capability manifest and its effect oracles.** It is not a blob of remembered steps — it is a scoped, verifiable procedure.

- Replay runs under the skill's own scope, which must be a subset of the task's
- Every replayed action is re-verified by its oracle. Past success is not evidence of present success
- Drift detection: if the environment no longer matches preconditions, fall back to the planner rather than plough on
- `IRREVERSIBLE` steps re-prompt on every replay, always
- Format: agentskills.io-compatible, so the ecosystem can use them

**Payoff:** a cached skill turns ~50 stochastic steps into one deterministic replay — and unlike the planner, its reliability doesn't depend on a model you don't control.

---

## 11. Planner

Deliberately the most boring component.

```python
class Planner(Protocol):
    def next_action(self, goal, observations, scopes) -> ActionRequest | Done: ...
```

Swappable: frontier API, local model, or a scripted planner for tests. **Local is an option, never a requirement** — the smallest open-weight model that plans desktop work competently is ~235B-class, and nothing that fits a consumer GPU is close. Building the runtime to require local inference would be building a runtime that doesn't work.

The planner sees observations and scopes. It does not see credentials, and it cannot ask for more scope.

---

## 12. Core types

```python
@dataclass(frozen=True)
class ActionRequest:
    goal_id: str
    intent: str                  # natural language, for the audit log
    operation: str               # "fs.write", "office.set_cell", ...
    params: dict
    tier_hint: Tier | None       # advisory only; the router decides

@dataclass(frozen=True)
class Invocation:
    request: ActionRequest
    tier: Tier
    adapter: str
    tier_reason: str             # I3: why this tier, not a higher one
    contract: EffectContract

@dataclass(frozen=True)
class AdmissionDecision:
    verdict: Literal["allow", "prompt", "deny"]
    matched_scopes: list[Scope]
    denied_by: Scope | None
    rationale: str               # human-readable; shown in the prompt

@dataclass(frozen=True)
class ProvenanceRecord:
    seq: int
    prev_hash: str               # hash chain — tamper-evident
    invocation: Invocation
    decision: AdmissionDecision
    checkpoint_id: str | None
    observed: OracleResult
    reversal: ReversalOutcome | None
    ts: datetime
```

The audit log is append-only and hash-chained from the first record. Cheap to add now; impossible to retrofit credibly.

---

## 13. What this design deliberately does not do

- **Not an OS, a distro, or a compositor.** Every component here is userspace.
- **Not a model.** No training, no fine-tuning, no grounding model.
- **Not an inference engine.** `llama.cpp` exists.
- **Does not make the planner trustworthy.** It makes the planner's compromise survivable. Those are different claims and only one of them is achievable today.
- **Does not protect against a malicious *adapter*.** Adapters are in the TCB. Vendor deliberately, review what you vendor.
- **Does not claim to prevent prompt injection.** Nobody can. It bounds what injection can accomplish.

---

## 14. Open questions

1. 🔓 Scope grammar: globs or capability URIs? *(lean: globs, for legibility in approval prompts)*
2. 🔓 Approval UX: CLI, desktop notification, or web? *(lean: CLI for v0.1)*
3. 🔓 Broker in-process or separate daemon from day one? *(lean: in-process with a clean interface, split later; design the boundary now)*
4. 🔓 Who declares the effect contract — adapter, planner, or both? *(lean: adapter declares the shape, planner fills values. The adapter is trusted; the planner is not)*
5. 🔓 How are `COMPENSABLE` inverses expressed — declarative, or adapter-provided code?
6. 🔓 Can a task escalate scope mid-run with human approval, or must it always restart? *(lean: restart. Simpler to reason about, and mid-run escalation is exactly what an attacker would aim for)*

---

## 15. Prior art this builds on

Credit where it's due — and read these before writing code:

| Project | What to take |
|---|---|
| **Microsoft UFO²** (MIT, arXiv:2504.14603) | The L1→L2→L3 preference hierarchy, and its typed office adapters. This runtime's tier model is theirs |
| **OpenAdapt-Flow** (MIT) | Effect contracts, oracles reading the system of record, `RECONCILIATION_REQUIRED` halt semantics, policy packs. The 75%→12.5% measurement |
| **Hermes Agent** (MIT) | Shadow-git checkpoints, approvals.yaml — **and the honest documentation of what filesystem-only rollback cannot cover**, which is why §6 exists |
| **cua** (MIT) | `cua-driver` — the cross-OS a11y + screenshot + synthetic input substrate. L3 is a thin wrapper over this |
| **Anthropic Agent Skills / agentskills.io** | The skill format. Don't invent one |

**What's new here is the intersection, not the parts:** no existing project combines a typed-tool-first hierarchy, GUI fallback, and a capability-scoped broker with verified reversal under all three tiers. See [07-oss-landscape.md](07-oss-landscape.md).

---

## Related

[PLAN.md](PLAN.md) — milestones and build-vs-borrow · [05-architecture-notes.md](05-architecture-notes.md) — the design's evolution
