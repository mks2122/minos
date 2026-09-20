# Status

Last updated 20 Sep 2026. **409 tests passing, 3 skipped. Eval 15/15, no regressions.
Ruff clean, mypy strict clean.**

Written so nobody has to guess which parts are real.

## Built and tested

| | |
|---|---|
| **Policy broker** | Capability scopes, admission, dry-run, checkpoint, verify, reverse, record |
| **Capability scopes** | Deny beats allow, ties deny, resolved-real-path matching, structurally immutable, narrowing-only |
| **Checkpointing** | Copy-before-write journal over declared targets. Portable; `clonefile`/`FICLONE` are optional fast paths. Refuses targets it cannot copy, restores mode/mtime, handles directories and symlinks, `gc()` reclaims |
| **Oracles** | File hash, file tree (collateral detection), path existence, cell, screen (weak), null (counted) |
| **Audit** | Append-only hash-chained JSONL, tamper-evident |
| **Tier router** | L1 → L2 → L3 with recorded degradation and published fallback rate |
| **L1** | Filesystem (9 operations), process spawn, **`app.open`** (default handler), memory recall |
| **L2** | Tabular: cell-level workbook operations. CSV built in, backends pluggable |
| **L3** | Click / type / key / screenshot behind a driver protocol — **stub driver only** |
| **Planner** | Protocol, scripted, Claude (manual tool loop), **local** (any OpenAI-compatible server) |
| **Offline** | `minos doctor` checks readiness; `--offline` refuses to call a remote model |
| **Agent loop** | Step budget, halt on `reconciliation_required`, abandon on repeated denial |
| **Eval** | 15 tasks, binary, seeded, 6 REFUSE tasks, regression detection, CI-enforced |
| **Memory** | File index, provenance, **app/open history**, **polling filesystem watcher**, FTS, deictic resolution with explanations -- wired into the agent as scope-gated `memory.recall` / `memory.recent` |
| **Skills** | Promotion with refusals, scoped replay, drift detection, agentskills-compatible storage |

## Stubbed or absent

| | |
|---|---|
| **Real GUI control** | `CuaDriver` raises `NotImplementedError` with instructions. Wiring up cua-driver is the work |
| **Frontier model runs** | The Claude planner is tested against a fake client only. **No remote model has been scored on the eval suite** (a local one has: 12/15) |
| **`net.http`** | Registered as a capability; no adapter implements it |
| **Compensation execution** | `COMPENSABLE` inverses are declared and scope-checked, but not yet *run* on failure |
| **Kernel confinement** | Landlock/seccomp/AppContainer. The broker's mediation is the only enforcement today |
| **Audit anchoring** | The chain is tamper-evident, not tamper-proof. An external anchor is not implemented |
| **Native file events** | The watcher polls. inotify / FSEvents / ReadDirectoryChangesW would be faster but are three different APIs with three sets of bugs |
| **OS-wide window history** | Only openings *this runtime* performed are recorded. A document you double-clicked in Explorer is invisible |
| **Content indexing** | Memory indexes filenames and metadata, never file contents. "the file about Q3 revenue" matches on the path, not the text |
| **Cost accounting** | No token or dollar tracking in the eval harness |
| **Checkpoint encryption** | The object store holds plaintext copies. `chmod 0700` on POSIX, inherited ACLs on Windows |
| **Checkpoint retention** | `gc()` exists; no policy calls it |
| **Atomic multi-target restore** | Objects are checked up front, but a mid-sequence write failure leaves earlier targets restored |
| **Multi-run variance** | Agents are stochastic; single-run scores overstate dependability |

## Running fully offline

Supported and verified on Windows 11 / RTX 5060 Laptop (8 GB VRAM):

```bash
uv run minos doctor      # reads real VRAM, says YES or exactly what is missing
```

`qwen3:8b` (~5 GB of Q4 weights) sits entirely in 8 GB of VRAM. `--offline`
makes local a guarantee rather than a preference -- it fails instead of calling
a remote model. See [LOCAL.md](LOCAL.md).

## Honest caveats

- **The 15/15 eval is the reference planner**, a fixed script. It proves the tasks are
  solvable and the runtime behaves. It says nothing about any model. The real model
  figure is **12/15 for `qwen3:8b`**, single run, short-horizon tasks only.
- **A bug in the broker is a full bypass.** There is no second line of defence on macOS or
  Windows, because kernel confinement is not portable. See [SECURITY.md](../SECURITY.md).
- **macOS is Tier 2**: CI-green, never hand-verified, because the maintainer does not own a Mac.
- **The eval suite is short-horizon.** Every task is under 10 steps, which is the regime where
  published agents look good and real work does not live.

## Next

See [PLAN-GENERALITY.md](PLAN-GENERALITY.md) for M17 onward, and [REVIEW.md](REVIEW.md)
for the audit those milestones answer.

1. `code.run` — a sandboxed scratchpad, so capability stops being a per-tool cost.
2. Execute `COMPENSABLE` inverses on failure — currently declared but not run, and
   `PLAN.md`'s own top risk says to stop and fix it.
3. Wire up `cua-driver` so L3 is real, and add GUI tasks to the eval suite.
4. Score a model on the suite and publish the number, with the model named and dated.
5. Landlock on Linux as defence-in-depth.
