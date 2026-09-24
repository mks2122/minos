# Models & Hardware

**Target machine:** Intel Core i7-14650HX (14C/20T) · **16 GB DDR5-5600** · **8 GB VRAM** (dGPU + Intel iGPU) · 954 GB SSD, **~149 GB free**
**Date:** 16 Sep 2026

---

## The short answer

| Role | Model | Where | Footprint |
|---|---|---|---|
| **Planner** | `claude-opus-5` | ☁️ API | — |
| **GUI grounding (L3)** | UI-TARS-1.5-7B, Q4_K_M | 🖥️ local GPU | ~4.7 GB VRAM |
| **Memory embeddings** | nomic-embed-text-v1.5 | 🖥️ local | ~0.3 GB |
| **Skill replay** | *none* | 🖥️ local | **0 — no model call** |
| **Offline degraded planner** | Qwen3-8B, Q4_K_M | 🖥️ local | ~5 GB VRAM |

**It does run locally — just not the planning.** On 8 GB of VRAM, "local" means grounding, embeddings and replay. That is not a compromise forced by weak hardware; it is the same split that would apply on a 24 GB card, because the smallest open-weight model that plans desktop work competently is ~235B-class.

---

## What your hardware actually supports

### VRAM: 8 GB is the ceiling for anything resident

| Model size | Q4_K_M weights | Fits in 8 GB with KV cache? |
|---|---|---|
| 3B | ~2.0 GB | ✅ comfortably |
| **7–8B** | **~4.5–5.0 GB** | ✅ **the sweet spot** |
| 14B | ~8.5 GB | ⚠️ spills, degrades hard |
| 32B | ~19 GB | ❌ CPU-offload only, unusable speeds |

⚠️ **You cannot hold the grounding VLM and a local planner in VRAM at the same time.** Two 5 GB models don't fit in 8 GB. If you ever run both, they swap — which costs seconds per switch. Design for one resident model.

### System RAM: 16 GB is the real constraint

Budget it before you hit it:

| Consumer | Allocation |
|---|---|
| Windows host | ~5 GB |
| WSL2 (cap it in `.wslconfig`) | ~9 GB |
| — Python + broker + SQLite | ~1.5 GB |
| — LibreOffice under test | ~1 GB |
| — overlayfs / snapshot working set | ~1 GB |
| — headroom | the rest |

`%UserProfile%\.wslconfig`:

```ini
[wsl2]
memory=9GB
processors=12
swap=8GB
```

Leave 2 threads and ~5 GB for Windows or the host stutters while the agent runs.

### Storage: ⚠️ 149 GB free is tight

Models ~10 GB, eval environments, snapshots, Docker/E2B images. **Filesystem checkpointing needs headroom by definition** — every `REVERSIBLE` action writes a diff. Free up space before M2, or checkpointing will fail in the least convenient way. 250 GB+ free is a comfortable working figure.

---

## Planner: `claude-opus-5`

Current model line and pricing (verified 16 Sep 2026):

| Model | ID | Context | Input $/1M | Output $/1M |
|---|---|---|---|---|
| **Claude Opus 5** | `claude-opus-5` | 1M | $5.00 | $25.00 |
| Claude Sonnet 5 | `claude-sonnet-5` | 1M | $2.00 | $10.00 |
| Claude Haiku 4.5 | `claude-haiku-4-5` | 200K | $1.00 | $5.00 |

**Default to `claude-opus-5`.** Long-horizon agentic planning is exactly the workload where the capability gap shows up, and this project's whole premise is that planning is the hard part.

### Rough cost per task

A multi-step GUI task runs 50k–200k tokens (screenshots dominate the input).

| Model | ~100k in / 5k out | With prompt caching |
|---|---|---|
| Opus 5 | ~$0.63 | ~$0.20–0.30 |
| Sonnet 5 | ~$0.25 | ~$0.10 |
| Haiku 4.5 | ~$0.13 | ~$0.05 |

**For a 50-task eval run:** Opus 5 ≈ $10–30, Sonnet 5 ≈ $5–12. If you're iterating several times a day that adds up on a side project. A defensible split: **Sonnet 5 while iterating, Opus 5 for the numbers you publish** — but say which model produced which number in `EVALUATION.md`, because a benchmark that doesn't name its model is noise.

**And note what the skill cache does to this line:** a replayed skill makes **zero** model calls. On your budget, that isn't an optimization — it's the difference between an eval suite you can afford to run daily and one you can't.

### API settings for the planner loop

```python
response = client.messages.create(
    model="claude-opus-5",
    max_tokens=16000,
    thinking={"type": "adaptive"},           # on by default on Opus 5
    output_config={"effort": "high"},        # xhigh for hard agentic runs
    tools=[...],                             # ActionRequest shapes, strict=True
    messages=[...],
)
```

