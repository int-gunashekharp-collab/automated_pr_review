#!/usr/bin/env python3
"""A real precision signal — not the findings-count proxy.

For a golden case we know the bug and the PR that FIXED it. If we show the
reviewer the *current* (fixed) PR diff and it still flags that bug, that's a
false positive: it's complaining about something already resolved. The
candidate's false-positive rate over a sample of fixed diffs is a precision
gate, and the offending outputs are fed back to the proposer so it can propose
*subtractions*, not just additions.
"""

from __future__ import annotations

import statistics
import subprocess
from pathlib import Path

import config
import harness_bridge as hb
import telemetry


def get_fixed_diff(case: dict) -> str:
    """Current (post-fix) diff of the PR, cached. Raises if unavailable."""
    config.FIXED_DIFF_CACHE.mkdir(parents=True, exist_ok=True)
    cached = config.FIXED_DIFF_CACHE / f"{case['pr']}.diff"
    if cached.exists():
        return cached.read_text()
    r = subprocess.run(["gh", "pr", "diff", str(case["pr"])],
                       capture_output=True, text=True, cwd=config.MAESTRO_ROOT)
    if r.returncode != 0 or len(r.stdout) < 200:
        raise RuntimeError(f"fixed diff for PR {case['pr']} unavailable")
    cached.write_text(r.stdout)
    return r.stdout


def evaluate_precision(skill_dir: Path, cases: list[dict], *, trials: int = 1,
                       get_fixed_diff_fn=None, run_reviewer_fn=None) -> dict:
    get_fixed_diff_fn = get_fixed_diff_fn or get_fixed_diff
    run_reviewer_fn = run_reviewer_fn or hb.default_run_reviewer
    skill_text = hb.load_skill(base=skill_dir)

    fps, checked, examples = 0, 0, []
    for ci, case in enumerate(cases):
        try:
            diff = get_fixed_diff_fn(case)
        except Exception:
            continue  # can't fetch the fixed diff — skip, don't penalise
        prompt = hb.build_prompt(diff, skill_text, config.USE_MCP, config.USE_WORKFLOW)
        votes, last = [], ""
        try:
            for t in range(max(1, trials)):
                telemetry.eval_progress(label="precision", case=case["id"],
                                        case_idx=ci + 1, n_cases=len(cases),
                                        trial=t + 1, trials=max(1, trials))
                pf = config.RUNS_DIR / f"prec-{case['id']}-t{t}-prompt.txt"
                last = run_reviewer_fn(prompt, pf)
                votes.append(bool(hb.score(case, last)["passed"]))
        except Exception:
            continue  # one flaky review must never sink the pass (or the loop)
        checked += 1
        if sum(votes) * 2 > len(votes):  # re-flagged an already-fixed bug
            fps += 1
            examples.append({"id": case["id"], "path": case["path"],
                             "bug": case["bug"], "excerpt": last[:800]})
    return {
        "fp_rate": round(fps / checked, 3) if checked else None,
        "checked": checked, "fps": fps, "fp_examples": examples,
    }
