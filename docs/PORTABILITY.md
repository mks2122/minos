# Cross-Platform Design

**Requirement:** Windows, Linux and macOS.
**Date:** 16 Sep 2026 — supersedes the "Linux first" decision in earlier drafts.

---

## The problem this creates

Reversal is the project's differentiator, and snapshotting is the *most* platform-specific thing in computing:

| Platform | Native snapshot | Verdict |
|---|---|---|
| **Linux** | overlayfs (universal), btrfs/ZFS (if present) | ✅ good |
| **macOS** | APFS snapshots via `fs_snapshot_create`; `clonefile(2)` for cheap CoW copies | ✅ good, needs privileges for volume snapshots |
| **Windows** | VSS — volume-level, admin-required, slow. ReFS block cloning exists but is rare; NTFS has nothing comparable | ❌ poor |

If checkpointing depended on filesystem snapshots, Windows support would be a second-class citizen with a different reliability story. That's unacceptable when reversal *is* the claim.

---

## The resolution — and it's better than the platform-specific version

> **The effect contract already declares its targets before the action runs. So you don't need a filesystem snapshot. You need to copy three files.**

This is the payoff of a design decision made for a different reason. Because `EffectContract.targets` is declared *before* execution, the checkpoint only has to cover **the declared targets**, not the volume.

```python
class CheckpointStore(Protocol):
    def checkpoint(self, targets: list[Path]) -> CheckpointId: ...
    def restore(self, id: CheckpointId) -> RestoreResult: ...
    def verify(self, id: CheckpointId) -> bool: ...
```

**Portable baseline — copy-before-write journal.** Before a `REVERSIBLE` action, copy each declared target into a content-addressed store, record the mapping, act. Restore = copy back, then re-run the oracle to confirm. Pure `shutil` + `hashlib`. Works identically on all three platforms.

**Platform fast paths are optimisations, not requirements:**

| Platform | Fast path | Gain |
|---|---|---|
| macOS | `clonefile(2)` — APFS copy-on-write | Near-zero cost, near-zero space |
| Linux | `FICLONE` ioctl (btrfs/XFS reflink), or overlayfs upper-diff | Near-zero cost |
| Windows | ReFS block clone where available, else plain copy | Plain copy is fine at this scale |
| Any | Hardlink when the file is about to be replaced rather than modified in place | Cheap |

Selection is automatic with a graceful fall to the baseline. **No platform gets a worse correctness story — only a slower one.** That's exactly the right shape for a portability boundary.

### The honest limit, and why the design already handles it

Copy-before-write only protects **declared** targets. If an action touches files it didn't declare, those writes are unprotected.

That case is already covered: `FileTreeOracle` compares a directory manifest before and after and detects collateral writes. When it fires, you know reversal is incomplete — so you **halt with `RECONCILIATION_REQUIRED`** rather than claiming a clean rollback. The system fails loudly at exactly the moment it cannot keep its promise, which is the only acceptable behaviour for this component.

---

## Capability enforcement — the thing I need to be straight about

An earlier draft said *"where the kernel can enforce it (mount namespaces, seccomp), use the kernel."* **That is not portable, and the cross-platform requirement forces an honest restatement.**

| Platform | Kernel-level confinement | Reality |
|---|---|---|
| Linux | mount namespaces, seccomp, Landlock | Strong, available |
| macOS | `sandbox-exec` (deprecated), Endpoint Security (needs an Apple entitlement) | Weak to unavailable for an OSS project |
| Windows | AppContainer, restricted tokens, Job Objects | Workable but awkward and poorly documented |

**So the primary enforcement is the broker's own mediation, on every platform.**

That is defensible *only* because of invariant I1: the planner has no filesystem handle, no subprocess API, no network client, no input device. The broker isn't checking permissions on the side while other code does I/O — **the broker is the only path to I/O.** It's a chokepoint, not an advisory check.

But say the consequence plainly, in `SECURITY.md`:

> **A bug in the broker is a full bypass.** There is no second line of defence on macOS or Windows. Keep the broker small enough to audit by reading it.

Kernel confinement becomes **per-platform defence-in-depth, added later** — Landlock on Linux first, because it's the cheapest real win. It is not the foundation.

---

