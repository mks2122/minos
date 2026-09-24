# Build Plan — Generality

**Status: M17–M25 are built and merged.** Section 5 is kept as written, with
outcomes noted, because a plan is more useful next to what it produced than
rewritten to look prescient.

M17 onward. The companion to [PLAN.md](PLAN.md), which took the project from
nothing to a working policy broker, and [REVIEW.md](REVIEW.md), which audits what
that produced.

---

## 1. The problem this plan exists to solve

After sixteen milestones the runtime can perform **22 operations**, every one of
them listed in `capabilities.py`. The safety chassis is finished and the engine
is nearly empty.

That is not an accident of effort. It is what the architecture asks for. Adding
one operation the correct way means writing an adapter, an effect contract that
declares its targets before acting, a checkpoint plan, an oracle that proves the
effect happened, a capability entry, and a reversal or compensation path.

**That cost is paid per capability and never amortizes.** Capability #200 costs
what capability #3 cost. And the target surface — "it should do almost anything"
— is not 200 items, it is unbounded. Linear cost against an unbounded surface
never finishes.

## 2. The move

> **From `enumerate → implement` to `generate → attest`.**

Stop adding tools. Add two capabilities that generate their own specifics at
runtime, and pay the depth tax once each:

| | What it generalizes | Tier |
|---|---|---|
| **Sandbox** (`code.run`) | Anything a program can compute — conversion, parsing, batch edits, data work | new `L2_CODE` |
| **Virtual device** (`ui.*`) | Anything a person can do by clicking — apps with no API and no file format | existing `L3_GUI` |

These are different mechanisms for different halves of "everything", and neither
substitutes for the other. The sandbox cannot send a WhatsApp message. The
virtual device cannot convert a PDF reliably.

### The sandbox is a scratchpad, not a machine

It is a place to run code: copies of data in, artifacts out, ephemeral, no
credentials, no network. It is explicitly **not** a virtual computer that the
agent lives inside — that design needs a file-sync bridge, a credential bridge
and a display bridge, and those bridges are harder and more dangerous than the
agent. Everything the user cares about is on the real machine.

### The virtual device drives the real desktop

A synthetic mouse and keyboard, acting on the machine the user is actually
using. No second screen, no VM, no mirrored state. The consequence, accepted
deliberately: the agent shares the desktop and takes the cursor while it works.
Mitigated by preferring window-targeted input over global cursor motion, and by
a panic key.

## 3. Why arbitrary code does not destroy the guarantee

The broker's contract is **declare-then-verify**: name your targets, act, prove
it happened. Arbitrary code cannot name its targets up front. That is why
`code.run` looked unguardable.

It can, if it runs somewhere else first:

```
1. run the code in the sandbox, against copies       nothing real is touched
2. observe what it actually wrote                    the artifact set
3. that set becomes the effect contract              retroactively, and precisely
4. checkpoint those destinations on the real machine
5. apply
6. verify with hash oracles                          targets are known now
```

**The sandbox is what makes retroactive contracts possible.** The computation is
unverified; the effect on the real machine stays fully verified and fully
reversible. Nothing in the project's claim is given up.

Both primitives already exist: `FileTreeOracle` (built for collateral detection)
does step 2, and `FileCheckpointStore` does step 4 unchanged.

The virtual device gets no such trick — you cannot speculatively click. L3 stays
the weakest tier, labelled as such, exactly as `STATUS.md` already labels its
screen oracle.

## 4. Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Sandbox network | **Offline** | Keeps `--offline` a guarantee rather than a slogan. Libraries come from a pre-baked wheel cache |
| Sandbox strength v1 | Jailed subprocess | See §7 for why not a Windows container |
| Virtual device scope | Real desktop, no separate display | The user's call: a separate device "might cause issues later" — and it avoids all three bridges |
| Tier order | L1 → L2 → **L2.5 code** → L3 GUI | Code is more reliable than clicking, so it must be tried first |

---

## 5. What was actually built

The plan below is kept as written. What shipped diverged from it in two ways
worth recording, because a plan next to its outcome is more useful than one
edited to look prescient.

