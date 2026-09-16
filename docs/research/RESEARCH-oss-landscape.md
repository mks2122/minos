# Open-Source Landscape — What Already Exists, and Where the Gap Is

**Research date:** 16 Sep 2026 · 12+ searches, GitHub repos and docs fetched directly.
**Question:** how much of the planned project already exists in OSS, and is there a genuine differentiator?

---

## Executive summary

**Four of the five pillars already exist in OSS — individually, and in mature form. Nothing combines them.**

The unclaimed thing is the *architecture*: a typed-tool-first hierarchy where GUI is an explicit last resort, with a **policy broker sitting under all three tiers**.

| Pillar | Status in OSS |
|---|---|
| 1. Three-tier hierarchy (L1 tools → L2 adapters → L3 GUI fallback) | **Exists** — Microsoft UFO², literally "Native APIs (preferred) / GUI Actions (fallback)". Windows-only, research-grade, 9.7k stars, MIT, arXiv:2504.14603 |
| 2. Policy broker + rollback | **Partial** — Hermes has approvals.yaml + shadow-git `/rollback`; OpenAdapt-Flow has real certification/admission/effect-verification. Neither is a general capability-scope broker |
| 3. Computer-state memory | **Partial** — OpenHuman indexes your *content*; nobody indexes your *computer's state over time* |
| 4. Skill learning | **Commoditised.** Hermes, OpenClaw, OS-Copilot, XSkill, agentskills.io. Do not lead with this |
| 5. Local-first swappable planner | **Fully commoditised.** Table stakes — claiming it signals you haven't surveyed the field |

---

## Comparison matrix

Legend: ✅ has it · 🟡 partial · ❌ lacks it

