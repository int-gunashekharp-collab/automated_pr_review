#!/usr/bin/env python3
"""Self-improving PR-reviewer loop — the orchestrator.

Runs from an ISOLATED folder (outside maestro-core), reading the repo read-only
and writing only under this folder. Each iteration mutates a copy of the
champion rubric in one of three ways, then promotes only if every gate passes:

  * CORPUS  — extract real human PR-review comments from docs/.pr-review-corpus,
              have Gemini understand the recurring pattern, and add a rule
              (the eval is the safety gate: incorporate iff nothing regresses)
  * PROPOSE — fix the TRAIN golden bugs the reviewer misses (+ stop known FPs)
  * CONSOLIDATE — shrink the rubric losslessly when it bloats

Run:
    python3 loop.py                 # always-on
    python3 loop.py --max-iters 3   # bounded (CI / smoke)
    python3 loop.py --fresh         # re-seed from the live skill
    python3 loop.py --list-history
    python3 loop.py --rollback 0007-r0.833
"""

from __future__ import annotations

import argparse
import atexit
import datetime
import json
import os
import shutil
import sys
import time
from pathlib import Path

import attempts
import config
import corpus
import dataset
import exporter
import harness_bridge as hb
import metrics
import outcomes
import precision
import proposer
import scorecard
import silver
import synthetic
import telemetry
from patcher import apply_patch
from reporter import Reporter


# --- skill / fs helpers -----------------------------------------------------
def snapshot_skill(src: Path, dst: Path):
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "references").mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / "SKILL.md", dst / "SKILL.md")
    for f in (src / "references").glob("*.md"):
        shutil.copy2(f, dst / "references" / f.name)


def ledger_append(rec: dict):
    rec.setdefault("ts", round(time.time(), 3))
    config.WORKSPACE.mkdir(parents=True, exist_ok=True)
    with config.LEDGER_FILE.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def snapshot_history(idx: int, recall) -> Path:
    config.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.HISTORY_DIR / f"{idx:04d}-r{recall}"
    snapshot_skill(config.CHAMPION_DIR, dest)
    return dest


def persist(champion_eval, champ_fp, champ_size, split, corpus_offset, totals, *,
            fp_examples=None, consec_rejects=0, synth_offset=0, shadow=None,
            calls_day=None, silver_offset=0):
    """Atomic state write (tmp + rename) — a crash mid-write can never leave a
    torn state.json behind. Also persists the FP memory, the plateau counter,
    the shadow-eval cursor and the daily call budget so restarts lose nothing."""
    payload = {
        "champion_eval": champion_eval, "champ_fp": champ_fp,
        "champ_size": champ_size, "corpus_offset": corpus_offset, "totals": totals,
        "train_ids": sorted(split[0]), "val_ids": sorted(split[1]),
        "model": config.GEMINI_MODEL, "project": config.GCP_PROJECT,
        "fp_examples": fp_examples or [], "consec_rejects": consec_rejects,
        "synth_offset": synth_offset, "shadow": shadow, "calls_day": calls_day,
        "silver_offset": silver_offset,
        "beyond_humans": outcomes.count_beyond_humans(),
    }
    tmp = config.STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, config.STATE_FILE)


def _today() -> str:
    return datetime.date.today().isoformat()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_lock():
    """Single-instance guard: two loops sharing one workspace would corrupt the
    champion mid-eval. Stale locks (dead pid) are taken over automatically."""
    config.WORKSPACE.mkdir(parents=True, exist_ok=True)
    if config.LOCK_FILE.exists():
        try:
            pid = int(json.loads(config.LOCK_FILE.read_text()).get("pid", -1))
        except (ValueError, json.JSONDecodeError):
            pid = -1
        if pid > 0 and pid != os.getpid() and _pid_alive(pid):
            sys.exit(f"another loop instance appears to be running (pid {pid}). "
                     f"If that's stale, delete {config.LOCK_FILE} and retry.")
    config.LOCK_FILE.write_text(json.dumps(
        {"pid": os.getpid(), "started": round(time.time(), 3)}))


def _release_lock():
    try:
        if config.LOCK_FILE.exists():
            if int(json.loads(config.LOCK_FILE.read_text()).get("pid", -1)) == os.getpid():
                config.LOCK_FILE.unlink()
    except Exception:
        pass


def _rotate(items: list[str], idx: int, n: int) -> list[str]:
    if not items or n <= 0:
        return []
    off = idx % len(items)
    return (items[off:] + items[:off])[:n]


def _subset_noise(ev: dict, ids: set[str]) -> float:
    vals = [r["findings_count"] for r in ev["per_case"]
            if r["id"] in ids and "findings_count" in r]
    return sum(vals) / len(vals) if vals else 0.0


def _merge_eval(champ: dict, add: dict) -> dict:
    """Fold a fresh evaluation of NEW (silver) cases into the champion eval so
    champion and candidates stay comparable on the same, grown set."""
    per = champ["per_case"] + add["per_case"]
    missed = champ.get("missed", []) + add.get("missed", [])
    scoreable = champ.get("scoreable", 0) + add.get("scoreable", 0)
    passed = champ.get("passed", 0) + add.get("passed", 0)
    noise_rows = [r["findings_count"] for r in per if "findings_count" in r]
    return dict(champ, per_case=per, missed=missed, scoreable=scoreable,
                passed=passed,
                recall=round(passed / scoreable, 3) if scoreable else None,
                noise=round(sum(noise_rows) / len(noise_rows), 2) if noise_rows else 0.0)


