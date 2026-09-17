# Running fully locally

**Yes, this runs entirely on your machine with a quantised model, and it is not slow.**

```bash
uv run writ doctor      # what's missing, sized to your GPU
```

---

## Three commands

```bash
# 1. install a runner
winget install Ollama.Ollama      # or https://ollama.com

# 2. pull a model that fits your card
ollama pull qwen3:8b

# 3. run
uv run python main.py             # auto-detects the server and uses it
```

`writ doctor` should now say **FULLY OFFLINE: YES**.

To make that a guarantee rather than a preference, turn on offline mode — Settings (8) → option 9, or `--offline` on the CLI. It then **fails rather than calling a remote model**, so there is no path where something quietly leaves the machine.

---

## Why an 8B is enough here

This is the part that surprised me, and it is worth understanding before picking a model.

Published numbers make local models look hopeless at desktop work — a 32B scoring single digits on OSWorld. **Those numbers are about GUI agents**: look at a screenshot, find a control, click the right pixel, repeat fifty times. Pixel-driving is genuinely hard and small models are genuinely bad at it.

That is not the job in this runtime. `writ` prefers typed tools, so the planner's task is to choose one of about a dozen functions and fill in its arguments:

```
sheet.set_cell(path="data/sales_2025.csv", cell="B4", value="48200")
```

That is **tool calling**. A 7–8B model does it competently. The tier hierarchy is what converts the hard problem into the easy one — which makes a local planner the *intended* configuration for L1/L2 work, not a downgrade.

---

## Picking a model

`writ doctor` reads your actual VRAM and lists what fits. Sizes are Q4_K_M weights; leave ~1 GB for the KV cache.

| Model | Weights | Needs | Notes |
|---|---|---|---|
| `qwen3:4b` | ~2.6 GB | 6 GB | short plans only |
| `qwen2.5:7b-instruct` | ~4.7 GB | 8 GB | solid, widely available |
| `llama3.1:8b` | ~4.9 GB | 8 GB | good tool calling |
| **`qwen3:8b`** | **~5.0 GB** | **8 GB** | **the default** |
| `qwen3:14b` | ~8.5 GB | 12 GB | better at longer plans |
| `qwen3:32b` | ~19 GB | 24 GB | strong |

**On an 8 GB card** — an RTX 4060/5060 laptop, say — `qwen3:8b` sits entirely in VRAM and runs at roughly 30–50 tok/s. A tool call is a few hundred tokens, so each step takes about a second. Fully local *and* fast.

---

## If you want a bigger model and don't mind slow

llama.cpp keeps most weights in system RAM and still serves an OpenAI-compatible endpoint, which this runtime already speaks:

```bash
llama-server -m qwen3-32b-q4_k_m.gguf --n-gpu-layers 20 --port 11434
uv run writ run "..." --planner local --base-url http://localhost:11434/v1 --offline
```

Expect single-digit tokens/sec. **That is usable here**, because a tool call is short — a five-step task is maybe 30 seconds of generation. This is exactly where the typed-tier architecture pays off: a GUI agent needing hundreds of screenshot-sized steps would be unusable at the same speed.

For a Mixture-of-Experts model, `--n-cpu-moe` keeps expert weights in RAM and attention on the GPU, which is the best ratio available on a small card.

---

## ⚠️ About AirLLM

**Don't.** AirLLM exists to run a 70B on 4 GB by streaming each layer from disk on every token. Measured throughput is **0.5–2 tok/s**, which turns a five-step task into an hour or more.

It solves a problem you don't have. You are not trying to run a 70B — you are trying to pick from twelve functions, and an 8B that fits in VRAM does that better and roughly **100× faster**. The same reasoning rules out dense-model layer streaming generally: for a dense transformer *every* layer runs on *every* token, so "load only what's in use" just converts a memory cost into a bandwidth cost you pay again each token.

If you want the layer-offload idea to actually pay, the axis is **sparsity, not layers** — an MoE model activates ~2 of N experts per token, so offloading experts is a real 10× win where offloading layers is not. `llama.cpp --n-cpu-moe` does this.

---

## Measure it, don't trust this page

```bash
uv run writ eval --planner local --model qwen3:8b
```

Fifteen tasks, binary pass/fail, including six containment tasks. You will get your own number for your own hardware. Compare against the reference planner (`uv run writ eval`) which proves the tasks are solvable at all.

Expect the local planner to do worse than the reference on multi-step tasks and fine on short ones. **Where it fails, the failure is safe**: scopes bound what a confused planner can reach, every effect is verified against the system of record, and anything that doesn't match gets reversed.

---

## The cheapest path isn't a smaller model

Once a task succeeds and is promoted to a verified skill, replay makes **zero model calls** — local or remote:

```bash
uv run writ skills            # what has been promoted
```

A cached skill is faster and more reliable than any planner, because it isn't planning. The way to run this cheaply is not a smaller model doing the same thinking badly; it is not doing the thinking twice.

---

## Related

[../README.md](../README.md) · [MODELS.md](MODELS.md) · [../EVALUATION.md](../EVALUATION.md)