**`minos undo` was inserted first**, as M17, ahead of the sandbox. It was a day's
work on machinery that already existed, and it is the most visible thing in the
project — leaving it unexposed while building more capability would have been
the wrong order.

**Concurrency locking was inserted second**, as M18, once the project was
confirmed as open source. Two runs sharing a state directory break the audit
hash chain, and that is a bug report waiting to happen the moment anyone else
runs it.

| Shipped | Branch | Was planned as |
|---|---|---|
| M17 `minos undo` + retention | `m17` (direct to main) | *unplanned — pulled forward* |
| M18 single-writer lock | `m18/concurrency-lock` | *unplanned — OSS requirement* |
| M19 sandbox + L2.5 code tier | `m19/sandbox` | M17 + M18 + M20 |
| M20 wiring, packages, proof task | `m20/code-wired` | M19 |
| M21 virtual device | `m21/virtual-device` | M21 |
| M22 grounding | `m22/grounding` | M22 |
| M23 `.env` configuration | `m23/env-config` | *unplanned — requested* |
| M24 invariant evals | `m24/invariant-evals` | M23 |
| M25 compensation execution | `m25/compensation` | *from PLAN.md §10, long overdue* |

Two things in the plan below did **not** ship as described. The pre-baked wheel
cache (M19 as planned) became a *report* on the interpreter the runtime already
has, plus an optional `sandbox` extra — installing packages is the user's
decision, made with the user's package manager. And the open-weights grounding
model (M22 as planned) was not needed: the accessibility tree alone was enough,
and adding a vision model to click a button that UIA can already name would have
been effort spent in the wrong place.

---

## 5a. Milestones as planned

One branch per milestone, merged to `main`, per existing convention.

### M17 — `code.run`: the scratchpad
New capability. A jailed subprocess: own virtualenv, cwd-jailed to a per-session
workspace, no network, CPU/memory/wall-clock limits, environment scrubbed of
credentials. Declared inputs are copied in read-only; everything written in the
workspace is an artifact.

**Nothing reaches the real machine in this milestone.** Deliberate — it makes
M17 safe to land and test on its own.

*Tests: escape attempts (`..`, symlinks, absolute paths), timeout kill, network
refusal, environment scrub.*

### M18 — Promotion: retroactive effect contracts
`code.materialize`. Artifact set → effect contract → checkpoint → apply →
hash-oracle verify → reversible. Reuses `Broker.dry_run` to show the diff before
anything lands.

**This is the milestone where the project becomes general.** M17 without M18 is
plumbing.

### M19 — Offline environment resolution
The sandbox needs libraries and has no network. A pre-baked wheel cache, pinned
and hash-checked, with `minos doctor` reporting what is available and what a task
would have needed. A task that needs an absent library fails with the name of
the library, not a traceback.

### M20 — Router: code as a tier
Insert `L2_CODE` between adapter and GUI. `RoutingStats` picks it up for free,
which yields a falsifiable claim for the README: **adding `code.run` should
measurably reduce `gui_rate`.**

### M21 — Virtual device: real mouse and keyboard
Implement `CuaDriver` at the seam in `tiers/l3_gui/driver.py`. Windows first.
Window-targeted input via UIA where the application supports it; `SendInput` as
the global fallback. A panic key aborts the run and releases input.

Fills `ui.click` / `ui.type` / `ui.key` / `ui.screenshot`, all four of which are
already registered capabilities backed by a stub.

### M22 — Grounding: the accessibility tree
Click *elements*, not coordinates — coordinate clicking is the largest single
source of GUI agent flakiness. An open-weights grounding model only where the
accessibility tree fails.

**This is where "open WhatsApp and message someone" becomes real**, not M21.

### M23 — Invariant evaluation
The eval problem a general system creates: you cannot enumerate an unbounded
surface, so you stop testing capabilities and test **invariants**.

