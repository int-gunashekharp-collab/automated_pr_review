#!/usr/bin/env python3
"""Lifetime scorecard — the OVERALL, cross-run measure of whether the system is
learning (improving) or hallucinating. NOT a per-run snapshot: it accumulates
over every run the loop has ever done.

The headline is the AUTONOMY rate, computed only from REAL, resolution-derived
signal the loop already collects — never the model's own opinion, so the metric
itself cannot be gamed by a hallucinating proposer:

    "a human flagged this"  -> harvested as a silver eval case
                            -> the reviewer is later scored on that diff
                               WITHOUT the human's comment.
    If it now catches it, the AI has learned to flag that pattern on its own.

    autonomy% = (distinct human-flagged patterns the current champion catches
                 UNAIDED)  /  (all distinct human-flagged patterns EVER seen)

Definitions
-----------
  autonomy%      caught_now / human_flagged_total   — the climbing improvement %
  graduated      patterns first MISSED but later caught (pure "learned from human")
  regressed      patterns first caught but now missed (forgetting / degradation)
  hallucination% latest false-alarm rate (re-flagging already-FIXED code)
  beyond_humans  acted-on AI catches with no overlapping human comment ("surpass")

Persisted across runs (NOT reset by --fresh):
  * workspace/learning_ledger.json — per-pattern lifetime status (source of truth)
  * workspace/scorecard.jsonl       — append-only trajectory (the climbing %)
"""
from __future__ import annotations

import json
import time

import config

LEDGER_FILE = config.WORKSPACE / "learning_ledger.json"
TRAJ_FILE = config.WORKSPACE / "scorecard.jsonl"


def _load_ledger() -> dict:
    try:
        return json.loads(LEDGER_FILE.read_text())
    except Exception:
        return {}


def _review_stats() -> dict:
    """Cumulative 'PRs reviewed completely' (from harness_bridge's review_stats.json)."""
    try:
        d = json.loads(config.REVIEW_STATS_FILE.read_text())
        return {"reviews_completed": int(d.get("reviews", 0)),
                "prs_reviewed": len(d.get("prs", []))}
    except Exception:
        return {"reviews_completed": 0, "prs_reviewed": 0}


def _silver_latest() -> dict:
    """Every human-flagged (silver) pattern, latest record per id (active+retired)."""
    out: dict = {}
    try:
        for line in config.SILVER_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue  # corpora contain truncated lines — skip defensively
            if r.get("id"):
                out[r["id"]] = r
    except Exception:
        pass
    return out


def _current_pass(champion_eval: dict) -> dict:
    """id -> True/False (caught) for cases actually SCORED this run. Errored or
    absent ids are omitted — an error is not a miss and must not move the metric."""
    out: dict = {}
    for r in (champion_eval or {}).get("per_case", []):
        if "error" in r or not r.get("id"):
            continue
        out[r["id"]] = bool(r.get("passed"))
    return out


def _compute(ledger: dict, *, fp_rate=None, beyond_humans=0, champ_recall=None) -> dict:
    total = len(ledger)
    autonomous_now = sum(1 for e in ledger.values() if e.get("last_caught"))
    graduated = sum(1 for e in ledger.values()
                    if e.get("first_caught") is False and e.get("ever_caught"))
    regressed = sum(1 for e in ledger.values()
                    if e.get("first_caught") is True and e.get("last_caught") is False)
    return {
        "human_flagged_total": total,
        "autonomous_now": autonomous_now,
        "autonomy_pct": round(100 * autonomous_now / total, 1) if total else None,
        "graduated": graduated,
        "regressed": regressed,
        "hallucination_pct": round(100 * fp_rate, 1) if fp_rate is not None else None,
        "beyond_humans": int(beyond_humans or 0),
        "champ_recall": champ_recall,
    }