def _plan_kind(idx: int, champ_size: int) -> str:
    if config.CONSOLIDATE_EVERY > 0 and idx % config.CONSOLIDATE_EVERY == 0 and champ_size > 0:
        return "consolidate"
    if config.CORPUS_EVERY > 0 and idx % config.CORPUS_EVERY == 0 and corpus.available():
        return "corpus"
    return "propose"


# --- one iteration ----------------------------------------------------------
def _screen_candidate(idx, beam_i, patch, miss_ids, sample, cases, champion_eval,
                      *, get_diff_fn, run_reviewer_fn):
    """Apply `patch` to a fresh candidate copy and screen it cheaply.
    Returns the screen verdict + stats; the caller picks the best of the beam."""
    snapshot_skill(config.CHAMPION_DIR, config.CANDIDATE_DIR)
    changelog = apply_patch(config.CANDIDATE_DIR, patch)
    size_cand = metrics.skill_size(config.CANDIDATE_DIR)
    if size_cand > config.MAX_SKILL_CHARS:
        return {"ok": False, "why": f"screened out: skill {size_cand} > budget "
                                    f"{config.MAX_SKILL_CHARS}",
                "calls": 0, "size": size_cand, "changelog": changelog,
                "newly": set(), "noise": 0.0}
    screen_cases = [c for c in cases if c["id"] in (miss_ids | set(sample))]
    telemetry.phase("screening", iter=idx, kind="propose", beam=beam_i,
                    cases=len(screen_cases))
    cand_s = hb.evaluate(config.CANDIDATE_DIR, screen_cases,
                         label=f"i{idx}-b{beam_i}-screen", trials=config.SCREEN_TRIALS,
                         get_diff_fn=get_diff_fn, run_reviewer_fn=run_reviewer_fn)
    calls = len(screen_cases) * config.SCREEN_TRIALS
    cand_pass = metrics.passing_ids(cand_s)
    newly = miss_ids & cand_pass
    regressed = set(sample) - cand_pass
    noise = _subset_noise(cand_s, set(sample))
    quiet = metrics.noise_ok(noise, _subset_noise(champion_eval, set(sample)),
                             config.NOISE_TOLERANCE)
    ok = bool(newly) and not regressed and quiet
    why = ("" if ok else "screened out: " +
           ("no new bug" if not newly else
            "regressed sample" if regressed else "noisier"))
    return {"ok": ok, "why": why, "calls": calls, "size": size_cand,
            "changelog": changelog, "newly": newly, "noise": noise}


def _propose_beam(idx, champion_eval, champ_train_misses, fp_holder, cases, split,
                  *, call_model_fn, get_diff_fn, run_reviewer_fn, plateaued,
                  dismissed_outcomes=None):
    """Beam search: up to BEAM_WIDTH distinct proposals per iteration (deduped
    against the attempt memory), each screened cheaply; only the best survivor
    goes on to the expensive full confirm. When the loop has plateaued the beam
    widens by one and the proposer reflects on its own recent failures."""
    train_ids, _ = split
    miss_ids = {m["id"] for m in champ_train_misses}
    passing_train = sorted(metrics.passing_ids(champion_eval) & train_ids)
    sample = _rotate(passing_train, idx, config.SCREEN_PASSING_SAMPLE)
    ev = dict(champion_eval, missed_train=champ_train_misses)
    failures = attempts.recent_failures(config.FAILED_ATTEMPTS_SHOWN)
    seen = attempts.seen_fingerprints()
    width = config.BEAM_WIDTH + (1 if plateaued else 0)

    calls, dupes = 0, 0
    tried_rationales: list[str] = []
    best = None  # (sort key, patch, screen result)
    last_reject = "all beam proposals failed"
    for bi in range(1, width + 1):
        telemetry.phase("proposing", iter=idx, kind="propose", beam=bi, of=width,
                        plateau=plateaued)
        try:
            patch = proposer.propose_edit(
                config.CHAMPION_DIR, ev, false_positives=fp_holder["examples"],
                dismissed_outcomes=dismissed_outcomes,
                call_model_fn=call_model_fn, failed_attempts=failures,
                avoid=tried_rationales)
        except Exception as e:
            calls += 1
            last_reject = f"proposer error: {str(e)[:150]}"
            continue
        calls += 1
        tried_rationales.append(patch.get("rationale", ""))
        fp_id = attempts.fingerprint(patch)
        if fp_id in seen:
            dupes += 1
            attempts.record(idx, "propose", patch, "duplicate",
                            "fingerprint already tried — skipped before any eval spend")
            last_reject = "duplicate of an earlier attempt"
            continue
        seen.add(fp_id)
        try:
            sc = _screen_candidate(idx, bi, patch, miss_ids, sample, cases,
                                   champion_eval, get_diff_fn=get_diff_fn,
                                   run_reviewer_fn=run_reviewer_fn)
        except Exception as e:
            attempts.record(idx, "propose", patch, "error", str(e)[:200])
            last_reject = f"patch apply/screen error: {str(e)[:150]}"
            continue
        calls += sc["calls"]
        if not sc["ok"]:
            attempts.record(idx, "propose", patch, "screened_out", sc["why"])
            last_reject = sc["why"]
            continue
        key = (len(sc["newly"]), -sc["noise"])
        if best is None or key > best[0]:
            best = (key, patch, sc)
    return best, calls, dupes, tried_rationales, last_reject


