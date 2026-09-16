# Timing & Feasibility Brief — Agentic Desktop Agent

**Research date:** 16 Sep 2026 · 14 web searches + 3 direct page fetches.

**Source-quality caveat, read first:** the strongest numbers below come from arXiv papers, Gartner, Microsoft Learn and StatCounter. A meaningful minority of 2026 search hits are SEO/aggregator blogs whose figures could not be independently corroborated; those are flagged `[low-confidence]`. **The verdict does not depend on any of them.**

---

## 1. Computer-use / GUI agent benchmarks — the headline is a trap

**1.1** **OSWorld (369 tasks, short-horizon) is effectively saturated past the human baseline.** Human baseline 72.36%. Leaderboard as of 4 Sep 2026: best systems in the **83–86%** band, up from ~12% in Apr 2024.

**1.2** **OSWorld 2.0 is the number that matters, and it is brutal.** arXiv 2606.29537 (submitted 28 Jun 2026): 108 *long-horizon* computer-use workflows, median task takes a human **~1.6 hours**. Best frontier system, with maximum thinking and a **500-step budget**, completes **20.6%** binary-complete (54.8% partial credit).

> **85% on 1–5 minute tasks. ~21% on the kind of multi-hour desktop work a human actually delegates.**

**1.3** **Horizon-length collapse is the governing law.** Frontier models are near-100% on tasks a skilled human does in <~4 minutes and **<10% on tasks taking humans >~4 hours** (arXiv 2604.11978, 2603.29231).

Arithmetic founders should tattoo on their wall — at 98% per-step reliability:

| Steps | Success |
|---|---|
| 5 | ~90% |
| 20 | ~67% |
| 50 | ~36% |

**1.4** **Grounding is solved-ish; sequencing is not.** ScreenSpot-Pro (1,581 instructions, 23 apps, 3 OSes): best frontier model **0.879**. Open-weight UI-TARS-72B-DPO and InternVL3-72B hit ~74.3% / 72.2%. **Clicking the right pixel is largely solved. Knowing what to click 40 steps in is not.**

**1.5** **WebArena (812 long-horizon browser tasks):** best ~68.7%, next 65.8%, 64.5% (May 2026). Still ~1 in 3 tasks failed in a *sandboxed, self-hosted, deterministic* web environment. Real desktops are harder.

**1.6** **WindowsAgentArena:** no credible current SOTA figure surfaced in 8 searches. Treat as stale/abandoned relative to OSWorld 2.0.

**1.7** **Reliability research explicitly says no to unsupervised operation.** arXiv 2604.17849: agents show execution stochasticity and behavioral variability requiring repeated evaluation; single-run benchmark scores **overstate dependability**. Five recurring long-horizon failure modes: information grounding/tracking, perception–action timing, domain workflow knowledge, verification/reflection, and **state drift** — plus limited ability to detect or roll back a wrong intermediate state, so one early mistake poisons the whole trajectory.

> **Verdict on §1: reliability is not good enough for unsupervised desktop work in 2026. It is not close. The gap is ~4x on the tasks that would justify the product.**

---

## 2. Cost and latency per task

**2.1** A multi-step GUI task burns **50,000–200,000+ tokens** (every step = screenshot image input + reasoning + action), costing **a few cents to >$1 per workflow**. Anthropic 2026 list pricing: Opus 4.8 $5/$25 per M in/out, Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5.

**2.2** Coding-agent analogue: **$0.03–$2.60 per task** across Aider / Claude Code / OpenHands; Anthropic's own enterprise figure ~**$13 per developer per active day**.

**2.3** Token prices fell ~4x in a year (~$10 → ~$2.50 avg per M tokens) `[low-confidence]`, **but total agent bills rose** because step counts and context grew faster than unit price fell.

> **Unit economics improve; task economics do not.**

**2.4** **Latency is structurally bad.** Each step = screenshot → multimodal reasoning → action: seconds per step, dozens of steps per workflow. A task a human does in 2 minutes routinely takes an agent longer in wall-clock *and* costs real money.

**2.5** **Implication:** long-horizon desktop tasks at 500 steps × screenshot-heavy context = single-digit dollars per attempt, at ~21% success. **Expected cost per *successful* long task is ~$5–25.** Nobody pays that for "organize my files."

---

## 3. Local / on-device — hardware is arriving, capability is not

**3.1** **AI PC shipments:** IDC expects ~**half of 2026 PC shipments** (~130M of ~260M units) to be NPU-equipped. Counterpoint: "AI Advanced PCs" at **~59% of global shipments in 2026**, up from ~39% in 2025. 30+ laptop models from 8 OEMs shipping new AI silicon in Fall 2026.

