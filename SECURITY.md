# Security Model

**Status: alpha (0.1.0a1). Do not point this at data you cannot afford to lose.**

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

There is **no second line of defence** for anything the broker mediates.

The sandbox now has kernel confinement on all three platforms (see below), but that
confines *code the model writes*. It does nothing for `fs.write`, `proc.spawn`,
`app.open` or `ui.input`, which the broker executes directly. For those, **the
broker's own mediation is the enforcement.**

That is defensible only because of invariant I1 — the planner has no filesystem handle, no subprocess API, no network client and no input device, so the broker is not a side-channel check but the *only path* to I/O. It is a chokepoint, not an advisory.

But the consequence is real and stated here rather than discovered later. The kernel confinement described below is defence-in-depth for the code tier only. It is **not** the foundation, and it does not stand behind the broker for anything else.

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

## What confines code the model writes

`code.run` executes code an LLM wrote. Three questions decide what happens to it:
**who wrote it**, **what the kernel here will enforce**, and **what is left if it
escapes**.

### The first question is the origin, not the mechanism

| Origin | Containment | Why |
|---|---|---|
| `local-planner` | Confined subprocess | A model on this machine, on a task you typed. Its failure mode is a **wrong** script, not a targeted one |
| `remote-planner` | Container | The task is yours; the context that produced the code is not one you can see |
| `shared-skill` | Container | Induced from someone else's trajectory |
| `downloaded` | Container | Assume it is trying |

This is the actual security decision; the sandbox is only how it is carried out.
An untrusted origin with no container engine **raises** rather than falling back
— quietly weakening the containment of code you do not trust is the failure this
policy exists to prevent. `--allow-unconfined` overrides that explicitly, and the
downgrade is recorded as one in the audit log.

### The second question is the platform

| Platform | Mechanism | Filesystem | Network | Limits |
|---|---|---|---|---|
| **Linux** | Landlock (ABI ≥ 1) + seccomp-bpf | Kernel: only the workspace and the interpreter's own prefix exist | Kernel: `socket(2)` and friends return EPERM | `RLIMIT_AS`, `NPROC`, `NOFILE`, `CORE` |
| **macOS** | `sandbox-exec` seatbelt profile | Kernel: `(deny default)`, reads allow-listed, writes only in the workspace | Kernel: `(deny network*)` | `RLIMIT_*` |
| **Windows** | Low-integrity token + Job Object | Kernel for **writes** (mandatory integrity). **Reads are not confined** | In-process only | Job Object: memory, process count, kill-on-close |
| anything else | none | in-process only | in-process only | none |

Both POSIX mechanisms are applied by the child to itself, before the script's
first line: they survive `exec` and cannot be revoked, so the child is the right
place and it runs as ordinary Python rather than in the window between `fork` and
`exec`. Windows needs the parent to act, so the child is created **suspended**,
assigned to the job, and only then resumed — there is no instant in which it runs
unconstrained.

Detection is a **probe**, not a version check. `sandbox-exec` is deprecated and
may be removed; a kernel may have Landlock compiled out. Both are tried once and
cached, and `minos doctor` prints what this machine actually offers.

### The third question is what is left over

On every platform, regardless of the kernel:

| | |
|---|---|
| Network | `socket.socket` raises before the script's first line. Defeats urllib, requests, httpx — everything ordinary |
| Credential theft from the environment | Allow-list then deny-list; `ANTHROPIC_API_KEY` and friends are not present |
| Process spawning | `subprocess.Popen`, `os.system` and `os.exec*` refused by an audit hook, which cannot be uninstalled once added |
| Reads, writes and directory listings outside the workspace | Refused by audit hooks on `open`, `os.listdir`, `os.scandir` and `glob`, matched on the **resolved real path** so `..` and symlinks do not help. Not covered: `os.stat` and `os.path.exists`, which raise no audit event, so a script can still ask whether one path it already knows exists |
| Runaway execution | Wall-clock kill, output truncation, and the per-platform limits above |
| Corrupting its inputs | Materials are copies |

**And what that layer does not stop, stated plainly:**

- **`ctypes` is not blocked by default, and this is a deliberate trade.** Refusing
  `ctypes.dlopen` breaks numpy, pandas and openpyxl, which load their own extension
  libraries through it — and on Windows it breaks `import ctypes` outright, because
  the module binds `kernel32` at import time. That is most of the sandbox's
  usefulness traded for a layer a determined script walks around anyway. What
  contains `ctypes` is the *kernel* row above: seccomp refuses the socket syscalls
  whatever ctypes does, Landlock refuses the paths, and the Windows token refuses
  the writes. `SubprocessSandbox(confine_ctypes=True)` turns it on for a script you
  know needs no compiled packages.
- **A library handle already open is still a handle.** The audit hook stops a script
  obtaining a *new* pointer into libc; it does not reach one an imported package is
  holding.