def run_iteration(idx, kind, cases, split, champion_eval, champ_fp, fp_holder,
                  corpus_comments, reporter, *, get_diff_fn, run_reviewer_fn,
                  call_model_fn, fixed_diff_fn, plateaued=False,
                  dismissed_outcomes=None, acted_on_outcomes=None):
    """One mutation -> gate cycle. Returns (champion_eval, champ_fp, accepted)."""
    train_ids, val_ids = split
    size_champ = metrics.skill_size(config.CHAMPION_DIR)
    champ_train_misses = [m for m in champion_eval["missed"] if m["id"] in train_ids]
    beam_meta = None

    # --- build a candidate per mutation kind ---
    telemetry.phase("proposing", iter=idx, kind=kind)
    snapshot_skill(config.CHAMPION_DIR, config.CANDIDATE_DIR)
    if kind == "consolidate":
        patch = proposer.propose_consolidation(config.CHAMPION_DIR, call_model_fn=call_model_fn)
        calls = 1
    elif kind == "corpus":
        patch = proposer.propose_from_corpus(config.CHAMPION_DIR, corpus_comments,
                                             acted_on_outcomes=acted_on_outcomes,
                                             call_model_fn=call_model_fn)
        calls = 1
        if not patch.get("edits"):
            rec = {"iter": idx, "kind": "corpus", "status": "rejected", "model_calls": 1,
                   "rationale": patch.get("rationale", "no clear new pattern"),
                   "reason": "no new human pattern in this batch", "changelog": []}
            reporter.record_iteration(rec); ledger_append(rec)
            return champion_eval, champ_fp, False
    else:  # propose — beam search over multiple distinct candidates
        if not champ_train_misses and not fp_holder["examples"]:
            rec = {"iter": idx, "kind": "idle", "status": "rejected", "model_calls": 0,
                   "rationale": "no train misses / FPs — idle", "reason": "nothing to target",
                   "changelog": []}
            reporter.record_iteration(rec); ledger_append(rec)
            return champion_eval, champ_fp, False
        best, calls, dupes, tried, last_reject = _propose_beam(
            idx, champion_eval, champ_train_misses, fp_holder, cases, split,
            call_model_fn=call_model_fn, get_diff_fn=get_diff_fn,
            run_reviewer_fn=run_reviewer_fn, plateaued=plateaued,
            dismissed_outcomes=dismissed_outcomes)
        if best is None:
            rec = {"iter": idx, "kind": kind, "status": "rejected", "model_calls": calls,
                   "rationale": "; ".join(r for r in tried if r)[:400] or "(none)",
                   "reason": last_reject, "changelog": [],
                   "beam": {"width": config.BEAM_WIDTH + (1 if plateaued else 0),
                            "duplicates_skipped": dupes}}
            reporter.record_iteration(rec); ledger_append(rec)
            return champion_eval, champ_fp, False
        _, patch, sc = best
        beam_meta = {"width": config.BEAM_WIDTH + (1 if plateaued else 0),
                     "duplicates_skipped": dupes,
                     "screen_winner_caught": sorted(sc["newly"])}
        # re-materialise the WINNING candidate (the dir may hold a later loser)
        snapshot_skill(config.CHAMPION_DIR, config.CANDIDATE_DIR)

    # --- duplicate guard for single-patch kinds (saves a full confirm) ---
    if kind != "propose":
        fp_id = attempts.fingerprint(patch)
        if fp_id in attempts.seen_fingerprints():
            attempts.record(idx, kind, patch, "duplicate",
                            "fingerprint already tried — skipped before confirm")
            rec = {"iter": idx, "kind": kind, "status": "rejected", "model_calls": calls,
                   "rationale": patch.get("rationale", "(none)"),
                   "reason": "duplicate of an earlier attempt", "changelog": []}
            reporter.record_iteration(rec); ledger_append(rec)
            return champion_eval, champ_fp, False

    changelog = apply_patch(config.CANDIDATE_DIR, patch)
    size_cand = metrics.skill_size(config.CANDIDATE_DIR)
    base = {"iter": idx, "kind": kind, "rationale": patch.get("rationale", "(none)"),
            "changelog": changelog, "size": size_cand, "champ_recall": champion_eval["recall"]}
    if kind == "propose" and dismissed_outcomes:
        base["outcomes_dismissed_used"] = len(dismissed_outcomes)
    elif kind == "corpus" and acted_on_outcomes:
        base["outcomes_acted_on_used"] = len(acted_on_outcomes)
    if beam_meta:
        base["beam"] = beam_meta

    if size_cand > config.MAX_SKILL_CHARS:
        attempts.record(idx, kind, patch, "screened_out",
                        f"skill {size_cand} > budget {config.MAX_SKILL_CHARS}")
        rec = {**base, "status": "rejected", "model_calls": calls,
               "reason": f"skill {size_cand} > budget {config.MAX_SKILL_CHARS}"}
        reporter.record_iteration(rec); ledger_append(rec)
        return champion_eval, champ_fp, False

    # --- confirm on the full set, with voting ---
    telemetry.phase("confirming", iter=idx, kind=kind, cases=len(cases),
                    trials=config.CONFIRM_TRIALS)
    cand = hb.evaluate(config.CANDIDATE_DIR, cases, label=f"i{idx}-confirm",
                       trials=config.CONFIRM_TRIALS, get_diff_fn=get_diff_fn,
                       run_reviewer_fn=run_reviewer_fn)
    calls += len(cases) * config.CONFIRM_TRIALS

    # --- periodic precision gate ---
    cand_fp = champ_fp_gate = None
    if config.PRECISION_EVERY > 0 and idx % config.PRECISION_EVERY == 0:
        telemetry.phase("precision", iter=idx, kind=kind)
        prec = precision.evaluate_precision(
            config.CANDIDATE_DIR, cases[: config.PRECISION_SAMPLE], trials=1,
            get_fixed_diff_fn=fixed_diff_fn, run_reviewer_fn=run_reviewer_fn)
        calls += prec["checked"]
        cand_fp, champ_fp_gate = prec["fp_rate"], champ_fp
        if prec["fp_examples"]:
            fp_holder["examples"] = prec["fp_examples"]

    telemetry.phase("deciding", iter=idx, kind=kind)
    accept, reason = metrics.decide(
        kind=kind, champ=champion_eval, cand=cand, train_ids=train_ids, val_ids=val_ids,
        champ_size=size_champ, cand_size=size_cand, noise_tol=config.NOISE_TOLERANCE,
        max_size=config.MAX_SKILL_CHARS, champ_fp=champ_fp_gate, cand_fp=cand_fp,
        precision_tol=config.PRECISION_TOL)

    rec = {**base, "status": "accepted" if accept else "rejected", "reason": reason,
           "model_calls": calls, "cand_recall": cand["recall"], "cand_noise": cand["noise"],
           "train_recall": metrics.split_recall(cand, train_ids),
           "val_recall": metrics.split_recall(cand, val_ids), "fp_rate": cand_fp,
           "cases": {r["id"]: bool(r.get("passed")) for r in cand["per_case"]}}
    reporter.record_iteration(rec); ledger_append(rec)
    telemetry.phase("decided", iter=idx, kind=kind, status=rec["status"], reason=reason)

    if accept:
        attempts.record(idx, kind, patch, "accepted", reason)
        snapshot_skill(config.CANDIDATE_DIR, config.CHAMPION_DIR)  # promote (own copy)
        snapshot_history(idx, cand["recall"])
        new_fp = cand_fp if cand_fp is not None else champ_fp
        reporter.set_champion(cand)
        reporter.champ_size = size_cand
        reporter.champ_fp = new_fp
        return cand, new_fp, True
    # remember confirm-stage rejections too — the beam only records pre-confirm
    # failures, so without this the same patch could be re-proposed later
    attempts.record(idx, kind, patch, "rejected", reason)
    return champion_eval, champ_fp, False