**3.2** **What actually runs locally:** an 8B VLM on 12–16GB VRAM handles OCR/QA; **~32B is the entry point for genuine GUI-agent behavior and needs ~24GB VRAM**. Small 3B-class GUI models match 7B-scale *grounding* baselines. The strong open GUI models (UI-TARS-72B, InternVL3-72B) do **not** run on a normal laptop.

**3.3** **The honest read:** NPUs in AI PCs are sized for small models and background inference, not a 24GB+ VLM doing 500-step reasoning loops. In 2026 a locally-run agent can do *grounding* (where is the button) but not *planning* (what should I do for the next 40 minutes).

> **A local-first agentic product in 2026 either ships a weak agent or is a cloud client wearing a local-privacy costume. Pick your dishonesty.** (See `04-local-inference-and-learning.md` for the MoE escape hatch.)

---

## 4. MCP is eating the GUI-control thesis

**4.1** **Adoption is real and fast.** MCP SDK downloads **~97M/month by Mar 2026**, from ~100K at launch `[low-confidence figure, widely repeated]`. **41% of surveyed software orgs in limited or broad production with MCP servers.** MCP is now a multi-company open standard **under the Linux Foundation**, with AWS, Cloudflare and Google publishing production commitments. Enterprise-Managed Authorization extension promoted to **stable** in 2026 (OAuth, RBAC, audit logging) — the Fortune-500 unblocker.

**4.2** **Architectural consensus in 2026: screen control is a last resort.** "Use APIs when they exist, parse source documents instead of screenshots"; screen scraping "used only when no direct data access exists... an inelegant last resort for legacy systems without modern APIs." WebMCP and Chrome DevTools-for-agents push the web toward *declaring* tools rather than being clicked.

**4.3** **The most under-appreciated risk in the idea.** Every month, more valuable surface area (SaaS, browsers, IDEs, OS shells) exposes a typed, auditable, cheap tool interface. **Virtual keyboard/mouse control is a depreciating asset:** expensive, slow, brittle — and each new MCP server removes one more justification for it. Betting on pixel control in 2026 is betting on the shrinking half of the market.

**4.4** **Counterpoint worth holding:** Skills + MCP + tool-calling are complementary and production agents use all three; there will always be a legacy long tail (desktop apps with no API). **But a long tail is a feature, not an operating system.** *(Note: the long tail is also where the moat lives — see `06-moat-analysis.md`.)*

---

## 5. Safety, security, regulation — the part that can kill the company outright

**5.1** **Microsoft has already shipped your product's shape, with a warning label.** Copilot Actions + Windows Agent Workspace (Build 2026) reframe Windows as an agentic OS. Microsoft's *own* documentation warns of **cross-prompt injection (XPIA)** — malicious instructions planted in documents or UI elements overriding agent instructions, causing data exfiltration or malware installation. Microsoft explicitly conceded "novel security risks" and that "new and unexpected risks are possible."

**5.2** **Security community reaction was hostile.** Experts: Copilot agents' file/app access "greatly expands not only the scope of data that can be exfiltrated, but also the surface for an attacker to introduce an indirect prompt injection," with Microsoft offering "very little meaningful recommendations for customers." Mitigations are the obvious ones you'd also need: opt-in toggles, **separate agent accounts**, scoped folder access, auditable logs.

**5.3** **Prompt injection is now RCE-class, not output-class.** Microsoft disclosed prompt-injection-driven **RCE in AI agents** (May 2026) — injection manipulating tool parameters to execute arbitrary binaries. Cloud Security Alliance (6 Apr 2026) documented "Promptware: prompt injection as C2" — Check Point Research (Feb 2026) showed mainstream assistants usable as **C2 relays with no API keys, no accounts, no attacker infrastructure**. Confirmed findings against Slack AI, M365 Copilot, Cursor, GitHub MCP, Copilot Studio (data exfiltrated **after** the patch), Agentforce. Sandbox escapes are real.

**5.4** **Academic defenses exist but are immature:** VPI-Bench (visual prompt injection for computer-use agents, arXiv 2506.02456), AgentArmor (program analysis on runtime traces, 2508.01249), ceLLMate (browser agent sandboxing, 2512.12594), "The Blind Spot of Agent Safety" (2604.10577) showing *benign* user instructions expose critical CUA vulnerabilities.

> **There is no known robust defense against indirect prompt injection for an agent with desktop-wide access.**

