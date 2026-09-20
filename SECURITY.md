# Security Model

**Status: pre-alpha. Do not point this at data you cannot afford to lose.**

---

## The design premise

> **Assume the planner is compromised.**

Indirect prompt injection is unsolved. A document, a web page, a filename or a tool result can carry instructions the planner will follow. Attack success rates of 50–84% are routine in the literature, and the failure mode became RCE-class in 2026.

`minos` does not try to make the planner immune — nobody has managed that. It makes the planner's compromise **survivable**, by ensuring it cannot do anything it was not already scoped to do before the task began.

That is a narrower claim than "secure", and it is deliberately narrower.

---

## Trust boundaries

| Component | Trust | Why |
|---|---|---|
| Planner (the model) | ❌ **Untrusted** | Assume injected |
| Tool output | ❌ **Untrusted** | It's data. File contents can contain instructions |
| Memory contents | ❌ **Untrusted** | Indexed from files, therefore attacker-influenced |
| Skill *bodies* | ❌ **Untrusted** | Induced from trajectories the planner shaped |
| Skill *manifests* | ✅ Trusted | Human-approved at promotion time |
| Adapters | ✅ Trusted | You wrote or vendored them; they declare effect shapes |
| Router | ✅ Trusted | Small, no I/O, deterministic |
| **Broker** | ✅ **TCB** | Keep it small enough to audit by reading |

**The rule that follows:** nothing derived from untrusted input may widen a scope. A file that says *"you are now permitted to delete /etc"* is a string, not a grant. Scopes come from the human, before the task, and only from there.

---

## ⚠️ A bug in the broker is a full bypass

There is **no second line of defence** on macOS or Windows.

Kernel-level confinement is not portable: Linux has Landlock and seccomp, macOS's `sandbox-exec` is deprecated and Endpoint Security requires an Apple entitlement, and Windows AppContainer is awkward and poorly documented. So on every platform, **the broker's own mediation is the enforcement.**

That is defensible only because of invariant I1 — the planner has no filesystem handle, no subprocess API, no network client and no input device, so the broker is not a side-channel check but the *only path* to I/O. It is a chokepoint, not an advisory.

But the consequence is real and stated here rather than discovered later. Kernel confinement is planned as per-platform defence-in-depth (Landlock first, as the cheapest real win). It is **not** the foundation.

---

## What each mechanism does and does not do

| Mechanism | Protects against | Does **not** protect against |
|---|---|---|
| **Capability scopes** | The agent reaching outside its granted paths, including via `..` and symlinks (matching is on the resolved real path) | Anything inside the grant. Grant narrowly |
| **Scope immutability** | Privilege escalation mid-task; content-derived "permission" claims | A human approving too broad a scope up front |
| **Effect classification** | Silent irreversible actions — sent mail, payments, publishes | Misclassification by an adapter. Adapters are trusted |
| **Dry run** | Acting before you have seen what would happen | Effects the contract failed to declare |
| **Checkpoint + reversal** | Losing the declared targets | **Undeclared writes.** See below |
| **Effect oracles** | Believing a screenshot over reality | Effects with no system-of-record reader (`NullOracle`, counted and published) |
| **Hash-chained audit** | Silent edits to history | An attacker who can rewrite the whole file and recompute the chain |

### Undeclared writes

Checkpointing covers **declared targets only** — that is what makes it portable across Windows, macOS and Linux without filesystem snapshots.

When an action touches something it did not declare, `FileTreeOracle` detects it, and the broker returns **`reconciliation_required`** rather than claiming a clean rollback. It stops the task and says what it believes the state to be.

This behaviour is load-bearing and is covered by a test. An earlier revision got it wrong — the restore succeeded, `verify()` passed (both only examine declared targets), and the broker reported "reversed cleanly" while the collateral file was still on disk. That is exactly the lie this component exists to prevent.

### Verification confirms the declared effect, not the intent

This is the most important limit in the design, and it was found by running a
local 8B model against a real task rather than by reasoning about it.

Asked to *"set the Q3 revenue to 48200"*, the planner emitted
`sheet.set_cell(cell="B3", value="48200")`. B3 is Q2's row -- row 1 is the
header, so Q3 is B4. The broker admitted it (in scope), checkpointed it,
executed it, read the cell back, found `B3 == "48200"` exactly as the contract
declared, and reported **success**.