- **On Windows, reads are not confined by the kernel.**
  The mandatory integrity policy is no-write-up, not no-read-up. The read and listing
  hooks stop ordinary Python, but a script that reaches `ReadFile` through `ctypes`
  can read any file your account can, including `~/.gitconfig` and anything else in
  your home directory. Verified, not inferred. An AppContainer with an explicit
  capability set would close that and is the next step, not this one. Until then,
  on Windows treat the sandbox as **write-contained and network-contained, not
  read-contained**, and do not run code you would not let read your home directory.
- **Where no kernel mechanism exists at all**, everything above is in-process and
  therefore advisory. The runtime says so — in `doctor`, in every `CodeResult`, and
  by refusing outright when `require_confinement` is set. That check is made for
  each run, while the child is still suspended, not against what the platform
  advertises: a token that fails to lower means the script never starts.

### Nothing is assumed, including by this document

Every run records what actually confined it, and the report distinguishes
`enforced:` from `unavailable:`:

```
low-integrity+job -- writes confined to the workspace by mandatory integrity;
enforced: socket module patched, audit hook (9 events), filesystem allow-list;
unavailable: rlimits (no resource module on this platform),
             ctypes refusal (off by default; it breaks numpy and pandas)
```

A control that is absent and silent is worse than one that is absent. The escape
attempts in `tests/test_confinement.py` are the mechanism that keeps this section
true; one of them replaces an older test that asserted process spawning was *not*
blocked, which was honest, and left the hole open for sixteen milestones.

The effect that reaches the user is separately protected: `code.materialize` is
an ordinary `fs.write`, checkpointed and hash-verified, and it is the only way
anything leaves the sandbox.

## The virtual device drives your real desktop

`--allow-gui` grants `ui.input` and hands the agent your actual mouse and
keyboard. There is no virtual display and no VM: that is a deliberate design
decision (see `docs/PLAN-GENERALITY.md`), and its consequence is that the agent
shares your session.

- **Grant a window, not the desktop.** `--allow-gui` grants `ui.input:*`: input
  may go to any window. `--gui-window TITLE` grants only the windows named, and an
  action naming no window, or another one, is refused before anything is sent.
  Prefer it.
- **Input goes only to the window it was meant for.** An action that names a
  window brings it to the front first, and every synthesised event checks it is
  still in front. If a notification, a password prompt or your own editor has
  taken focus, nothing is sent and the action fails.
- **Every GUI action is irreversible and asks first**, unless it declares the files
  it will change, in which case they are checkpointed.
- **Ambiguity is refused.** Two controls with the same name, or two windows
  matching the same title, raise with the candidates listed. Nothing is picked.
- **`Ctrl+Alt+Esc` aborts** and releases input, checked before every synthesised
  event so it lands within one action.
- **You can see where it will act.** The ghost cursor, an orange pointer drawn by
  the agent, moves to each target and pauses before the input is sent.
- Synthetic input cannot reach a window running at higher integrity than the
  runtime. That is Windows UIPI, and the driver raises rather than silently
  doing nothing.
- **What this does not protect against:** anything you type or click yourself
  while it works goes to the same window, and a model that picks the wrong control
  by name, in the right window, is not stopped by any of the above. Approval is the
  control for that, which is why it is never skipped for the GUI unless you pass
  `--yes`.
- L3 remains the weakest tier. Its only observation is a window title and a size.
  That is recorded, and never used to decide that an action worked.

## The checkpoint store holds plaintext copies

Every checkpoint is an unencrypted copy of the file it protects, kept in
`.minos/checkpoints/` until the retention policy prunes it (7 days or 2 GB by
default). It is `chmod 0700` on POSIX and inherits directory ACLs on Windows.
Anyone who can read that directory can read every file the agent has touched.

## Known limitations in the current alpha

- Kernel confinement covers `code.run` only; the broker's other operations have none
- Windows confines sandbox *writes* but not *reads*; an AppContainer would close that
- The GUI tier is Windows only; macOS and Linux have no driver yet
- The container backend is not exercised by CI, which has no engine available
- No network capability enforcement — `net.http` scopes parse and match, but nothing consumes them yet
- No process sandboxing for `proc.spawn`
- `NullOracle` means some effects are recorded as unverified; the rate is published rather than hidden
- Audit log is not anchored externally

---

## Reporting a vulnerability

**Please do not open a public issue for a vulnerability.** Report it privately through
GitHub: on the repository, go to **Security → Report a vulnerability**. That opens a
private advisory only the maintainer can see.

Include what you ran, what you expected to be contained, and what got out. A script
that demonstrates the escape is the most useful report there is. It is also exactly
how this document's list of what does *not* hold was written.

This is alpha software with a single maintainer, so there is no bug bounty and no
guaranteed response time. Expect an acknowledgement within a week. A confirmed issue
gets a fix, a note in [CHANGELOG.md](CHANGELOG.md), and credit if you want it.

Things already listed above as not contained (for example, reads through `ctypes` on
Windows) are known limits rather than vulnerabilities. A way past one of the
boundaries this document says *does* hold is exactly what should be reported.