**5.5** **Persistent memory of the user's work is the Recall landmine, and it has already detonated once.** Recall announced May 2024 → immediate backlash → delayed → opt-in on Copilot+ PCs Apr 2025 → **one year later still "raises security red flags"**; researchers cite new vulnerabilities post-redesign; Microsoft placed Recall **under review** while scaling back AI in Windows 11. **Your "persistent memory of the user's work" is Recall with a worse threat model, because an agent can *act* on the memory and the memory is itself an injection surface.**

**5.6** **Regulatory:** EU AI Act Art. 50 transparency duties and **AI Office enforcement powers over GPAI took effect 2 Aug 2026** — not delayed by the Omnibus — with fines to **3% of global turnover or €15M**. High-risk obligations deferred: Annex III standalone to **2 Dec 2027**, Annex I embedded to **2 Aug 2028**. Read: ~15-month grace on high-risk classification, but transparency/disclosure obligations bind *now*, and an autonomous desktop agent touching hiring/credit/education workflows lands in Annex III in Dec 2027. **Design for it from day one or eat a rewrite.**

---

## 6. Enterprise adoption — demand is loud, deployment is not

**6.1** **Gartner: >40% of agentic AI projects will be canceled by end-2027**, on cost, unclear value, or inadequate risk controls; most current projects are hype-driven PoCs, often misapplied.

**6.2** **Gartner 2026 Hype Cycle for Agentic AI places the category at the Peak of Inflated Expectations**, with only **17% of organizations having deployed AI agents** vs **>60% expecting to within two years.** That gap is the definition of a hype peak.

**6.3** **MIT (Aug 2025, NANDA "GenAI Divide"): 95% of enterprise GenAI pilots delivered no measurable P&L impact.** Widely criticized as methodologically thin — use as directional signal, not a hard number.

**6.4** Pilot-to-production conversion in 2026 clusters around **12–15%**; ~31% of orgs have at least one agentic system in production, ~14% scaled one org-wide; financial services highest (21%), healthcare lowest (8%). `[low-confidence aggregators — but the consistency of the 10–15% figure across independent weak sources is itself weak evidence]`

**6.5** **Where it does work, it works well** — Pinterest: ~66,000 monthly MCP tool invocations, 844 active users, ~7,000 hours/month saved. **Note the shape: MCP tool calls, not GUI control.**

---

## 7. Consumer appetite and OS switching

**7.1** **Linux desktop share, global:** ~2.76% (Jul 2022) → ~4.7% (2025) → **4.36% (Jun 2026)**. Roughly +70% over four years off a tiny base, with month-to-month noise >1pp. "Unknown" hit 21.45% of desktop pageviews in Jun 2026, so true Linux share is likely understated.

**7.2** **Regional spike:** North America **10.65% in Jul 2026**, up from 5.52% in Jun 2026. A near-doubling in one month is almost certainly a measurement artifact. **Do not build a plan on it.**

**7.3** **Windows 10 EOL (14 Oct 2025) produced a real but modest tailwind:** Zorin OS 18 download surges, "how to install Linux" search spikes (~5x, Feb 2026), Steam Linux crossing **3.05%** in Oct 2025. The best consumer-switching moment in Linux desktop history — and it moved global share ~1–2 percentage points.

**7.4** **Consumer trust in autonomous agents is the real blocker, and it is bad.**
- **8%** of consumers fully comfortable with AI assistants accessing their data without conditions
- **19%** trust AI assistants to follow rules for everyday purchasing (vs 55% for a human adviser)
- **69%** do not trust AI even when it follows rules they set
- **60% would stop using an AI agent after one mistake** (ACI Worldwide, UK, Jun 2026)
- **44%** fear unauthorized autonomous actions; 51% want AI features limited
- **9%** would let an agent act fully autonomously

**7.5** **Cross-reference 7.4 with 1.2.** Your agent fails ~79% of long-horizon tasks. 60% of users churn after one failure. **That is not a funnel; it is a cliff.**

---

# TIMING VERDICT

**2026 is too early for the product as originally specified, and simultaneously at risk of being too late for the wedge that actually works. Both, not one.**

**Too early, because:** the core capability claim — an agent that executes desktop tasks from conversation, unsupervised — fails on the only benchmark that resembles the use case (**20.6% on OSWorld 2.0**, Jul 2026). The 85% OSWorld figure everyone will cite in your deck measures 1–5 minute tasks and is actively misleading for this product. Layer on: no robust defense against indirect prompt injection for a desktop-scoped agent (§5.3–5.4); 60% single-strike consumer churn against a ~21% success rate (§7.4); $0.05–$1+ and 500 steps per long task (§2); and a local model story that doesn't close (§3.2).

