# pr-review-loop — a self-improving PR reviewer

A loop that keeps making your existing Gemini PR reviewer better by learning from
your team's **real human review comments**, and reports what it did every hour.

It runs from **this folder, which lives outside maestro-core**. It only ever
*reads* maestro-core (the mined corpus, the evals, the scripts, the conventions
skill) and writes nothing back into it. Your repo is never modified.

## Where things live

- **This folder** (isolated): all the loop's code, plus everything it generates
  under `workspace/` and `reports/`.
- **maestro-core** (read-only, resolved from `$MAESTRO_ROOT`): the reviewer brain
  (`scripts/gemini_vertex.py`), the quality bar (`scripts/eval_pr_reviewer.py` +
  `evals/pr-review/golden.jsonl`), the conventions skill it tunes a *copy* of,
  and the learning source (`docs/.pr-review-corpus/`).

If maestro-core isn't at `~/snabbit chatbot/maestro-core`, set its path once:

```bash
export MAESTRO_ROOT="/path/to/maestro-core"
```

## What each iteration does

It mutates a **copy** of the champion rubric one of three ways, then promotes
only if every gate passes:

1. **Learn from humans (corpus).** Extract real review comments from
   `docs/.pr-review-corpus/` (the 5,349-comment `review_comments.jsonl` + which
   ones authors actually acted on), have Gemini 3.1 Pro understand the recurring
   pattern, and add a rule. The eval is the **safety gate**: a mined human rule
   is incorporated only if it regresses nothing — so the corpus is the *source*
   of improvements and your evals are the *guardrail*.
2. **Fix golden misses (propose).** Target the train-split bugs the reviewer
   misses, and stop known false positives.
3. **Consolidate.** Periodically shrink the rubric losslessly so it doesn't bloat.

Then it screens cheaply, confirms on the full golden set with majority voting,
and promotes only through `metrics.decide()`.

## How it avoids fooling itself

- **Overfitting / leakage** — a held-out **train/validation split**. The proposer
  only sees *train* misses; *val* recall (never trained on) is reported every hour
  as the honest number.
- **Regression** — any case the champion caught that flips to a miss rejects the
  promotion.
- **Variance** — promotion decisions use majority vote over `CONFIRM_TRIALS`.
- **Precision** — candidates review the *fixed* PR diffs; re-flagging an
  already-resolved bug is a false positive, and promotions can't raise the FP
  rate. Those FPs are fed back so the proposer can propose subtractions.
- **Bloat** — a hard size budget plus the consolidation pass.

## Run it (always-on, local)

Prereqs: `gcloud auth application-default login`, `pip install google-genai`,
`gh` authenticated, and `MAESTRO_ROOT` pointing at maestro-core.

```bash
python3 loop.py                 # runs forever, reports hourly
python3 loop.py --max-iters 5   # bounded first try
python3 loop.py --fresh         # re-seed from the live skill
python3 loop.py --list-history
python3 loop.py --rollback 0007-r0.833
```

It resumes across restarts (`workspace/state.json`), including its place in the
corpus.

## Run it (scheduled, CI)

`workflow/pr-review-loop.yml` is a bounded cron job using the same Vertex WIF
auth as your other workflows. It expects this loop folder checked out alongside
maestro-core; set `MAESTRO_ROOT` in the job. It uploads the improved rubric +
reports as an artifact and never edits the live skill.

## Hourly reports

Each digest lists what was promoted (golden-bug rule / human-corpus rule /
consolidation), the champion's **train vs. held-out val** recall, rubric size,
precision FP rate, and **how much of the human corpus has been read and
incorporated**. Written to `reports/hourly/<ts>.md` and `reports/LATEST.md`. Set
`LOOP_REPORT_WEBHOOK` to a Slack incoming webhook to have it pushed to you.

## Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `MAESTRO_ROOT` | `~/snabbit chatbot/maestro-core` | read-only path to the repo |
| `GEMINI_MODEL` | `gemini-3.1-pro-preview` | Vertex model |
| `GOOGLE_CLOUD_PROJECT` | `snabbit-ai-productivity` | Vertex project |
| `LOOP_CORPUS_EVERY` | `2` | learn-from-corpus cadence (0 = off) |
| `LOOP_CORPUS_BATCH` | `120` | human comments understood per pass |
| `LOOP_VAL_FRACTION` | `0.33` | held-out validation share |
| `LOOP_CONFIRM_TRIALS` | `3` | majority-vote trials on promotion |
| `LOOP_NOISE_TOLERANCE` | `0.10` | allowed added chatter on passing cases |
| `LOOP_MAX_SKILL_CHARS` | `60000` | hard rubric size budget |
| `LOOP_CONSOLIDATE_EVERY` | `5` | shrink-pass cadence |
| `LOOP_PRECISION_EVERY` | `3` | FP-rate gate cadence |
| `LOOP_REPORT_INTERVAL_SEC` | `3600` | digest cadence (0 = every iteration) |
| `LOOP_REPORT_WEBHOOK` | — | Slack-compatible webhook |
| `LOOP_MAX_ITERS` | `0` | 0 = run forever |
| `LOOP_BEAM` | `2` | proposals per propose-iteration (beam search) |
| `LOOP_PLATEAU_ITERS` | `6` | non-promotions before reflect mode (0 = off) |
| `LOOP_FAILED_SHOWN` | `8` | own failed attempts shown to the proposer |
| `LOOP_SYNTH_EVERY` | `4` | corpus shadow-eval cadence (0 = off) |
| `LOOP_SYNTH_SAMPLE` | `12` | human-caught bugs scored per shadow pass |
| `LOOP_MODEL_RETRIES` | `2` | extra tries on Vertex flake (backoff) |
| `LOOP_MAX_CALLS_PER_DAY` | `0` | daily Gemini-call budget (0 = unlimited) |
| `LOOP_SILVER_EVERY` | `8` | silver-harvest cadence (0 = off) |
| `LOOP_SILVER_BATCH` | `6` | max silver cases admitted per harvest |
| `LOOP_SILVER_MAX` | `30` | cap on active silver cases (eval cost) |
| `LOOP_SILVER_MIN_CONF` | `0.7` | min formaliser confidence to admit |
| `LOOP_DASH_PORT` | `8123` | default port hint for the dashboard server |