| Project | License | Stars | Activity (Sep 2026) | Maintainer | 1. Tiered | 2. Policy/rollback | 3. Computer-state memory | 4. Skill learning | 5. Local-first |
|---|---|---|---|---|---|---|---|---|---|
| **Microsoft UFO² / UFO³** | MIT | 9.7k | Active (1,926 commits) | Microsoft Research | ✅ **best in class** | ❌ | 🟡 RAG over execution history | 🟡 RAG substrate, no procedure promotion | ✅ |
| **Hermes Agent / Desktop** | MIT | ~246k | Very active (35.8k commits) | Nous Research | 🟡 L1 ✅ / L2 via MCP / L3 via community MCP | 🟡 approvals.yaml + denylist + sandbox backends + **shadow-git checkpoints & `/rollback`** | 🟡 FTS5 session search — *sessions, not files* | ✅ autonomous skill creation, self-improving | ✅ |
| **OpenClaw** | MIT (Foundation) | ~389.8k | Extremely active (95k commits) | Foundation + community | 🟡 L1 ✅, L2 via skills/MCP, L3 weak | 🟡 pairing approval, plugin permission manifests; **tools run on host unless you configure sandboxing** | 🟡 workspace-as-memory (git repo) | ✅ ClawHub marketplace | ✅ |
| **OpenHuman** | GPL-3.0 | 39.8k | Active, early beta | tinyhumans | ❌ no OS control | 🟡 approval gate, OS-keyring, Rust privacy core | ✅ **best in class** — emails/docs/chats/calendar/repos → SQLite tree + Obsidian mirror | 🟡 installed, not induced | ✅ |
| **cua (trycua)** | MIT | 22.7k | Very active (4,744 commits) | Cua (company) | 🟡 a11y tree **+** screenshot, **no declared precedence** | ❌ sandbox/fleet isolation only | ❌ | ❌ | ✅ |
| **UI-TARS Desktop** | Apache-2.0 | 39.0k | Active (v0.3.0) | ByteDance | ❌ GUI-primary | ❌ | ❌ | ❌ | ✅ |
| **Agent-S / S2 / S3** | Apache-2.0 | 12.3k | Active | Simular AI | ❌ vision-primary by design | ❌ README warns "only enable in trusted environments" | ❌ trajectory buffer only | 🟡 episodic memory | ✅ |
| **Bytebot** | Apache-2.0 | 11.1k | ⚠️ **ARCHIVED 7 Mar 2026**, forks only | bytebot-ai | ❌ GUI-primary in container | ❌ **container ≠ policy** | ❌ | ❌ | ✅ |
| **OpenAdapt / openadapt-flow** | MIT | OpenAdapt ~1k; **flow: 9 stars**, 661 commits | Active, tiny audience | OpenAdaptAI | ❌ GUI-demonstration only | ✅ **best in class** — policy packs, certification/admission gates, signed/expiring admissions, `RECONCILIATION_REQUIRED` halts, effect oracles | ❌ | ✅ multi-trace induction into deterministic bundles | ✅ healthy replay makes **zero** model calls |
| **OS-Copilot / FRIDAY** | MIT | ~1.5k | Stale (2024) | academic | 🟡 modular | ❌ | ❌ | ✅ the original skill library | ✅ |
| **Open Interpreter** | AGPL-3.0 | ~60k | Rewritten in Rust | Open Interpreter | ❌ code-execution-primary | 🟡 y/n confirm only | ❌ | ❌ | ✅ |
| **OpenCUA** | research | — | Active | XLANG-AI | n/a — models + data, not a runtime | ❌ | ❌ | ❌ | ✅ 7B/32B/72B weights |
| **Khoj** | AGPL-3.0 | ~36k | Active | Khoj AI | ❌ | ❌ | ✅ files/notes/PDF/Obsidian | ❌ | ✅ |
| **AnythingLLM** | MIT | ~65k | Active | Mintplex Labs | ❌ | ❌ | ✅ docs only | ❌ | ✅ |
| **Browser Use / Stagehand / Skyvern** | — / MIT / AGPL | 112k / 21k / 7k | Active | — | L2-browser only | ❌ | ❌ | ❌ | ✅ |
| **E2B** | Apache-2.0 | ~9k | Active | E2B | n/a | 🟡 Firecracker microVM, ~150ms cold start | ❌ | ❌ | n/a |
| **Daytona** | AGPL-3.0 | ~22k | ⚠️ **Closed-source June 2026**, frozen at v0.190.0 | Daytona | n/a | 🟡 Docker | ❌ | ❌ | n/a |
| **Letta (MemGPT)** | Apache-2.0 | ~18k | Active | Letta | n/a | ❌ | 🟡 *agent* memory, not *computer* state | 🟡 | ✅ |
| **Mem0** | Apache-2.0 | ~52k | Active | Mem0 | n/a | ❌ | ❌ conversational facts only | ❌ | ✅ |
| **snapshot-contain-protect** | OSS | tiny | New | individual | n/a | 🟡 millisecond git-style OS snapshot/restore **built for CUAs** | ❌ | ❌ | n/a |
| **Microsoft Agent Governance Toolkit** | OSS (Apr 2026) | — | Active | Microsoft | n/a | 🟡 OWASP Agentic Top 10 / EU AI Act framing | ❌ | ❌ | n/a |

---

## The four detailed questions

### (a) Real policy layer, or just a sandbox?

**Only two projects have anything deserving the name.**

- **OpenAdapt-Flow — genuinely yes.** Human-reviewed effect contracts, `--strict` certification, signed/expiring/revocable release admissions, policy packs (`permissive`, `clinical-write`, `regulated`), and crucially **effect verification through an independent system-of-record read, not a screenshot.** Their benchmark: *"screen-only checks silently accepted 75.0% of the wrong effects; one oracle that reads the system of record cut that to 12.5%."* On failure it halts with `RECONCILIATION_REQUIRED` rather than retrying. **Strongest prior art against pillar 2 — but it governs replayed demonstrations, not a live LLM agent.**
- **Hermes — partial but real.** `~/.hermes/approvals.yaml` allowlist, path denylist for writes (blocked writes fail hard, not promptable from chat), swappable sandbox backends, shadow-git checkpoints with `/rollback`. **Documented limitation: "checkpoints are filesystem-only. They do not capture external effects like API calls, database modifications, or environment changes."** Also skipped for >50k-file projects and >10MB files.