**Too late, because:** Microsoft shipped the category framing at Build 2026 — Copilot Actions, Agent Workspace, separate agent accounts, scoped folders, audit logs. They have the OS, the distribution, the NPU hardware partnerships and the enterprise trust surface. And MCP (Linux Foundation, stable enterprise auth, 41% of orgs in production) is systematically removing the *reason* to control a GUI at all.

## Which of the three layers is binding

**1. Computer-use reliability — BINDING CONSTRAINT. Unambiguously.**
Everything else is engineering; this is a capability wall you do not control. 20.6% vs a human baseline you'd need to beat is a ~3.5x gap, driven by state drift and no-rollback architectures — **research problems, not integration problems.** Per-step reliability would need to go from ~98% to ~99.9% to make 50-step tasks feel dependable (36% → 95%). Nothing in the 2026 literature says that arrives in 12 months. **If you build the OS and the runtime perfectly today, the product still doesn't work, because this layer doesn't work.**

**2. Agent runtime (memory + permission/safety layer) — second, and the one you could legitimately own.**
Prompt injection against a desktop-scoped agent with persistent memory is unsolved with active exploitation (§5.3), and Microsoft's own answer is a warning label plus folder scoping. Persistent work memory carries Recall's full political liability (§5.5). But this is the only layer where a startup can build defensible IP in 2026: capability-scoped permissions, provenance-tracked memory, per-action sandboxing, deterministic rollback, human-in-loop checkpointing. AgentArmor / ceLLMate / VPI-Bench show the research direction exists and is **commercially unclaimed**. **Hard, but tractable, and valuable independent of whether the GUI agent ever gets good.**

**3. OS layer (Arch-Linux base) — NOT a constraint, and not a moat either.**
Linux desktop at ~4.4% is a 4–5% TAM ceiling for a consumer OS play, and Arch narrows that to a fraction of a fraction. But it is also the *cheapest* layer to build and the easiest to change later. It is neither the bottleneck nor the differentiator — **which means shipping a whole distribution is the highest-cost, lowest-return decision in the plan.** Building an OS to solve a problem whose bottleneck is a model capability is misallocating your only scarce resource.

## Blunt strategic read

The idea is three bets stacked, and it only pays if all three land. **Bet 3 (OS) is cheap and worthless. Bet 1 (reliability) is expensive and you don't control it. Bet 2 (safe agent runtime) is the only one where 2026 is genuinely the right time** — the threat is proven, defenses are immature, Microsoft has publicly admitted it can't solve it, and regulatory deadlines are creating forced demand.

**Recommendation implied by the evidence:** drop the OS. Build the permission/sandbox/memory-provenance runtime as a cross-platform layer, **MCP-first with GUI control as the explicit legacy fallback** (matching where architectural consensus already went, §4.2). That inverts the dependency — you sell into the agentic wave whether or not computer-use reliability improves, and if it does improve, you're the safety layer everyone needs rather than a competitor to Windows.

**If the founder insists on the OS:** the only defensible 2026 version is a *supervised* agentic workstation — agent proposes, human approves, every action reversible — sold to a vertical where desktop apps genuinely lack APIs. Not "unsupervised desktop tasks from conversation." The benchmarks say that product is a 2028 conversation at the earliest.

---

## Sources

