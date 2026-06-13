#!/usr/bin/env python3
"""Central config for the self-improving PR-reviewer loop.

This folder lives OUTSIDE maestro-core and only ever READS it. The maestro-core
location is resolved from $MAESTRO_ROOT, then a couple of known/sibling paths.
Everything the loop WRITES stays under this folder (workspace/, reports/).

ISOLATION GUARANTEE: the loop never writes anywhere under MAESTRO_ROOT. The live
skill, scripts, evals, and the mined corpus are read-only inputs.
"""

from __future__ import annotations

import os
from pathlib import Path


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _csv(name: str) -> set[str]:
    return {x.strip() for x in os.environ.get(name, "").split(",") if x.strip()}


# --- where this loop lives (writes here only) ------------------------------
LOOP_DIR = Path(__file__).resolve().parent


def _detect_maestro_root() -> Path:
    """maestro-core lives elsewhere on disk. Resolve it without ever writing to it."""
    env = os.environ.get("MAESTRO_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    for cand in ("/Users/gunashekharp/snabbit chatbot/maestro-core",
                 str(LOOP_DIR.parent / "maestro-core")):
        p = Path(cand).expanduser()
        if (p / "scripts" / "eval_pr_reviewer.py").exists():
            return p.resolve()
    # best effort; the harness import will error clearly if this is wrong
    return Path("/Users/gunashekharp/snabbit chatbot/maestro-core")


# --- read-only maestro-core inputs -----------------------------------------
MAESTRO_ROOT = _detect_maestro_root()
SCRIPTS_DIR = MAESTRO_ROOT / "scripts"
LIVE_SKILL_DIR = MAESTRO_ROOT / ".claude" / "skills" / "maestro-review-conventions"
EVALS_DIR = MAESTRO_ROOT / "evals" / "pr-review"
GOLDEN_FILE = EVALS_DIR / "golden.jsonl"
# The mined human review corpus — the loop's learning source (read-only).
CORPUS_DIR = MAESTRO_ROOT / "docs" / ".pr-review-corpus"

# --- locations the loop OWNS and writes to ---------------------------------
WORKSPACE = LOOP_DIR / "workspace"
CHAMPION_DIR = WORKSPACE / "champion-skill"
CANDIDATE_DIR = WORKSPACE / "candidate-skill"
DIFF_CACHE = WORKSPACE / "diffs"
FIXED_DIFF_CACHE = WORKSPACE / "diffs-fixed"
RUNS_DIR = WORKSPACE / "runs"
HISTORY_DIR = WORKSPACE / "history"
LEDGER_FILE = WORKSPACE / "ledger.jsonl"
STATE_FILE = WORKSPACE / "state.json"
REPORTS_DIR = LOOP_DIR / "reports"
HOURLY_DIR = REPORTS_DIR / "hourly"
ATTEMPTS_FILE = WORKSPACE / "attempts.jsonl"   # patch-fingerprint memory
STATUS_FILE = WORKSPACE / "status.json"        # live heartbeat for the dashboard
LOCK_FILE = WORKSPACE / "loop.lock"            # single-instance guard
PROPOSAL_DIR = REPORTS_DIR / "proposal"        # human-reviewable adoption bundle

# --- model / Vertex (matches CI) -------------------------------------------
GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "snabbit-ai-productivity")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-pro-preview")

# --- dataset / overfitting controls ----------------------------------------
VAL_FRACTION = _f("LOOP_VAL_FRACTION", 0.33)
SPLIT_SEED = _i("LOOP_SPLIT_SEED", 0)
FORCE_VAL_IDS = _csv("LOOP_FORCE_VAL_IDS")
MAX_CASES = _i("LOOP_MAX_CASES", 0)

# --- variance / decision stability -----------------------------------------
CONFIRM_TRIALS = _i("LOOP_CONFIRM_TRIALS", 3)
SCREEN_TRIALS = _i("LOOP_SCREEN_TRIALS", 1)
SCREEN_PASSING_SAMPLE = _i("LOOP_EVAL_SUBSET", 6)
NOISE_TOLERANCE = _f("LOOP_NOISE_TOLERANCE", 0.10)

# --- corpus learning (extract + understand human PR comments) --------------
CORPUS_EVERY = _i("LOOP_CORPUS_EVERY", 2)          # corpus mutation cadence (0=off)
CORPUS_BATCH = _i("LOOP_CORPUS_BATCH", 120)        # human comments understood per pass
CORPUS_MIN_LEN = _i("LOOP_CORPUS_MIN_LEN", 80)     # ignore trivial one-word comments