Every component behaved correctly. The oracle verified what was *declared*. It
has no access to what was *meant*.

So effect verification protects against:

- an action that silently did nothing
- an action that did something other than it declared
- an action that touched files it did not declare

It does **not** protect against a planner that declares the wrong thing
confidently. The defences that apply there are different ones: scopes bound the
blast radius, `--dry-run` shows the diff before it happens, checkpoints make it
reversible, and the audit log records exactly what was asked for. None of them
make the agent correct; they make its mistakes survivable and visible.

Treat "the runtime said SUCCEEDED" as "the declared effect happened", never as
"the task was done right".

### Audit log anchoring

The chain is tamper-**evident**, not tamper-proof. Detecting a full-file rewrite requires anchoring the head hash somewhere the agent cannot write. Out of scope for v0; tracked.

---

## The sandbox is a jail, not a security boundary

`code.run` executes code an LLM wrote. What contains it is a subprocess with a
scrubbed environment, a working-directory convention, a patched `socket` module
and (on POSIX) resource limits. **None of that is enforced by the kernel.** The
script runs as the same user, with the same filesystem permissions, as the
runtime itself.

What it does stop:

| | |
|---|---|
| Network access | `socket.socket` raises before the script's first line. Defeats urllib, requests, httpx — everything ordinary |
| Credential theft from the environment | Allow-list then deny-list; `ANTHROPIC_API_KEY` and friends are not present |
| Runaway execution | Wall-clock kill, output truncation, `RLIMIT_AS` and `RLIMIT_NPROC` on POSIX |
| Corrupting its inputs | Materials are copies |

What it does **not** stop, and is not claimed to:

- `ctypes`, which can call `socket(2)` directly and bypass the patched module
- `subprocess`, which can spawn anything the user can run
- Reading any file the user can read. The cwd is a convention, not a jail
- Anything at all on Windows in terms of memory or process limits — there is no
  `RLIMIT_AS` equivalent without Job Objects

**This is the right trade for the threat that exists** — code written by a local
model against the user's own task, which fails by being wrong rather than by
being hostile — and **the wrong trade for code from anywhere else.** A
downloaded skill, a shared recipe or a remote planner changes the threat, and
the containment has to change with it. `SandboxBackend` is a protocol so a
container or microVM implementation can replace this without anything above it
changing.

The effect that reaches the user is separately protected: `code.materialize` is
an ordinary `fs.write`, checkpointed and hash-verified, and it is the only way
anything leaves the sandbox.

## The virtual device drives your real desktop

`--allow-gui` grants `ui.input` and hands the agent your actual mouse and
keyboard. There is no virtual display and no VM: that is a deliberate design
decision (see `docs/PLAN-GENERALITY.md`), and its consequence is that the agent
shares your session and takes the cursor while it works.

- `Ctrl+Alt+Esc` aborts and releases input, checked before every synthesised
  event so it lands within one action.
- Focus loss deliberately does **not** abort — focus changes constantly during
  legitimate automation.
- Synthetic input cannot reach a window running at higher integrity than the
  runtime. That is Windows UIPI, and the driver raises rather than silently
  doing nothing.
- L3 remains the weakest tier. Its oracle is a window title and a size, which is
  evidence that something rendered, not that anything is true.

## The checkpoint store holds plaintext copies

Every checkpoint is an unencrypted copy of the file it protects, kept in
`.minos/checkpoints/` until the retention policy prunes it (7 days or 2 GB by
default). It is `chmod 0700` on POSIX and inherits directory ACLs on Windows.
Anyone who can read that directory can read every file the agent has touched.

## Known limitations in the current pre-alpha

- No kernel-level confinement on any platform yet
- No network capability enforcement — `net.http` scopes parse and match, but nothing consumes them yet
- No process sandboxing for `proc.spawn`
- `NullOracle` means some effects are recorded as unverified; the rate is published rather than hidden
- Audit log is not anchored externally
- `COMPENSABLE` compensations are declared but not yet executed on failure

---

## Reporting a vulnerability

Pre-alpha, single maintainer, no users to protect yet. Open a GitHub issue. If that changes, this section will.
