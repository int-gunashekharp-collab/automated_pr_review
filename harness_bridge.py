#!/usr/bin/env python3
"""Read-only bridge to the existing maestro-core eval harness.

We import `scripts/eval_pr_reviewer.py` as a *library* and reuse its machinery
verbatim — diff fetching, the production review prompt, the Gemini-on-Vertex
runner, and the recall scorer. The only thing we change is where it *writes*:
its cache/run paths are redirected into the loop's workspace so nothing lands in
the parent repo's evals/ tree.

The loop therefore optimises against the SAME quality bar the real reviewer is
graded on — not a fork that could drift.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path

import config
import telemetry

# The harness's reviewer subprocess reads these from the environment. Inject
# the configured defaults so a bare shell (no exports) can never silently kill
# every review — the proposer path already does this; now both paths match.
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", config.GCP_PROJECT)
os.environ.setdefault("GEMINI_MODEL", config.GEMINI_MODEL)

# Make `import eval_pr_reviewer` resolve to the parent repo's scripts/.
# If maestro-core is missing we FAIL LOUDLY — unless the caller explicitly
# opted into the offline stub (LOOP_ALLOW_STUB=1; the smoke test does, so the
# full control flow is testable on machines/CI without maestro-core). A real
# run can never silently produce fake reviews.
sys.path.insert(0, str(config.SCRIPTS_DIR))
try:
    import eval_pr_reviewer as H  # noqa: E402
except Exception as _imp_err:  # pragma: no cover - exercised via smoke env
    if not config.ALLOW_STUB:
        raise ImportError(
            f"cannot import eval_pr_reviewer from {config.SCRIPTS_DIR} ({_imp_err}). "
            "Set MAESTRO_ROOT to your maestro-core checkout. "
            "(Tests may set LOOP_ALLOW_STUB=1 to use the offline stub harness.)")
    sys.path.insert(0, str(config.LOOP_DIR / "stub_harness"))
    import eval_pr_reviewer as H  # noqa: E402
    print("WARNING: using stub_harness/eval_pr_reviewer.py (LOOP_ALLOW_STUB=1) — "
          "test mode only, reviews are NOT real", flush=True)

# Redirect every write target of the harness into OUR workspace (isolation).
config.DIFF_CACHE.mkdir(parents=True, exist_ok=True)
config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
H.DIFF_CACHE = config.DIFF_CACHE
H.RUNS_DIR = config.RUNS_DIR

get_diff = H.get_diff
load_skill = H.load_skill
build_prompt = H.build_prompt
score = H.score


def load_routed_skill(skill_dir: Path, diff: str) -> str:
    """Load only the reference files that match the files in the diff.
    If routing.json exists in skill_dir, use it; else load everything (back-compat)."""
    sk = (skill_dir / "SKILL.md").read_text()
    parts = [sk]

    routing_file = skill_dir / "routing.json"
    routing = {}
    if routing_file.exists():
        try:
            routing = json.loads(routing_file.read_text())
        except Exception:
            pass

    ref_dir = skill_dir / "references"
    all_refs = sorted(ref_dir.glob("*.md")) if ref_dir.exists() else []

    if not config.LOOP_ROUTING or not routing:
        for f in all_refs:
            parts.append(f.read_text())
        return "\n\n".join(parts)

    # LOOP_ROUTING=1 and routing map exists.
    changed_files = set()
    for line in diff.splitlines():
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            path = line[6:].strip()
            if path:
                changed_files.add(path)

    to_load = set()
    core = routing.get("core", [])
    map_ = routing.get("map", {})

    # always include core files
    for pattern in core:
        for f in all_refs:
            if fnmatch.fnmatch(f.name, pattern):
                to_load.add(f)

    # include files matching the changed paths
    for path in changed_files:
        for pattern, refs in map_.items():
            if fnmatch.fnmatch(path, pattern):
                for ref_pat in refs:
                    for f in all_refs:
                        if fnmatch.fnmatch(f.name, ref_pat):
                            to_load.add(f)

    # any file not in core or map is also considered core for safety
    mapped_refs = set(core)
    for refs in map_.values():
        mapped_refs.update(refs)
    for f in all_refs:
        is_mapped = any(fnmatch.fnmatch(f.name, p) for p in mapped_refs)
        if not is_mapped:
            to_load.add(f)

    for f in sorted(list(to_load), key=lambda x: x.name):
        parts.append(f.read_text())

    return "\n\n".join(parts)


def default_run_reviewer(prompt: str, prompt_file: Path) -> str:
    """One review with Gemini 3.1 Pro on Vertex via the harness runner, with
    bounded retry/backoff so a transient Vertex flake costs seconds, not the
    case (an errored case scores as a miss — too expensive to lose to a 429)."""
    last_err: Exception | None = None
    for attempt in range(1 + max(0, config.MODEL_RETRIES)):
        if attempt:
            time.sleep(config.RETRY_BACKOFF_SEC * attempt)
        try:
            return H.run_reviewer(prompt, model="", cmd_template=None,
                                  prompt_file=prompt_file, with_mcp=config.USE_MCP,
                                  engine="gemini")
        except Exception as e:  # noqa: BLE001 - harness raises plain Exceptions
            last_err = e
    raise RuntimeError(
        f"reviewer failed after {1 + config.MODEL_RETRIES} tries: {last_err}")


def _prune_eval_cache(keep: int = 80):
    """Bound the checkpoint dir: drop the oldest files beyond `keep`."""
    try:
        files = sorted(config.EVAL_CACHE_DIR.glob("*.jsonl"),
                       key=lambda p: p.stat().st_mtime)
        for f in files[:-keep] if len(files) > keep else []:
            f.unlink()
    except Exception:
        pass


def _bump_review_stats(prs: list) -> None:
    """Cumulative 'PRs reviewed completely' counter — survives champion changes
    (unlike the per-fingerprint eval cache). reviews = total review completions;
    prs = the distinct PRs ever reviewed. Crash-proof, atomic."""
    if not prs:
        return
    try:
        f = config.REVIEW_STATS_FILE
        cur = {"reviews": 0, "prs": []}
        if f.exists():
            try:
                cur = json.loads(f.read_text())
            except Exception:
                pass
        cur["reviews"] = int(cur.get("reviews", 0)) + len(prs)
        seen = set(cur.get("prs", [])) | {p for p in prs if p is not None}
        cur["prs"] = sorted(seen, key=lambda x: str(x))
        cur["updated"] = round(time.time(), 3)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cur))
        os.replace(tmp, f)
    except Exception:
        pass


def evaluate(skill_dir: Path, cases: list[dict], *, label: str, trials: int = 1,
             get_diff_fn=None, run_reviewer_fn=None) -> dict:
    """Score `skill_dir` over `cases`. With trials>1 each case is reviewed
    `trials` times and passes on a MAJORITY vote (variance control). Returns
    recall, mean noise, per-case results, and the misses.

    `get_diff_fn` / `run_reviewer_fn` are injectable so tests run the whole
    control flow without network or Vertex.
    """
    get_diff_fn = get_diff_fn or get_diff
    run_reviewer_fn = run_reviewer_fn or default_run_reviewer

    # We load skill_text per case if routing is enabled, otherwise once here.
    skill_text_global = load_skill(base=skill_dir) if not config.LOOP_ROUTING else None

    # --- per-case checkpointing: a crash/restart never re-buys finished work.
    # Rows are keyed by a fingerprint of the EXACT skill text (+ trial count),
    # so results can only ever be reused for the identical rubric.
    n_trials = max(1, trials)
    # Use skill_text_global for the fingerprint if available, else load_skill.
    # When routing is on, the fingerprint is of the WHOLE skill, even though
    # per-case loads are routed. This ensures a change to any ref file busts the cache.
    _fp_text = skill_text_global or load_skill(base=skill_dir)
    fp = hashlib.sha256(_fp_text.encode()).hexdigest()[:12]
    config.EVAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ck = config.EVAL_CACHE_DIR / f"{label}.jsonl"
    done: dict = {}
    if ck.exists():
        for line in ck.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("skill") == fp and row.get("trials") == n_trials:
                done[row["id"]] = row
    _prune_eval_cache()

    per_case, missed, noise_samples = [], [], []
    fresh_prs: list = []          # PRs reviewed FRESH this call (for the cumulative counter)
    passed = errored = reused = 0

    for ci, case in enumerate(cases):
        prev = done.get(case["id"])
        if prev is not None:
            reused += 1
            per_case.append({"id": case["id"], "passed": prev["passed"],
                             "findings_count": prev["findings_count"],
                             "severity": prev.get("severity", "high")})
            noise_samples.append(prev["findings_count"])
            if prev["passed"]:
                passed += 1
            else:
                missed.append({"id": case["id"], "pr": prev.get("pr"),
                               "path": prev.get("path"), "bug": prev.get("bug"),
                               "severity": prev.get("severity", "high"),
                               "must_match_any": prev.get("must_match_any", []),
                               "excerpt": prev.get("excerpt", "")})
            continue
        try:
            diff = get_diff_fn(case)
            skill_text = load_routed_skill(skill_dir, diff) if config.LOOP_ROUTING else skill_text_global
            prompt = build_prompt(diff, skill_text, config.USE_MCP, config.USE_WORKFLOW)
            votes, findings, last_out = [], [], ""
            for t in range(n_trials):
                telemetry.eval_progress(label=label, case=case["id"], case_idx=ci + 1,
                                        n_cases=len(cases), trial=t + 1,
                                        trials=n_trials)
                pf = config.RUNS_DIR / f"{label}-{case['id']}-t{t}-prompt.txt"
                out = run_reviewer_fn(prompt, pf)
                res = score(case, out)
                votes.append(bool(res["passed"]))
                findings.append(res["findings_count"])
                last_out = out
            (config.RUNS_DIR / f"{label}-{case['id']}.md").write_text(last_out)
        except Exception as e:
            per_case.append({"id": case["id"], "passed": False, "error": str(e)[:300]})
            errored += 1
            continue  # errors are NOT checkpointed — they retry on resume

        case_passed = sum(votes) * 2 > len(votes)
        fc = round(statistics.mean(findings))
        per_case.append({"id": case["id"], "passed": case_passed, "findings_count": fc,
                         "severity": case.get("severity", "high")})
        noise_samples.append(fc)
        if case_passed:
            passed += 1
        else:
            missed.append({
                "id": case["id"], "pr": case["pr"], "path": case["path"],
                "bug": case["bug"], "severity": case.get("severity", "high"),
                "must_match_any": case.get("must_match_any", []),
                "excerpt": last_out[:1200],
            })
        with ck.open("a") as fh:
            fh.write(json.dumps({
                "id": case["id"], "skill": fp, "trials": n_trials,
                "passed": case_passed, "findings_count": fc,
                "pr": case.get("pr"), "path": case.get("path"),
                "bug": case.get("bug"), "severity": case.get("severity", "high"),
                "must_match_any": case.get("must_match_any", []),
                "excerpt": ("" if case_passed else last_out[:1200]),
                "ts": round(time.time(), 3)}) + "\n")
        fresh_prs.append(case.get("pr"))

    if reused:
        print(f"[eval {label}] resumed from checkpoint: {reused} case(s) reused, "
              f"{len(cases) - reused} evaluated fresh", flush=True)

    _bump_review_stats(fresh_prs)
    scoreable = len(cases) - errored
    return {
        "label": label,
        "recall": round(passed / scoreable, 3) if scoreable else None,
        "noise": round(sum(noise_samples) / len(noise_samples), 2) if noise_samples else 0.0,
        "passed": passed, "scoreable": scoreable, "errored": errored,
        "per_case": per_case, "missed": missed,
    }