- [OSWorld Leaderboard 2026 — Steel.dev](https://leaderboard.steel.dev/leaderboards/osworld/)
- [OSWorld 2.0: Benchmarking Computer Use Agents on Long-Horizon Real-World Tasks (arXiv 2606.29537)](https://arxiv.org/abs/2606.29537)
- [On the Reliability of Computer Use Agents (arXiv 2604.17849)](https://arxiv.org/pdf/2604.17849)
- [The Long-Horizon Task Mirage? (arXiv 2604.11978)](https://arxiv.org/html/2604.11978v1)
- [Beyond pass@1: A Reliability Science Framework for Long-Horizon LLM Agents (arXiv 2603.29231)](https://arxiv.org/pdf/2603.29231)
- [ScreenSpot Pro Leaderboard — llm-stats](https://llm-stats.com/benchmarks/screenspot-pro) · [WebArena Leaderboard](https://leaderboard.steel.dev/leaderboards/webarena/)
- [Computer Use Agents in Production: Reliability and Cost Reality (2026)](https://contracollective.com/blog/computer-use-agents-production-reliability-cost-2026)
- [Claude Computer Use API: What It Does and What It Costs](https://valueaddvc.com/blog/claude-computer-use-the-api-feature-that-lets-ai-control-your-desktop) · [Anthropic API pricing 2026](https://pricepertoken.com/pricing-page/provider/anthropic)
- [Counterpoint — AI Advanced PCs to surpass half of global shipments in 2026](https://counterpointresearch.com/en/reports/ai-advanced-pcs-to-surpass-half-of-global-shipments-in-2026) · [Canalys/Omdia — Now and next for AI-capable PCs](https://omdia.tech.informa.com/insights/2025/now-and-next-for-ai-capable-pcs)
- [UI-TARS (ByteDance)](https://github.com/bytedance/ui-tars) · [MMBench-GUI (arXiv 2507.19478)](https://arxiv.org/pdf/2507.19478)
- [The New MCP Roadmap](https://blog.modelcontextprotocol.io/posts/mcp-roadmap/) · [MCP Enterprise Adoption: July 2026 State of Play](https://andrew.ooo/answers/mcp-model-context-protocol-enterprise-adoption-july-2026/) · [MCP vs CLI for AI Agents — Firecrawl](https://www.firecrawl.dev/blog/mcp-vs-cli)
- [Windows 11 security book — Agentic security](https://learn.microsoft.com/en-us/windows/security/book/operating-system-agentic-security) · [Tom's Hardware — new agentic AI features introduce new security risks](https://www.tomshardware.com/software/windows/microsofts-new-agentic-ai-features-introduce-new-security-risks-introduced-by-ai-like-prompt-injection-firm-acknowledges-new-and-unexpected-risks-are-possible) · [SC Media — Agent Workspace ships with a security warning](https://www.scworld.com/news/new-agent-workspace-feature-comes-with-security-warning-from-microsoft)
- [VentureBeat — Copilot Studio prompt injection; data exfiltrated anyway](https://venturebeat.com/security/microsoft-salesforce-copilot-agentforce-prompt-injection-cve-agent-remediation-playbook)
- [Cloud Security Alliance — Promptware: prompt injection as C2](https://labs.cloudsecurityalliance.org/research/csa-research-note-promptware-c2-agent-exploitation-20260406/)
- [VPI-Bench (arXiv 2506.02456)](https://arxiv.org/pdf/2506.02456) · [AgentArmor (arXiv 2508.01249)](https://arxiv.org/pdf/2508.01249) · [ceLLMate (arXiv 2512.12594)](https://arxiv.org/pdf/2512.12594) · [The Blind Spot of Agent Safety (arXiv 2604.10577)](https://arxiv.org/pdf/2604.10577)
- [GeekWire — One year after its rocky launch, Recall still raises security red flags](https://www.geekwire.com/2026/one-year-after-its-rocky-launch-microsofts-windows-recall-still-raises-security-red-flags/)
- [Gibson Dunn — EU AI Act Omnibus agreement](https://www.gibsondunn.com/eu-ai-act-omnibus-agreement-postponed-high-risk-deadlines-and-other-key-changes/) · [Covington — EU AI Act update: timeline relief](https://www.insideglobaltech.com/2026/05/28/eu-ai-act-update-timeline-relief-targeted-simplification-and-new-prohibitions/)
- [Gartner — >40% of agentic AI projects canceled by end-2027](https://www.gartner.com/en/newsroom/press-releases/2025-06-25-gartner-predicts-over-40-percent-of-agentic-ai-projects-will-be-canceled-by-end-of-2027)
- [MIT report — 95% of AI pilots fail to deliver ROI](https://www.legal.io/blog/5719519/MIT-Report-Finds-95-of-AI-Pilots-Fail-to-Deliver-ROI-Exposing-GenAI-Divide) · [Critique — Marketing AI Institute](https://www.marketingaiinstitute.com/blog/mit-study-ai-pilots)
- [It's FOSS — Linux market share](https://itsfoss.com/linux-market-share/) · [Linuxiac — >10% in North America](https://linuxiac.com/linux-desktop-market-share-surpasses-10-in-north-america/)
- [ACI Worldwide — Six in ten UK consumers would stop using an AI shopping agent after one mistake](https://investor.aciworldwide.com/news-releases/news-release-details/six-ten-uk-consumers-would-stop-using-ai-shopping-agent-after) · [Checkout.com — trust for agentic commerce](https://www.checkout.com/newsroom/consumer-demand-for-ai-shopping-is-forming-fast-but-trust-for-agentic-commerce-is-still-catching-up)
- [Visual Studio Magazine — At Build 2026, Microsoft sets up Windows as an OS for AI agents](https://visualstudiomagazine.com/articles/2026/06/02/at-build-2026-microsoft-sets-up-windows-as-an-os-for-ai-agents.aspx)
- [AgenticOS @ SOSP 2026 workshop](https://os-for-agent.github.io/)
