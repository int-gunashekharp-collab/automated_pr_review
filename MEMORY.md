# Project memory — self-improving PR-reviewer loop

This file is the durable memory + conversation log for this project. Read it
first to resume work. It records what we're building, the hard rules, the
decisions made, the current state, and what's still open.

Last updated: 2026-06-11.

---

## TL;DR (current state)

- We built a **self-improving PR-reviewer loop** that uses **Gemini 3.1 Pro on
  Vertex** to keep improving maestro-core's AI code-review rubric, learning from
  the team's **real human review comments**, gated by the existing eval harness.
- It lives in an **isolated folder OUTSIDE maestro-core** and only ever *reads*
  maestro-core. maestro-core is never modified.
- Status: built, corpus-integrated, and **smoke test passes 31/31** (fully
  offline — no Vertex/network needed; runs even without maestro-core via the
  opt-in stub harness). Was 19/19 before the turn-8 upgrade wave.
- v2 (turn 8): beam search, attempt memory + reflect mode, corpus shadow
  recall, Wilson CIs, proposal exporter, lock/atomic-state/retries/budget
  hardening, live dashboard (`dashboard_server.py` + `dashboard.html` +
  `telemetry.py`).
- Isolated folder: `~/testing workflow/src/data/loop_pr_reviewer`
  (this folder). maestro-core: `~/snabbit chatbot/maestro-core` (read-only).

---

## Hard rules (do not break)

1. **Never write anything inside maestro-core.** Not even a new folder. It is a
   read-only input. (We learned this the hard way — see log, turn 5.) Everything
   the loop generates goes under this folder's `workspace/` and `reports/`.
2. The loop reads maestro-core via `$MAESTRO_ROOT` (default
   `/Users/gunashekharp/snabbit chatbot/maestro-core`).
3. Promotions stay in this folder's `workspace/champion-skill/` for human review.
   The loop does NOT auto-edit the live conventions skill or open PRs (that
   "publish back" step is intentionally not built — see Open items).

---

## What it is / why

maestro-core already had a mature PR-review setup: a Gemini reviewer in shadow
mode (`auto-review.yml`), a weekly miner of human review comments
(`mine-pr-reviews.yml` → `docs/.pr-review-corpus/`), a conventions skill the
reviewer follows, and a recall eval (`evals/pr-review/golden.jsonl`). What was
missing was the **loop that closes the feedback**: measure misses → reason about
them with Gemini → propose a rubric edit → test it → keep it only if it actually
improves. That's what this project adds, in isolation.

## Architecture

### Reads from maestro-core (read-only inputs)