- **Adaptive thinking**, not `budget_tokens` — that parameter is removed on Opus 5 and returns a 400.
- **`effort`** is the main cost/quality dial: `low` for simple steps, `high` as the default, `xhigh` for long-horizon runs. Tune per route, not globally.
- **`strict: true`** on every tool definition so `ActionRequest` params validate exactly — cheap insurance on the untrusted-planner boundary.
- **Prompt caching matters enormously here.** Your system prompt, tool definitions and capability scopes are stable across every step of a task. Order them first, put volatile content (screenshots, step results) after the last cache breakpoint. Verify with `usage.cache_read_input_tokens` — if it's zero across steps, something is silently invalidating the prefix.
- **Task budgets** (beta `task-budgets-2026-03-13`, supported on Opus 5) give the loop a token ceiling it can pace itself against. Good fit for a runtime that already thinks in bounded authority.

### ⚠️ Do NOT use the SDK Tool Runner

The Anthropic SDK's `client.beta.messages.tool_runner` automatically executes your tool functions and loops. **That is precisely what invariant I1 forbids** — it hands execution authority back to the model's turn.

**Write the manual loop.** A `tool_use` block from the model becomes an `ActionRequest`; the broker decides; the broker executes; the result comes back as a `tool_result`. The planner never touches a filesystem handle.

This isn't a stylistic preference — using the Tool Runner would quietly dissolve the architecture's central claim.

---

## Local: grounding, embeddings, replay

### UI-TARS-1.5-7B (L3 grounding)

The right local job. Grounding — *which pixel is the button* — is close to solved at 7B: open-source models hit ~75% on ScreenSpot-Pro. It's planning that needs 200B+.

- Q4_K_M ≈ 4.7 GB, fits 8 GB VRAM with room for KV cache
- Serve via llama.cpp or Ollama with CUDA through WSL2
- **Not needed until M8** — L3 GUI fallback is the last tier in the plan, so don't download it yet

### nomic-embed-text-v1.5 (memory index)

~270 MB, CPU-fine, good enough for the file/window index. Alternative: `bge-small-en-v1.5`.

### Qwen3-8B (offline degraded planner)

For demos without network, or as the honest "here's what local-only costs you" comparison in your eval. **Label it as degraded, and publish the gap** — that comparison is itself a useful contribution, since nobody has measured it properly on desktop tasks.

### What will not work, and why to stop wanting it

| Idea | Why not |
|---|---|
| 32B planner locally | ~19 GB at Q4. CPU offload ≈ 2–4 tok/s. A 500-step task would take hours |
| Dense 70B via layer streaming | 0.5–2 tok/s measured. Bandwidth-bound; PCIe Gen5 doesn't rescue it |
| MoE offload (gpt-oss-120b class) | ~12 tok/s on a *24 GB* card with 128 GB RAM. You have 8 GB and 16 GB |
| Local-only end to end | Best open-weight desktop-agent model is 235B (66.7% OSWorld). What fits your GPU scores **5.9%** |

---

## ⚠️ Checkpointing: a copy-before-write journal, not filesystem snapshots

Earlier drafts proposed btrfs snapshots, then overlayfs. **Neither survives the cross-platform requirement** — and WSL2 doesn't even have btrfs (ext4 in a VHDX).

**Decision: copy-before-write journal.** The effect contract declares its targets before acting, so a checkpoint copies *those files*, not a volume. Pure `shutil` + `hashlib`, identical on all three platforms; `clonefile` (APFS), `FICLONE` (btrfs/XFS) and ReFS block cloning are optional fast paths. See [PORTABILITY.md](PORTABILITY.md).

---

## Development setup — native Windows, not WSL2

**WSL2 cannot host this project.** Not a configuration problem:

1. **It cannot see or drive Windows GUI applications.** WSLg renders *Linux* GUI apps. Your actual Excel, your actual browser, your actual windows are invisible to it.
2. **`inotify` does not fire for files under `/mnt/c`.** The 9P filesystem bridge doesn't propagate change events, so the computer-state memory index — watching your real files — silently never updates.
3. **Windows processes and windows aren't enumerable** from the WSL2 kernel.

A desktop agent has to run natively on the desktop it acts on. So: **develop natively on Windows**, which is also where you can dogfood it daily.

```powershell
winget install --id=astral-sh.uv -e
python -m venv .venv; .\.venv\Scripts\Activate.ps1
nvidia-smi                 # confirm the dGPU is visible
```

Keep a WSL2 instance around **only** as a convenient Linux CI target for local debugging — never as the runtime.

**GPU note:** "Multiple GPUs installed" means Intel iGPU + dGPU. Pin inference to the discrete GPU explicitly (`CUDA_VISIBLE_DEVICES`, or Ollama's device selection) — Windows will otherwise sometimes route to the iGPU and you'll be debugging a phantom slowdown.

---

## Upgrade path, if it ever matters

| Upgrade | Unlocks |
|---|---|
| **32 GB RAM** | The single highest-value change. Comfortable WSL2 + LibreOffice + snapshots |
| 16 GB VRAM | Grounding VLM and a small local planner resident simultaneously |
| 64 GB + 24 GB VRAM | 32B-class local planning — still well below frontier |
| 128 GB unified memory | MoE offload at ~30 tok/s |

**Don't buy anything yet.** The plan through M5 needs none of it, and by M5 you'll have measured numbers telling you what actually binds.

---

## Related

[PLAN.md](PLAN.md) · [ARCHITECTURE.md](../ARCHITECTURE.md) · [LOCAL.md](LOCAL.md)