# --- driver -----------------------------------------------------------------
def run_loop(*, max_iters=None, fresh=False, get_diff_fn=None, run_reviewer_fn=None,
             call_model_fn=None, fixed_diff_fn=None) -> Reporter:
    config.WORKSPACE.mkdir(parents=True, exist_ok=True)
    _acquire_lock()
    atexit.register(_release_lock)  # safety net for crashes / sys.exit paths
    telemetry.phase("startup")
    cases = dataset.load_cases()   # [] when golden is absent or LOOP_SILVER_PRIMARY (human-free mode)

    # silver cases carry their FROZEN diff inline; golden cases fetch via gh.
    base_get_diff = get_diff_fn or hb.get_diff

    def eff_get_diff(case):
        return case["diff"] if case.get("diff") else base_get_diff(case)

    silver_cases = silver.active_cases()
    if silver_cases:
        print(f"eval set: {len(cases)} golden + {len(silver_cases)} silver "
              f"(self-grown from resolved human comments)", flush=True)
    cases = cases + silver_cases
    if not cases:
        # Human-free mode runs on silver alone; if neither source has cases there
        # is nothing to measure against — guide the operator instead of crashing.
        _release_lock()
        if config.SILVER_PRIMARY:
            sys.exit("LOOP_SILVER_PRIMARY is set but the silver eval is empty — seed it from "
                     "your resolved PR comments first:  python3 loop.py --harvest-silver")
        sys.exit("no eval cases — add a golden set (evals/pr-review/golden.jsonl under "
                 "MAESTRO_ROOT), or run LOOP_SILVER_PRIMARY=1 after  python3 loop.py --harvest-silver")

    cases, dropped = dataset.prefetch_diffs(cases, eff_get_diff)
    if dropped:
        print(f"dropped {len(dropped)} cases with unfetchable diffs: {dropped}", flush=True)
    if not cases:
        sys.exit("no usable cases after diff prefetch")

    split = dataset.make_split(cases)
    fp_holder = {"examples": []}
    _, corpus_total = corpus.load_human_comments(0, 1)
    corpus_offset = 0
    consec_rejects, synth_offset, shadow = 0, 0, None
    silver_offset = 0
    calls_day = {"day": _today(), "calls": 0}

    def _state_usable():
        """A saved state where NOTHING scored is poison (e.g. every review
        errored on a broken env) — fall through to a fresh re-baseline."""
        try:
            s = json.loads(config.STATE_FILE.read_text())
            return (s.get("champion_eval") or {}).get("scoreable", 0) > 0
        except Exception:
            return False

    if not fresh and config.STATE_FILE.exists() and config.CHAMPION_DIR.exists() \
            and _state_usable():
        s = json.loads(config.STATE_FILE.read_text())
        champion_eval = s["champion_eval"]
        champ_fp = s.get("champ_fp")
        champ_size = s.get("champ_size", metrics.skill_size(config.CHAMPION_DIR))
        corpus_offset = s.get("corpus_offset", 0)
        totals_seed = s.get("totals")
        if s.get("train_ids") and s.get("val_ids"):
            split = (set(s["train_ids"]), set(s["val_ids"]))
        split = dataset.assign_new_ids(split, [c["id"] for c in cases])
        # restarts lose nothing: FP memory, plateau counter, shadow cursor, budget
        fp_holder["examples"] = s.get("fp_examples") or []
        consec_rejects = s.get("consec_rejects", 0)
        synth_offset = s.get("synth_offset", 0)
        shadow = s.get("shadow")
        calls_day = s.get("calls_day") or calls_day
        silver_offset = s.get("silver_offset", 0)
        print(f"resumed champion: recall {champion_eval['recall']} "
              f"(val {metrics.split_recall(champion_eval, split[1])})", flush=True)
    else:
        print("seeding champion from live skill (read-only copy) + baseline eval…", flush=True)
        snapshot_skill(config.LIVE_SKILL_DIR, config.CHAMPION_DIR)
        telemetry.phase("baseline", cases=len(cases), trials=config.CONFIRM_TRIALS)
        champion_eval = hb.evaluate(config.CHAMPION_DIR, cases, label="baseline",
                                    trials=config.CONFIRM_TRIALS, get_diff_fn=eff_get_diff,
                                    run_reviewer_fn=run_reviewer_fn)
        champ_size = metrics.skill_size(config.CHAMPION_DIR)
        champ_fp = None
        if config.PRECISION_EVERY > 0:
            telemetry.phase("precision", note="baseline FP measurement")
            try:
                pb = precision.evaluate_precision(
                    config.CHAMPION_DIR, cases[: config.PRECISION_SAMPLE], trials=1,
                    get_fixed_diff_fn=fixed_diff_fn, run_reviewer_fn=run_reviewer_fn)
                champ_fp, fp_holder["examples"] = pb["fp_rate"], pb["fp_examples"]
            except Exception as e:
                print(f"baseline precision failed — continuing without FP baseline: "
                      f"{e}", flush=True)
        totals_seed = None
        snapshot_history(0, champion_eval["recall"])
        print(f"baseline: recall {champion_eval['recall']} "
              f"(train {metrics.split_recall(champion_eval, split[0])}, "
              f"val {metrics.split_recall(champion_eval, split[1])}), "
              f"noise {champion_eval['noise']}, FP {champ_fp}, "
              f"corpus {corpus_total} human comments", flush=True)

    # --- dead-eval guard: never burn iterations against a zero-information bar ---
    if champion_eval.get("scoreable", 0) == 0:
        first_err = next((r.get("error") for r in champion_eval.get("per_case", [])
                          if r.get("error")), "(unknown)")
        _release_lock()
        sys.exit("FATAL: baseline scored 0 cases — every review errored, so no "
                 "promotion could ever be measured.\nFirst error: "
                 f"{first_err}\nFix the cause and re-run; nothing else was spent.")
    if champion_eval.get("errored", 0) > champion_eval.get("scoreable", 0):
        print(f"WARNING: {champion_eval['errored']} cases errored vs "
              f"{champion_eval['scoreable']} scored — results will be weak; "
              "check the env/auth if unexpected.", flush=True)

    reporter = Reporter(baseline=champion_eval, split=split)
    reporter.set_champion(champion_eval)
    reporter.champ_size = champ_size
    reporter.champ_fp = champ_fp
    reporter.corpus_total = corpus_total
    reporter.shadow = shadow
    reporter.plateau = consec_rejects
    if totals_seed:
        reporter.totals.update(totals_seed)
    reporter.maybe_emit(force=True)
    persist(champion_eval, champ_fp, champ_size, split, corpus_offset, reporter.totals,
            fp_examples=fp_holder["examples"], consec_rejects=consec_rejects,
            synth_offset=synth_offset, shadow=shadow, calls_day=calls_day,
            silver_offset=silver_offset)

    def _tick_scorecard():
        """Publish the lifetime scorecard NOW (not only at run-end) so the
        dashboard strip appears within a run and climbs live. Crash-proof."""
        try:
            scorecard.update(champion_eval, fp_rate=champ_fp,
                             beyond_humans=outcomes.count_beyond_humans())
        except Exception:  # noqa: BLE001 — a metric must never break the loop
            pass

    _tick_scorecard()   # right after baseline → the strip shows immediately

    limit = config.MAX_ITERS if max_iters is None else max_iters
    idx = 0
    if config.LOOP_OUTCOMES:
        out_data = outcomes.load_outcomes()
        dismissed_outcomes = out_data["dismissed"][-20:]
        acted_on_outcomes = out_data["acted_on"][-20:]
    else:
        dismissed_outcomes = []
        acted_on_outcomes = []

    while limit == 0 or idx < limit:
        idx += 1
        t0 = time.time()

        # --- daily call-budget brake (cost control; off by default) ---
        if config.MAX_CALLS_PER_DAY > 0:
            if calls_day.get("day") != _today():
                calls_day = {"day": _today(), "calls": 0}
            if calls_day["calls"] >= config.MAX_CALLS_PER_DAY:
                rec = {"iter": idx, "kind": "budget-idle", "status": "rejected",
                       "model_calls": 0, "changelog": [],
                       "rationale": f"daily budget {config.MAX_CALLS_PER_DAY} calls spent",
                       "reason": "call budget exhausted — idling until tomorrow"}
                reporter.record_iteration(rec); ledger_append(rec)
                telemetry.phase("budget-idle", iter=idx, spent=calls_day["calls"],
                                budget=config.MAX_CALLS_PER_DAY)
                time.sleep(max(config.MIN_ITER_GAP_SEC, 300))
                continue

        kind = _plan_kind(idx, metrics.skill_size(config.CHAMPION_DIR))
        plateaued = config.PLATEAU_ITERS > 0 and consec_rejects >= config.PLATEAU_ITERS
        telemetry.phase("plan", iter=idx, kind=kind, plateau=consec_rejects)
        batch = []
        if kind == "corpus":
            telemetry.phase("extract", iter=idx, kind=kind, offset=corpus_offset,
                            batch=config.CORPUS_BATCH)
            batch, corpus_total = corpus.load_human_comments(corpus_offset, config.CORPUS_BATCH)
            if not batch:
                kind = "propose"
            else:
                corpus_offset += len(batch)
                reporter.corpus_read += len(batch)
                reporter.corpus_total = corpus_total
        calls_before = reporter.totals["model_calls"]
        accepted = False
        try:
            champion_eval, champ_fp, accepted = run_iteration(
                idx, kind, cases, split, champion_eval, champ_fp, fp_holder, batch, reporter,
                get_diff_fn=eff_get_diff, run_reviewer_fn=run_reviewer_fn,
                call_model_fn=call_model_fn, fixed_diff_fn=fixed_diff_fn,
                plateaued=plateaued, dismissed_outcomes=dismissed_outcomes,
                acted_on_outcomes=acted_on_outcomes)
        except Exception as e:
            rec = {"iter": idx, "kind": kind, "status": "error", "reason": str(e)[:300],
                   "model_calls": 0}
            reporter.record_iteration(rec); ledger_append(rec)
            telemetry.phase("error", iter=idx, kind=kind, reason=str(e)[:200])
            print(f"[iter {idx}] error: {e}", flush=True)
        consec_rejects = 0 if accepted else consec_rejects + 1
        reporter.plateau = consec_rejects
        calls_day["calls"] += reporter.totals["model_calls"] - calls_before

        # --- corpus shadow eval: human-parity scoreboard (never gates) ---
        if config.SYNTH_EVERY > 0 and idx % config.SYNTH_EVERY == 0:
            try:
                syn_cases, syn_total = synthetic.build_cases(synth_offset, config.SYNTH_SAMPLE)
                if syn_cases:
                    telemetry.phase("shadow-eval", iter=idx, cases=len(syn_cases))
                    shadow = synthetic.evaluate_shadow(
                        config.CHAMPION_DIR, syn_cases,
                        run_reviewer_fn=run_reviewer_fn or hb.default_run_reviewer,
                        build_prompt_fn=hb.build_prompt, load_skill_fn=hb.load_skill)
                    shadow["total_pool"] = syn_total
                    shadow["cursor"] = synth_offset
                    shadow["ts"] = round(time.time(), 3)
                    synth_offset += len(syn_cases)
                    spent = shadow["checked"] + shadow["errored"]
                    reporter.totals["model_calls"] += spent
                    calls_day["calls"] += spent
                    reporter.shadow = shadow
            except Exception as e:
                print(f"[iter {idx}] shadow eval error: {e}", flush=True)

        # --- silver harvest: GROW the eval set from resolved human comments ---
        if config.SILVER_EVERY > 0 and idx % config.SILVER_EVERY == 0:
            try:
                telemetry.phase("silver-harvest", iter=idx, offset=silver_offset)
                hv = silver.harvest(silver_offset, call_model_fn=call_model_fn)
                silver_offset = hv["offset"]
                spent = 1 if hv["scanned"] else 0
                if hv["admitted"]:
                    new_ids = set(hv["ids"])
                    new_cases = [c for c in silver.active_cases() if c["id"] in new_ids]
                    # measure the CHAMPION on the new cases immediately so champ
                    # and future candidates are compared on the same, grown set
                    telemetry.phase("silver-baseline", iter=idx, cases=len(new_cases))
                    add_eval = hb.evaluate(config.CHAMPION_DIR, new_cases,
                                           label=f"i{idx}-silver-baseline",
                                           trials=config.CONFIRM_TRIALS,
                                           get_diff_fn=eff_get_diff,
                                           run_reviewer_fn=run_reviewer_fn)
                    champion_eval = _merge_eval(champion_eval, add_eval)
                    reporter.set_champion(champion_eval)
                    cases = cases + new_cases
                    split = dataset.assign_new_ids(split, list(new_ids))
                    spent += len(new_cases) * config.CONFIRM_TRIALS
                    ledger_append({
                        "iter": idx, "kind": "silver-harvest", "status": "info",
                        "reason": f"eval set grew: +{hv['admitted']} silver case(s)",
                        "rationale": ", ".join(hv["ids"]),
                        "eval_set": {"golden": sum(1 for c in cases if not c.get("silver")),
                                     "silver": sum(1 for c in cases if c.get("silver"))},
                        "cases": {r["id"]: bool(r.get("passed"))
                                  for r in add_eval["per_case"]}})
                    print(f"[iter {idx}] silver: +{hv['admitted']} eval case(s) "
                          f"({', '.join(hv['ids'])}) — set now {len(cases)}", flush=True)
                reporter.totals["model_calls"] += spent
                calls_day["calls"] += spent
            except Exception as e:
                print(f"[iter {idx}] silver harvest error: {e}", flush=True)

        persist(champion_eval, champ_fp, metrics.skill_size(config.CHAMPION_DIR),
                split, corpus_offset, reporter.totals,
                fp_examples=fp_holder["examples"], consec_rejects=consec_rejects,
                synth_offset=synth_offset, shadow=shadow, calls_day=calls_day,
                silver_offset=silver_offset)
        _tick_scorecard()   # keep the lifetime scorecard current every iteration

        # --- on promotion: refresh the human-reviewable adoption bundle ---
        # (after persist, so PROPOSAL.md reads the freshly written state)
        if accepted:
            try:
                telemetry.phase("exporting", iter=idx)
                exporter.export_proposal()
            except Exception as e:
                print(f"[iter {idx}] proposal export failed: {e}", flush=True)

        reporter.maybe_emit()
        dt = time.time() - t0
        if config.MIN_ITER_GAP_SEC and dt < config.MIN_ITER_GAP_SEC:
            telemetry.phase("idle", iter=idx,
                            resume_at=round(time.time() + config.MIN_ITER_GAP_SEC - dt, 1))
            time.sleep(config.MIN_ITER_GAP_SEC - dt)

    # --- lifetime scorecard: fold this run into the OVERALL autonomy / hallucination
    # trajectory (persisted across runs; crash-proof — never breaks the loop) ---
    try:
        sc = scorecard.update(champion_eval, fp_rate=champ_fp,
                              beyond_humans=outcomes.count_beyond_humans())
        if sc.get("autonomy_pct") is not None:
            print(f"lifetime autonomy: {sc['autonomy_pct']}% "
                  f"({sc['autonomous_now']}/{sc['human_flagged_total']} human-flagged patterns "
                  f"caught unaided) · graduated {sc['graduated']} · beyond-humans "
                  f"{sc['beyond_humans']}  →  python3 loop.py --scorecard", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"scorecard update skipped: {e}", flush=True)

    telemetry.phase("stopped", iter=idx)
    reporter.maybe_emit(force=True)
    _release_lock()
    return reporter