- `scripts/gemini_vertex.py` — the reviewer brain (Gemini 3.1 Pro on Vertex).
- `scripts/eval_pr_reviewer.py` — the eval harness (imported as a library; its
  write paths are redirected into this folder's workspace).
- `evals/pr-review/golden.jsonl` — 18 historical, human-verified bugs = the
  quality bar.
- `.claude/skills/maestro-review-conventions/` — the rubric the loop tunes a
  COPY of (SKILL.md + references/).
- `docs/.pr-review-corpus/` — the mined human review corpus, the learning source:
  `review_comments.jsonl` (7,580 comments, 5,349 substantive human ones) and
  `review_threads.jsonl` (resolution signal — did the author act on it?).
- `.github/workflows/auto-review.yml` — reference for the production reviewer
  config (4-pass workflow + maestro-docs MCP + Vertex WIF auth).

### This folder's files

- `config.py` — all paths + env knobs; resolves `MAESTRO_ROOT`; everything it
  writes is under `LOOP_DIR`.
- `dataset.py` — load golden cases, deterministic train/val split, diff prefetch.
- `corpus.py` — extract substantive human comments from the mined corpus,
  windowed by a cursor so it works through the whole corpus over time.
- `harness_bridge.py` — imports the real eval harness read-only; `evaluate()`
  with majority-vote trials; redirects all harness writes into the workspace.
- `proposer.py` — Gemini calls: `propose_edit` (fix golden misses + stop FPs),
  `propose_from_corpus` (understand human comments → rule), `propose_consolidation`
  (shrink the rubric).
- `patcher.py` — apply a JSON patch (append / create / rewrite) to the candidate
  skill copy; path-validated.
- `metrics.py` — pure promotion gates: `decide()` (regression, noise, size,
  precision, val-not-down, kind-specific gains), split_recall, skill_size, etc.
- `precision.py` — false-positive gate: review the *fixed* PR diffs; re-flagging
  a resolved bug = a false positive.
- `reporter.py` — hourly digest (train/val recall, FP rate, rubric size, corpus
  progress, what was promoted); optional Slack webhook.
- `loop.py` — the orchestrator + CLI (`--max-iters`, `--fresh`, `--once`,
  `--list-history`, `--rollback`).
- `smoke_test.py` — full offline end-to-end test (stubs Gemini/diffs/corpus).
- `attempts.py` — attempt memory: fingerprint every proposed patch, skip exact
  re-proposals before any eval spend, feed recent failures back to the proposer.
- `synthetic.py` — corpus shadow eval: score the champion against real RESOLVED
  human comments + hunks ("human-parity scoreboard"; reported, never gates).
- `exporter.py` — adoption bundle: `reports/proposal/` with PROPOSAL.md,
  rubric.diff (live → champion) and the champion copy; auto-refreshed on every
  promotion. Adoption stays manual.
- `telemetry.py` — crash-proof status heartbeat (`workspace/status.json`) +
  Gemini thought capture (`workspace/thoughts.jsonl`) for the dashboard.
- `dashboard_server.py` + `dashboard.html` — zero-dependency live dashboard
  (SSE + demo mode): current phase, what Gemini is thinking, recall trajectory,
  gates, decision feed.
- `stub_harness/eval_pr_reviewer.py` — offline stand-in for the real harness,
  used ONLY with `LOOP_ALLOW_STUB=1` (smoke test/CI without maestro-core).
- `workflow/pr-review-loop.yml` — bounded scheduled CI runner (copy into a repo's
  `.github/workflows/` to activate; uses Vertex WIF; uploads artifacts; never
  edits the live skill).
- `README.md` — user-facing docs.
- Runtime (gitignored): `workspace/` (champion-skill, candidate-skill, diffs,
  runs, history, ledger.jsonl, state.json) and `reports/`.

## How the loop works

Each iteration mutates a COPY of the champion rubric one of three ways, then
promotes only if every gate in `metrics.decide()` passes:

1. **corpus** — extract human comments → Gemini understands the recurring pattern
   → adds a rule. The eval is the safety gate: incorporate iff nothing regresses.
2. **propose** — fix the train-split golden bugs the reviewer misses (+ stop
   known false positives).
3. **consolidate** — periodically shrink the rubric losslessly.

Then: cheap **screen** (1 trial) → **confirm** on the full golden set with
majority voting (`CONFIRM_TRIALS`, default 3) → **decide**.

### Guards against self-deception (the important part)

- **Held-out train/val split** — proposer only sees TRAIN misses; VAL recall
  (never trained on) is the honest reported number.
- **Per-case regression guard** — any previously-passing case flipping to a miss
  rejects the promotion.
- **Variance** — promotions decided by majority vote over trials.
- **Precision gate** — candidates can't raise the false-positive rate on fixed
  diffs; FP examples are fed back so the proposer can propose subtractions.
- **Anti-bloat** — hard `MAX_SKILL_CHARS` budget + the consolidation pass.
- **Noise guard** — measures ADDED chatter only on cases the champion already
  passed (catching a new bug legitimately adds findings, so the global mean is
  the wrong thing to gate on — this was a real bug we found and fixed).

## How to run / test

```bash
export MAESTRO_ROOT="/Users/gunashekharp/snabbit chatbot/maestro-core"
cd "~/testing workflow/src/data/loop_pr_reviewer"
gcloud auth application-default login && pip install google-genai   # for real runs
python3 smoke_test.py            # offline self-test (no Vertex/network) — 31/31
python3 loop.py --max-iters 5    # bounded real run; drop flag for always-on
python3 loop.py --status         # one-glance live status (terminal)
python3 dashboard_server.py        # live dashboard → http://127.0.0.1:8123/
python3 dashboard_server.py --demo # dashboard preview w/ synthetic data (no loop)
python3 loop.py --list-history   # promotion snapshots
python3 loop.py --rollback <id>  # revert a promotion

# In-house ralph builder (autonomously implements the prd.json roadmap)
python3 scripts/ralph/ralph.py --selftest  # offline proof (no Vertex/network) — 14/14
python3 scripts/ralph/ralph.py 5           # real run: build up to 5 stories on Vertex
./scripts/ralph/ralph.sh 5                 # same thing (thin wrapper)
```

Model: `gemini-3.1-pro-preview` on Vertex, project `snabbit-ai-productivity`,
GLOBAL endpoint. (Note: `gemini-3-pro-preview` was discontinued 2026-03-26.)

---

## Conversation log (session of 2026-06-11)

1. **Request**: build an always-running, self-improving PR-reviewer loop on
   Gemini 3.1 Pro / Vertex. → Verified the model exists (search), explored
   maestro-core, found the existing review infra. Asked 3 scoping questions.
   User chose: isolated folder + hourly reports (don't edit existing
   architecture), both runtimes (local daemon + CI), recall + noise-guard
   fitness. Built v1 of the loop. Smoke passed.
2. **"any improvements u think"** → Proposed 6: held-out val (overfitting),
   variance/trials, hard regression guard, anti-bloat/consolidation, real
   precision gate, frozen diffs + rollback.
3. **"build all"** → Built them all (dataset/metrics/precision modules, trials,
   consolidation, history/rollback/ledger). Found and fixed a real flaw: the
   noise guard was penalising the loop for catching new bugs. Smoke 19/19.
4. **"integrate the current architecture so we don't waste findings"** → Asked
   which direction. User pointed at `evals/` and said: extract PR comments,
   understand human comments, incorporate, build. → Verified the corpus
   (5,349 human comments). Built the corpus extractor + corpus-driven proposer +
   corpus decide gate; began wiring the loop.
5. **"don't change any file inside maestro-core; make changes in an isolated
   folder where it will run"** (said twice — important) → Got delete permission,
   removed the `pr-review-loop/` folder I'd wrongly created inside maestro-core,
   relocated everything to the connected isolated folder
   `~/testing workflow/src/data/loop_pr_reviewer`, repointed `config.MAESTRO_ROOT`,
   finished the corpus wiring (loop/reporter/smoke/README). Smoke 19/19. Proved
   maestro-core clean (`git status`).
6. **"small futuristic dashboard to see what the LLM is thinking + progress"** →
   Gave the concept: the loop ALREADY emits everything a dashboard needs
   (`ledger.jsonl`, `state.json`, `runs/*.md`, `reports/`, `history/`); main
   addition would be a small `status.json` phase heartbeat for live state. Tech
   options: self-contained `dashboard.html` reading a generated JSON (simplest),
   or a tiny FastAPI + SSE server that streams "thoughts" live. NOT yet built.
7. **"create a file to store this memory and conversation"** → this file.
8. **"make this the best in the world… you take all the decisions, add things,
   don't delete, keep improving"** (2026-06-11, later session) → upgrade wave,
   all additive, gates semantics unchanged. Two sessions worked the folder in
   parallel (telemetry + dashboard came from the sibling session; integrated,
   not duplicated). Added: **beam search** (`LOOP_BEAM` proposals/iteration,
   screen all, confirm only the best), **attempt memory + reflection**
   (`attempts.py`: fingerprint dedupe before eval spend; failed attempts fed
   back into the proposer prompt), **plateau reflect mode** (widen beam +
   demand a different strategy after `LOOP_PLATEAU_ITERS` rejections),
   **corpus shadow recall** (`synthetic.py`: score champion on real resolved
   human comments — human-parity metric, reported only, NEVER a gate),
   **Wilson 95% CIs** in reports, **proposal exporter** (`exporter.py` →
   `reports/proposal/` bundle, auto-refreshed on promotion; adoption stays
   manual), **ops hardening** (lock file, atomic state, Vertex retries w/
   backoff, FP-memory/plateau/shadow-cursor persisted, optional
   `LOOP_MAX_CALLS_PER_DAY` budget brake), **stub harness**
   (`stub_harness/`, only with `LOOP_ALLOW_STUB=1`, so smoke runs anywhere),
   new CLI `--status` / `--export-proposal`, severity-ordered miss targeting.
   Smoke extended 19 → **31 checks, all passing** (verified offline via stub).
9. **Dashboard session (sibling, same day) — what it built + integration fixes.**
   Built `telemetry.py` (atomic `status.json` heartbeat at 17 loop phase points,
   per-case/per-trial eval progress from harness_bridge, `thoughts.jsonl` capture
   at all 3 proposer call sites — crash-proof, smoke-redirect-safe),
   `dashboard_server.py` (stdlib snapshot API + SSE + `--demo` simulator +
   `--workspace`; read-only), `dashboard.html` (self-contained dark
   mission-control UI; SSE w/ polling fallback; offline detection; surfaces beam/
   plateau/shadow/attempts too). Cross-session integration fixes, all verified by
   the 31/31 smoke + demo-mode end-to-end run:
   - `exporter.py` had a SyntaxError (starred unpack inside a ternary) — parens.
   - `run_iteration` returned 2-tuples at the decide stage while the driver
     unpacked 3 → EVERY confirmed decision would have crashed (no promotion
     possible). Fixed to `(eval, fp, accepted)`.
   - Confirm-stage outcomes are now recorded in attempt memory (the beam only
     recorded pre-confirm failures, so confirm-rejected patches could have been
     re-proposed forever).
   - `smoke_test.py` now redirects the NEW config paths (ATTEMPTS_FILE,
     STATUS_FILE, LOCK_FILE, PROPOSAL_DIR) into its sandbox — without this the
     test wrote attempts into the REAL workspace (isolation hole, caused a
     phantom iter-1 error).
10. **"make this html look more good, better color palettes" (/canvas-design)** →
   restyled `dashboard.html` under a written design philosophy
   (`DESIGN_PHILOSOPHY.md`, movement: "Phosphor Ledger"): blue-ink neutrals
   (#0a0d15 base, dot lattice, layered ink-on-ink surfaces), five calibrated
   phosphors with fixed meanings (ice #7cd6ff = live/instrument, mint #5fe3b2 =
   verified gain, coral #ff8b7e = loss, gold #f4c06a = caution, iris #b7a4ff =
   human corpus), one aurora gradient reserved for the pipeline seam + title +
   recall ring, sans (labels/judgments) + mono (evidence/numbers, tabular)
   type pairing, numbered plates 01–08 on panels, pill chips, neon glow removed,
   thin-stroke chart w/ mint area fill, breath/halo live indicator,
   reduced-motion support. Server/JS contract unchanged; verified serving 200
   in demo mode.
11a. **"what addition would be a game changer" → SILVER EVAL built (the eval set
   grows itself).** New `silver.py`: every `SILVER_EVERY` iters (default 8) the
   loop harvests RESOLVED human comments from the corpus, ONE Gemini call
   formalises a batch into eval cases (bug, 2-4 distinctive must_match_any
   keywords anchored in the evidence, severity, confidence), gates admission
   (resolved-only, conf ≥ SILVER_MIN_CONF, keyword validity, fingerprint
   dedupe, SILVER_MAX cap) and appends to `workspace/silver.jsonl` with the
   diff hunk FROZEN inline. Silver cases are FULL eval citizens: composed into
   `cases` at startup and mid-run, split via `dataset.assign_new_ids` (same
   hash rule), and on admission the champion is immediately re-measured on the
   new cases (`_merge_eval`) so champ vs candidate always compare on the same
   grown set — no spurious gate rejections. Inline-diff wrapper `eff_get_diff`
   keeps golden cases on gh fetch. Persisted `silver_offset`; ledger records
   `kind: silver-harvest, status: info` with eval_set counts. CLI:
   `--harvest-silver / --list-silver / --retire-silver ID`; `--status` shows
   silver stats. Manual promotion path: `reports/silver/candidates.jsonl`
   (golden.jsonl-compatible; human copies into maestro-core — loop still never
   writes there). Dashboard: eval-set line in vitals, violet `·s` rows in the
   matrix, violet "info" feed items, silver in snapshot + demo (3 fake cases).
   Smoke 31→37 checks, all passing offline. Rationale: the 18-case ruler was
   the system's ceiling; now the ruler grows weekly with the team's reviews.
   Roadmap still open (logged): production outcome feedback (act-on/dismiss +
   caught-beyond-humans counter), counterfactual weekly replay champion-vs-live
   on real PRs, severity-weighted scoring, rubric routing by path, FP verifier.
11b. **"integrate snarktank/ralph?"** → assessed + scaffolded. Verdict: do NOT
   replace loop.py's orchestration with Ralph (our eval-gated promotions are
   strictly stronger than Ralph's typecheck/tests feedback); DO use Ralph to
   autonomously BUILD the roadmap. Scaffolded `scripts/ralph/`: `ralph.sh`
   (GEMINI CLI runner on Vertex — user mandated "vertex gemini 3.1 pro only";
   exports GOOGLE_GENAI_USE_VERTEXAI=true + project, model default
   gemini-3.1-pro-preview, RALPH_MODEL to override; python3 instead of jq;
   runner-level guard aborts if maestro-core goes dirty; runs the offline
   smoke after every iteration and writes GREEN/RED to progress.txt),
   `prompt.md` (per-iteration prompt with the HARD RULES + quality gate +
   project map; CLAUDE.md is just a pointer to it), `prd.json` (6 right-sized
   stories: S1 outcome mining, S2 outcome feedback, S3 caught-beyond-humans
   counter, S4 counterfactual replay, S5 severity-weighted recall, S6 rubric
   routing — all additive + env-gated + offline-smoke-tested), `progress.txt`
   (seeded with gotchas). Prereqs to run: `git init` this folder + Claude Code
   CLI. ALSO this session: eval checkpointing in harness_bridge (per-case,
   skill-fingerprint-keyed, crash-resume; smoke 37/37), GOOGLE_CLOUD_PROJECT/
   GEMINI_MODEL auto-injection (root cause of the 0/0 overnight run), dead-eval
   guard + dead-state auto-discard, dashboard errored-case honesty, JSONL
   defensive parsing in corpus/synthetic/silver, precision per-case error
   guards + heartbeats, offline threshold 240s.
11. **"lighter color palettes, more attractive and clean"** → switched the
   dashboard to the LIGHT daylight variant, "Porcelain Ledger" (addendum in
   DESIGN_PHILOSOPHY.md): porcelain paper #f5f6fa + white trays with soft
   shadows, ink text (#1a2233/#5d6b85/#98a2b8), pigments azure #1290e0 ·
   emerald #0fa573 · terracotta #e5604f · ochre #c98a0a · violet #7a5af8,
   same aurora-in-three-places rule, same semantics per hue. All dark values
   swept (grep-verified clean); demo serve re-verified 200 + valid snapshot.
12. **"integrate ralph in-house — it's also a loop; share the features/ideas"
   (2026-06-12).** Replaced the external gemini-cli shell runner with a pure
   **in-house Python builder** driven by the SAME Vertex brain the loop uses.
   Decision preserved from turn 11b: do NOT merge the two loops' gates — they
   stay distinct in WHAT they optimise (loop = rubric, eval-gated; ralph = code,
   smoke-gated) but now share one brain + config + telemetry + one `python3`
   entry point. Changes:
   - `proposer.py`: factored the Vertex call out of `_default_call_model` into a
     shared `call_vertex(prompt, log_name=...)`; `_default_call_model` is now a
     thin shim (loop behaviour byte-identical, smoke still 37/37).
   - NEW `scripts/ralph/ralph.py`: per iteration picks the highest-priority
     `passes:false` prd.json story, asks `proposer.call_vertex` for a STRICT JSON
     file-patch (`{files:[{path,action,content}], done, progress_note}`), applies
     it under hard path validation (`_safe_target`: never escapes the loop
     folder, never `.git`, never under maestro-core — symlinks collapsed via
     resolve()), gates with `py_compile` + offline smoke (`LOOP_ALLOW_STUB=1`),
     and on GREEN marks the story done + best-effort `git commit`; on RED, after
     a bounded refine round, rolls the iteration's edits back via a per-file
     snapshot (`_safe_unlink` tolerates no-delete filesystems) so the tree stays
     green. Emits `telemetry.phase`/`thought` so the builder shows on the
     dashboard. `--selftest` proves the whole harness offline (14 checks, no
     Vertex/network): path-safety rejections, a full green iteration, and the
     rollback-on-red path.
   - The maestro read-only guard is now ACTIONABLE: prints `git status --short`
     of what's dirty + the exact `checkout`/`stash`/bypass commands (the old
     runner just aborted opaquely — that was the FATAL the user hit).
   - `ralph.sh` is now a 3-line wrapper that `exec`s `ralph.py` (old
     `./scripts/ralph/ralph.sh N` command still works); `prompt.md` rewritten for
     the in-process brain + JSON-patch contract (no gemini-cli, no shell/file
     tools for the model). This removes ALL the friction the user hit: no
     `npm install -g @google/gemini-cli` (the EACCES), no manual "baseline before
     ralph" commit (`ensure_git_baseline` makes it if missing), no opaque guard.
   - Known limitation (logged for future iterations): the builder is SINGLE-SHOT
     patch-based, not a multi-turn file-editing agent — it sees a curated context
     (config.py + smoke_test.py + story-named files + repo map, capped) and
     returns one patch. A read-back/tool-call round could be added later.
13. **"integrate completely so I can view what all changed" (2026-06-13).** Wired
   the in-house builder into the live dashboard end to end, so a ralph run is
   watchable like a loop run. Three files:
   - `ralph.py` now publishes two gitignored streams under `workspace/`:
     `ralph_state.json` (roadmap snapshot — each story's status: done/building/
     red/pending, done/total, current, iteration) and `ralph.jsonl` (append-only
     per-iteration build events, each carrying the changelog + a `difflib`
     unified diff per file with +/- counts, the GREEN/RED/rejected result, note,
     and commit hash). `_change_records()` computes diffs from the per-file
     snapshot BEFORE any rollback, so a rolled-back RED attempt is still
     inspectable. `git_commit` now returns the short hash. Publishing is
     crash-proof (swallows all exceptions, like telemetry).
   - `dashboard_server.py`: `build_snapshot` gains a `ralph` block (state +
     last 30 events); both files added to `_watch_signature` so SSE pushes
     builder updates live; `run_demo_writer` seeds a realistic roadmap (S1 done
     with a real diff event, S2 building) and advances a story every ~4 demo
     iters, so `--demo` previews the builder with no Vertex.
   - `dashboard.html`: a new "Builder · ralph roadmap" panel (aurora-seam,
     Porcelain Ledger style) above the loop grid — story chips coloured by
     status (emerald done / azure-pulsing building / terracotta red / muted
     pending), a done/total progress bar, and a "what ralph changed" feed of
     build events with expandable per-file unified diffs (green/red line
     tinting). The phase strip now recognises `ralph-*` phases (shows a builder
     message instead of the loop pipeline). Hidden entirely when no ralph data.
   - Verified offline: ralph `--selftest` now 17/17 (asserts state + events +
     diff are published; selftest redirects `config.WORKSPACE` to a sandbox so
     it never pollutes the real workspace); `node --check` on the dashboard JS;
     `--demo` snapshot serves the ralph block (6 stories, 1 event, 2 diffs) and
     the page serves the builder DOM; loop smoke still 37/37.
   - To watch a real run: `python3 dashboard_server.py` (terminal 1) +
     `RALPH_SKIP_MAESTRO_GUARD=1 python3 scripts/ralph/ralph.py 5` (terminal 2).
14. **Builder reliability fix (2026-06-13).** A real run exposed the single-shot
   weakness: ralph built S1 well (2 iters, real `outcomes.py` + `loop.py`
   `--mine-outcomes`, smoke green) but then **burned iterations 3–5 on S2** with
   "brain proposed no files." Root cause from telemetry: S2 needs to edit big
   existing files (`proposer.py`/`config.py`), the model returned them as full
   17–88 KB `rewrite` `content` strings that **truncated at the output limit**,
   so `_extract_json` fell back to a fragment with no `files` — and the no-files
   path **silently skipped with no feedback**, so each fresh iteration repeated
   the identical failure. Fixes (all in `scripts/ralph/ralph.py` + `prompt.md`):
   - **Anchored `edit` action**: `{action:"edit", find, replace}` find/replace
     that must match EXACTLY ONCE — preferred over rewrites so output stays tiny
     and can't truncate. `_apply` is now **two-phase** (validate-all → write), so
     a malformed/unsafe patch writes nothing (no partial state, no rollback need).
   - **`_parse_patch`**: strips ```json fences and surfaces truncation as a clear
     error instead of a silent fragment.
   - **Unified feedback-retry loop** (`MAX_ATTEMPTS`, env `RALPH_MAX_ATTEMPTS`,
     default 3): parse-fail / no-files / unsafe-patch / RED-gate each feed their
     SPECIFIC reason back to the model and re-ask within the same iteration,
     replacing the old silent-skip + gate-only refine. Closes the wasted-
     iteration gap that lost iters 3–5.
   - **Lightweight read-back**: model returns `"files":[]` + `"need":[paths]` and
     is re-asked with those files' full contents (the documented single-shot gap).
   - `_CONTRACT` + `prompt.md` now teach edit-first, warn that big rewrites
     truncate, and document `need`. Removed the `RALPH_REFINE_TRIES` knob.
   - Verified offline: selftest **17 → 20** (adds: edit find/replace, non-unique
     anchor rejected, and feedback-retry recovering from an empty reply — the
     trace literally shows "retry 1/2 — reply had no files" then promote); loop
     smoke still green; selftest leaves the real workspace clean. Still open:
     full multi-turn agentic editing (this is robust single-shot + retries, not a
     true tool-use agent) and the loop's own `recall=None` health issue (separate).
15. **Eval-hardening + human-free core built (2026-06-13).** First increment of the
   `ARCHITECTURE.md` human-free variant, on top of the now-live loop (recall came
   back `None → 0.667` once `GOOGLE_CLOUD_PROJECT` was set):
   - **Env auto-inject confirmed already present** — `harness_bridge.py` does
     `os.environ.setdefault("GOOGLE_CLOUD_PROJECT", config.GCP_PROJECT)` (+
     `GEMINI_MODEL`), so the eval/reviewer subprocess inherits them like the
     proposer path. That is *why* the eval revived. Left as-is.
   - **Explicit eval-liveness guard** at the top of `metrics.decide()` — refuses to
     promote when the candidate eval is dead/degraded (`scoreable<=0` or > half the
     cases errored). Errors aren't misses, so a dead candidate would otherwise slip
     the regression guard. Complements the existing baseline guard (loop.py: FATAL
     if baseline `scoreable==0`) and dead-state auto-discard.
   - **Golden-optional / silver-primary** — new `LOOP_SILVER_PRIMARY` knob (default
     OFF). `dataset.load_cases()` now returns `[]` (no crash) when golden is absent
     or silver-primary; `loop.py main()` no longer hard-exits on missing golden — it
     runs on the resolution-derived **silver** eval alone, with a clear
     `--harvest-silver` message when both sources are empty. Default behaviour is
     unchanged (loop still runs golden at 0.667).
   - Verified: 3 new smoke checks (liveness rejects all-errored; silver-primary and
     absent-golden `load_cases` return `[]`), full smoke green, py_compile clean;
     `__pycache__` cleared. **Still designed-not-built:** shadow deployment + the
     retrieval index. Run human-free: `python3 loop.py --harvest-silver` then
     `LOOP_SILVER_PRIMARY=1 python3 loop.py --max-iters 5`.
16. **Lifetime scorecard built (2026-06-13).** A cumulative, cross-run metric for
   "is it improving or hallucinating, OVERALL" (not per-run). New `scorecard.py`
   computes, from the resolution-derived silver eval:
   - **AUTONOMY%** = (human-flagged patterns the current champion catches UNAIDED)
     / (all-time human-flagged patterns) — the climbing improvement number.
   - **GRADUATED** = patterns first MISSED but later caught (the pure "learned from
     the human" count); **REGRESSED** = first caught, now missed (forgetting);
     **HALLUCINATION%** = precision FP rate (false alarms on fixed code);
     **BEYOND-HUMANS** = the S3 counter.
   Grounded only in REAL signals (resolved comments, re-flagged fixed code), so the
   metric can't be gamed by a hallucinating proposer; errors are NOT misses, so a
   dead eval never moves it. Persisted across runs (survives `--fresh`):
   `workspace/learning_ledger.json` (per-pattern source of truth) + append-only
   `workspace/scorecard.jsonl` (the trajectory). Wired: `scorecard.update()` at
   run-end in `run_loop` (prints the autonomy line), `python3 loop.py --scorecard`
   CLI, and a dashboard "Autonomy · learned from humans" strip with a climbing
   sparkline + graduated/hallucination/beyond-human chips + demo seed. Verified
   offline: standalone engine (50%→100% with one graduation), 4 smoke checks,
   demo snapshot serves the block (autonomy 41.5%→81.9% over 8 runs), JS valid,
   full smoke green. View it: `python3 loop.py --scorecard`.
17. **maestro-core grounding integrated (2026-06-13).** Gave the system maestro-core
   context for better PR review — two sides:
   - **Reviewer (already plumbed, now first-class):** `LOOP_WITH_MCP` /
     `LOOP_WORKFLOW` flow through `harness_bridge` into the maestro-core harness's
     **maestro-docs MCP** + 4-pass workflow. The MCP server + `with_mcp` logic live
     inside maestro-core (read-only) — the loop just flips the flags; the live MCP
     is verified on the Mac, not here.
   - **Proposer (the new build, our side):** `maestro_context.py` reads a bounded,
     **read-only** digest of maestro-core's OWN docs (architecture, conventions,
     README, auto-review.yml) and injects it into `EDIT_PROMPT` + `CORPUS_PROMPT`,
     so rubric edits cite real modules/patterns instead of generic advice. Gated by
     `LOOP_MAESTRO_CONTEXT` (default ON; `LOOP_MAESTRO_CONTEXT_CHARS=8000`); degrades
     to a no-op note when maestro-core is absent (smoke + maestro-less runs
     unaffected). Never writes to maestro-core.
   - **Dashboard:** config snapshot carries `use_mcp`/`use_workflow`/`maestro_context`;
     footer shows `grounding: reviewer·MCP + proposer·docs`.
   - Verified offline: 3 smoke checks (reads docs / tolerant when absent / prompts
     format with the field), full smoke green, py_compile + dashboard JS clean.
   - Run with full grounding:
     `LOOP_WITH_MCP=1 LOOP_WORKFLOW=1 python3 loop.py` (proposer grounding is on by
     default). Still maestro-core-side (read-only, not testable here): the MCP server.
18. **Live maestro-docs MCP client wired into proposer grounding (2026-06-13).**
   Extended `maestro_context.py` with a best-effort MCP-over-HTTP client (stdlib
   `urllib`): initialize → tools/list → tools/call, returns text or None on ANY
   failure, **falling back to local docs**. `load_context()` now prefers the LIVE
   maestro-docs MCP when `MAESTRO_DOCS_MCP_URL` + `_TOKEN` are set, else reads
   local docs. Config knobs (URL/TOKEN/TOOL/QUERY) load from a **gitignored `.env`**
   via `config._load_dotenv()` (setdefault, so shell exports win) — the token
   **never lives in code/git** (verified: `.env` is gitignored and excluded from
   `git add -A`) and is never logged. `python3 maestro_context.py --tools` lists
   the MCP's tools so the right one can be pinned (`MAESTRO_DOCS_MCP_TOOL`).
   Verified offline (stubbed): live-context path used, graceful fallback on
   failure, tolerant when nothing configured; smoke +1 (MCP-failure→docs), full
   smoke green. **LIVE test is on the Mac:** `.env` has the url+token →
   `python3 maestro_context.py --tools`, then `python3 loop.py`. Token rotation is
   pending (user will rotate after testing).
19. **"PRs reviewed completely" counter + stuck-metric diagnosis (2026-06-13).**
   User saw the scorecard "stuck" — actually it moved correctly: autonomy 100%
   (3/3 patterns) → **42.9% (3/7)** after harvesting 4 more silver patterns, i.e.
   the reviewer catches 3 of 7 human-flagged patterns and misses 4 of the new
   harder ones (exactly the intended behaviour — now there's room to learn). It's
   flat at 42.9% because the loop hasn't PROMOTED yet (0 promotions; first edit
   rejected as a regression) — honest plateau, not a bug; the maestro grounding
   should help it propose better. NEW metric: `harness_bridge` appends each FRESH
   review's PR to `workspace/review_stats.json` (`_bump_review_stats`, cumulative,
   survives champion changes); `config.REVIEW_STATS_FILE`. `scorecard` surfaces
   `prs_reviewed` (distinct) + `reviews_completed` (total) in update/lifetime/
   `format_scorecard` (`PRS REVIEWED` line) and the dashboard strip (`scPrs`).
   Backfilled from the existing evalcache → **71 reviews, 15 distinct PRs**.
   Verified: counter math, full smoke green, JS valid. Note: checkpoint reuse on
   restart won't re-count old reviews (hence the backfill); the dashboard shows the
   stat after a loop restart (new code writes it into the scorecard trajectory).

---

## Open / pending items

- **Dashboard** — ✅ BUILT (turn 8 era, sibling session): `dashboard_server.py`
  (stdlib, SSE + `--demo` mode) + `dashboard.html` + `telemetry.py` heartbeat/
  thought capture. Run `python3 dashboard_server.py`. (Original idea kept for
  context: live "what it's thinking", recall trajectory, gauges, decision feed.)
- **Publish-back outflow** — ⚙️ PARTIALLY addressed (turn 8): `exporter.py`
  auto-writes `reports/proposal/` (PROPOSAL.md + rubric.diff + champion copy)
  on every promotion — but strictly INSIDE this folder; nothing is written to
  maestro-core and no PR is opened. A true PR-opening flow remains not built;
  offer before building (and it must stay gated — never auto-merge).
- **Production fidelity** (optional, still open): run the eval with the 4-pass
  workflow + maestro-docs MCP grounding (`LOOP_WORKFLOW=1`, `LOOP_WITH_MCP=1`)
  so improvements transfer to the real CI reviewer.
- **Shadow-recall as a gate** (future idea, deliberately NOT done): corpus
  shadow recall is keyword-matched and noisy, so it stays informational. If it
  proves stable over weeks, a conservative "must not drop >X over N passes"
  gate could be considered.

## Key paths

- Isolated loop folder: `/Users/gunashekharp/testing workflow/src/data/loop_pr_reviewer`
- maestro-core (read-only): `/Users/gunashekharp/snabbit chatbot/maestro-core`
- Corpus: `<maestro-core>/docs/.pr-review-corpus/`
- Golden eval: `<maestro-core>/evals/pr-review/golden.jsonl`
- Conventions skill (copied, tuned): `<maestro-core>/.claude/skills/maestro-review-conventions/`
