#!/usr/bin/env python3
"""STUB harness — a faithful, offline stand-in for maestro-core's
scripts/eval_pr_reviewer.py, used ONLY when the real one can't be imported AND
the caller has explicitly opted in with LOOP_ALLOW_STUB=1 (the smoke test does).

Purpose: the smoke test (and any CI box without maestro-core checked out) can
exercise the loop's full control flow with zero network and zero Vertex. It
mirrors the exact library surface harness_bridge consumes:

    DIFF_CACHE, RUNS_DIR        (assignable module attrs)
    get_diff(case)              -> str
    load_skill(base=Path)       -> str
    build_prompt(diff, skill, with_mcp, use_workflow) -> str
    score(case, out)            -> {"passed": bool, "findings_count": int}
    run_reviewer(...)           -> str

A REAL run never lands here: without LOOP_ALLOW_STUB=1, harness_bridge still
fails loudly if maestro-core is missing — fake reviews must never look real.
"""

from __future__ import annotations

import re
from pathlib import Path

# Reassigned by harness_bridge into the loop workspace; defaults are inert.
DIFF_CACHE = Path("/tmp/loop-stub-diffs")
RUNS_DIR = Path("/tmp/loop-stub-runs")


def get_diff(case: dict) -> str:  # pragma: no cover - smoke injects its own
    raise RuntimeError(
        "stub harness has no network — inject get_diff_fn (smoke test does)")


def run_reviewer(prompt: str, model: str = "", cmd_template=None,
                 prompt_file: Path | None = None, with_mcp: bool = False,
                 engine: str = "gemini") -> str:  # pragma: no cover
    raise RuntimeError(
        "stub harness has no Vertex — inject run_reviewer_fn (smoke test does)")


def load_skill(base: Path) -> str:
    parts = [(base / "SKILL.md").read_text()]
    refs = base / "references"
    if refs.exists():
        for f in sorted(refs.glob("*.md")):
            parts.append(f.read_text())
    return "\n\n".join(parts)


def build_prompt(diff: str, skill_text: str, with_mcp: bool = False,
                 use_workflow: bool = False) -> str:
    return (
        "You are a senior code reviewer. Apply the conventions below and review "
        "the diff. Cite file:line, severity, and the concrete failure scenario.\n\n"
        f"=== CONVENTIONS ===\n{skill_text}\n\n=== DIFF ===\n{diff}\n")


def score(case: dict, out: str) -> dict:
    """passed = the review mentions any required evidence for the known bug;
    findings_count = how many distinct flagged findings the review raised."""
    low = (out or "").lower()
    passed = any(p.lower() in low for p in case.get("must_match_any", []))
    findings = len(re.findall(r"🔴|🟠|🟡|^\s*[-*]\s+\S", out or "", flags=re.M))
    return {"passed": passed, "findings_count": findings}