# --- rollback ---------------------------------------------------------------
def list_history():
    if not config.HISTORY_DIR.exists():
        print("no history yet")
        return
    for d in sorted(config.HISTORY_DIR.glob("*")):
        print(d.name)


def do_rollback(name: str):
    if not config.HISTORY_DIR.exists():
        sys.exit("no history to roll back to")
    matches = [d for d in config.HISTORY_DIR.glob("*") if d.name == name or d.name.startswith(name)]
    if not matches:
        avail = ", ".join(sorted(d.name for d in config.HISTORY_DIR.glob("*")))
        sys.exit(f"no history snapshot '{name}'. available: {avail}")
    snapshot_skill(matches[0], config.CHAMPION_DIR)
    if config.STATE_FILE.exists():
        config.STATE_FILE.unlink()
    print(f"rolled back champion to {matches[0].name}; re-run the loop to re-baseline from it.")


def show_status():
    """One-glance answer to 'what is the loop doing right now?' (no Vertex)."""
    st = {}
    if config.STATUS_FILE.exists():
        try:
            st = json.loads(config.STATUS_FILE.read_text())
        except json.JSONDecodeError:
            st = {}
    age = f"{round(time.time() - st['ts'])}s ago" if st.get("ts") else "n/a"
    print(f"phase: {st.get('phase', 'not running')} (iter {st.get('iter', '-')}, "
          f"heartbeat {age})")
    if config.LOCK_FILE.exists():
        try:
            pid = json.loads(config.LOCK_FILE.read_text()).get("pid")
            alive = _pid_alive(int(pid))
            print(f"lock: pid {pid} ({'alive' if alive else 'STALE — safe to delete'})")
        except Exception:
            print("lock: present (unreadable)")
    else:
        print("lock: free (loop not running)")
    if config.STATE_FILE.exists():
        s = json.loads(config.STATE_FILE.read_text())
        ev = s.get("champion_eval") or {}
        print(f"champion: recall {ev.get('recall')} ({ev.get('passed')}/"
              f"{ev.get('scoreable')}) · noise {ev.get('noise')} · "
              f"size {s.get('champ_size')} · FP {s.get('champ_fp')}")
        sh = s.get("shadow") or {}
        if sh.get("shadow_recall") is not None:
            print(f"corpus shadow recall: {sh['shadow_recall']} "
                  f"({sh.get('caught')}/{sh.get('checked')} human-caught bugs matched)")
        print(f"plateau: {s.get('consec_rejects', 0)} consecutive non-promotions · "
              f"calls today: {(s.get('calls_day') or {}).get('calls', 0)}")
    if config.LEDGER_FILE.exists():
        print("last decisions:")
        for line in config.LEDGER_FILE.read_text().splitlines()[-5:]:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            print(f"  iter {r.get('iter')} {r.get('kind')} -> {r.get('status')}: "
                  f"{str(r.get('reason', ''))[:90]}")
    a = attempts.stats()
    sv = silver.stats()
    if sv["active"] or sv["retired"]:
        print(f"silver eval: {sv['active']} active case(s), {sv['retired']} retired "
              f"(self-grown; candidates in {config.SILVER_DIR})")
    print(f"attempt memory: {a['unique']} unique patches tried · "
          f"{a['duplicates_skipped']} duplicates skipped (eval spend saved)")
    print(f"latest report: {config.REPORTS_DIR / 'LATEST.md'}")
    print(f"proposal bundle: {config.PROPOSAL_DIR / 'PROPOSAL.md'}")