def update(champion_eval: dict, *, fp_rate=None, beyond_humans: int = 0) -> dict:
    """Fold this run into the lifetime ledger and append a trajectory point.
    Returns the current lifetime metrics. CRASH-PROOF: never raises into the loop."""
    try:
        ledger = _load_ledger()
        silver = _silver_latest()
        cur = _current_pass(champion_eval)
        now = round(time.time(), 3)

        for cid, row in silver.items():
            caught = cur.get(cid)                 # True / False / None(not scored)
            base = row.get("baseline_passed")     # True / False / None (at harvest)
            e = ledger.get(cid)
            if e is None:
                first = base if base is not None else caught
                ledger[cid] = {
                    "first_ts": now,
                    "first_caught": first,        # could the AI catch it at FIRST sight?
                    "ever_caught": bool(caught) if caught is not None else bool(first),
                    "last_caught": caught if caught is not None else first,
                    "human_said": (row.get("human_said") or row.get("bug") or "")[:160],
                }
            else:
                if caught is not None:
                    e["ever_caught"] = bool(e.get("ever_caught")) or caught
                    e["last_caught"] = caught
                # backfill a baseline if we learn one later
                if e.get("first_caught") is None and base is not None:
                    e["first_caught"] = base
                elif e.get("first_caught") is None and caught is not None:
                    e["first_caught"] = caught

        LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        LEDGER_FILE.write_text(json.dumps(ledger))

        m = _compute(ledger, fp_rate=fp_rate, beyond_humans=beyond_humans,
                     champ_recall=(champion_eval or {}).get("recall"))
        m["ts"] = now
        m.update(_review_stats())
        with TRAJ_FILE.open("a") as fh:
            fh.write(json.dumps(m) + "\n")
        return m
    except Exception as ex:  # noqa: BLE001 — a metric must never break the loop
        return {"error": str(ex)[:200]}


def lifetime() -> dict:
    """Current lifetime metrics + the trajectory, for the dashboard / CLI.
    Computes fresh from the ledger so it's correct even without a recent update()."""
    ledger = _load_ledger()
    traj: list = []
    try:
        for line in TRAJ_FILE.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    traj.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    latest = traj[-1] if traj else {}
    fp = latest.get("hallucination_pct")
    m = _compute(ledger,
                 fp_rate=(fp / 100 if fp is not None else None),
                 beyond_humans=latest.get("beyond_humans", 0),
                 champ_recall=latest.get("champ_recall"))
    m["runs"] = len(traj)
    firsts = [t.get("autonomy_pct") for t in traj if t.get("autonomy_pct") is not None]
    m["autonomy_first"] = firsts[0] if firsts else None
    m["trajectory"] = [{"ts": t.get("ts"), "autonomy_pct": t.get("autonomy_pct"),
                        "graduated": t.get("graduated"),
                        "beyond_humans": t.get("beyond_humans")} for t in traj[-60:]]
    m.update(_review_stats())
    return m


def format_scorecard() -> str:
    m = lifetime()
    if not m.get("human_flagged_total"):
        return ("Lifetime scorecard: no human-flagged patterns harvested yet.\n"
                "Seed them with:  python3 loop.py --harvest-silver")
    delta = ""
    if m.get("autonomy_first") is not None and m.get("autonomy_pct") is not None:
        d = round(m["autonomy_pct"] - m["autonomy_first"], 1)
        delta = f"   ({'+' if d >= 0 else ''}{d} pts since first run)"
    hp = m["hallucination_pct"]
    return "\n".join([
        "===============  LIFETIME SCORECARD  (overall — all runs)  ===============",
        f"  AUTONOMY        {m['autonomy_pct']}%{delta}",
        f"                  {m['autonomous_now']} of {m['human_flagged_total']} human-flagged "
        "patterns now caught WITHOUT a human",
        f"  GRADUATED       {m['graduated']}   first missed, later learned to catch on its own",
        f"  REGRESSED       {m['regressed']}   first caught, now missed  (forgetting — watch)",
        f"  HALLUCINATION   {hp if hp is not None else 'n/a'}%   false alarms on already-fixed code",
        f"  BEYOND HUMANS   {m['beyond_humans']}   real catches no human flagged",
        f"  PRS REVIEWED    {m.get('prs_reviewed', 0)}   distinct PRs "
        f"({m.get('reviews_completed', 0)} reviews completed)",
        f"  runs recorded   {m['runs']}",
        "==========================================================================",
    ])


if __name__ == "__main__":
    print(format_scorecard())