- Containment holds under adversarial scripts
- Nothing irreversible happens without a checkpoint
- Every effect that reached the real machine carries an oracle or an explicit confirmation
- A whole session undoes cleanly
- Prompt injection through filenames is refused (`memory/store.py` already documents memory content as untrusted)

Invariant tests scale to infinite capability. Capability tests never will.

---

## 6. What this touches

| File | Change |
|---|---|
| `capabilities.py` | `+code.run`, `+code.materialize`; `ui.*` go from stub to live |
| `scopes.py` | New capabilities, plus a workspace-scoped path class |
| `types.py`, `router.py` | `L2_CODE` tier; `RoutingStats` follows for free |
| `broker.py` | Retroactive-contract admission path — the one genuinely new code path |
| `oracles.py` | `FileTreeOracle` promoted from collateral detection to contract derivation |
| `tiers/l3_gui/driver.py` | `CuaDriver` implemented at the existing seam |
| `planner/` | Planners learn "write code for it" as the general fallback |
| `evals/` | Invariant suite alongside the capability suite |

Nothing there is a rewrite. The architecture anticipated most of it.

---

## 7. Rejected: a Windows container for the sandbox

Considered for M17 and rejected for v1. The reasons, since they will come up
again:

- **Windows containers are not Linux containers.** Process isolation
  (`--isolation=process`) shares the host kernel and is not a security boundary
  against hostile code; Hyper-V isolation is, and costs a full VM boot per run —
  seconds to tens of seconds against a subprocess's milliseconds.
- **Image size.** The smallest usable Windows base images are gigabytes, against
  a virtualenv's tens of megabytes, on a machine `MODELS.md` already flags as
  disk-constrained.
- **It fights the GPU.** Local inference wants the GPU. GPU access from Windows
  containers is narrow and version-brittle, and the project's whole offline story
  depends on the local model working.
- **Host version coupling.** Windows container images must match the host build
  closely. That turns "clone and run" into a support matrix, on a project whose
  CI already treats macOS as Tier 2 for want of hardware.
- **It requires Docker Desktop or Hyper-V.** A licensing and installation
  precondition for every contributor, to protect against a threat that is not
  the realistic one.
- **The realistic threat is buggy, not hostile.** The code is written by a local
  model against the user's own task. A jailed subprocess with no network, a
  scrubbed environment and hard resource limits addresses badly-written code,
  which is what will actually happen.

**Revisit when** the sandbox runs code from an untrusted source — a downloaded
skill, a shared recipe, a remote planner. Containment requirements change with
the threat, and that is a different threat.

The subprocess jail is designed to be replaced: `code.run` takes a sandbox
backend, and the container becomes one implementation of it.

**Revisited, and the trigger above is now the switch.** `ContainerSandbox`
implements `SandboxBackend`, and `minos.sandbox.origin` decides between them from
where the code came from rather than from a setting: `local-planner` keeps the
subprocess, and `remote-planner`, `shared-skill` and `downloaded` get a container
or a refusal. Every objection in the list above survives, because none of them
was about *Linux* containers — which is what Docker or Podman runs here, on
Windows too, through WSL2. What was rejected was making a container the default
for the common case, and it still is not the default for the common case.

The subprocess path did not stay as it was either: it now carries Landlock and
seccomp on Linux, a low-integrity token and a Job Object on Windows, and a
seatbelt profile on macOS. "A jailed subprocess with no network, a scrubbed
environment and hard resource limits" understated what was needed even for the
buggy-not-hostile threat, because *buggy* code reads the user's home directory
by accident just as readily.

---

## 8. Open questions

Tracked in [REVIEW.md](REVIEW.md) as gaps; listed here as decisions the next
milestones will force.

1. Does `COMPENSABLE` execution land before M17, closing `PLAN.md`'s own top
   risk, or after?
2. Does the broker split into its own process, as `PLAN.md` §11 recommended for
   M6?
3. What is the retention policy for the checkpoint store, now that `gc()` exists
   but nothing calls it?
4. Is a user-facing `minos undo` its own milestone?
5. What is the credential model, before the sandbox or the virtual device meets a
   logged-in application?
