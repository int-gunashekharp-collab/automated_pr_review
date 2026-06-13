#!/usr/bin/env python3
"""Attempt memory — the loop remembers every patch it ever tried.

Why: without memory, the proposer can burn an iteration (and a full confirm,
~18×trials reviews) re-proposing an edit that was already rejected. With it:

  * every proposed patch gets a stable FINGERPRINT (file/action/content,
    whitespace-normalised) and is logged to workspace/attempts.jsonl
  * a patch whose fingerprint was already tried is skipped BEFORE any eval
    spend ("duplicate"), and the savings are reported
  * the most recent FAILED attempts (rationale + rejection reason) are fed
    back into the proposer prompt so the next proposal takes a different
    angle — the loop learns from its own mistakes, not just the corpus.

Append-only JSONL; survives restarts. Pure stdlib.
"""

from __future__ import annotations

import hashlib
import json
import re
import time

import config


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def fingerprint(patch: dict) -> str:
    """Stable id for a proposed patch — same edits => same fingerprint."""
    edits = patch.get("edits") or []
    parts = sorted(
        f"{_norm(e.get('file', ''))}|{_norm(e.get('action', 'append'))}|{_norm(e.get('content', ''))}"
        for e in edits)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _load() -> list[dict]:
    if not config.ATTEMPTS_FILE.exists():
        return []
    out = []
    for line in config.ATTEMPTS_FILE.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def record(iter_idx: int, kind: str, patch: dict, status: str, reason: str = "") -> str:
    """Log one attempt. status: accepted | rejected | screened_out | duplicate | error."""
    config.WORKSPACE.mkdir(parents=True, exist_ok=True)
    fp = fingerprint(patch)
    rec = {"ts": time.time(), "iter": iter_idx, "kind": kind, "fp": fp,
           "rationale": (patch.get("rationale") or "")[:300],
           "status": status, "reason": (reason or "")[:300]}
    with config.ATTEMPTS_FILE.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return fp


def seen_fingerprints() -> set[str]:
    """Fingerprints of every patch already tried (any outcome except duplicate)."""
    return {a["fp"] for a in _load() if a.get("status") != "duplicate"}


def recent_failures(n: int = 8) -> list[dict]:
    """Most recent non-accepted attempts, for proposer feedback (newest first)."""
    fails = [a for a in _load()
             if a.get("status") in ("rejected", "screened_out")]
    return list(reversed(fails))[:n]


def stats() -> dict:
    rows = _load()
    return {
        "total": len(rows),
        "unique": len({a["fp"] for a in rows}),
        "duplicates_skipped": sum(a.get("status") == "duplicate" for a in rows),
        "accepted": sum(a.get("status") == "accepted" for a in rows),
        "rejected": sum(a.get("status") in ("rejected", "screened_out") for a in rows),
    }
