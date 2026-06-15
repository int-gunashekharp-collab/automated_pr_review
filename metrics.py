#!/usr/bin/env python3
"""Pure metric + promotion-gate logic — no I/O, so it's directly unit-testable.

`decide()` is the single place every promotion rule lives:
  * never regress a previously-passing case (hard)
  * stay within the noise and size budgets
  * not worsen precision (when measured)
  * propose: must catch a NEW train bug AND not drop held-out VAL recall
  * consolidate: must keep total recall AND shrink the rubric
"""

from __future__ import annotations

import math
from pathlib import Path


def wilson_ci(passed: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval — honest uncertainty for small-n recall numbers.
    With 18 golden cases a point estimate alone over-states certainty; reports
    show `recall (CI lo–hi)` so promotions read as evidence, not gospel."""
    if n <= 0:
        return None
    p = passed / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3))


def split_ci(ev: dict, ids: set[str]) -> tuple[float, float] | None:
    """Wilson CI for recall over a subset of case ids."""
    rows = [r for r in ev.get("per_case", []) if r["id"] in ids and "error" not in r]
    if not rows:
        return None
    return wilson_ci(sum(bool(r.get("passed")) for r in rows), len(rows))


def fmt_ci(ci: tuple[float, float] | None) -> str:
    return f"(95% CI {ci[0]}–{ci[1]})" if ci else ""


def passing_ids(ev: dict) -> set[str]:
    return {r["id"] for r in ev["per_case"] if r.get("passed")}


def failed_ids(ev: dict) -> set[str]:
    """Explicitly failed (scored, not passed) — excludes errored cases."""
    return {r["id"] for r in ev["per_case"] if "error" not in r and not r.get("passed")}


def split_recall(ev: dict, ids: set[str]) -> float | None:
    rows = [r for r in ev["per_case"] if r["id"] in ids and "error" not in r]
    if not rows:
        return None
    return round(sum(bool(r.get("passed")) for r in rows) / len(rows), 3)


def weighted_recall(ev: dict, id_to_sev: dict[str, str], weights: dict[str, float]) -> float | None:
    """Recall weighted by case severity. critical=4, high=3, medium=2, low=1."""
    rows = [r for r in ev.get("per_case", []) if "error" not in r]
    if not rows:
        return None
    total_w = sum_passed_w = 0.0
    for r in rows:
        sev = str(id_to_sev.get(r["id"], r.get("severity", "high"))).lower()
        w = weights.get(sev, weights.get("high", 3.0))
        total_w += w
        if r.get("passed"):
            sum_passed_w += w
    return round(sum_passed_w / total_w, 3) if total_w > 0 else None


def regressions(champ: dict, cand: dict) -> set[str]:
    """Cases the champion caught that the candidate now explicitly misses."""
    return passing_ids(champ) & failed_ids(cand)


def noise_ok(cand: float, champ: float, tol: float) -> bool:
    allowed = champ * (1 + tol) if champ > 0 else champ + 1.0
    return cand <= allowed + 1e-9


def subset_noise(ev: dict, ids: set[str]) -> float:
    """Mean findings over the given cases. Used to detect *added* chatter on
    cases the champion already passed — catching a NEW bug legitimately raises
    findings on its own case, so the global mean is the wrong thing to gate on."""
    vals = [r["findings_count"] for r in ev["per_case"]
            if r["id"] in ids and "findings_count" in r]
    return sum(vals) / len(vals) if vals else 0.0


def skill_size(skill_dir: Path, routed: bool = False, cases: list[dict] | None = None,
               get_diff_fn=None) -> int:
    """Calculates skill size. If routed=True, returns the MAX routed size over
    all given cases (the worst-case prompt size)."""
    if not routed:
        total = len((skill_dir / "SKILL.md").read_text())
        ref_dir = skill_dir / "references"
        if ref_dir.exists():
            for f in ref_dir.glob("*.md"):
                total += len(f.read_text())
        return total

    # Routed size: max over cases.
    if not cases:
        return skill_size(skill_dir, routed=False)

    import harness_bridge as hb
    mx = 0
    for case in cases:
        try:
            diff = get_diff_fn(case) if get_diff_fn else case.get("diff", "")
            sz = len(hb.load_routed_skill(skill_dir, diff))
            if sz > mx:
                mx = sz
        except Exception:
            continue
    return mx


def decide(*, kind: str, champ: dict, cand: dict, train_ids: set[str],
           val_ids: set[str], champ_size: int, cand_size: int,
           noise_tol: float, max_size: int,
           champ_fp: float | None = None, cand_fp: float | None = None,
           precision_tol: float = 0.0) -> tuple[bool, str]:
    # --- liveness: never promote against a DEAD measurement ---
    # If the candidate eval couldn't actually score (every / most case errored —
    # e.g. a missing GOOGLE_CLOUD_PROJECT killed the reviewer), there is no
    # evidence to act on. Errors are NOT misses, so a dead candidate would
    # otherwise slip past the regression guard (which ignores errored cases).
    cper = cand.get("per_case", [])
    n_err = cand.get("errored")
    if n_err is None:
        n_err = sum(1 for r in cper if "error" in r)
    n_score = cand.get("scoreable")
    if n_score is None:
        n_score = len(cper) - n_err
    if n_score <= 0 or (cper and n_err > len(cper) / 2):
        return False, (f"eval dead — {n_err}/{len(cper)} cases errored; "
                       "refusing to promote on a broken measurement")

    # --- hard guards (apply to every mutation kind) ---
    regs = regressions(champ, cand)
    if regs:
        return False, f"regressed {sorted(regs)}"
    if cand_size > max_size:
        return False, f"skill {cand_size} > budget {max_size}"
    # noise guard: only on cases the champion already passed (added chatter
    # there is suspect; extra findings on newly-caught bugs are the point).
    held = passing_ids(champ)
    if not noise_ok(subset_noise(cand, held), subset_noise(champ, held), noise_tol):
        return False, "noise grew on already-passing cases"
    if champ_fp is not None and cand_fp is not None and cand_fp > champ_fp + precision_tol + 1e-9:
        return False, f"precision worse (FP {cand_fp} > {champ_fp})"

    # --- kind-specific gains ---
    if kind == "corpus":
        # the corpus is the SOURCE of the rule; the eval is the SAFETY GATE.
        # Incorporate a mined human pattern as long as it harms nothing measured.
        if cand["recall"] is None or champ["recall"] is None or cand["recall"] < champ["recall"]:
            return False, f"recall dropped ({cand['recall']} < {champ['recall']})"
        return True, "incorporated human-review pattern (no regression)"

    if kind == "consolidate":
        if cand["recall"] is None or champ["recall"] is None or cand["recall"] < champ["recall"]:
            return False, f"recall dropped ({cand['recall']} < {champ['recall']})"
        if cand_size >= champ_size:
            return False, f"no shrink ({cand_size} >= {champ_size})"
        return True, f"consolidated: -{champ_size - cand_size} chars, recall held"

    # kind == "propose"
    train_misses = {i for i in train_ids if i in failed_ids(champ)}
    caught = train_misses & passing_ids(cand)
    if not caught:
        return False, "no new train bug caught"
    cv, pv = split_recall(cand, val_ids), split_recall(champ, val_ids)
    if cv is not None and pv is not None and cv < pv:
        return False, f"val recall regressed ({cv} < {pv})"
    return True, f"caught {sorted(caught)}; val {pv}->{cv}"