## Per-tier portability

| Tier | Portable? | Notes |
|---|---|---|
| **L1 filesystem** | ✅ | `pathlib`. Watch: case-insensitivity on macOS/Windows, path length limits, symlink semantics, `chmod` being a no-op on Windows |
| **L1 process** | ✅ | `subprocess` + `psutil` |
| **L1 window mgmt** | ⚠️ | Genuinely platform-specific. **Don't write it — use `cua-driver`**, which already covers all three |
| **L1 clipboard** | ✅ | `pyperclip` or per-platform, small surface |
| **L2 LibreOffice** | ✅ | Runs on all three. **Make this the first adapter — one adapter, three platforms** |
| **L2 MS Office** | ❌ | Windows `win32com`, macOS AppleScript. Two implementations, or skip it |
| **L2 browser** | ✅ | Playwright is cross-platform |
| **L3 GUI** | ✅ | `cua-driver` — a11y tree + screenshot + synthetic input on macOS/Windows/Linux |
| **Checkpoint** | ✅ | Copy-before-write baseline + platform fast paths (above) |
| **Audit log** | ✅ | JSONL + hash chain |
| **Memory index** | ⚠️ | File watching differs: `inotify` / `FSEvents` / `ReadDirectoryChangesW`. Use `watchdog`, which abstracts all three |

**Adapters declare their platforms** in the capability manifest. The router filters by current platform, and an unavailable adapter is a normal L2→L3 degradation with a recorded reason — no special-casing needed. The architecture absorbs platform gaps through a mechanism it already has.

---

## Revised development strategy

**Portable by design, from day one. Sequential by *validation*.**

Previous advice was "Linux first, develop in WSL2." **That was wrong once cross-platform became a requirement**, for two reasons: it bakes in Linux assumptions that are brutal to retrofit, and — more practically — **you'd be developing on a machine where you can't dogfood.** You run Windows. A solo OSS project you don't use daily is a project that stalls.

**Develop on Windows. Run CI on all three from the first commit.**

CI is what actually enforces portability — not intentions:

```yaml
strategy:
  matrix:
    os: [ubuntu-latest, windows-latest, macos-latest]
```

Turn it on at M0, when the test suite is trivial and fixing a portability break costs minutes. Adding it at M4 means discovering six months of accumulated Windows-isms at once.

### Support tiers — set expectations in the README

| Tier | Platform | Meaning |
|---|---|---|
| **Tier 1** | Windows, Linux | Full test suite green in CI, eval suite runs |
| **Tier 2** | macOS | Core suite green in CI; eval suite unverified until you borrow a Mac |
| — | — | Promote macOS to Tier 1 when you can actually run the eval on one |

**Be honest that you don't own a Mac.** "CI-tested, not hand-verified" is a perfectly respectable claim and far better than implying coverage you don't have. It's also the kind of thing that attracts a contributor who *does* own one.

---

## What this costs

Roughly **+30–40% effort**, and it is front-loaded rather than spread out — mostly path handling, process APIs, file watching and three-way CI debugging.

**Worth paying, because:**
- Retrofitting portability after a Linux-only M5 costs far more than 40%
- The copy-before-write design removes Windows' *only* real disadvantage, so the tax is smaller than it first appears
- You get to dogfood on your own machine, which for a solo project is worth more than any of the above

**The one thing genuinely lost:** kernel-enforced confinement as a foundation. That was a real property and it's now a per-platform enhancement. Say so in `SECURITY.md` rather than letting anyone assume otherwise.

---

## Changes to make in the other docs

- [x] `ARCHITECTURE.md` §7 — reversal is a copy-before-write journal, not btrfs/overlayfs
- [x] `ARCHITECTURE.md` §5 — enforcement honesty; broker mediation is primary
- [x] `PLAN.md` §4 — platform decision reversed
- [x] `MODELS.md` — WSL2 no longer the default dev environment
- [ ] `SECURITY.md` (at M0) — state the broker-bug-is-full-bypass consequence explicitly
- [ ] M0 — CI matrix on all three from the first commit

---

## Related

[ARCHITECTURE.md](ARCHITECTURE.md) · [PLAN.md](PLAN.md) · [MODELS.md](MODELS.md)
