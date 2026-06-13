#!/usr/bin/env python3
"""Extract human PR-review comments from the mined corpus — the learning source.

This is the integration with your existing review architecture: instead of only
reasoning from the 18 golden bugs, the loop reads what your reviewers ACTUALLY
said across thousands of PRs (docs/.pr-review-corpus/, refreshed weekly by
mine-pr-reviews.yml), attaches the resolution signal (did the author act on it?),
and feeds it to the proposer to be understood and turned into rubric rules — each
then gated by the evals/ harness before it sticks.

Mirrors the filtering in scripts/redistill_conventions.py so the same notion of
a "substantive human comment" is used everywhere.
"""

from __future__ import annotations

import json

import config


def _comments_file():
    return config.CORPUS_DIR / "review_comments.jsonl"


def available() -> bool:
    return _comments_file().exists()


def _resolution_map() -> dict:
    """(pr, first-comment-prefix) -> is_resolved, from the GraphQL thread mine."""
    out: dict = {}
    tf = config.CORPUS_DIR / "review_threads.jsonl"
    if not tf.exists():
        return out
    for line in tf.read_text().splitlines():
        if not line.strip():
            continue
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            continue
        if t.get("comments"):
            key = (t["pr"], (t["comments"][0].get("body") or "")[:80])
            out[key] = t.get("is_resolved")
    return out


def _substantive(c: dict) -> bool:
    return (not c.get("is_bot")
            and not c.get("in_reply_to_id")
            and len(c.get("body") or "") >= config.CORPUS_MIN_LEN)


def load_human_comments(offset: int = 0, limit: int | None = None) -> tuple[list[dict], int]:
    """Return (batch, total). Newest-first, windowed by `offset` so successive
    corpus passes work through the whole corpus instead of re-reading the top."""
    if not available():
        return [], 0
    limit = limit or config.CORPUS_BATCH
    resolved = _resolution_map()

    rows = []
    for line in _comments_file().read_text().splitlines():
        if not line.strip():
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _substantive(c):
            rows.append(c)
    rows.sort(key=lambda c: c.get("created_at", ""), reverse=True)

    total = len(rows)
    if total == 0:
        return [], 0
    start = offset % total
    window = (rows + rows)[start:start + limit]  # wrap-around slice

    batch = []
    for c in window:
        batch.append({
            "pr": c["pr"], "path": c.get("path"),
            "resolved": resolved.get((c["pr"], (c["body"] or "")[:80])),
            "body": (c["body"] or "")[:500],
            "hunk": (c.get("diff_hunk") or "")[:300],
        })
    return batch, total
