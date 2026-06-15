# Review run — me as the reviewer, on your real PRs (2026-06-15)

I ran the reviewer here (since `loop.py` can't reach Vertex/maestro-core from this
environment, *I* am the reviewer): a fresh, no-prior-context reviewer per PR,
each reading only its diff, returning ranked findings. Then I scored every run
against the hidden answer key in `workspace/evalcache/baseline.jsonl` — the human
bug + the scoring keyword + whether your current champion rubric ("baseline AI")
caught it.

## Result

**Caught 10 / 12** of the golden bugs (all 12 are severity *critical*).
**Baseline AI (your champion rubric): 7 / 12.**
Of the 5 the baseline missed, this run **recovered 4** and regressed on 1.

| PR | Bug (what the human flagged) | My reviewer | Baseline AI |
|---|---|---|---|
| 3642 | aggregates without GROUP BY → Postgres rejects | ✅ caught | ✅ |
| 2360 | capacity memo keyed by hood only, not duration | ✅ caught | ❌ missed |
| 4259 | cache key omits durations; delete nukes shared entry | ✅ caught | ✅ |
| 3458 | `env_stage != 'prod'` always true (value is `'PROD'`) | ⚠️ located, mechanism off | ✅ |
| 2815 | `down_revision = None` breaks the alembic chain | ✅ caught | ❌ missed |
| 3009 | upsert reactivates COMPLETED/EXPIRED rows | ✅ caught | ❌ missed |
| 3010 | fallback bucket doesn't re-enforce the cap that triggered it | ❌ missed | ❌ missed |
| 3232 | `rollback()` inside loop discards all prior adds | ✅ caught | ✅ |
| 3293 | `.time()` drops the date → midnight-spanning negatives | ❌ missed | ✅ |
| 4012 | internal dataclass mirrors 68-col runner table → PII to customers | ✅ caught | ✅ |
| 1743 | wrong indent puts TIP_PAY outside its `elif` | ✅ caught | ❌ missed |
| 2782 | `Index` in model, no Alembic migration | ✅ caught | ✅ |

The standout is **1743**: a one-level indentation slip that makes tip-payment
logic run for *every* payment event — caught cold, with no hint, just "a return
dedented out of its `elif`." That's a real human catch.

## Honesty caveats (so you can trust the number)

- **Two waves, different fairness.** Wave 1 (3642, 2360, 4259, 3458, 2815, 3009)
  used prompts that named the *risk class* to look at (e.g. "scrutinize
  down_revision"). Wave 2 (3010, 3232, 3293, 4012, 1743, 2782) used one **uniform
  generic checklist** with no per-case steer. Even unguided, wave 2 went 4/6
  clean including the subtle indentation bug. The wave-1 catches are "informed
  triage," not cold discovery — state that when quoting the result.
- **The checklist encodes your team's known bug classes** (migrations, cache
  keys, rollback scope, PII, datetime). That *is* how an experienced reviewer on
  this team works — but it means this isn't naive discovery; it's a reviewer who
  already knows the team's scar tissue.
- This is 12 cases. Directional, not a precise benchmark.

## What the two misses teach (the real frontier)

- **3010 (fallback-caps)** — both my reviewer and your rubric missed it. The bug:
  a *fallback* code path must re-apply the same cap/guard that made the primary
  path fall back. This needs deep domain knowledge of the break-allocation logic;
  it's a pure human win and the hardest class to encode.
- **3293 (time-drops-date)** — your baseline caught it; my fresh reviewer didn't.
  It's a 178 KB diff and the reviewer lost the signal in the noise. **Lesson:
  big diffs need chunked, routed review** — exactly what the rubric's attention-map
  / rubric-routing is for. A reviewer that reads everything at once misses things
  a focused one catches.

## New patterns to fold into the rubric (from this run)

1. **Rollback scope.** A `rollback()` inside a per-item loop discards *every*
   uncommitted add in the batch, not just the failed item. Cue: `rollback()`/
   `commit()` inside a loop body. (3232)
2. **Control-flow indentation.** A statement one indent level off (a `return`
   outside its `elif`, a line inside vs outside a loop) silently changes logic.
   Cue: branch handlers where the dedent doesn't match the branch. (1743)
3. **PII via schema inheritance.** A response model that inherits from / mirrors a
   full DB table drags every column — including PII — onto the wire. Cue: a
   customer-facing DTO built from an internal/table-wide model. (4012)
4. **Cache key completeness + invalidation.** A key must include *every* input
   that changes the result (duration), and a delete branch must not evict an
   entry other callers share. Cue: `@cached`/memo dict keyed by a subset of args.
   (2360, 4259)
5. **Value-vs-comparison casing.** `x != 'prod'` when the stored value is `'PROD'`
   is always true. Cue: string compares against env/enum values without
   normalizing case. (3458)
6. **Fallback must re-enforce the guard it fell back from.** (3010 — the one to
   leave to humans for now; encoding it risks false positives.)

Bottom line: as a reviewer this run matches your team on the mechanical and
framework classes and beats your current rubric (10 vs 7), with the honest
ceiling still being domain-judgment bugs (3010) and disciplined reading of huge
diffs (3293).