Everyone else: **sandbox or nothing.** Bytebot = a container. cua = a VM/fleet. Agent-S = a 30-second bash timeout and a README warning. OpenClaw runs tools **on the host by default**, and its own docs concede "a single compromised skill inherits all of those permissions."

> **No OSS project has capability *scopes* (this task may touch `~/Invoices` read-only and nothing else), action provenance, or dry-run as a first-class mode across a live agent.**

### (b) Indexes actual files and work state, or only conversation?

- **Files + content:** OpenHuman (emails, docs, chats, calendar, repos → SQLite + Obsidian mirror, 20-min refresh), Khoj, AnythingLLM — **but all three are retrieval apps that cannot act on the OS.**
- **Sessions only:** Hermes (FTS5 + summarisation), OpenClaw (`memory.md` in a git repo), Letta, Mem0.
- **Nothing:** cua, UI-TARS, Agent-S, Bytebot, OpenCUA, browser agents.

> **Nobody indexes *computer state*** — which app had which document open, what was edited yesterday, window/app history. **"The Excel we were working on yesterday" is resolvable by no existing OSS project.** Cleanest single gap.

### (c) Skill learning, and how?

- **Hermes:** autonomous skill creation after complex tasks, self-improving on reuse, agentskills.io-compatible. Mature.
- **OpenClaw:** ClawHub marketplace, permission manifests. Distribution, not induction.
- **OpenAdapt-Flow:** multi-trace induction into deterministic bundles, quarantines underdetermined intent, emits MCP + Agent Skills. Most rigorous — and the only one that *verifies* the induced procedure.
- **UFO²:** RAG over help docs, Bing and execution traces — retrieval, not promotion to a callable procedure.
- **Research saturated:** XSkill (ICML 2026), SkillGen, Trace2Skill, MIND-Skill, SkillForge, SPARK, SkillLearnBench.

### (d) GUI primary or fallback?

- **Fallback (the planned model): UFO² only.** Explicit — "Native APIs (preferred)… GUI Actions (fallback)", with typed adapters: Excel via xlwings, Outlook via win32com, PowerPoint via python-pptx.
- **Ambiguous:** cua — driver returns a11y tree **and** screenshot together, no declared precedence.
- **Primary:** UI-TARS, Agent-S, Bytebot, OpenCUA, Self-Operating Computer, Skyvern, OpenAdapt.
- **Absent:** Hermes, OpenClaw, OpenHuman, Khoj, AnythingLLM.

---

## USP VERDICT

### Closest single project: Hermes Agent — ~55% overlap

Not close on architecture, close on *product surface*: MIT, local-first, swappable models, autonomous skill creation, persistent memory, approval rules, sandbox backends, filesystem checkpoints with `/rollback`, desktop app. ~246k stars, enormous velocity.

> **If you ship "local-first desktop agent with skills, memory, approvals and rollback," reviewers will call it a Hermes clone and they will be largely right.**

**Runner-up: Microsoft UFO² — ~45%, and it is closest on the pillar you thought was your idea.** The three-tier hierarchy is UFO²'s published architecture verbatim, down to the typed office adapters. Its weaknesses are real (Windows-only, research code, no policy layer, no computer-state memory) but **"typed adapters first, GUI as fallback" is not unclaimed — it is a Microsoft paper with an MIT repo.**

**No project exceeds ~55%. The combination is genuinely new; the individual pillars mostly are not.**

### Does anything combine tiered hierarchy + GUI fallback + real policy/rollback?

**No. Explicitly: no such OSS project exists as of Sep 2026.**

- UFO² has tiers 1+2+3 with correct precedence and **zero** policy, scoping, approvals, provenance or rollback
- Hermes has approvals + rollback and **no** tier hierarchy and **no** GUI tier of its own
- OpenAdapt-Flow has the best governance in the field and **no** LLM-agent hierarchy — it governs replayed demonstrations
- cua has the best cross-OS GUI substrate and **no** policy layer whatsoever