## Cost note

Majority-voted confirms re-run the golden set `CONFIRM_TRIALS` times, so a
confirm is ~`18 × trials` Gemini reviews. Screen-then-confirm keeps most
iterations cheap, but this loop spends Vertex tokens continuously by design.
Bound it with `LOOP_MAX_ITERS` / `LOOP_MIN_ITER_GAP_SEC`, drop `CONFIRM_TRIALS`,
or run the scheduled workflow instead of the daemon.

## Known limitations (honest)

- A corpus rule that doesn't map to a golden case is accepted on a "do no harm"
  basis (no regression), not a measured recall gain — the size budget +
  consolidation keep that from bloating the rubric.
- The split reduces but doesn't eliminate leakage; with 18 golden cases, `val`
  is a directional signal, not a precise number.
- Precision and fixed-diff checks need `gh` (network); cases whose diffs can't be
  fetched are skipped, not penalised.

## Test it

```bash
python3 smoke_test.py   # no Vertex/network/real-corpus; exits non-zero on failure
```

It self-tests even on a machine WITHOUT maestro-core: `LOOP_ALLOW_STUB=1` (set by
the test) falls back to `stub_harness/`, a faithful offline stand-in. A real run
never uses the stub — without that flag a missing maestro-core fails loudly.

## How it gets smarter than a single prompt ever could

- **Beam search** (`LOOP_BEAM`, default 2): each propose-iteration generates
  several DIFFERENT candidate patches, screens each cheaply, and only the best
  survivor pays for the expensive full confirm.
- **Attempt memory** (`workspace/attempts.jsonl`): every patch ever tried is
  fingerprinted. Exact re-proposals are skipped before any eval spend, and the
  proposer is shown its recent failures + rejection reasons so it changes
  strategy instead of circling.
- **Reflect mode** (`LOOP_PLATEAU_ITERS`): after N consecutive non-promotions
  the beam widens and the prompt demands a fundamentally different approach
  (different file, do-not-flag subtraction, severity recalibration).
- **Corpus shadow recall** (`LOOP_SYNTH_EVERY`): the reviewer is periodically
  scored against REAL resolved human review comments + their diff hunks — "of
  the bugs your humans caught, what fraction does the AI catch?" It's the
  human-parity scoreboard over thousands of samples, reported with the honest
  caveat that keyword-matching is directional. It NEVER gates promotions.
- **Honest statistics**: recall numbers in reports carry Wilson 95% confidence
  intervals (18 cases is a small ruler — the reports say so).

## The eval set grows itself (silver cases)

The golden set is a fixed 18-case ruler; the loop saturates it quickly. Every
`LOOP_SILVER_EVERY` iterations (default 8) the loop harvests the corpus for
RESOLVED human comments (a real bug a human caught and the author fixed), has
Gemini formalise the best into machine-checkable eval cases — one-line bug,
2-4 distinctive `must_match_any` keywords, severity, confidence — and admits
the survivors as **silver cases** in `workspace/silver.jsonl`, with their diff
hunk frozen inline (offline, deterministic, no `gh` refetch).

Silver cases are full citizens: they join train/val (same deterministic split
rule), the regression guard, and every promotion gate. On admission the
champion is immediately measured on them so champion vs candidate always
compare on the same, grown set. The ruler gets longer every week your team
reviews code — that, not any single rubric edit, is what raises the ceiling.

Safeguards: resolved-only sources, confidence + keyword-validity filters,
fingerprint dedupe, `LOOP_SILVER_MAX` cap (each active case costs
`CONFIRM_TRIALS` reviews per confirm), and a paper trail:

```bash
python3 loop.py --harvest-silver     # one manual harvest pass
python3 loop.py --list-silver        # active + retired cases
python3 loop.py --retire-silver ID   # kick a badly-formalised case out
```

