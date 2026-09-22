# Running fully locally

**Yes, this runs entirely on your machine with a quantised model, and it is not slow.**

```bash
uv run minos doctor      # what's missing, sized to your GPU
```

---

## Three commands

Verified on Windows 11 with an RTX 5060 Laptop (8 GB VRAM):

```bash
# 1. install a runner -- the installer also starts the service
winget install --id Ollama.Ollama --accept-package-agreements --accept-source-agreements

# 2. pull a model that fits your card (~5 GB)
ollama pull qwen3:8b

# 3. run
uv run python main.py             # auto-detects the server and uses it
```

`minos doctor` should now say **FULLY OFFLINE: YES**.

> **If step 2 says `ollama` is not recognised**, the installer put it in
> `%LOCALAPPDATA%\Programs\Ollama` and your open terminal has not picked that
> up. Open a new terminal, or call it by full path. `minos doctor` looks in that
> location itself, so it will still report the runner correctly.

To make that a guarantee rather than a preference, turn on offline mode — Settings (8) → option 9, or `--offline` on the CLI. It then **fails rather than calling a remote model**, so there is no path where something quietly leaves the machine.

---

## Context length: the setting that decides whether tool calling works

The tool schemas and system prompt are **~2600 tokens before your task starts**.
Ollama serves **4096 by default**, whatever the model supports. That leaves
roughly 1500 tokens for the goal, every tool result and every reply -- and when
it runs out, the server truncates from the *oldest* message, which is the system
prompt and the tool schemas. The model stops being able to call tools, and it
looks exactly like the model being bad at its job.

**Ollama's OpenAI-compatible endpoint ignores per-request context settings**, so
asking for more in the request does nothing. Set it on the server:

```bash
OLLAMA_CONTEXT_LENGTH=6144 ollama serve
```

Measured on an 8 GB laptop GPU with `qwen3:8b`:

| Context | Footprint | Placement |
|---|---|---|
| 4096 | 5.6 GB | 100% GPU -- but only ~1500 tokens to work in |
| **6144** | **5.9 GB** | **100% GPU. The sweet spot on 8 GB** |
| 8192 | 6.6 GB | 16% spills to CPU |
| 16384 | 7.8 GB | 20% spills to CPU, and noticeably slower |

`minos doctor` reads what your server is actually serving and warns when it
disagrees with `MINOS_CONTEXT_TOKENS`, because this is otherwise invisible until
your tasks quietly start failing.

## Why an 8B is enough here

This is the part that surprised me, and it is worth understanding before picking a model.

Published numbers make local models look hopeless at desktop work — a 32B scoring single digits on OSWorld. **Those numbers are about GUI agents**: look at a screenshot, find a control, click the right pixel, repeat fifty times. Pixel-driving is genuinely hard and small models are genuinely bad at it.

That is not the job in this runtime. `minos` prefers typed tools, so the planner's task is to choose one of about a dozen functions and fill in its arguments:

```
sheet.set_cell(path="data/sales_2025.csv", cell="B4", value="48200")
```

That is **tool calling**. A 7–8B model does it competently. The tier hierarchy is what converts the hard problem into the easy one — which makes a local planner the *intended* configuration for L1/L2 work, not a downgrade.

---

## Picking a model

`minos doctor` reads your actual VRAM and lists what fits. Sizes are Q4_K_M weights; leave ~1 GB for the KV cache.

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
uv run minos run "..." --planner local --base-url http://localhost:11434/v1 --offline
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
uv run minos eval --planner local --model qwen3:8b
```

Fifteen tasks, binary pass/fail, including six containment tasks. You will get your own number for your own hardware. Compare against the reference planner (`uv run minos eval`) which proves the tasks are solvable at all.

Expect the local planner to do worse than the reference on multi-step tasks and fine on short ones. **Where it fails, the failure is safe**: scopes bound what a confused planner can reach, every effect is verified against the system of record, and anything that doesn't match gets reversed.

---

## The cheapest path isn't a smaller model

Once a task succeeds and is promoted to a verified skill, replay makes **zero model calls** — local or remote:

```bash
uv run minos skills            # what has been promoted
```

A cached skill is faster and more reliable than any planner, because it isn't planning. The way to run this cheaply is not a smaller model doing the same thinking badly; it is not doing the thinking twice.

---

## Related

[../README.md](../README.md) · [MODELS.md](MODELS.md) · [../EVALUATION.md](../EVALUATION.md)