> **This intersection is empty. It is the most defensible claim available.**

### Ranked white space

1. **Pillar 2 — the permission broker, as a broker. WIDE OPEN.** Every desktop agent in OSS grants ambient OS authority and hopes. Nobody does capability scopes, per-action provenance, dry-run-by-default, or rollback spanning tiers. The gap: **a broker where the model emits a *request*, the runtime holds the authority, and every tier — L1 syscall, L2 typed adapter, L3 synthetic click — passes the same gate with the same audit record.**
2. **Pillar 3 — computer-state memory. NEARLY OPEN.** Resolving deictic references ("yesterday's Excel") to real OS objects is unclaimed.
3. **Pillar 1 — the three-tier hierarchy. HALF-CLAIMED.** UFO² owns it on Windows; cua owns the cross-OS substrate. Unclaimed: a **cross-platform, pluggable adapter registry** with capability manifests and an *auditable* degrade path L1→L2→L3 ("I fell back to pixels because no adapter covered this — here's the record").
4. **Pillar 4 — skill learning. CROWDED.** Only differentiable if skills are **policy-scoped and verified** — a promoted procedure carrying its capability manifest and an effect oracle.
5. **Pillar 5 — local-first swappable planner. FULLY OCCUPIED.**

### What NOT to rebuild

| Need | Use this | Why |
|---|---|---|
| L3 GUI/vision substrate | **`cua-driver`** (MIT) | a11y tree + screenshot + synthetic cursor without stealing focus, macOS/Windows/Linux. Reimplementing cross-OS input injection is 12+ months of thankless platform work |
| Grounding model | **UI-TARS-1.5-7B** or **OpenCUA-7B/32B** | Open weights, vLLM-supported. Do not train a grounder |
| L2 browser | **Playwright MCP** or **Stagehand** (MIT) | Avoid Skyvern (AGPL) if you want permissive licensing |
| L2 office | Fork **`kittrellbj/mcp-libre`** / **`WaterPistolAI/libreoffice-mcp`**, lift **UFO²'s AppAgent adapters** (MIT) | Your L2 tier, already written, MIT-licensed |
| Skill format | **agentskills.io / Anthropic Agent Skills** | Don't invent a format; you'd forfeit the ecosystem Hermes and OpenClaw feed |
| Filesystem rollback | **`snapshot-contain-protect`** or Hermes' shadow-git | Don't write a snapshot engine |
| Sandboxing | **E2B** (Apache-2.0, Firecracker) | ⚠️ **Do NOT build on Daytona** — closed-source since June 2026 |
| Effect verification | Steal **OpenAdapt-Flow's oracle pattern** (SQL/REST/FHIR/doc-hash reads) | 9 stars, 661 commits, right ideas, no distribution — **best merge candidate in the space** |
| Computer-state memory | ❌ **Not Letta or Mem0** | Conversational/agent memory. Your problem is a filesystem + window + app-state index. Different data model |

### The README headline

> **The model asks. The runtime decides.**
>
> *An open-source desktop agent where every action — a syscall, a typed Excel write, or a synthetic click — passes through one capability-scoped policy broker, with full provenance and a rollback you can actually trust.*

Why this and not the alternatives:

- "Three-tier hierarchy" — **taken** by UFO², with a Microsoft paper behind it
- "Learns skills from experience" — **taken** by Hermes and 389k stars of OpenClaw
- "Local-first, any model" — **table stakes**; claiming it looks naive
- "Computer-state memory" — strong and nearly unclaimed, but it's a *feature* and hard to demo in one screenshot. Make it bullet #2, with the concrete demo: *"the Excel we were working on yesterday" resolves to a real file, and it can prove how it knew*
- The broker is the only claim that is simultaneously (i) empirically unoccupied, (ii) the reason a serious person would run an agent on their real machine rather than a throwaway container, and (iii) provable in a 30-second demo: **run the same task twice — once in dry-run showing the diff of what *would* happen, once for real — then `rollback`**