`reports/silver/candidates.jsonl` always holds golden.jsonl-compatible lines —
promote the ones you trust into maestro-core's real golden set yourself; the
loop never writes there. A machine-formalised case can be imperfect: a dead
keyword set lowers absolute recall but not decisions (champion and candidate
share the set), and `--list-silver` + the dashboard matrix make dead rows easy
to spot and retire.

## Status, dashboard, adoption

```bash
python3 loop.py --status            # live phase, champion metrics, last decisions
python3 dashboard_server.py         # live dashboard (SSE) — what it's thinking now
python3 dashboard_server.py --demo  # try the dashboard with synthetic data
python3 loop.py --export-proposal   # (re)write reports/proposal/ adoption bundle
```

### The live dashboard

`dashboard_server.py` (stdlib only, zero deps) serves `dashboard.html` at
`http://127.0.0.1:8123/` and streams updates over SSE (falls back to 2s polling).
It is read-only: it never writes to the workspace and never touches maestro-core.

What it shows, live:

- **Phase pipeline** — plan → extract → propose → screen → confirm → precision →
  decide, with per-case/per-trial progress bars during evals, beam index, and
  plateau state. Stale heartbeat (>2 min) flips the dashboard to "loop offline".
- **Cortex panel** — the latest Gemini exchange: its rationale typewritten as it
  lands, raw model output + prompt head expandable, and a clickable history.
- **Champion vitals** — recall ring, train vs held-out val bars, FP rate, noise,
  rubric size vs budget, corpus progress, human-parity shadow recall, attempt
  memory, session totals.
- **Recall trajectory** — train/val/overall per decided iteration with promotion
  markers and hover detail.
- **Golden-case matrix** — every case × recent iterations (pass/fail/flip), plus
  the current champion column.
- **Promotion gates** — the last verdict decomposed gate by gate (regression /
  size / noise / precision / kind-specific), failures highlighted with reason.
- **Decision feed, rubric anatomy, promotion history** — ledger stream, per-file
  rubric sizes, and one-click copy of `--rollback` commands.

Data sources (all written by the loop itself, all inside `workspace/`):
`status.json` (heartbeat via `telemetry.py`), `thoughts.jsonl` (Gemini prompt/
response heads), `ledger.jsonl`, `state.json`, `attempts.jsonl`, `history/`.

Flags: `--port` (default `LOOP_DASH_PORT`, 8123) · `--workspace PATH` ·
`--demo [--speed 0.5]` (synthetic live data, no Vertex/loop needed) · `--no-open`.

Every promotion auto-refreshes `reports/proposal/`: a PR-ready `PROPOSAL.md`,
a reviewable `rubric.diff` (live skill → champion), and the full
`champion-skill/` ready to copy. Adoption stays a HUMAN decision — the loop
still never writes into maestro-core.

## Autonomous development with Ralph (scripts/ralph/)

The loop's remaining roadmap (outcome mining, beyond-humans counter,
counterfactual replay, severity weighting, rubric routing) is encoded as
right-sized stories in `scripts/ralph/prd.json`, runnable with the
[Ralph pattern](https://github.com/snarktank/ralph): fresh **Gemini CLI**
instances (Gemini 3.1 Pro on Vertex — the same model/project as the loop)
implement one story each, gated by this repo's offline smoke test, with a
runner-level guard that aborts if maestro-core ever goes dirty.

```bash
# one-time
npm install -g @google/gemini-cli
git init && git add -A && git commit -m "baseline before ralph"
# auth: uses Vertex via the same gcloud ADC the loop already uses

./scripts/ralph/ralph.sh 10        # up to 10 story-iterations
cat scripts/ralph/progress.txt     # learnings + smoke verdict per iteration
```

The prompt lives in `scripts/ralph/prompt.md`; model override:
`RALPH_MODEL=... ./scripts/ralph/ralph.sh`. (If your gemini-cli version
renamed `--yolo`, use its equivalent approval flag.)

Note: Ralph develops THIS codebase. The rubric-tuning loop itself stays
`loop.py` — its eval-gated promotions are stricter than any freeform agent
loop and should not be replaced by one.

## Ops hardening

Single-instance lock (`workspace/loop.lock`, stale locks auto-cleared), atomic
`state.json` writes, bounded Vertex retries with backoff, FP-memory/plateau/
shadow-cursor all persisted across restarts, and an optional daily call budget
(`LOOP_MAX_CALLS_PER_DAY`) that idles the loop instead of overspending.

**Eval checkpointing (crash = nothing lost):** every evaluation (baseline,
screens, confirms) checkpoints per CASE to `workspace/evalcache/`, keyed by a
fingerprint of the exact rubric text + trial count. Kill the loop mid-baseline
and restart: already-reviewed cases are reused for free ("resumed from
checkpoint: N reused"), only the remainder is paid for. A different rubric can
never reuse another rubric's results; errored cases always retry; the cache is
bounded (oldest files pruned). Env safety: `GOOGLE_CLOUD_PROJECT`/`GEMINI_MODEL`
are auto-injected from config, and a baseline where nothing scores aborts
loudly instead of burning iterations.