def main():
    ap = argparse.ArgumentParser(description="Self-improving PR-reviewer loop")
    ap.add_argument("--max-iters", type=int, default=None)
    ap.add_argument("--once", action="store_true", help="single iteration then exit")
    ap.add_argument("--fresh", action="store_true",
                    help="re-seed champion from the live skill, ignore saved state")
    ap.add_argument("--list-history", action="store_true")
    ap.add_argument("--rollback", metavar="ID", default=None)
    ap.add_argument("--status", action="store_true",
                    help="print live phase, champion metrics, last decisions")
    ap.add_argument("--export-proposal", action="store_true",
                    help="(re)write reports/proposal/ from the current champion")
    ap.add_argument("--harvest-silver", action="store_true",
                    help="one harvest pass: resolved human comments -> new silver eval cases")
    ap.add_argument("--list-silver", action="store_true",
                    help="list silver eval cases (active + retired)")
    ap.add_argument("--retire-silver", metavar="ID", default=None,
                    help="retire a bad silver case (kept in history, out of the eval)")
    ap.add_argument("--mine-outcomes", action="store_true",
                    help="mine outcomes of the AI reviewer's comments on merged PRs")
    ap.add_argument("--replay", type=int, metavar="N", default=0,
                    help="review the last N merged PR diffs with BOTH live and champion skills")
    ap.add_argument("--scorecard", action="store_true",
                    help="print the lifetime autonomy/hallucination scorecard (overall, all runs)")
    args = ap.parse_args()

    if args.mine_outcomes:
        stats = outcomes.mine_outcomes()
        print(f"mined {stats.get('scanned_prs', 0)} PRs: "
              f"+{stats.get('new_acted_on', 0)} acted_on, "
              f"+{stats.get('new_dismissed', 0)} dismissed, "
              f"+{stats.get('new_unknown', 0)} unknown "
              f"(append-only to {config.WORKSPACE / 'outcomes.jsonl'})")
        return None
    if args.replay > 0:
        import replay
        s = {}
        if config.STATE_FILE.exists():
            try:
                s = json.loads(config.STATE_FILE.read_text())
            except Exception:
                pass
        calls_day = s.get("calls_day") or {"day": _today(), "calls": 0}
        
        def tracked_reviewer(prompt, pf):
            if config.MAX_CALLS_PER_DAY > 0:
                if calls_day.get("day") != _today():
                    calls_day.update({"day": _today(), "calls": 0})
                if calls_day["calls"] >= config.MAX_CALLS_PER_DAY:
                    print("Call budget exhausted.", flush=True)
                    raise RuntimeError("MAX_CALLS_PER_DAY exceeded")
            out = hb.default_run_reviewer(prompt, pf)
            calls_day["calls"] += 1
            return out
            
        try:
            replay.run_replay(args.replay, run_reviewer_fn=tracked_reviewer)
        finally:
            if config.STATE_FILE.exists():
                s["calls_day"] = calls_day
                tmp = config.STATE_FILE.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(s, indent=2))
                os.replace(tmp, config.STATE_FILE)
        return None
    if args.harvest_silver:
        hv = silver.harvest(0)
        print(f"admitted {hv['admitted']} case(s): {', '.join(hv['ids']) or '—'}"
              + (f" [{hv['skipped']}]" if hv.get("skipped") else "")
              + f" · candidates bundle: {config.SILVER_DIR}")
        return None
    if args.list_silver:
        rows = silver.list_rows()
        if not rows:
            print("no silver cases yet — run --harvest-silver")
        for r in rows:
            print(f"{r['id']:>10} {r.get('status', '?'):8} conf {r.get('confidence')} "
                  f"sev {str(r.get('severity')):8} {r['bug'][:70]}")
        return None
    if args.retire_silver:
        print("retired" if silver.retire(args.retire_silver)
              else "not found (or already retired)")
        return None
    if args.status:
        return show_status()
    if args.export_proposal:
        out = exporter.export_proposal()
        print(f"proposal bundle written: {out}" if out
              else "no champion yet — run the loop first")
        return None
    if args.list_history:
        return list_history()
    if args.scorecard:
        print(scorecard.format_scorecard())
        return None
    if args.rollback:
        return do_rollback(args.rollback)
    run_loop(max_iters=1 if args.once else args.max_iters, fresh=args.fresh)


if __name__ == "__main__":
    main()
