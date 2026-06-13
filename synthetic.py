#!/usr/bin/env python3
"""Corpus shadow eval — measure the reviewer against REAL human catches.

The golden set is 18 cases; that's a precise but narrow ruler. The mined corpus
holds thousands of bugs your reviewers actually caught (a RESOLVED review
comment + its diff hunk = a real bug, found by a human, fixed by the author).

Every SYNTH_EVERY iterations this module samples a rotating window of those,
shows the champion reviewer each hunk, and checks whether it raises the same
issue the human did (keyword overlap with the human's comment). The result is
the **corpus shadow recall** — "of the things your humans caught, what fraction
does the AI catch?" — i.e. the literal scoreboard for matching human reviewers.

HONESTY: keyword matching against free-text comments is noisy, so this is a
REPORTED, directional metric. It never gates promotions (the golden set +
precision gates do). It exists to (a) track human-parity over a far larger
sample than 18 cases and (b) surface which human-caught bug classes the rubric
still misses.
"""

from __future__ import annotations

import json
import re

import config
import corpus

# words that appear in almost every review comment — useless as evidence
_STOP = {
    "should", "would", "could", "please", "think", "maybe", "instead", "rather",
    "there", "these", "those", "which", "where", "while", "about", "after",
    "before", "because", "since", "other", "really", "actually", "probably",
    "needs", "need", "want", "wants", "going", "doing", "done", "make", "makes",
    "made", "making", "thing", "things", "something", "code", "change", "changes",
    "changed", "this", "that", "with", "from", "have", "here", "just", "also",
    "will", "when", "what", "then", "them", "they", "your", "youre", "dont",
    "doesnt", "isnt", "cant", "wont", "better", "worth", "looks", "look", "like",
    "small", "minor", "might", "must", "shall", "still", "same", "issue", "fix",
    "fixed", "right", "wrong", "good", "great", "consider", "suggest", "suggestion",
    "remove", "removed", "added", "update", "updated", "review", "comment", "file",
    "line", "lines", "function", "method", "case", "cases", "value", "values",
    "every", "always", "never", "using", "used", "uses",
}


def _keywords(body: str, k: int = 4) -> list[str]:
    """Distinctive tokens from a human comment — code-ish tokens first."""
    body = re.sub(r"```.*?```", " ", body, flags=re.S)          # drop code fences
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_.]{3,}", body)
    seen, codeish, plain = set(), [], []
    for w in raw:
        lw = w.lower().strip(".")
        if lw in seen or lw in _STOP or len(lw) < 5:
            continue
        seen.add(lw)
        # identifiers (snake_case, camelCase, dotted.paths) are strong evidence
        if "_" in w or "." in w or (w[0].islower() and any(c.isupper() for c in w[1:])):
            codeish.append(lw)
        else:
            plain.append(lw)
    return (codeish + plain)[:k]


def build_cases(offset: int = 0, limit: int | None = None) -> tuple[list[dict], int]:
    """Rotating window of synthetic cases from RESOLVED human comments.

    Returns (cases, total_eligible). Each case: id, pr, diff (the hunk),
    keywords (evidence the reviewer must echo), body (for the report).
    """
    if not corpus.available():
        return [], 0
    limit = limit or config.SYNTH_SAMPLE
    resolved = corpus._resolution_map()

    rows = []
    for line in (config.CORPUS_DIR / "review_comments.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        if c.get("is_bot") or c.get("in_reply_to_id"):
            continue
        body, hunk = c.get("body") or "", c.get("diff_hunk") or ""
        if len(body) < config.CORPUS_MIN_LEN or len(hunk) < config.SYNTH_MIN_HUNK:
            continue
        if resolved.get((c["pr"], body[:80])) is not True:
            continue  # only comments the author demonstrably acted on
        kws = _keywords(body)
        if len(kws) < 2:
            continue
        rows.append({"id": f"syn-{c['pr']}-{c.get('id', len(rows))}",
                     "pr": c["pr"], "path": c.get("path") or "unknown",
                     "diff": hunk[:4000], "keywords": kws, "body": body[:300]})

    total = len(rows)
    if total == 0:
        return [], 0
    start = offset % total
    return (rows + rows)[start:start + min(limit, total)], total


def evaluate_shadow(skill_dir, cases: list[dict], *, run_reviewer_fn,
                    build_prompt_fn, load_skill_fn) -> dict:
    """Review each human-caught hunk; caught = reviewer echoes the human's evidence.

    Per-case errors are skipped (not penalised) so a malformed hunk can't sink
    the metric. Never used as a promotion gate.
    """
    skill_text = load_skill_fn(base=skill_dir)
    caught, checked, errored, misses = 0, 0, 0, []
    for case in cases:
        try:
            diff = (f"diff --git a/{case['path']} b/{case['path']}\n"
                    f"--- a/{case['path']}\n+++ b/{case['path']}\n{case['diff']}")
            prompt = build_prompt_fn(diff, skill_text, config.USE_MCP, config.USE_WORKFLOW)
            pf = config.RUNS_DIR / f"shadow-{case['id']}-prompt.txt"
            out = (run_reviewer_fn(prompt, pf) or "").lower()
        except Exception:
            errored += 1
            continue
        checked += 1
        if any(k in out for k in case["keywords"]):
            caught += 1
        else:
            misses.append({"id": case["id"], "pr": case["pr"],
                           "human_said": case["body"][:160],
                           "keywords": case["keywords"]})
    return {
        "shadow_recall": round(caught / checked, 3) if checked else None,
        "checked": checked, "caught": caught, "errored": errored,
        "misses": misses[:10],
    }