# --- anti-bloat / consolidation --------------------------------------------
MAX_SKILL_CHARS = _i("LOOP_MAX_SKILL_CHARS", 60_000)
CONSOLIDATE_EVERY = _i("LOOP_CONSOLIDATE_EVERY", 5)

# --- precision gate ---------------------------------------------------------
PRECISION_EVERY = _i("LOOP_PRECISION_EVERY", 3)
PRECISION_SAMPLE = _i("LOOP_PRECISION_SAMPLE", 4)
PRECISION_TOL = _f("LOOP_PRECISION_TOL", 0.0)

# --- reviewer prompt fidelity ----------------------------------------------
USE_WORKFLOW = _b("LOOP_WORKFLOW", False)
USE_MCP = _b("LOOP_WITH_MCP", False)

# --- loop cadence -----------------------------------------------------------
REPORT_INTERVAL_SEC = _i("LOOP_REPORT_INTERVAL_SEC", 3600)
MAX_ITERS = _i("LOOP_MAX_ITERS", 0)
MIN_ITER_GAP_SEC = _i("LOOP_MIN_ITER_GAP_SEC", 0)

# --- reporting delivery -----------------------------------------------------
REPORT_WEBHOOK = os.environ.get("LOOP_REPORT_WEBHOOK", "").strip()

# --- beam search (multi-candidate proposals) --------------------------------
# Per propose-iteration, generate up to BEAM_WIDTH distinct patches, screen each
# cheaply, and only the best one goes to the expensive full confirm.
BEAM_WIDTH = max(1, _i("LOOP_BEAM", 2))

# --- attempt memory / reflection ---------------------------------------------
# After PLATEAU_ITERS consecutive non-promotions, the proposer is shown its own
# recent failed attempts and instructed to take a fundamentally different angle.
PLATEAU_ITERS = _i("LOOP_PLATEAU_ITERS", 6)
FAILED_ATTEMPTS_SHOWN = _i("LOOP_FAILED_SHOWN", 8)

# --- corpus shadow eval (human-parity scoreboard; reported, never gates) ----
SYNTH_EVERY = _i("LOOP_SYNTH_EVERY", 4)        # cadence in iterations (0 = off)
SYNTH_SAMPLE = _i("LOOP_SYNTH_SAMPLE", 12)     # human-caught bugs per pass
SYNTH_MIN_HUNK = _i("LOOP_SYNTH_MIN_HUNK", 80) # ignore tiny/contextless hunks

# --- resilience ---------------------------------------------------------------
MODEL_RETRIES = _i("LOOP_MODEL_RETRIES", 2)          # extra tries on Vertex flake
RETRY_BACKOFF_SEC = _f("LOOP_RETRY_BACKOFF_SEC", 15) # base backoff (x attempt)
MAX_CALLS_PER_DAY = _i("LOOP_MAX_CALLS_PER_DAY", 0)  # 0 = unlimited (cost brake)

# --- dashboard ----------------------------------------------------------------
DASH_PORT = _i("LOOP_DASH_PORT", 8123)         # python3 dashboard.py serves here

# --- silver eval (self-growing eval set) --------------------------------------
# Harvest RESOLVED human review comments into machine-checkable eval cases that
# join the golden set (frozen inline diffs; loop-owned; never touches maestro-core).
SILVER_FILE = WORKSPACE / "silver.jsonl"
SILVER_DIR = REPORTS_DIR / "silver"
EVAL_CACHE_DIR = WORKSPACE / "evalcache"   # per-case eval checkpoints (crash-resume)
SILVER_EVERY = _i("LOOP_SILVER_EVERY", 8)      # harvest cadence in iterations (0 = off)
SILVER_SCAN = _i("LOOP_SILVER_SCAN", 200)      # corpus comments scanned per harvest
SILVER_BATCH = _i("LOOP_SILVER_BATCH", 6)      # max cases admitted per harvest
SILVER_MAX = _i("LOOP_SILVER_MAX", 30)         # cap on active silver cases (eval cost!)
SILVER_MIN_CONF = _f("LOOP_SILVER_MIN_CONF", 0.7)

# --- stub harness (tests only) ------------------------------------------------
# Real runs FAIL LOUDLY if maestro-core is missing. Only when LOOP_ALLOW_STUB=1
# (the smoke test sets it) may harness_bridge fall back to stub_harness/.
ALLOW_STUB = _b("LOOP_ALLOW_STUB", False)
