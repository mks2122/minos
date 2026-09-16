# Build Plan — Open-Source Desktop Agent Runtime

**Owner:** Karthick · **Date:** 16 September 2026 · **Type:** Open-source project, not a startup
**Status:** Pre-repo. Nothing built yet.

---

## 0. What this is, in one sentence

> **The model asks. The runtime decides.**
>
> An open-source desktop agent where every action — a syscall, a typed Excel write, or a synthetic click — passes through one capability-scoped policy broker, with declared effects, full provenance, and a rollback you can actually trust.

### Why this claim and no other

Per [07-oss-landscape.md](07-oss-landscape.md), every other framing is taken:

| Framing | Verdict |
|---|---|
| "Three-tier hierarchy, GUI as fallback" | ❌ Taken — Microsoft UFO², MIT repo + arXiv:2504.14603 |
| "Learns skills from experience" | ❌ Taken — Hermes (246k ⭐), OpenClaw (389k ⭐), agentskills.io standard |
| "Local-first, any model" | ❌ Table stakes — every project has it; claiming it signals no survey |
| "Computer-state memory" | 🟡 Nearly unclaimed, but it's a *feature*, hard to demo in one screenshot → **bullet #2** |
| **"One policy broker under all tiers, with verified reversal"** | ✅ **Intersection is empty.** No OSS project combines tiered control + GUI fallback + a real policy/rollback layer |

### Non-goals — write these in the README

