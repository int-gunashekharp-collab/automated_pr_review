# Reviewing like a human — run on your real PRs

Date 2026-06-15. I reviewed real code your team reviewed, found the problems
myself, then checked against what your reviewers actually wrote, and distilled
the method. Source data: `workspace/silver.jsonl` (7 resolved human-caught bugs,
exact hunks + the reviewer's own words) and `workspace/diffs/` (full PR diffs).

Honesty caveat: during the earlier diagnosis I'd glimpsed short snippets of the
human comments for the 7 silver cases, so I don't claim a pristine blind score on
those — the reasoning is mine, but treat it as "method demonstration," not a
benchmark. PR #2782 below I reviewed fully blind (I knew only a label).

## Scoreboard — my review vs. the human

| Case | File | What I flagged | What the human flagged | Match | Sev (theirs) |
|---|---|---|---|---|---|
| s26 | sms_client.py | No `raise_for_status()` → `@retry` never triggers on HTTP errors; also no `timeout` | Same — call `raise_for_status()`, drop the else | ✅ caught | high |
| s103 | capacity_calculator.py | `get_shift_lunches` reads `geo_date_shifts_map`, name says "lunches" | Copy-paste: should be `geo_date_shifts_lunches_map` | ✅ caught | high |
| s103a | phonepe/provider.py | Validate the provider response before treating payment as initiated | Check `resp["success"]` is False | ✅ caught | high |
| s49 | booking/views.py | OTP as a query param leaks in URLs/logs → use a body model | Send OTP in a request body | ✅ caught | medium |
| s279 | runner/schemas.py | `model_validator(mode="after")` must use `self`, not `(cls, values)` | Same — reference attrs via `self` | ✅ caught | high |
| s141 | payment/service.py | `datetime.utcnow()` deprecated → tz-aware `now()` | Same — use the `now()` util | ✅ caught | low |
| s93 | capacity_calculator.py | Smelled the global constant; raised it as a *question*, no fix | Must be the runner's avg L7 attendance, not a global | ⚠️ partial | medium |
| #2782 | booking/models.py | New `Index(...)` on `Job` with **no Alembic migration** in the PR | (blind — team's #1 rule) | ✅ caught blind | high |

6 clean + 1 partial on the silver set; 1 clean blind catch on the full diff.

## How a human on this team finds bugs — the transferable method

Humans don't scan top-to-bottom. They map each changed line to a **risk class**,
then ask the one question that class demands. The classes that actually fired:

1. **Schema change ⇒ demand the migration.** Any `Index`, column, table, or
   constraint added to a model file → "where's the Alembic migration?" Cue: the
   *model file changed structurally*. (#2782) This is the most-enforced rule.

2. **External call ⇒ check the response, the timeout, the retry.** Any
   `requests.*` / provider call → is the status checked? does `@retry` actually
   fire (it only retries on *exceptions*, so you need `raise_for_status()`)? is
   there a `timeout`? Cue: *a network call appears*. (s26, s103a)

3. **Sensitive data ⇒ trace where it travels.** An OTP/token must never ride in
   a URL/query string. In FastAPI a scalar `otp: str` on a POST *is* a query
   param → move it to a body model. Cue: *secret + transport*. (s49)

4. **Constant where a variable belongs (domain reasoning).** A hardcoded/global
   value inside a per-entity calculation → "should this vary per runner / per
   geo?" Cue: *global value in a per-entity formula*. (s93) **This is the
   hardest one and needs business context — see below.**

5. **Framework-version idioms.** Spotting an old idiom in a new world:
   `utcnow()` (deprecated, naive) → tz-aware `now()`; Pydantic-v2
   `model_validator(mode="after")` receives the instance, so use `self` not
   `(cls, values)`. Cue: *pattern memory of the framework's evolution*. (s141, s279)

6. **Name-vs-behavior mismatch / copy-paste.** The name says X, the code does Y
   (`get_shift_lunches` reading the shifts map; `_intiate` typo). Cue: *read the
   identifier, then check the body honors it*. (s103)

7. **Cut to the idiom.** "No need for the else"; redundant double dict lookup.
   Cue: *more code than the idiom requires*. (s26, s103)

## Where humans still beat the model (the honest ceiling)

s93 is the tell. Catching it requires knowing that runner capacity should reflect
*that runner's own* recent attendance, not a company-wide constant — pure domain
knowledge that lives in people's heads, not in the diff. The mechanical and
framework classes (1, 2, 3, 5, 6, 7) generalize into rules cleanly; the
domain-invariant class (4) is where a rubric under-catches. This matches the
system's own architecture note: with the team as the only oracle, it learns to
match the team on mechanical classes and lags on domain judgment.

## Severity calibration — a learned, team-specific scale

My instinct over-rated convention issues. The team's actual scale:

- **high**: missing migration, unchecked payment response, missing
  `raise_for_status`, Pydantic-validator correctness.
- **medium**: OTP-in-query, domain-constant mistakes.
- **low**: `utcnow()` deprecation (cleanup, not a live bug).

Learning *this scale* — not just the bug classes — is part of reviewing like
this team. A reviewer who flags `utcnow` as high and a missing migration as low
is technically right and practically miscalibrated.

## What this means for the loop

These seven heuristics, with their **cues**, are exactly what the proposer should
encode into the rubric — and #4 (domain invariants) is the class to *stop*
forcing into rules, since it's where false positives come from. The durable lever
remains growing the silver eval so classes 1-3/5-7 get more signal, while domain
judgment stays a human (or future thin-human-signal) job.
