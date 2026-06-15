# Architecture — Self-Improving PR-Reviewer Loop

**Author role:** Principal Software Architect
**System goal:** A continuously-running system that learns from human PR-review *behavior* to produce an AI reviewer that **matches or surpasses** human reviewers. It must (1) understand *why* a human left a review comment, (2) understand the code under review, and (3) decide whether the human was *right* or *missed something*.

**Optimize for:** production readiness · monotonically improving metrics · matching/surpassing human reviewers.
**Hard constraints (treated as first-class design forces):** avoid over-engineering · never break the loop · resist hallucination · **no human-in-the-loop at rollout** — the system must bootstrap and run unattended purely from existing PR history (see §4, *human-free operating mode*).

The constraints are the interesting part. Any LLM system can *generate* review comments; the engineering problem is producing **measurable, auditable, non-regressing** improvement from noisy human signal without the system fooling itself. The architecture below is organized around that problem.

---

## 1. Architectural approaches evaluated

### Approach A — Continuous fine-tuning / RLHF (weight-space learning)
Treat human comments as labels and resolution outcomes (acted-on vs dismissed) as reward; periodically fine-tune or DPO/RLHF a model so the reviewer's *weights* encode the team's conventions.

- **Strengths:** highest theoretical ceiling; the model internalizes subtle style; "surpass" is most plausible if it generalizes.
- **Weaknesses:** catastrophic forgetting degrades general code understanding; a single bad batch silently poisons the model (rollback = redeploy a checkpoint); the signal (one team's few-thousand comments) is far too small and contradictory for stable gradient learning; weight changes are **un-auditable** — you cannot show *why* it flagged something; heavy MLOps (GPUs, versioning, eval-before-deploy). It maximally violates *avoid over-engineering* and *don't break the loop*, and worsens *hallucination* (a fine-tuned model still hallucinates, now opaquely).

### Approach B — Rubric/policy evolution, eval-gated (symbol-space learning) — *and the basis of the current system*
Keep the base model **frozen**. Improve a structured, human-readable **rubric** (a conventions skill: `SKILL.md` + `references/*.md`) that the reviewer follows. An LLM *analyst* turns clustered human comments + their code into a small, generalizable rule edit; a **multi-gate evaluator** decides — against a held-out set of real, verified bugs (human-verified, or in unattended mode resolution-verified; see §4) — whether to keep it. Improvement is a hill-climb in *text space*, admitted only when measured metrics improve.

- **Strengths:** no training, no GPUs, cheap, fast iteration; the policy is **auditable and editable** (you can read the rule, so "*why*" is first-class); the eval gate is a **hard, measurable** improvement signal and the primary defense against hallucination/regression; a bad proposal is simply *rejected* — the champion never changes, so the loop is robust by construction; rollback is trivial (versioned text + ledger).
- **Weaknesses (addressed in §4):** the eval set is the ceiling (overfit risk); the "is the human right?" judge is itself an LLM (hallucination surface); rubric bloat raises false positives; recall can plateau.

### Approach C — Retrieval-augmented / memory-based reviewer (retrieval-space)
No rubric, no training. Embed every historical human comment + code context into a vector store; at review time retrieve the most similar precedents and condition the reviewer on them ("humans flagged X in code like this").

- **Strengths:** simplest; improves automatically as memory grows; excellent provenance ("*why*" = cite the precedent); low hallucination when strictly grounded in retrieved cases.
- **Weaknesses:** it **mimics, it doesn't reason** — it can only echo patterns humans already wrote about, so it **cannot surpass humans**, only reproduce them *including their blind spots*; "decide if the human missed something" is structurally impossible from precedent alone; spurious similarity → false positives; **no promotion gate**, so "did it get better?" is unmeasurable. Strong as a *feeder*, weak as the *spine*.

---

## 2. Trade-off comparison

| Axis | A: Fine-tune/RLHF | B: Rubric-evolution (gated) | C: RAG / memory |
|---|---|---|---|
| Improvement ceiling | Highest (theoretical) | High (with growing eval) | Low — capped at human distribution |
| Can *surpass* humans? | Maybe, opaquely | **Yes, and measurably** | No — echoes humans |
| Auditable "why" | ✗ black box | **✓ read the rule + rationale** | ✓ cite precedent |
| "Is the human right?" | implicit, unverifiable | **✓ code + outcome + eval** | ✗ precedent only |
| Hallucination control | Poor (opaque) | **Strong (gate rejects)** | Good (grounded) |
| Never-break-the-loop | Poor (poisonable) | **Strong (reject ≠ change)** | Strong |
| Measurable metric signal | indirect | **✓ direct, per-change** | ✗ |
| Production ops weight | Heavy (GPUs/MLOps) | **Light (LLM API + eval)** | Medium (vector DB) |
| Over-engineering risk | High | **Low** | Low–Medium |
| Cost | High | Low–Medium | Medium |

---

## 3. Selection — **B (rubric-evolution, eval-gated), refined with C as a feeder**

**Selected:** Approach B as the spine, absorbing C's retrieval as an *input* mechanism (to surface candidate patterns and ground proposals) and explicitly *not* adopting A.

**Why A is rejected:** the dominant forces here are *auditability*, *non-regression*, *anti-hallucination*, and *avoid over-engineering*. Weight-space learning is the worst on all four, and the available signal is too small/noisy for stable training. The ceiling advantage is theoretical and unrealizable at this data scale; the operational and safety costs are real and immediate.

**Why C is rejected as the spine:** the explicit goal is to **match or surpass** humans and to **optimize a metric**. Pure retrieval can do neither — it reproduces the human distribution (blind spots included) and offers no promotion gate. It is, however, an excellent *feeder* into B's analyst, so it is retained as a sub-component rather than discarded.

**Why B wins:** it is the only approach that turns noisy human behavior into an **explicit, auditable policy** while providing a **hard, measurable, per-change improvement gate** that is simultaneously the anti-hallucination and never-break-the-loop mechanism. The model stays frozen (cheap, safe, no forgetting); improvement is reversible text; and "surpass humans" becomes a *measured* claim via counterfactual replay and a self-growing eval set, not an assumption.

---

## 4. Self-review — critical weaknesses and revisions

A Principal-level design must attack its own choice. B's failure modes and the revisions that fold their mitigations into the architecture as first-class components:

1. **The eval set is the ceiling → overfitting.** If the loop only ever optimizes against a fixed golden set, it games the ruler, not reality.
   **Revision:** a held-out **train/val split** (the proposer only sees train; val is the honest reported number) + a **Silver Harvester** that continuously grows the eval set from *resolved* human comments + periodic **Counterfactual Replay** on real merged PRs. The ruler grows with the team.

2. **The "is-the-human-right" judge is an LLM → it can hallucinate a verdict.**
   **Revision:** the judge never decides; it *proposes*. Ground truth comes from **resolution outcomes** (did the author act on the comment, or rebut it?) and the **code itself**, and the proposal still must pass the eval gate. The LLM proposes; *measured outcomes dispose*.

3. **Rubric bloat → prompt noise → false positives.**
   **Revision:** a hard **size budget**, a periodic **consolidation** pass (shrink losslessly), and a **precision gate** (a candidate may not raise the false-positive rate on already-fixed diffs). Plus **rubric routing** (load only path-relevant reference files per review) so the prompt stays small as the policy grows.

4. **Silent self-deception (a change "improves" by luck / variance).**
   **Revision:** **majority-vote over N trials**, a **per-case regression guard** (any previously-passing case flipping to a miss rejects the promotion), and a **noise guard** measured only on cases the champion already passed.

5. **A dead eval that still "passes."** (Real incident in this system: a missing `GOOGLE_CLOUD_PROJECT` made every reviewer call error, yielding `recall = None` while the loop kept spinning.)
   **Revision:** an **eval-liveness guard** — if all/most cases *error* (vs. legitimately miss), the iteration **refuses to promote** (never promote against `0/0`), raises an alert, and auto-discards the dead state. *No decision is made on a broken measurement.*

6. **Plateau.** Hill-climbing stalls in local optima.
   **Revision:** **attempt memory** (fingerprint and never re-propose a rejected edit) + a **plateau/reflect mode** (widen the search beam and demand a different strategy after K consecutive rejections).

The revisions don't change the *spine* — they harden it. The result is a gated evolutionary optimizer over a symbolic policy, with anti-self-deception guards as primary components rather than afterthoughts.

### Revision for the "no humans in the loop, now" constraint — the corpus is the only oracle

The base design leaned on two humans: golden-set *curators* (ground truth) and a promotion *approver* (production safety). At rollout neither is available — the system must bootstrap and run unattended from existing PR history. Both humans are replaceable by signal **already latent in the data**, because *every PR comment is already labeled by what the author did next.*

- **Ground truth = resolution outcomes, not human curation.** A comment whose thread was *resolved* / answered by a fix commit is a **positive** (the reviewer *should* catch it); a comment that was *rebutted / won't-fixed / left unresolved* is a **do-not-flag negative** (it protects precision). This turns the corpus into a labeled eval set with **zero new human effort** — the humans already labeled it as a byproduct of normal work. The **resolution-derived (silver) eval becomes the PRIMARY bar**; a human-verified golden set drops to an *optional later booster*, not a prerequisite. Cold-start runs from history alone.
- **Hold the "experience of all PR comments" two ways at once.** The **rubric compresses** recurring patterns into generalizable rules; a **retrieval index recalls** the nearest past comments at review time as precedent. Distillation gives generalization; retrieval gives "we've flagged this exact shape before." Both read the whole corpus.
- **Shadow deployment replaces the human approval gate.** The reviewer ships **advisory / non-blocking** on real PRs — it posts findings but cannot block a merge. That makes a bad rubric **low-stakes** (no one is wrongly blocked), which is the safety net the approver used to provide. Promotion to a *blocking* reviewer stays an explicit, later, human decision.

**The honest ceiling this imposes.** With the team as the *only* oracle, the system learns to **match the team faithfully — including its blind spots and biases** — and it cannot independently know when the whole team was wrong, because its truth *is* the team. The resolution signal mitigates this (it scores what authors *did*, not mere opinion, and is harder to game than raw comment counts), so it beats naive comment-parroting — but **"surpass humans *objectively*" is capped until even a thin human signal exists.** The design keeps that seam open: a ~30-minute weekly spot-check of a sampled handful of cases can be added later to lift the ceiling, with no architectural change. Until then the honest, still-valuable target is **match-the-team at machine scale and consistency** — a tireless, uniform reviewer catches the fatigue-and-inconsistency misses that humans make even when their *judgment* is sound.

> **Implementation status (2026-06-13).** Built and offline-verified: the eval **env auto-inject** (so the reviewer never dies for a missing project var), the explicit **eval-liveness guard** in the promotion gate (never promote on a dead/degraded measurement), and **golden-optional / silver-primary** mode (`LOOP_SILVER_PRIMARY`) so the loop runs unattended on the resolution-derived eval with no human-curated golden set. Also built: a **lifetime autonomy scorecard** (`scorecard.py`, `python3 loop.py --scorecard`, dashboard strip) that accumulates across all runs — **autonomy %** (human-flagged patterns now caught unaided / all-time), **graduated**, **hallucination %**, **beyond-humans** — grounded only in resolution signal so it can't be gamed by a hallucinating proposer. Also built: **maestro-core grounding** — the reviewer's maestro-docs MCP (`LOOP_WITH_MCP`/`LOOP_WORKFLOW`, handled inside the read-only maestro-core harness) plus a new read-only **proposer** grounding (`maestro_context.py`) that feeds the codebase's own docs — or the **live maestro-docs MCP** when configured via a gitignored `.env` — into rubric-edit prompts so changes cite real modules/patterns. Designed but **not yet built:** the shadow (advisory, non-blocking) production deployment and the retrieval index.

---

## 5. Architecture diagram (text form)

```
                        ┌───────────────────────── SERVING (data) PLANE ─────────────────────────┐
   PR opened ──────────►│  CI hook ─► Reviewer (FROZEN LLM + champion rubric) ─► structured       │
   (GitHub/GitLab)      │            findings ─► posted as NON-BLOCKING advisory comments          │
        ▲               └───────────────────────────────┬─────────────────────────────────────────┘
        │ author acts / rebuts (resolution)             │ findings + outcomes
        │                                                ▼
   ┌────┴───────────────────────── SIGNAL STORE (immutable, versioned) ──────────────────────────┐
   │  all PR comments · RESOLUTION/OUTCOMES = the oracle · SILVER eval auto-built from RESOLVED    │
   │  comments = PRIMARY bar · [optional/later] human golden bugs · retrieval index · ledger      │
   └───┬───────────────▲───────────────────────────▲──────────────────────────────▲──────────────┘
       │ read           │ append (mine)             │ append (harvest)             │ append (ledger)
       ▼                │                           │                              │
 ┌─────────── IMPROVEMENT (control) PLANE — the "mind", runs async on a schedule ───────────────┐
 │                                                                                               │
 │  Orchestrator ─► picks mutation kind (learn-corpus | fix-misses | consolidate | harvest)      │
 │      │                                                                                        │
 │      ▼                                                                                        │
 │  ① Analyst/Proposer ── reads comment-cluster + code + outcomes ──► proposes SMALL rule edit   │
 │      │   ("understand WHY": abstract comment → mechanism + bug class, not a memorized case)   │
 │      ▼                                                                                        │
 │  ② Judge ── grounds it in code + resolution signal ──► encode | do-not-flag | beyond-human    │
 │      │   ("is the human RIGHT?": outcomes are ground truth, not the LLM's opinion)            │
 │      ▼                                                                                        │
 │  ③ Evaluator / PROMOTION GATE  (the Controller — anti-self-deception)                         │
 │      held-out val · majority vote · per-case regression guard · precision gate ·              │
 │      noise guard · size budget · EVAL-LIVENESS guard                                          │
 │      │                                                                                        │
 │      ├── REJECT ─► attempt-memory (don't re-propose); champion UNCHANGED                      │
 │      └── PROMOTE ─► Champion Registry (atomic swap + history snapshot + ledger)               │
 │                         │                                                                     │
 │                         └─► Adoption bundle (diff + rationale + before/after metrics)         │
 │                              └─► SHADOW reviewer = default · BLOCKING = a later human opt-in   │
 │                                                                                               │
 │  Silver Harvester (grow eval) · Counterfactual Replayer (champion vs humans → "beyond human") │
 └───────────────────────────────────────────────┬──────────────────────────────────────────────┘
                                                  │ status · thoughts · metrics · alerts
                                                  ▼
                       OBSERVABILITY PLANE — heartbeat · live dashboard · ledger · alerts
```

---

## 6. Components

**Minimal viable core (do not omit):** Miner → Analyst/Proposer → **Promotion Gate** → Champion Registry → Reviewer. Everything else is the "surpass-humans / production-hardening" layer, added incrementally — this is how the design *avoids over-engineering*.

1. **Ingestion / Corpus Miner** — on a schedule, pulls human review comments, threads, and **resolution status** from the VCS; normalizes, dedupes, defensively parses (real corpora contain truncated lines); also mines *production AI-reviewer outcomes* (acted-on vs dismissed). Output is append-only.
2. **Signal Store** — immutable, versioned home for the corpus, the **resolution/outcomes** (the human-free oracle), the **resolution-derived silver eval = the quality bar** (auto-grown from resolved comments), an optional/later human golden set, a retrieval index, and the ledger/history. JSONL + object store at one-team scale; a warehouse + columnar at org scale.
3. **Reviewer (Actor)** — the *frozen* LLM that, given a diff + champion rubric, emits structured findings. **Stateless → horizontally scalable.** Identical code path online (CI) and offline (eval) so improvements transfer faithfully.
4. **Analyst / Proposer — the "why" engine** — clusters human comments, reads the code they sat on and the resolution signal, and proposes the *smallest generalizable* rule edit. Abstracts a comment into *mechanism + bug class* (transfers to unseen code) rather than memorizing a PR. Optionally retrieval-grounded (the absorbed Approach C).
5. **Judge / Adjudicator — the "is the human right" engine** — emits one of: **encode** (author acted → human right → add rule), **do-not-flag** (author rebutted/dismissed → noise → raise precision), or **beyond-human** (AI flagged a real issue no human raised). Proposes only; never auto-applies.
6. **Evaluator / Promotion Gate — the Controller** — runs candidate-vs-champion on the resolution-derived eval (silver primary; optional golden) with: held-out val, majority-vote trials, per-case regression guard, precision/FP gate, noise guard, size budget, and the **eval-liveness guard**. Emits `PROMOTE | REJECT` + reasons. This single component is both the *improvement signal* and the *anti-hallucination / non-regression* backbone.
7. **Champion Registry / Policy Store** — versioned champion rubric + full history; **atomic promotion** and **instant rollback** (`rollback <id>`). The serving Reviewer reads the champion from here.
8. **Silver Harvester** — converts resolved human comments into machine-checkable eval cases (frozen inline diff), so the measuring stick grows weekly → defeats the static-ceiling/overfit failure mode.
9. **Counterfactual Replayer** — replays champion vs live (and vs humans) on recent merged PRs → measures real-world lift and **caught-beyond-humans** (the *surpass* metric). Reported, **not gated** (keyword-noisy signals must not gate).
10. **Orchestrator / Control Loop** — schedules iterations, enforces the **budget brake** (max calls/tokens per day), single-instance lock, atomic state, per-case checkpoint/crash-resume, and selects the per-iteration mutation kind.
11. **Observability Plane** — crash-proof heartbeat (`status.json`), captured model reasoning (`thoughts.jsonl`), the decision ledger, a live dashboard (recall trajectory, gates, rubric anatomy, beyond-human counter), and alerts (plateau, val-drop, eval-dead, budget-spent).
12. **Adoption / Deployment** — the promoted champion serves the **shadow (advisory, non-blocking) reviewer** automatically — no human gate, and a bad rubric cannot block a merge. A proposal bundle (rubric diff + rationale + before/after metrics) is still exported, used *only* for the later, optional human decision to upgrade the shadow reviewer to a **blocking** one.

---

## 7. Data flow

**Online (serving):** PR opened → CI invokes Reviewer with the champion rubric → structured findings posted → author **acts or rebuts** → resolution captured back into the Signal Store. *This closes the loop:* the system's own output becomes tomorrow's training signal.

**Offline (one improvement iteration — the "mind"):**
1. Orchestrator checks liveness/budget and picks a mutation kind.
2. **Analyst** reads a comment cluster + its code + outcomes → proposes a small rule edit *(why)*.
3. **Judge** grounds it in code + resolution → encode / do-not-flag / beyond-human *(is the human right)*.
4. **Cheap screen** (1 trial on a subset) → if promising, **full confirm** (majority vote on the resolution-derived eval).
5. **Promotion Gate** runs every guard → `PROMOTE | REJECT`.
6. PROMOTE → atomic champion swap + history snapshot + ledger entry + adoption bundle. REJECT → attempt-memory; champion unchanged.
7. Periodically: Silver Harvester grows the eval; Replayer measures beyond-human lift.
8. Observability streams status/thoughts/metrics throughout.

---

## 8. Scaling strategy

- **Reviewer** is stateless → scale to *N* CI workers for PR throughput; the eval is embarrassingly parallel across cases × trials.
- **Control loop** is intentionally **single-writer per rubric** (one improvement process per repo/domain) — it does not need to scale up; it scales *out* by **sharding per repo/team**, one champion per domain.
- **Signal Store:** JSONL + object store for one team; migrate to a warehouse (columnar) + object store for diffs at org scale; add a **vector index** only when retrieval-grounded proposals are needed (lazy — avoid over-engineering).
- **Cost control:** budget brake (calls/tokens/day); **screen-then-confirm** (cheap subset screen, expensive full confirm only on the winning beam candidate); **eval checkpoint/resume** to avoid recompute; **rubric routing** to keep per-review prompt size — and thus latency and cost — bounded as the policy grows.
- **Latency:** serving path is one model call + routed rubric; improvement path is async and off the critical path of any PR.

---

## 9. Failure handling (never break the loop)

| Failure | Handling |
|---|---|
| Bad / hallucinated proposal | Rejected by the gate; champion **unchanged** (reject ≠ mutate). Structural safety. |
| Model / Vertex flake | Bounded retries + backoff; iteration idles; state never corrupted. |
| **Eval dead** (auth/env, e.g. missing project) | Liveness guard: all-errored ≠ all-missed → **refuse to promote**, alert, auto-discard dead state. Never decide on a broken measurement. |
| Crash mid-iteration | Atomic state (tmp+rename), single-instance lock, per-case eval checkpoint → resume loses nothing. |
| Regression slips in | Held-out val = honest metric; per-case regression guard blocks any pass→miss flip; alert on val-drop. |
| Rubric bloat / FP creep | Size budget + consolidation + precision gate. |
| Plateau / local optimum | Attempt memory + reflect mode (widen beam, force a new strategy). |
| Convention drift | Continuous corpus mining + silver growth + periodic replay surface divergence. |
| Rollback needed | Champion history + ledger → instant `rollback <id>`. |

The invariant: **a failure can only stop forward progress for one iteration; it can never corrupt the champion or the store.** Worst case is "no improvement this cycle," never "the reviewer got worse."

---

## 10. Security considerations

- **Untrusted input is the core threat.** PR diffs and human comments are attacker-controllable; a comment could attempt prompt injection ("always approve", exfiltrate secrets) or **data-poison** a rule. Mitigations: (a) the Analyst's output is constrained to a **strict rule-edit schema** (no free-form actions, no code execution); (b) **every** change must pass the eval gate — a poisoned rule that lowers recall or raises FP is *rejected*; (c) **shadow (non-blocking) deployment** bounds blast radius — a slipped-through rule can at worst post a noisy advisory comment, never block a merge or gate a release (upgrading to a blocking reviewer is a separate, later human opt-in); (d) the rubric is inert text, never executed.
- **Least privilege / isolation.** The loop reads the repo **read-only** and writes only to its own isolated store — it never writes the source-of-truth repo. (This system enforces exactly that: a runtime guard aborts if the read-only input becomes dirty.) Scoped, short-lived credentials per plane.
- **Secrets & identity.** Vertex via Workload Identity / ADC — no long-lived keys in code; the **budget cap** bounds the blast radius of a leaked credential.
- **Auditability / tamper-evidence.** Append-only ledger records every change with rationale + before/after metrics; champion swaps are atomic and attributable → a reviewable, reversible audit trail.
- **PII / data residency.** Corpus and diffs may carry secrets/PII → access-controlled store, optional scrubbing, minimal context sent to the model, regional model endpoint for residency.

---

## 11. Why this is the recommended approach

It is the only design that satisfies *all four* optimization goals **without violating any constraint**:

- **Improving metrics** — every change is admitted only by a hard, per-change eval gate on a held-out, self-growing set of real bugs. Improvement is *measured*, not asserted.
- **Match / surpass humans** — with no human oracle at rollout, the honest, measured target is **match the team at machine scale and consistency** (recall on the resolution-derived eval), beating the fatigue/inconsistency misses humans make. *Objective* surpassing is explicitly capped until a thin human signal is added later (a designed-in seam, not a rewrite); the *caught-beyond-humans* replayer reports lift in the meantime.
- **Understand *why* + *is the human right*** — human-readable rubric + rationale makes "why" auditable; resolution outcomes + code (not the LLM's opinion) ground "right or missed."
- **Production-ready & unattended** — stateless serving, observability, budget control, atomic/rollback, and **shadow (non-blocking) deployment** as the safety net in place of a human approver, so the loop bootstraps and runs entirely from existing PR history.
- **Avoid over-engineering** — frozen model (no training infra), no mandatory vector DB, a small mandatory core with optional hardening added only as needed.
- **Never break the loop** — reject-don't-mutate, atomic state, liveness guard, instant rollback: failures cost a cycle, never the champion.
- **Resist hallucination** — the LLM only ever *proposes*; measurable gates *dispose*. Hallucination is not prevented at the model — it is made *unable to ship*.

In one line: **a frozen reviewer plus a gated, evolutionary optimizer over an auditable symbolic policy, where every guard exists so the system cannot fool itself.** That property — *it cannot fool itself* — is precisely what "production-ready, improving, non-hallucinating, unbreakable" requires.