- ❌ Not an operating system. Not a distro. Not a compositor.
- ❌ Not a model. Not a fine-tune. Not a grounding model.
- ❌ Not a new inference engine or loading algorithm (use llama.cpp's `--n-cpu-moe`).
- ❌ Not a skill marketplace.
- ❌ Not trying to beat Hermes or OpenClaw on breadth. Narrower and more trustworthy, not bigger.

---

## 1. The one thing that decides whether this succeeds

**Rollback must cover external effects, not just the filesystem.**

Hermes' own docs concede: *"checkpoints are filesystem-only. They do not capture external effects like API calls, database modifications, or environment changes."*

If your rollback is also filesystem-only, **you have rebuilt Hermes' checkpoint system with a different README.** Everything else in this plan is negotiable. This is not.

The answer is OpenAdapt-Flow's pattern: **declare the intended effect before acting, verify it against the system of record after, refuse to claim success from a screenshot.** Their benchmark — screen-only checks silently accepted **75%** of wrong effects; one system-of-record oracle cut that to **12.5%**.

---

## 2. Architecture

```
                         USER
                          │
                    natural language
                          │
                          ▼
         ┌────────────────────────────────────┐
         │  PLANNER  (swappable)              │
         │  frontier API │ local MoE │ hybrid │
         └────────────────┬───────────────────┘
                          │  emits ActionRequest
                          │  (never executes)
                          ▼
    ╔══════════════════════════════════════════════════╗
    ║  POLICY BROKER            ← THE PRODUCT          ║
    ║                                                  ║
    ║  capability scopes   · what may this touch?      ║
    ║  effect contract     · what should change?       ║
    ║  admission decision  · allow / prompt / deny     ║
    ║  provenance record   · who asked, why, when      ║
    ║  dry-run mode        · diff without doing        ║
    ║  checkpoint          · pre-action snapshot       ║
    ║  effect oracle       · verify against SoR        ║
    ║  reversal            · undo, or halt loudly      ║
    ╚═══════════════════════┬══════════════════════════╝
                            │  only the broker executes
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
    ┌───────────┐    ┌─────────────┐   ┌──────────────┐
    │ L1 SYSTEM │    │ L2 ADAPTERS │   │ L3 GUI       │
    │ fs, proc, │    │ typed, MCP  │   │ FALLBACK     │
    │ window,   │    │ office,     │   │ vision +     │
    │ clipboard,│    │ browser,    │   │ synthetic    │
    │ terminal  │    │ editor, pdf │   │ input        │
    └───────────┘    └─────────────┘   └──────────────┘
          │                 │                 │
          └─────────────────┴─────────────────┘
                            │
                 ┌──────────┴──────────┐
                 ▼                     ▼
        ┌─────────────────┐   ┌──────────────────┐
        │ COMPUTER-STATE  │   │ SKILL STORE      │
        │ MEMORY          │   │ verified,        │
        │ files · windows │   │ policy-scoped,   │
        │ apps · edits    │   │ deterministic    │
        │ over time       │   │ replay           │
        └─────────────────┘   └──────────────────┘
```

### The three invariants

1. **The planner never executes.** It emits `ActionRequest` objects. The broker is the only component with authority. This is the whole design.
2. **Every tier goes through the same gate.** An L3 synthetic click is audited identically to an L1 `unlink()`. No tier is privileged, no tier bypasses.
3. **Degradation is auditable.** Falling back L1→L2→L3 is recorded with a reason: *"no adapter covered this app, fell back to pixels."* Fallback rate becomes a headline metric, not a hidden failure.

---

## 3. Build vs. borrow

**Rule: if it isn't the broker, the memory index, or the tier router — borrow it.**

| Layer | Decision | What |
|---|---|---|
| L3 GUI substrate | 🔵 **Borrow** | `cua-driver` (MIT) — a11y tree + screenshot + synthetic cursor, cross-OS |
| Grounding model | 🔵 **Borrow** | UI-TARS-1.5-7B or OpenCUA-7B (open weights, vLLM) |
| L2 browser | 🔵 **Borrow** | Playwright MCP or Stagehand (MIT). ⚠️ avoid Skyvern (AGPL) |
| L2 office | 🔵 **Fork** | `mcp-libre` / `libreoffice-mcp`; lift UFO²'s AppAgent adapters (MIT) |
| Skill format | 🔵 **Adopt** | agentskills.io / Anthropic Agent Skills — don't invent a format |
| FS snapshot | 🔵 **Borrow** | `snapshot-contain-protect`, or btrfs/overlayfs directly |
| Sandbox | 🔵 **Borrow** | E2B (Apache-2.0, Firecracker). ⚠️ **NOT Daytona** — closed-source since Jun 2026 |
| Local inference | 🔵 **Borrow** | llama.cpp (`--n-cpu-moe`) / Ollama |
| Effect oracles | 🟣 **Steal the pattern, write your own** | OpenAdapt-Flow (9 ⭐, 661 commits — talk to them, best merge candidate) |
| **Policy broker** | 🟢 **BUILD** | The product |
| **Tier router + capability manifests** | 🟢 **BUILD** | The architecture |
| **Computer-state memory** | 🟢 **BUILD** | The #2 differentiator |
| **Eval harness** | 🟢 **BUILD** | The credibility |

---

## 4. Platform and stack decisions

### Cross-platform from day one: Windows, macOS, Linux. Decided.

**This reverses an earlier "Linux first, develop in WSL2" call.** WSL2 cannot host this product — it cannot see or drive Windows GUI applications, and `inotify` does not fire for files under `/mnt/c`, which breaks the computer-state memory index against your real files. A desktop agent has to run natively on the desktop it acts on.

The portability problem this creates is reversal, and it resolves better than expected: because the effect contract **declares its targets before acting**, checkpointing means copying a few named files, not snapshotting a volume. A copy-before-write journal is pure `shutil` + `hashlib` and behaves identically everywhere; `clonefile` (APFS), `FICLONE` (btrfs/XFS) and ReFS block cloning are optional fast paths. Windows loses no correctness — only some speed.

What is genuinely given up: **kernel-enforced confinement as a foundation.** Landlock/seccomp have no portable equivalent, so the broker's own mediation is the enforcement on every platform. That holds only because of invariant I1 — nothing but the broker can do I/O — and the consequence (a broker bug is a full bypass) belongs in `SECURITY.md`.

**Develop on Windows** — it's your machine, and a solo OSS project you can't dogfood daily is a project that stalls. **CI on all three from the first commit**, when fixing a portability break costs minutes rather than months.

Support tiers for the README: **Tier 1 Windows + Linux** (full suite + eval), **Tier 2 macOS** (CI-green, eval unverified until you can borrow a Mac — say so honestly).

Detail in [PORTABILITY.md](PORTABILITY.md). Cost: roughly **+30–40% effort**, front-loaded.

### Python. Decided.

MCP SDK, model clients, vision models, llama.cpp bindings are all Python-native. The broker's value is its *semantics*, not its throughput. Ship first.

**But:** keep the broker behind a clean process boundary (a daemon with a typed IPC surface) so it can be rewritten in Rust later without touching callers. Design for that now; don't do it now.

### Other decisions

| Choice | Decision |
|---|---|
| License | **Apache-2.0** — permissive, patent grant, no ambiguity for contributors |
| Policy language | Start with declarative YAML. Consider OPA/Rego only if users ask |
| Audit log | Append-only, hash-chained JSONL. Tamper-evident from day one — cheap now, impossible to retrofit |
| Memory store | SQLite + FTS5 + `fanotify`/`inotify` watcher. Not Letta, not Mem0 — wrong data model |
| Planner default | Frontier API. Local is an option, never a requirement |
| Python deps | `uv`. Fast, and signals you know the ecosystem |

---

## 5. Milestones

Estimates assume **focused full-time weeks**. Halve your confidence if this is evenings-and-weekends; roughly double the calendar.

### M0 — Foundations · 1 week
- Repo, Apache-2.0, `uv`, CI, pre-commit
- `ARCHITECTURE.md` with the three invariants and the diagram above
- `ActionRequest` / `EffectContract` / `AdmissionDecision` type definitions
- **Done when:** a stranger can read `ARCHITECTURE.md` and correctly explain why the planner can't execute anything

### M1 — The broker core · 3 weeks 🎯 *this is the project*
- Capability scope grammar (`fs.read:~/Invoices/**`, `fs.write:~/Invoices/2026/**`, `net.http:api.example.com`, `proc.spawn:libreoffice`)
- Admission pipeline: request → scope check → effect contract → allow / prompt / deny
- Hash-chained append-only audit log
- **Dry-run as a first-class mode** — produce the full diff of what *would* happen, execute nothing
- **Done when:** you can express "this task may read `~/Invoices` and write only to `~/Invoices/2026`, and may not touch the network" and the broker enforces it against a deliberately hostile test suite

### M2 — L1 tools + reversal · 3 weeks
- Filesystem, process, window, clipboard, terminal — all behind the broker
- Pre-action checkpointing: copy-before-write journal over the contract's declared targets, with platform fast paths behind a `CheckpointStore` interface
- `rollback` that restores state and **verifies** the restoration
- **Effect oracles v1:** file hash, file existence, process state
- **Done when:** the 30-second demo works — run a task in dry-run, see the diff; run it for real; `rollback`; prove byte-identical restoration

### M3 — Tier router + L2 adapters · 3 weeks
- Adapter registry with capability manifests (*what can this adapter do, what scopes does it need*)
- Router: prefer L1 → L2 → L3, with **recorded reason** for each degradation
- First adapters: LibreOffice Calc (forked from `mcp-libre`), filesystem, browser via Playwright MCP
- **Done when:** a task routes to a typed Calc adapter, and the log says why it didn't need pixels

### M4 — Planner loop + flagship task · 3 weeks
- Agent loop: plan → request → admit → execute → verify → record
- Frontier API planner; pluggable interface with a local option stubbed
- **One flagship task working end to end**, reliably: *"update Q3 in last year's sales spreadsheet"*
- **Done when:** the flagship task succeeds ≥8/10 runs from a cold start

### M5 — Eval harness + 🚀 PUBLIC RELEASE · 2 weeks
- 20–30 desktop tasks, binary completion scoring, seeded environments
- Regression detection across model versions; **fallback-rate and rollback-success as first-class metrics**
- **Publish v0.1 with measured numbers, including the failures**
- Writeup: *"I measured desktop agent reliability and here's what I found"* — and resolve the OSWorld 2.0 discrepancy (paper says 20.6%, leaderboard says 73%) as part of it
- **Done when:** the README states real success rates per task category and you'd defend every number

> **Release here, not later.** ~15 weeks in. Honest numbers in a field full of overclaiming is the whole positioning. Shipping at M5 also means the eval exists *before* the features it would flatter.

### M6 — Computer-state memory · 4 weeks
- File index (`fanotify`), window/app history, edit provenance
- Deictic resolution: "the Excel from yesterday" → a concrete path, **with an explanation of how it decided**
- **Done when:** it resolves a reference you didn't spell out, and shows its reasoning

### M7 — Skill promotion · 4 weeks
- Verified trajectory → deterministic replay script
- **Each skill carries its capability manifest and effect oracle** — this is the only thing that differentiates it from Hermes
- Drift detection; fall back to the planner when replay no longer matches
- agentskills.io-compatible export
- **Done when:** a repeated task runs from cache with zero model calls, and refuses to run when the UI changed

### M8+ — Later
Local planner path · L3 GUI fallback with UI-TARS-7B · external-effect oracles (HTTP/SQL) · Windows support · Wayland portal integration

---

## 6. Metrics that go in the README

Not stars. These:

| Metric | Why |
|---|---|
| Task success rate (binary, per category) | The only honest capability number |
| **Fallback rate** L1→L2→L3 | Lower is better; proves the architecture works |
| **Rollback success rate** | The core claim; anything under 100% is a bug report |
| **Unverified-effect rate** | Actions claimed successful without oracle confirmation. **Target: 0** |
| Dry-run/actual divergence | If the dry-run diff didn't match reality, the broker lied |
| Cost and wall-clock per task | Grounds the whole thing in reality |

---

## 7. Threat model — write this in `SECURITY.md` before v0.1

You are publishing an agent with filesystem access. Prompt injection is unsolved, RCE-class since May 2026, with 50–84% attack success rates in the literature.

**Assume the planner is compromised.** That is the design premise, and it's why the broker holds the authority.

| Threat | Mitigation |
|---|---|
| Indirect prompt injection via file/web content | Capability scopes bound blast radius; content-derived instructions never widen scope |
| Agent escalates its own permissions | Scopes are set before the task and immutable during it |
| Silent destructive action | Dry-run default; checkpoint before every write; approval checkpoints |
| Success claimed from a screenshot | Effect oracles read the system of record |
| Audit tampering | Hash-chained append-only log |
| Exfiltration via network tool | Network is a scoped capability, default-deny |

**Be explicit in the README about what it does *not* protect against.** That honesty is the differentiator.

---

## 8. Repo layout

```
.
├── README.md                 # the claim, the demo GIF, the numbers
├── ARCHITECTURE.md           # three invariants, diagram, why the planner can't execute
├── SECURITY.md               # threat model, and what it does NOT protect against
├── EVALUATION.md             # methodology, task suite, current results
├── CONTRIBUTING.md           # low-obligation: no support promise, no roadmap promises
├── src/
│   ├── broker/               # 🟢 capability scopes, admission, audit, dry-run, rollback
│   ├── router/               # 🟢 tier selection + degradation accounting
│   ├── tiers/
│   │   ├── l1_system/
│   │   ├── l2_adapters/      # registry + manifests; adapters mostly vendored
│   │   └── l3_gui/           # thin wrapper over cua-driver
│   ├── memory/               # 🟢 file + window + app-state index
│   ├── skills/               # 🟢 promotion, manifests, drift detection
│   └── planner/              # thin; swappable; deliberately boring
├── eval/
│   ├── tasks/                # seeded, reproducible
│   └── harness/
└── examples/
```

---

## 9. Publishing plan

1. **Don't announce at M0.** Announcing an empty repo spends credibility you haven't earned.
2. **Ship M5 with numbers.** Post to r/LocalLLaMA, HN Show HN, and the cua / OpenAdapt / Hermes discussions. Lead with the measurement writeup, not the project.
3. **Talk to OpenAdapt-Flow early** — 9 stars, 661 commits, the right ideas about effect verification, no distribution. Best merge candidate in the space. Contact them around M2, once you have something real to show.
4. **Credit everything you build on, prominently.** It costs nothing and it's how you get read by the maintainers of cua, UFO² and Hermes.
5. **Set expectations low in the README:** personal project, no support promise, use at your own risk. You can never disappoint.
6. **Don't build a community.** Build the thing, publish measured results, let people find it.

---

## 10. Risks

| Risk | Signal | Response |
|---|---|---|
| **Rollback stays filesystem-only** | You're at M2 and external effects still aren't modelled | **Stop and fix.** This is the whole differentiator |
| Hermes or UFO² ships a broker | Their changelog | Your tier-spanning scope model is still distinct — but re-read the claim honestly |
| Scope creep into an OS | You're writing a compositor | Re-read `00-validation-report.md` |
| Flagship task never gets reliable | M4 runs below 5/10 | **Ship anyway at M5 with the honest number.** The measurement is the contribution |
| Motivation decay at month 4 | The usual | M5 is deliberately early. Get it public before then |
| Nobody cares | Silence after release | Acceptable. The reputation and the learning were the point |

---

## 11. Open questions to resolve before M1

1. Scope grammar — path globs only, or a capability URI scheme? *(Recommend: start with globs, they're legible)*
2. Approval UX — CLI prompt, desktop notification, or web UI? *(Recommend: CLI for v0.1)*
3. Does the broker run as a separate process from day one, or same-process with a clean interface? *(Recommend: same-process, clean interface, split at M6)*
4. Do effect contracts get declared by the planner, by the adapter, or both? *(Recommend: adapter declares the shape, planner fills the values — the adapter is trusted, the planner is not)*

---

## 12. This week

1. Create the repo. Apache-2.0. Empty but real.
2. Write `ARCHITECTURE.md` — the three invariants and the diagram. **Writing it will expose the parts you haven't thought through.**
3. Define `ActionRequest`, `EffectContract`, `AdmissionDecision` as types. No implementation.
4. Read, in this order: UFO²'s tier code, Hermes' checkpoint docs (especially the stated limitation), OpenAdapt-Flow's oracle implementation.
5. Write down your flagship task in precise, testable terms. It becomes eval task #1.

---

## Related

[00-validation-report.md](00-validation-report.md) · [05-architecture-notes.md](05-architecture-notes.md) · [07-oss-landscape.md](07-oss-landscape.md)
