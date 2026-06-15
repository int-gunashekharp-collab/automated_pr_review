# What the loop has learned — patterns of human-found issues

Snapshot 2026-06-15. This is the "find the issues humans found, learn the
patterns" output, read from `workspace/` and `champion-skill/`.

## How it learns a pattern

Each iteration the proposer reads a cluster of **real human review comments**
(mined from 5,349 comments across 3,437 merged maestro-core PRs) together with
the code they sat on and the **resolution outcome** (did the author fix it, or
push back?). It abstracts each comment from a one-off remark into a *mechanism +
bug class* that transfers to unseen code, proposes one small rubric rule, and the
**eval gate keeps it only if it regresses nothing**. Outcomes — not the model's
opinion — are the ground truth, which is why a pushed-back comment becomes a
*do-not-flag* rule instead of a new check.

## The patterns it now encodes (champion rubric, ~37 KB)

The distilled rubric is `champion-skill/SKILL.md` plus eight reference files.
Its ten most-enforced, memorized rules — the recurring issue classes this team
catches over and over:

1. **Model change ⇒ Alembic migration in the same PR** (PR #1743, #2415, #2782, #4290).
2. **No auth/ownership gap on endpoints** — every mutating or `/{id}` route needs the same dependency its siblings use.
3. DB/transaction correctness (sessions, `down_revision` chains, index-without-migration).
4. Caching correctness (`@cached`/`@local_cached` key + duration mistakes).
5. Time handling — `utcnow()` is deprecated; use the tz-aware `now()` util.
6. API/schema conventions (request-body vs query, `model_validator` usage).
7. PII handling.
8. State-lifecycle / status-transition flows.
9. Hot-path / external-client robustness.
10. A **do-not-flag** list of false-positive classes authors have rebutted.

Severity is calibrated against the team's own taxonomy, and findings cite the
source PR ("same issue as #3232").

## Concretely: caught vs. missed (the honest scoreboard)

The loop tracks specific human-found issues and whether the AI reviewer catches
them unaided (`workspace/learning_ledger.json`). Of 7 tracked, 3 caught, 4 missed:

| Status | Issue | The human comment |
|---|---|---|
| ✅ caught | `s26` | redundant `if/else` after `raise_for_status()` |
| ✅ caught | `s103` | nested `dict.get(...) is not None` redundancy |
| ✅ caught | `s141` | `utcnow` deprecated → tz-aware `now()` from utils |
| ❌ missed | `s49` | OTP should be sent in the request body, not as a field |
| ❌ missed | `s93` | value isn't constant — should be per-runner (avg of L7 attendance) |
| ❌ missed | `s103a` | also check the **status** of the response, not just the result |
| ❌ missed | `s279` | in an after-mode `model_validator`, reference attrs via `self` |

The misses are the frontier: they're **semantic/domain judgments** ("this should
be runner-specific," "send it in the body") that are hard to turn into a
generalizable rule without raising false positives. The caught ones are
**mechanical patterns** that generalize cleanly.

## Current numbers (and the caveat)

- Eval set: 18 golden + 7 silver cases (self-grown from resolved comments).
- Champion recall **0.667** (10/15 scoreable) · train **0.75** · held-out **val 0.333** (95% CI 0.061–0.792 — 4 val cases is a tiny ruler).
- Noise 5.8 findings/review · precision FP-rate 0.25 · corpus 6,000 comments read.
- **Promotions: 0** across 125 iterations / 41.6h → plateau + reflect mode.

## Why it hasn't improved on the rubric yet (honest)

Two reasons, in order of impact:

1. **The last ~12 iterations were auth-dead** (expired Vertex creds) — no real
   attempts happened, they just errored. Fixing auth (see `keepalive/README.md`)
   is the prerequisite to *any* further learning.
2. Even before that, the gate **rejected 52 proposals and promoted 0**. That's
   the gate working as designed: at recall 0.667 on a ~15-case ruler, the
   remaining misses are the hard semantic ones, and no proposed rule cleared
   "improve a miss without regressing a pass or raising false positives." The
   real lever here is **growing the eval set** (more silver cases) so there's
   more signal to climb, not forcing a rubric edit through.
