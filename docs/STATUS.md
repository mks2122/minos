# Status

Last updated 5 Oct 2026. **962 tests passing, 10 skipped. Reference eval 22/22, 0 invariant
violations. Ruff clean, mypy strict clean.**

Written so nobody has to guess which parts are real.

## Built and tested

| | |
|---|---|
| **Policy broker** | Capability scopes, admission, dry-run, checkpoint, verify, reverse, record |
| **Capability scopes** | Deny beats allow, ties deny, resolved-real-path matching, structurally immutable, narrowing-only |
| **Checkpointing** | Copy-before-write journal over declared targets. Portable; `clonefile`/`FICLONE` are optional fast paths. Refuses targets it cannot copy, restores mode/mtime, handles directories and symlinks. Pruned on exit by age and size (`MINOS_CHECKPOINT_DAYS`, `MINOS_CHECKPOINT_GB`) |
| **Oracles** | File hash, file tree (collateral detection), path existence, cell, screen (weak), null (counted) |
| **Audit** | Append-only hash-chained JSONL, tamper-evident |
| **Tier router** | L1 → L2 → L2.5 → L3 with recorded degradation and published fallback rate |
| **L1** | Filesystem, process spawn, `app.open` (default handler), browser, asking the user, memory recall |
| **L2** | Tabular: cell-level workbook operations. CSV built in, backends pluggable |
| **L2.5** | **The sandbox**: `code.run` writes and runs Python in a kernel-confined workspace (Landlock+seccomp, a low-integrity token, or seatbelt), or a container when the code's origin is not trusted; `code.materialize` promotes one artifact through the broker as an ordinary checkpointed, verified `fs.write` |
| **L3** | Click / type / key / screenshot. **Real on Windows** via `SendInput`, with `Ctrl+Alt+Esc` to abort; a simulated desktop for the eval suite on every platform |
| **Grounding** | `ui.click` takes an element name and resolves it against the UI Automation tree (or a driver's own); ambiguity raises rather than guessing; invoking avoids moving the cursor |
| **Planner** | Protocol, scripted, Claude (manual tool loop), and any OpenAI-compatible server: local (Ollama, LM Studio, llama.cpp, vLLM) or hosted (OpenRouter, OpenAI, Groq, Together, DeepSeek, Mistral, Fireworks, Gemini, xAI, Cerebras, or a custom URL) |
| **Planner isolation** | The planner runs in its own process that cannot write files or start programs (low-integrity token and one-process job on Windows, Landlock on Linux, seatbelt on macOS). It talks to the runtime only through validated JSON lines |
| **Offline** | `minos doctor` checks readiness; `--offline` refuses any model that is not on this machine |
| **Agent loop** | Step budget, halt on `reconciliation_required`, abandon on repeated denial |
| **Context management** | Every request to an OpenAI-compatible model is fitted to a token budget before it is sent: system prompt and goal always kept, newest steps kept while they fit, the rest named in one line, an oversized latest result shrunk rather than dropped. Estimates are calibrated against the server's own count; a server overflow is retried once at half the budget; a request that cannot fit fails with a reason. The window is what Ollama really serves, or what a hosted provider reports. Claude stays append-only (thinking blocks forbid edits) and old tool results are cleared server-side |
| **Session resume** | `minos run --resume` continues a recorded run in a fresh conversation primed with what it did. Scopes are never inherited |
| **Eval** | 22 tasks, binary, seeded: 7 REFUSE, 4 long-horizon (up to 25 steps), 3 GUI. Regression detection, CI-enforced. `--runs N` reports the spread across runs |
| **GUI tasks** | Type and save, fill a form from a file, refuse input to an ungranted window, against a simulated desktop. Input is approved by a named policy for the simulated windows only, and the audit log records it as a policy, not a person |
| **Container backend** | Tested against its command line in CI, and for real wherever an engine answers: 6/6 on Docker 29.2.1, Windows 11, 5 Oct 2026 |
| **Memory** | File index, provenance, app/open history, polling filesystem watcher, FTS, deictic resolution with explanations -- wired into the agent as scope-gated `memory.recall` / `memory.recent` |
| **Skills** | Promotion with refusals, scoped replay, drift detection, agentskills-compatible storage |
| **Undo** | `minos undo` restores any past action's declared targets. Itself undoable, and recorded in the chain as a human-initiated `state.undo` |
| **Compensation** | `COMPENSABLE` inverses are **executed** on failure, routed back through the broker so they are scoped and logged |
| **Locking** | One writer per state directory, `O_EXCL`, with stale-lock reclaim. Concurrent runs can no longer break the hash chain |
| **Config** | `.env` + environment + defaults, local-model-first; secrets redacted in `doctor` output |
| **Invariants** | 11 runtime promises checked against every task on every eval run, reported separately from task success |
| **Live trace** | Every step printed as it happens -- the model's reasoning, the code it wrote, the verdict, the result |
| **Session transcripts** | `.minos/sessions/*.jsonl`, pruned to 20. A debugging record, deliberately *not* the audit chain |

## Stubbed or absent

| | |
|---|---|
| **Frontier model runs** | The Claude planner and the hosted providers are tested against fakes only. **No frontier or hosted model has been scored on the suite.** It is one command (`minos eval --planner openrouter --runs 3`) and an API key away, and this checkout has no key |
| **GUI on macOS/Linux** | `WindowsDriver` is Windows-only; `CuaDriver` is still the unimplemented seam for the other two |
| **Real input delivery is untested** | Every synthetic event's *encoding* is tested; delivery is not, because a test suite must not drive the developer's cursor. Exercised by hand, and by `tests/gui/live_check.py` |
| **The planner process can read and has the network** | Isolation refuses writes and new processes. Reads are not confined, and the network is needed to reach the model, so a compromised planner process could read a file and send it somewhere. A network allow-list for the child is not done |
| **`net.http`** | Registered as a capability; no adapter implements it |
| **Sandbox reads on Windows** | Writes are confined by a low-integrity token; reads are not. Windows' mandatory policy is no-write-up, and closing the rest needs an AppContainer |
| **Kernel confinement behind the broker** | Landlock, seccomp, seatbelt and the Windows token confine code the model writes and the planner process. The broker's own operations have only the broker's mediation |
| **Container backend not in CI** | CI has no engine. The end-to-end tests skip there and run wherever one is |
| **Audit anchoring** | The chain is tamper-evident, not tamper-proof. An external anchor is not implemented |
| **Native file events** | The watcher polls. inotify / FSEvents / ReadDirectoryChangesW would be faster but are three different APIs with three sets of bugs |
| **OS-wide window history** | Only openings *this runtime* performed are recorded. A document you double-clicked in Explorer is invisible |
| **Content indexing** | Memory indexes filenames and metadata, never file contents. "the file about Q3 revenue" matches on the path, not the text |
| **Cost accounting** | No token or dollar tracking in the eval harness |
| **Checkpoint encryption** | The object store holds plaintext copies. `chmod 0700` on POSIX, inherited ACLs on Windows |
| **Atomic multi-target restore** | Objects are checked up front, but a mid-sequence write failure leaves earlier targets restored |

## Running fully offline

Supported and verified on Windows 11 / RTX 5060 Laptop (8 GB VRAM):

```bash
uv run minos doctor      # reads real VRAM, says YES or exactly what is missing
```

`qwen3:8b` (~5 GB of Q4 weights) sits entirely in 8 GB of VRAM. `--offline`
makes local a guarantee rather than a preference -- it fails instead of calling
a remote model. See [LOCAL.md](LOCAL.md).

## Honest caveats

- **The 22/22 eval is the reference planner**, a fixed script. It proves the tasks are
  solvable and the runtime behaves. It says nothing about any model. The model figure is
  **15/18 and 14/18 for `qwen3:8b`** over two runs on the 18-task alpha suite; a
  three-run score on the current 22-task suite is being recorded with `--runs 3`.
- **A bug in the broker is a full bypass.** The planner now runs outside the broker's
  process, but nothing stands behind the broker itself. See [SECURITY.md](../SECURITY.md).
- **The GUI tasks run against a simulated desktop.** They measure the runtime's handling of
  GUI work -- grants per window, grounding, approval, verification -- not how well a model
  drives a real application.
- **macOS is Tier 2**: CI-green, never hand-verified, because the maintainer does not own a
  Mac. That includes the planner's seatbelt profile.

## Beta

**0.1.0b1.** The criteria set for leaving alpha, and the evidence for each:

| Criterion | Evidence |
|---|---|
| The broker is no longer in the planner's process | The planner runs in a confined child that cannot write files or start programs, verified on Windows by a test that has the child try both. See [SECURITY.md](../SECURITY.md#the-planner-runs-in-its-own-process) |
| A model scored over several runs, with the spread published | `qwen3:8b`: 15/18 and 14/18 over two full runs, with per-task variance, in [EVALUATION.md](../EVALUATION.md). A three-run score on the 22-task suite is being recorded with `--runs 3` |
| GUI tasks in the eval suite | Three, on every CI run, with the window grant tested as a REFUSE task |
| The container backend run end to end | 6/6 against a real engine, and the first run found and fixed a gap in what it records |
| Context management that measures tokens | Every request fitted to the window; a 60-step run in a 6k window never sends one over budget; overflow errors recovered once, then reported |

Beta means the evidence exists, not that the work is done. What beta does *not* claim:

- **No frontier model has been scored.** The criterion was met with a local model, because
  this checkout has no API key. The hosted providers and `--runs` make it one command.
- **A bug in the broker is still a full bypass.** Isolation moved the planner out; it did
  not put a second check behind the broker.
- **The isolated planner can still read your files and reach the network.**
- GUI control of real applications is Windows-only, and synthetic input *delivery* is
  exercised by hand rather than by a test.

## Alpha

**0.1.0a1**, 22 Sep 2026. The criteria for leaving pre-alpha were: a model completes a
real task end to end (`qwen3:8b` converted a PDF to a Word document unaided), long-horizon
tasks in the suite (three), and a model scored on the current suite and published (15/18
and 14/18 over two runs). See [EVALUATION.md](../EVALUATION.md).

## Next

1. Score a frontier model and a hosted open model on the suite, three runs each, and
   publish both with the spread.
2. A network allow-list for the planner process: its model's host and nothing else.
3. An AppContainer for the Windows sandbox and planner, so reads are confined too.
4. GUI on macOS and Linux behind the `CuaDriver` seam.
5. Anchor the audit chain's head somewhere the agent cannot write.
