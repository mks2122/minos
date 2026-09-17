# Status

Last updated 16 Sep 2026. **269 tests passing, 1 skipped. Eval 15/15, no regressions.
Ruff clean, mypy strict clean.**

Written so nobody has to guess which parts are real.

## Built and tested

| | |
|---|---|
| **Policy broker** | Capability scopes, admission, dry-run, checkpoint, verify, reverse, record |
| **Capability scopes** | Deny beats allow, ties deny, resolved-real-path matching, structurally immutable, narrowing-only |
| **Checkpointing** | Copy-before-write journal over declared targets. Portable; `clonefile`/`FICLONE` are optional fast paths |
| **Oracles** | File hash, file tree (collateral detection), path existence, cell, screen (weak), null (counted) |
| **Audit** | Append-only hash-chained JSONL, tamper-evident |
| **Tier router** | L1 → L2 → L3 with recorded degradation and published fallback rate |
| **L1** | Filesystem (9 operations), process spawn |
| **L2** | Tabular: cell-level workbook operations. CSV built in, backends pluggable |
| **L3** | Click / type / key / screenshot behind a driver protocol — **stub driver only** |
| **Planner** | Protocol, scripted, Claude (manual tool loop), **local** (any OpenAI-compatible server) |
| **Offline** | `writ doctor` checks readiness; `--offline` refuses to call a remote model |
| **Agent loop** | Step budget, halt on `reconciliation_required`, abandon on repeated denial |
| **Eval** | 15 tasks, binary, seeded, 6 REFUSE tasks, regression detection, CI-enforced |
| **Memory** | File index, provenance, FTS, deictic resolution with explanations |
| **Skills** | Promotion with refusals, scoped replay, drift detection, agentskills-compatible storage |

## Stubbed or absent

| | |
|---|---|
| **Real GUI control** | `CuaDriver` raises `NotImplementedError` with instructions. Wiring up cua-driver is the work |
| **Live model runs** | Both model planners are tested against fakes. **No model, local or remote, has been scored on the eval suite** |
| **`net.http`** | Registered as a capability; no adapter implements it |
| **Compensation execution** | `COMPENSABLE` inverses are declared and scope-checked, but not yet *run* on failure |
| **Kernel confinement** | Landlock/seccomp/AppContainer. The broker's mediation is the only enforcement today |
| **Audit anchoring** | The chain is tamper-evident, not tamper-proof. An external anchor is not implemented |
| **Memory watcher** | Indexing is scan-based. inotify / FSEvents / ReadDirectoryChangesW would be an optimisation |
| **Cost accounting** | No token or dollar tracking in the eval harness |
| **Multi-run variance** | Agents are stochastic; single-run scores overstate dependability |

## Honest caveats

- **The 15/15 eval is the reference planner**, a fixed script. It proves the tasks are
  solvable and the runtime behaves. It says nothing about any model.
- **A bug in the broker is a full bypass.** There is no second line of defence on macOS or
  Windows, because kernel confinement is not portable. See [SECURITY.md](../SECURITY.md).
- **macOS is Tier 2**: CI-green, never hand-verified, because the maintainer does not own a Mac.
- **The eval suite is short-horizon.** Every task is under 10 steps, which is the regime where
  published agents look good and real work does not live.

## Next

1. Wire up `cua-driver` so L3 is real, and add GUI tasks to the eval suite.
2. Score a model on the suite and publish the number, with the model named and dated.
3. Execute `COMPENSABLE` inverses on failure — currently declared but not run.
4. Landlock on Linux as defence-in-depth.
