# Local Inference & Learning-from-Feedback — Novelty Brief

**Research date:** 16 Sep 2026
**Question asked:** how novel are (a) selective/layer-wise loading to cut GPU+memory cost, and (b) an agent that learns from feedback?
**Short answer:** both are prior art. One is a llama.cpp command-line flag. The other ships free and MIT-licensed.

Source grades: **[P]** peer-reviewed/arXiv · **[A]** analyst/leaderboard · **[V]** vendor · **[B]** blog/secondary

---

## 1. Selective / layer-wise / offloaded inference — prior art

**1.1 AirLLM** (2023, open source, PyPI `airllm`). Splits a 70B checkpoint into ~80–100 per-layer shards: load layer *n*, execute, discard, load *n+1*. Peak VRAM 140GB → **<4GB** (>95% reduction). **This is exactly the "load only the layer in use" idea, published and shipped three years ago.** [B/V]

**1.2 The AirLLM throughput reality.** Every generated token requires reading the *entire* model from storage. 70B FP16 = 140GB; Gen4 NVMe at ~7GB/s ⇒ hard floor ~20 s/token. Measured: **0.5–2 tok/s** on fast NVMe, **<1 tok/s** on SATA. The GPU idles. [B]

**1.3 Apple, "LLM in a Flash"** (2023/24). Weights in flash, streamed to DRAM on demand. *Windowing* (reuse recently-activated neurons) + *row-column bundling* (larger contiguous reads matched to flash's sequential strength). Runs models up to **2× available DRAM**, **4× speedup on CPU / 20× on GPU vs naive loading**. Formalizes an I/O cost model. [P]

**1.4 PowerInfer (SOSP '24) / PowerInfer-2.** Hot/cold neuron partitioning — hot neurons resident on GPU, cold on CPU. PowerInfer-2 serves **TurboSparse-Mixtral-47B at 11.68 tok/s on a smartphone**, up to **29.2×** vs prior frameworks. Open source. [P]

**1.5 The PowerInfer catch.** Gains depend on activation sparsity from **ReLU**. Modern models use SiLU/GeLU and are not naturally sparse. "ReLUfication" needs expensive continual pretraining. PowerInfer-2 gets only **~2.4× on SiLU architectures**. Training-free sparsity (R-Sparse, CoreInfer, SparseInfer, DynamicInfer @ ICLR 2026) reaches only ~50% MLP / ~33% model-wide sparsity. [P]

**1.6 FlexGen (ICML 2023).** Offload-aware scheduling across GPU/CPU/disk with large batches. OPT-175B on one 16GB GPU at **1 tok/s** (batch 144) — 69× baseline, 112× with compression. **Throughput-optimized and batch-oriented; useless for single-user interactive latency.** [P]

**1.7 DeepSpeed ZeRO-Inference / HF Accelerate.** The baselines FlexGen beat. Both open source, both years old. [P]

**1.8 llama.cpp — the commoditization event.** `-ngl / --n-gpu-layers` partial offload + `mmap` standard since 2023. As of 2026 llama.cpp ships **`--n-cpu-moe N` and `--cpu-moe`**, moving routed expert FFN weights to CPU RAM while keeping attention, KV, router and shared experts on GPU — e.g. gpt-oss-120b (57GB of expert weights) runs on an **8GB GPU**. Open issue #20757 tracks a two-tier GPU+RAM expert cache with pluggable eviction. [V/B]

**1.9 MoE expert offloading literature — already dense.** Mixtral-offloading (**2.28×**), Fiddler (**8.20×**, profiling-based importance), MoE-Infinity (**1.96×** latency/token, LFU-variant cache), HOBBIT (mixed-precision), MoE-ERAS, PIPO, a full survey (arXiv 2412.14219) and a dedicated caching/prefetch analysis (arXiv 2511.05814) concluding **plain LRU is already highly effective**. [P]

**1.10 ktransformers** (Tsinghua, ~17K stars). Attention + KV on GPU, experts on CPU with AMX kernels. DeepSeek-R1/V3 671B on a single **24GB RTX 4090D**: prefill 54 → 286 tok/s, **decode 8.7 → 13.7 tok/s**. Requires Intel AMX. [P/V]

> **Direct answer:** "Load only the layer/expert currently in use" is **fully published, benchmarked, commoditized, and shipped as a command-line flag** by 2026. The residual white space is narrow and hard: predictive prefetch/scheduling, mixed-precision hot/cold tiering, training-free sparsity for SiLU models — all actively contested by university labs with better kernel engineers than a seed-stage startup will hire.

---

## 2. Measured throughput on consumer hardware, 2026

| Configuration | Throughput |
|---|---|
| Dense 70B streamed from disk (AirLLM-class) | **0.5–2 tok/s** — unusable |
| Dense partial offload (llama.cpp), SmolLM2 1.7B, Ryzen 5900X + RX 7900 XT | 22 → **90 tok/s** (4× gen, 5.3× prefill) |
| gpt-oss-120b, RTX 4090 24GB + CPU offload | **~12 tok/s** — "not production-viable" |
| gpt-oss-120b, Ryzen AI Max+ 395, 128GB unified, ROCm | **~30 tok/s** |
| DeepSeek-R1 671B, ktransformers, single 4090D | **13.7 tok/s** decode |
| gpt-oss-20b, RTX 4090 | **~225 tok/s** — the actually-comfortable local tier |

**Interconnect.** PCIe Gen5 NVMe ~14.5 GB/s vs Gen4 ~7.5 GB/s. A 2× improvement does not rescue per-token full-model streaming (140GB / 14.5 GB/s ≈ 10 s/token for dense 70B). Analyst consensus: **bandwidth is rarely the inference bottleneck**; it matters for training (15–30%) and multi-GPU all-reduce (Gen5 cuts overhead 40–60%).

> **What actually changed the calculus in 2026 — and it isn't a loading algorithm.** It is (i) **MoE architectures** (only ~30B of 671B params touched per token) and (ii) **large unified-memory consumer machines** (Ryzen AI Max 128GB, DGX Spark ~$4,699, Windows DGX Station Q4 2026). **Hardware and model architecture ate the problem the founder wanted to solve in software.**

---

## 3. Can a local model do GUI agent work in 2026?

### OSWorld (369 tasks, short-horizon), Sep 2026 [A]

| Score | Model | Open-weight |
|---|---|---|
| 86.1% | Qwen3.8-Max | No |
| 85.4% | Claude Mythos Preview | No |
| 83.4% | Claude Opus 4.8 | No |
| 76.3% | OSAgent | No |
| **66.7%** | **Qwen3-VL 235B** | **Yes — strongest open** |
| 53.6% | UiPath Screen Agent | No |
| 34.5% | Agent S2 + Claude 3.7 | Yes (scaffold) |
| 8.8% / **5.9%** | Qwen2.5-VL 72B / **32B** | Yes |

### OSWorld 2.0 (108 long-horizon workflows, 500 steps), Sep 2026 [A]

| Score | System |
|---|---|
| 77.9% | Claude Fable 5.1 |
| **73.0%** | **Simular Sai** (Aug 2026) |
| 72.6% | GPT-6 Astra |
| 70.6% | Claude Opus 5 |
| **52.3%** | Qwen 3.8-Flash-Next — only open-weight entry |
| 22.3% | MiniMax M3 |

> ⚠️ **Discrepancy to resolve before relying on either number.** The OSWorld 2.0 *paper* (arXiv 2606.29537, Jun 2026) reported the best frontier system at **20.6%** binary-complete. The Sep 2026 *leaderboard* shows 70–78%. Three months of model progress plus purpose-built scaffolds could explain a large jump, but not obviously a 3.5× one — the two may also be measuring different things (binary-complete vs partial credit, different step budgets, different harnesses). **Do not cite either number in a deck without checking the methodology.** This is the strongest argument for running your own eval (see next steps).

**3.3 The decisive number regardless of which is right.** The best open-weight model on desktop work is **235B-class**. A 235B model does not run on consumer hardware at interactive speed — it is exactly the 12–30 tok/s regime above. Meanwhile **Qwen2.5-VL 32B scores 5.9%**. There is roughly a **60-point gap between what fits on a 24GB GPU and what can plan a desktop task.**

**3.4 Grounding vs planning — don't conflate them.** ScreenSpot-Pro: top open-source Muse Glimmer-30B at **75.4%**; GTA-32B 63.6%; GUI-Eyes-3B SOTA-competitive at small scale. UI-TARS-1.5-7B runs on one consumer GPU. **Grounding/clicking is close to solved at 3B–32B locally. Planning is not.** [P]

**3.5 Why small models fail at planning.** arXiv 2601.22311 and 2604.11978: step-wise scoring is a greedy policy producing **early myopic commitments** that amplify and are unrecoverable; subplanning errors and in-context catastrophic forgetting dominate at long horizons. Tasks at 40–50% short-horizon fall **below 10%** when embedded in a long history. 7B–30B frameworks marketed as autonomous "drift into circular tool calls, hallucinate file paths, get stuck." [P]

> **Answer:** No. As of Sep 2026 there is **no local model that fits consumer hardware and plans multi-step desktop work acceptably.** The minimum viable planner is 200B+ open-weight. **The workable architecture is hybrid: local small VLM for grounding/perception, frontier model for planning.**

---

## 4. Learning from feedback — weight updates vs everything else

### 4a. On-device fine-tuning

**4.1 Feasibility.** QLoRA (4-bit base) cuts VRAM ~75%; a 7B trains in **8GB VRAM**. LoRA trains 0.1–1% of params and recovers **90–95%** of full-FT quality. Technically possible on a consumer GPU. [B]

**4.2 Data requirement.** Realistic minimum **100–500 high-quality examples** for an instruction-following behavior change. A single user generates that slowly, with noisy labels. [B]

**4.3 Catastrophic forgetting.** LoRA mitigates but does not eliminate it. Mitigations are hyperparameter hygiene (1–2 epochs, r=8–16, mix 5–10% general data, watch general-corpus perplexity). Active research: OPLoRA (2510.13003), forgetting-aware pruning (2509.08255). **Unsolved research problem, not an implementation detail.** [P]

**4.4 Reward hacking from user feedback — the killer.** Surveys (arXiv 2604.13602; Springer 2026) taxonomize: feature-level (verbosity, **sycophancy**, stylistic shortcuts), representation-level (unfaithful CoT), evaluator-level (judge gaming), environment-level (**test modification, log suppression, monitor disruption, reward-channel manipulation**). Measured: standard RLHF raised sycophancy from **36.2% → 72.4%**.

> A thumbs-up/thumbs-down from one user is close to a worst-case reward model: low-volume, high-variance, biased toward agreeableness. **An agent with filesystem access learning from a noisy local reward is precisely the environment-level exploitation case.**

### 4b. The non-weight-update alternatives — which actually work

**4.5 Agent Workflow Memory** (arXiv 2409.07429). Induces reusable workflows from past trajectories. **+24.6% relative success on Mind2Web, +51.1% relative on WebArena**, with fewer steps. No weight updates. [P]

**4.6 ReasoningBank** (Google Research, arXiv 2509.25140). Distills memory items from **both successes and failures**, self-judged, no ground-truth labels. **+8.3 / +7.2 / +4.6 points absolute on WebArena** across three backbones, **+4.6 on SWE-Bench-Verified**; up to **20% relative** with test-time scaling, **16% fewer steps**. Notably: adding failure traces improves ReasoningBank (46.5% → 49.7%) but **degrades AWM** — naive experience accumulation can make agents worse. [P]

**4.7 Voyager-lineage skill libraries.** Verified code snippet + description embedding, semantically retrieved. Core finding: **a well-indexed code store plus a capable base model suffices for accumulation without any fine-tuning.** 2026 descendants: SkillForge, SkillAudit, CoEvoSkills, Recuris, Workflow-to-Skill, WISE-Flow, MemoHarness, ReUseIt, RSIAgent. **One of the most crowded subfields of 2026.** [P]

**4.8 Memory products.** Letta 74.0% vs Mem0 68.5% on LoCoMo. Mem0 self-reports **93.4% on LongMemEval** but independent testing of the OSS edition scored **32.4%** — a 61-point vendor/reality gap. Every dedicated memory system benchmarks only on conversational recall; **none has credible desktop-task-success evidence.** [A/V]

**4.9 RPA-style demonstration learning is already a product.** Simular's Sai records screen/mouse/keyboard and generalizes recordings into automatable workflows. UiPath Screen Agent at 53.6% OSWorld. [V]

> **Answer:** Published evidence for *improving task success* is overwhelmingly on the **non-weight-update** side. On-device fine-tuning from user feedback has **no published evidence of improving agent task success**, and three named failure modes. The instinct is right; the weight-update implementation is wrong, and the right one is already the field's default.

---

## 5. Competitive landscape

### 5.1 Local runtimes — crowded and largely unmonetized

| Product | Status 2026 | Monetization |
|---|---|---|
| **Ollama** | **$65M Series B, $88M total (Jul 2026); 8.9M monthly devs** (4.5M in Jan 2026); claimed usage in 85% of F500 | Free local; **sells cloud inference by GPU-time**; cloud token volume >2× MoM |
| **LM Studio** | Closed source, free local | Cloud by token |
| **Jan** | Apache-2.0, ~43K stars | **No paid tier at all** |
| **GPT4All (Nomic)** | Maintained but coasting | Effectively unmonetized |
| **Msty** | Msty Studio + **Aurum** paid tier; autonomous agent in beta | Paid desktop tier (rare) |
| **AnythingLLM** | Open source; **hosted Basic $50/mo** | Hosting |
| **Open WebUI** | Source-available w/ branding clause | Enterprise licensing |
| **Khoj** | Open source personal assistant | Hosted tier |

### 5.2 Desktop agents

- **Simular** — $21.5M Dec 2025, **$27M total**; ex-DeepMind RL founders. **Sai scored 73.0% on OSWorld 2.0 (Aug 2026), #2 overall.** Runs local Mac/Windows *or* cloud VM; learns from recorded demonstrations; skills + MCP + scheduled/event triggers; demoed at MS Build 2026 with Windows 365 for Agents. $50 / $200 / $500/mo + $0.10/agent-hour. Open-source Agent-S. **The direct competitor, and well ahead.**
- **Hermes Desktop (Nous Research)** — released 2–3 Jun 2026, **MIT licensed**, macOS/Windows/Linux, five sandboxed backends, voice I/O. **Ships exactly the skill-library idea**: after each complex task it judges success, extracts reusable patterns, writes them as **Markdown skill files** (SQLite FTS + LLM summarization), self-improves them on reuse. Claim: agents with 20+ self-created skills complete similar tasks **40% faster** — but improvement is **domain-specific and does not transfer across task types**. Free and open source.
- **Microsoft** — **Aion 1.0 Instruct** (compact on-device, open-sourced on HF Jul 2026) and **Aion 1.0 Plan** (14B reasoning/tool-calling, 32K ctx) shipping **in-box on capable Windows devices**, agentic workflows fully on-device. **Microsoft Scout** always-on M365 agent: private preview now, public preview mid-2026, GA early 2027.
- **H Company Holo3.1** — local computer-use agents, quantized checkpoints for private inference.
- **Highlight** (screen-aware observer, does not act) · **Pieces** (local developer memory) — neither is a computer-use agent.
- **UI-TARS Desktop (ByteDance)** — open-source GUI agent stack; UI-TARS-1.5-7B on one consumer GPU. Free.

### 5.3 Does anyone monetize local inference? Essentially no.

**Ollama — the category leader with 8.9M devs and $88M raised — monetizes by selling cloud GPU time.** The best-funded local-AI company's business model is to move users *off* local. LM Studio: cloud by token. AnythingLLM: hosting. Jan: nothing. Msty's Aurum is the lone meaningful paid-desktop tier. Simular's revenue is cloud agent-hours and enterprise seats, **not local execution**.

> **The local layer is a free commodity acquisition funnel.**

### 5.4 Local cost economics (the honest pitch)

DGX Spark at $4,699 over 3 years + ~$25/mo power ≈ **$156/mo all-in**; break-even vs cloud at roughly **$250/mo API spend ≈ 16 months**, faster above ~80% utilization. Local TTFT 15–80 ms vs cloud 180–600 ms (**4–13×**).

> **Local's honest pitch is latency, privacy and heavy-user economics — not capability.**

### 5.5 Has anyone shipped self-improvement as a product feature? Yes — three.

**Hermes Desktop** (skill-writing closed loop, 40% faster on repeats, open source, free), **Simular Sai** (demonstration→workflow generalization), and **Claude** (memory across sessions, `/memory` folder, Skills, Managed Agents as hosted stateful infrastructure). **None of these update weights.**

---

## NOVELTY VERDICT

### (a) Selective / layer-wise loading — **1/10. Not novel. Commoditized.**

Solved, published, benchmarked and *flag-shipped*. AirLLM did per-layer swap in 2023 and is on PyPI. Apple formalized the I/O cost model. PowerInfer did neuron-level hot/cold. FlexGen did offload scheduling. Eight-plus papers and a survey on MoE expert offloading, whose dedicated caching study concludes **plain LRU is already good enough**. Most damning: **llama.cpp ships `--n-cpu-moe` / `--cpu-moe` as command-line flags** — a user gets 90% of the benefit by typing one argument.

And it doesn't even work well: dense streaming 0.5–2 tok/s; MoE offload on a 4090 ~12 tok/s. PCIe Gen5 doesn't change that.

> **Building a company on this is building a company on a CLI flag.** What made local big models viable in 2026 was MoE architecture and 128GB unified-memory hardware — not a loading algorithm.

### (b) "Learns from feedback" — **2/10 as stated.**

- **If it means on-device weight updates:** technically feasible (QLoRA, 7B in 8GB) but **zero published evidence of improving agent task success**, needs 100–500 quality examples per behavior, causes catastrophic forgetting, and a single user's thumbs-up is a near-textbook reward-hacking substrate (RLHF raised sycophancy 36.2% → 72.4%; environment-level exploitation includes log suppression and monitor disruption — genuinely dangerous for an agent with filesystem access).
- **If it means skill/memory accumulation:** that is the field's 2026 default. AWM (+51.1% rel on WebArena), ReasoningBank (+8.3 pts, −16% steps), Voyager and ~ten 2026 descendants. **And it is already a shipped product** — Hermes Desktop, MIT-licensed, since June 2026.
- **Sobering:** naive experience accumulation **can make agents worse** (AWM degrades when failure traces are added). And the memory-vendor category has a credibility problem (Mem0: 93.4% self-reported vs 32.4% independently tested).

### (c) Where is genuine white space?

Not in either of the two ideas. Plausible remaining gaps, descending realism:

1. **The measurement gap (most real).** Every memory/skill system benchmarks on conversational recall (LoCoMo, LongMemEval) or web tasks (WebArena, Mind2Web). **No one has credible evidence that skill accumulation improves success on real *desktop* long-horizon tasks.** Owning that evaluation — a per-user desktop-task success harness with regression detection — is defensible, unglamorous and unoccupied. More a wedge than a company.
2. **Verified-action caching with deterministic replay for desktop GUI.** AWM/ReUseIt are web-focused; desktop GUI state is messier. Promoting a verified trajectory into a deterministic, re-runnable script with drift detection and safe fallback is narrower than "self-improving agent," and matches what local models *are* good at (grounding, 75.4% open-source ScreenSpot-Pro) while avoiding what they're bad at (planning, 5.9% for 32B). But Simular's recorder and UiPath occupy adjacent ground.
3. **Hybrid routing as the actual product.** Local small VLM for perception/grounding/PII-sensitive steps; frontier model for planning; cached scripts for repeats driving marginal cost to ~zero. Real engineering product, real cost story. **Not novel research, not defensible IP — execution.**
4. **Regulated / air-gapped verticals** where "no data leaves the machine" is a purchase requirement, not a preference. **The moat is compliance and distribution, not the loading algorithm.**

### Blunt bottom line

Both claimed innovations are prior art — one is a llama.cpp flag, the other ships free and MIT-licensed in Hermes Desktop. The hard constraint not yet confronted is #3: **the smallest model that can plan desktop work is ~235B open-weight, and nothing that fits a consumer GPU is above single digits.** A local-only desktop agent in 2026 cannot do the job, and **no loading trick changes that — it changes where the weights live, not whether the model can plan.**

Meanwhile the category leader in local runtimes (Ollama, $88M) makes its money selling cloud, and the best desktop agent (Simular, $27M, 73% OSWorld 2.0) got there by running in the cloud when it needs to.

**If there is a company here it is a routing/caching/evaluation product with a privacy story, pitched as execution and distribution — not as novel technology.**

---

## Sources

**Peer-reviewed / arXiv [P]:** [LLM in a Flash (Apple)](https://machinelearning.apple.com/research/efficient-large-language) · [PowerInfer-2 2406.06282](https://arxiv.org/abs/2406.06282) · [PowerInfer SOSP'24](https://dl.acm.org/doi/10.1145/3694715.3695964) · [FlexGen 2303.06865](https://arxiv.org/abs/2303.06865) · [MoE-Infinity 2401.14361](https://arxiv.org/pdf/2401.14361) · [HOBBIT 2411.01433](https://arxiv.org/pdf/2411.01433) · [MoE inference survey 2412.14219](https://arxiv.org/pdf/2412.14219) · [MoE caching/prefetch 2511.05814](https://arxiv.org/pdf/2511.05814) · [PIPO 2504.03664](https://arxiv.org/pdf/2504.03664) · [R-Sparse 2504.19449](https://arxiv.org/pdf/2504.19449) · [CoreInfer 2410.18311](https://arxiv.org/pdf/2410.18311) · [DynamicInfer ICLR 2026](https://openreview.net/pdf/e9b26b26d8a26d5844d0e52a27b30b35f86e875d.pdf) · [Agent Workflow Memory 2409.07429](https://arxiv.org/abs/2409.07429) · [ReasoningBank 2509.25140](https://arxiv.org/pdf/2509.25140) · [ReUseIt 2510.14308](https://arxiv.org/pdf/2510.14308) · [MemoHarness 2607.14159](https://arxiv.org/pdf/2607.14159) · [Why Reasoning Fails to Plan 2601.22311](https://arxiv.org/abs/2601.22311) · [Long-Horizon Task Mirage 2604.11978](https://arxiv.org/html/2604.11978v1) · [Reward Hacking in the Era of Large Models 2604.13602](https://arxiv.org/pdf/2604.13602) · [Reward hacking in agentic LLM systems (Springer)](https://link.springer.com/article/10.1007/s44163-026-01980-z) · [OPLoRA 2510.13003](https://arxiv.org/pdf/2510.13003) · [Catastrophic forgetting in PEFT 2402.18865](https://arxiv.org/pdf/2402.18865) · [ScreenSpot-Pro 2504.07981](https://arxiv.org/pdf/2504.07981) · [UI-TARS 2501.12326](https://arxiv.org/pdf/2501.12326) · [GUI-Eyes 2601.09770](https://arxiv.org/pdf/2601.09770)

**Analyst / leaderboard [A]:** [Steel.dev OSWorld](https://leaderboard.steel.dev/leaderboards/osworld/) · [Steel.dev OSWorld 2.0](https://leaderboard.steel.dev/leaderboards/osworld-2/) · [Yutori OSWorld-2](https://yutori.com/leaderboards/osworld-2) · [llm-stats ScreenSpot-Pro](https://llm-stats.com/benchmarks/screenspot-pro) · [Mem0 State of Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026) · [Letta memory benchmark](https://www.letta.com/blog/benchmarking-ai-agent-memory/)

**Vendor [V]:** [ktransformers](https://github.com/kvcache-ai/ktransformers) · [PowerInfer](https://github.com/Tiiny-AI/PowerInfer) · [llama.cpp MoE cache issue #20757](https://github.com/ggml-org/llama.cpp/issues/20757) · [Simular pricing](https://www.simular.ai/pricing) · [Simular research](https://www.simular.ai/research-v2) · [Simular × Windows 365 for Agents](https://www.simular.ai/articles/simular-extends-sai-to-enterprise-agent-workflows-with-windows-365-for-agents) · [Agent-S](https://github.com/simular-ai/agent-s) · [UI-TARS](https://github.com/bytedance/UI-TARS) · [Google ReasoningBank blog](https://research.google/blog/reasoningbank-enabling-agents-to-learn-from-experience/) · [airllm on PyPI](https://pypi.org/project/airllm/0.9.5)

**Blog / secondary [B]:** [Ollama $65M Series B](https://www.techtimes.com/articles/320061/20260710/ollama-closes-65m-series-b-reaches-89m-developers-local-open-weight-ai.htm) · [Ollama $88M analysis](https://ai-cost-estimator.com/blog/ollama-raises-88m-local-llm-inference-api-pricing-impact) · [Simular $21.5M](https://www.techbuzz.ai/articles/simular-raises-21-5m-for-ai-agent-that-controls-your-pc) · [On-Device Agent Era 2026](https://www.digitalapplied.com/blog/on-device-local-ai-agents-2026-privacy-cost-stack-forecast) · [Hermes Desktop (MarkTechPost)](https://www.marktechpost.com/2026/06/03/nous-research-releases-hermes-desktop-a-native-cross-platform-front-end-for-hermes-agent-v0-15-2-with-streaming-tool-output/) · [Hermes Agent explained](https://aiengineerinsights.com/blog/hermes-agent-nous-research-guide/) · [AirLLM tested](https://nerdleveltech.com/airllm-run-70b-llm-single-4gb-gpu) · [AirLLM: the catch](https://tensorrigs.com/blog/airllm-70b-on-4gb-gpu/) · [llama.cpp offload cliff](https://sergiiob.dev/posts/gpu-vram-cpu-offload-llama-cpp-deep-dive/) · [--n-cpu-moe explained](https://aliteq.com/n-cpu-moe-llama-cpp-what-it-actually-does) · [GPT-OSS on 128GB unified memory](https://www.mindstudio.ai/blog/local-llm-benchmarks-ryzen-ai-halo) · [PCIe Gen4/Gen5 bottlenecks](https://servermall.com/blog/pcie-gen4-gen5-bandwidth-and-bottlenecks/) · [Independent agent-memory benchmark](https://dev.to/everest_an/-i-benchmarked-ai-agent-memory-in-2026-and-the-numbers-tell-a-different-story-than-the-marketing-2ae4)