### ⚠️ The blunt caveat

**The broker is only credible if it covers what Hermes' rollback explicitly does not: external API calls, emails sent, database writes.**

If your rollback is also filesystem-only, **you have rebuilt Hermes' checkpoint system with a different README.**

Pair the broker with OpenAdapt-style **effect contracts and oracles** — declare the intended effect before acting, verify it against the system of record after, and refuse to claim success from a screenshot. That combination — **scoped authority + declared effects + verified reversal, across a typed-first tool hierarchy** — is the one thing in this entire survey that nobody has built.

---

## Sources

- [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) · [checkpoints & rollback](https://hermes-agent.nousresearch.com/docs/user-guide/checkpoints-and-rollback) · [security docs](https://hermes-agent.nousresearch.com/docs/user-guide/security)
- [openclaw/openclaw](https://github.com/openclaw/openclaw) · [agent-skills](https://github.com/openclaw/agent-skills) · [docs.openclaw.ai](https://docs.openclaw.ai/reference/AGENTS.default)
- [microsoft/UFO](https://github.com/microsoft/UFO) · [UFO² overview](https://microsoft.github.io/UFO/ufo2/overview/) · [arXiv:2504.14603](https://arxiv.org/pdf/2504.14603)
- [trycua/cua](https://github.com/trycua/cua) · [cua-driver](https://github.com/trycua/cua/tree/main/libs/cua-driver) · [Computer-Use 2.0](https://cua.ai/blog/computer-use-2-ai-engineer-worlds-fair)
- [OpenAdaptAI/openadapt-flow](https://github.com/OpenAdaptAI/openadapt-flow) · [OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt) · [openadapt.ai/research](https://openadapt.ai/research)
- [tinyhumansai/openhuman](https://github.com/tinyhumansai/openhuman) · [khoj-ai/khoj](https://github.com/khoj-ai/khoj)
- [simular-ai/Agent-S](https://github.com/simular-ai/Agent-S) · [Agent S2 paper](https://arxiv.org/pdf/2504.00906) · [bytedance/UI-TARS-desktop](https://github.com/bytedance/UI-TARS-desktop)
- [bytebot-ai/bytebot](https://github.com/bytebot-ai/bytebot) (archived) · [xlang-ai/OpenCUA](https://github.com/xlang-ai/OpenCUA) · [arXiv:2508.09123](https://arxiv.org/abs/2508.09123)
- [OS-Copilot](https://github.com/OS-Copilot/OS-Copilot) · [arXiv:2402.07456](https://arxiv.org/pdf/2402.07456) · [openinterpreter](https://github.com/openinterpreter/openinterpreter)
- [Daytona went closed-source](https://bex.co/blog/2026/09/12/daytona-closed-source-agent-sandbox-oss-risk) · [E2B vs Daytona](https://northflank.com/blog/daytona-vs-e2b-ai-code-execution-sandboxes)
- [ThyFriendlyFox/snapshot-contain-protect](https://github.com/ThyFriendlyFox/snapshot-contain-protect) · [Crab checkpoint/restore](https://arxiv.org/html/2604.28138v1)
- [mcp-libre](https://github.com/kittrellbj/mcp-libre) · [libreoffice-mcp](https://github.com/WaterPistolAI/libreoffice-mcp)
- [agentdesktop.dev](https://agentdesktop.dev/blog/2026/09/introducing-agentdesktop/) · [Microsoft Agent Governance Toolkit](https://agenticcontrolplane.com/blog/microsoft-agent-governance-toolkit-coverage) · [OPA for agents](https://tianpan.co/blog/2026-04-25-policy-as-code-agent-permissions-opa-rego)
- [XSkill (ICML 2026)](https://github.com/XSkill-Agent/XSkill) · [Best OSS computer-use agents 2026](https://fazm.ai/blog/best-open-source-computer-use-agents-2026-local-desktop-control)
