#!/usr/bin/env python3
"""Silver eval — the eval set that grows itself.

The golden set (18 hand-curated cases) is a precise but FIXED ruler; the loop
saturates it quickly. The corpus, refreshed weekly, holds thousands of verified
bugs: a RESOLVED human review comment + the diff hunk it was on is a real bug,
found by a human, fixed by the author. This module turns the best of those into
first-class eval cases — the SILVER set — which the loop then trains on and is
gated by exactly like golden cases.

Pipeline per harvest:
  scan corpus window -> filter (resolved, substantive, hunk big enough, not
  already harvested) -> ONE Gemini call formalises a batch into eval cases
  (one-line bug, 2-4 distinctive must_match keywords, severity, confidence)
  -> validity + confidence + dedupe gates -> admitted cases appended to
  workspace/silver.jsonl with their FROZEN diff inline (offline, deterministic,
  no gh refetch) -> reports/silver/ bundle refreshed for optional manual
  promotion into maestro-core's real golden.jsonl (by a human; the loop still
  never writes into maestro-core).

Honesty notes:
  * Silver cases are machine-formalised; a badly keyworded case can never pass.
    That hurts ABSOLUTE recall but not the loop's decisions — champion and
    candidate are always compared on the SAME set, so a dead case penalises
    both equally. `--list-silver` exposes never-passed cases; `--retire-silver`
    removes them (status flip, append-only history preserved).
  * Every admitted case grows confirm cost by CONFIRM_TRIALS reviews/iteration.
    SILVER_MAX caps the active set.
"""

from __future__ import annotations

import json
import re
import time

import config
import corpus
import proposer

FORMALIZE_PROMPT = """You convert real, RESOLVED human PR-review comments into
machine-checkable eval cases for an AI code reviewer. Each input has: pr, path,
the human's comment (a real bug — the author fixed it), and the diff hunk the
comment was on.

For EACH input, either formalise it or reject it.

Formalise only when the comment points at a concrete code defect visible in the
hunk (not style taste, not praise, not a question). Then produce:
- "bug": one precise sentence naming the defect and its consequence.
- "must_match_any": 2-4 lowercase keywords/identifiers such that ANY one of
  them appearing in a code-review of this hunk means the reviewer caught THIS
  bug. Prefer distinctive identifiers from the hunk/comment (function names,
  field names, mechanism words like "idempotency", "timezone", "n+1").
  NEVER generic words ("bug", "issue", "error", "fix", "problem", "code").
- "severity": one of critical|high|medium|low.
- "confidence": 0.0-1.0 that this is a faithful, checkable case.

Reply with ONLY a JSON array, one object per input, same order:
[
  {{"idx": 0, "reject": false, "bug": "...", "must_match_any": ["...",".."],
    "severity": "high", "confidence": 0.9}},
  {{"idx": 1, "reject": true, "reason": "style preference, not a defect"}}
]

=== INPUTS ===
{inputs}
"""

_GENERIC = {"bug", "issue", "error", "fix", "problem", "code", "wrong", "bad",
            "incorrect", "missing", "change", "update", "review"}


def _records() -> list[dict]:
    if not config.SILVER_FILE.exists():
        return []
    out = []
    for line in config.SILVER_FILE.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _append(rec: dict):
    config.WORKSPACE.mkdir(parents=True, exist_ok=True)
    with config.SILVER_FILE.open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _latest_by_id() -> dict[str, dict]:
    """Last record per id wins (retire = newer record with status retired)."""
    out: dict[str, dict] = {}
    for r in _records():
        out[r["id"]] = r
    return out


def active_cases() -> list[dict]:
    """Golden-shaped cases (id/pr/path/bug/must_match_any/severity) with the
    frozen `diff` inline, plus silver=True for display."""
    out = []
    for r in _latest_by_id().values():
        if r.get("status") != "active":
            continue
        out.append({"id": r["id"], "pr": r["pr"], "path": r["path"],
                    "bug": r["bug"], "must_match_any": r["must_match_any"],
                    "severity": r.get("severity", "high"), "diff": r["diff"],
                    "silver": True})
    return sorted(out, key=lambda c: c["id"])


def stats() -> dict:
    latest = _latest_by_id()
    active = [r for r in latest.values() if r.get("status") == "active"]
    return {"active": len(active),
            "retired": sum(r.get("status") == "retired" for r in latest.values()),
            "last_harvest_ts": max((r.get("added_ts", 0) for r in latest.values()),
                                   default=None)}


def _seen_fingerprints() -> set[str]:
    return {r.get("src_fp") for r in _records() if r.get("src_fp")}


def _src_fp(c: dict) -> str:
    body = re.sub(r"\s+", " ", (c.get("body") or "")[:80].lower())
    return f"{c['pr']}|{body}"


def _mk_id(pr: int, taken: set[str]) -> str:
    base = f"s{pr}"
    if base not in taken:
        return base
    for suf in "abcdefghij":
        if base + suf not in taken:
            return base + suf
    return f"{base}-{len(taken)}"


def _wrap_diff(path: str, hunk: str) -> str:
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{hunk}\n")


def _candidates(offset: int, scan: int) -> tuple[list[dict], int]:
    """Resolved, substantive, big-hunk comments not yet harvested."""
    if not corpus.available():
        return [], offset
    resolved = corpus._resolution_map()
    seen = _seen_fingerprints()
    rows = []
    for line in (config.CORPUS_DIR / "review_comments.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue  # real corpora contain truncated lines — skip, never crash
        if c.get("is_bot") or c.get("in_reply_to_id"):
            continue
        body, hunk = c.get("body") or "", c.get("diff_hunk") or ""
        if len(body) < config.CORPUS_MIN_LEN or len(hunk) < config.SYNTH_MIN_HUNK:
            continue
        if resolved.get((c["pr"], body[:80])) is not True:
            continue
        rows.append(c)
    total = len(rows)
    if not total:
        return [], offset
    picked, i = [], 0
    while i < min(scan, total) and len(picked) < config.SILVER_SCAN:
        c = rows[(offset + i) % total]
        i += 1
        if _src_fp(c) in seen:
            continue
        picked.append(c)
    return picked, offset + i


def _parse_array(text: str) -> list[dict]:
    start = text.find("[")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                esc = (ch == "\\" and not esc)
                if ch == '"' and not esc:
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        arr = json.loads(text[start:i + 1])
                        if isinstance(arr, list):
                            return [a for a in arr if isinstance(a, dict)]
                    except json.JSONDecodeError:
                        break
        start = text.find("[", start + 1)
    raise ValueError("no valid JSON array in formaliser output")


def _valid_keywords(kws, body: str, hunk: str) -> list[str]:
    """Keep distinctive, plausibly-checkable keywords."""
    out = []
    blob = (body + " " + hunk).lower()
    for k in (kws or []):
        if not isinstance(k, str):
            continue
        k = k.strip().lower()
        if not (3 <= len(k) <= 60) or k in _GENERIC:
            continue
        # keyword should be anchored in the evidence (comment or code)
        if k in blob or any(t in blob for t in k.split()):
            out.append(k)
    return out[:4]


def harvest(offset: int = 0, *, call_model_fn=None, run_reviewer_fn=None) -> dict:
    """One harvest pass. Returns {admitted, scanned, offset, ids, skipped}."""
    call_model_fn = call_model_fn or proposer._default_call_model
    cands, new_offset = _candidates(offset, config.SILVER_SCAN)
    cur = stats()
    room = max(0, config.SILVER_MAX - cur["active"])
    if not cands or room == 0:
        return {"admitted": 0, "scanned": len(cands), "offset": new_offset,
                "ids": [], "skipped": "no candidates" if not cands else "SILVER_MAX reached"}

    batch = cands[: max(config.SILVER_BATCH * 3, 6)]   # give the model headroom
    inputs = "\n".join(json.dumps({"idx": i, "pr": c["pr"], "path": c.get("path"),
                                   "comment": (c.get("body") or "")[:400],
                                   "hunk": (c.get("diff_hunk") or "")[:1200]},
                                  ensure_ascii=False)
                       for i, c in enumerate(batch))
    raw = call_model_fn(FORMALIZE_PROMPT.format(inputs=inputs))
    try:
        import telemetry
        telemetry.thought("silver", FORMALIZE_PROMPT.format(inputs=inputs)[:1500], raw)
    except Exception:
        pass
    results = _parse_array(raw)

    taken = set(_latest_by_id().keys())
    admitted, ids = 0, []
    for res in results:
        if admitted >= min(config.SILVER_BATCH, room):
            break
        if res.get("reject"):
            continue
        try:
            c = batch[int(res.get("idx", -1))]
        except (ValueError, TypeError, IndexError):
            continue
        conf = res.get("confidence")
        if not isinstance(conf, (int, float)) or conf < config.SILVER_MIN_CONF:
            continue
        kws = _valid_keywords(res.get("must_match_any"),
                              c.get("body") or "", c.get("diff_hunk") or "")
        if len(kws) < 2 or not res.get("bug"):
            continue
        sev = str(res.get("severity", "high")).lower()
        if sev not in ("critical", "high", "medium", "low"):
            sev = "high"
        cid = _mk_id(c["pr"], taken)
        taken.add(cid)
        rec = {"id": cid, "pr": c["pr"], "path": c.get("path") or "unknown",
               "bug": str(res["bug"])[:300], "must_match_any": kws,
               "severity": sev, "confidence": round(float(conf), 2),
               "diff": _wrap_diff(c.get("path") or "unknown",
                                  (c.get("diff_hunk") or "")[:4000]),
               "human_said": (c.get("body") or "")[:300],
               "src_fp": _src_fp(c), "status": "active",
               "added_ts": round(time.time(), 3), "baseline_passed": None}
        if run_reviewer_fn is not None:
            try:
                import harness_bridge as hb
                skill_text = hb.load_skill(base=config.CHAMPION_DIR)
                prompt = hb.build_prompt(rec["diff"], skill_text,
                                         config.USE_MCP, config.USE_WORKFLOW)
                pf = config.RUNS_DIR / f"silver-{cid}-baseline-prompt.txt"
                out = run_reviewer_fn(prompt, pf)
                rec["baseline_passed"] = bool(hb.score(rec, out)["passed"])
            except Exception:
                rec["baseline_passed"] = None
        _append(rec)
        admitted += 1
        ids.append(cid)

    if admitted:
        export_candidates()
    return {"admitted": admitted, "scanned": len(cands), "offset": new_offset,
            "ids": ids, "skipped": None}


def retire(case_id: str) -> bool:
    latest = _latest_by_id()
    rec = latest.get(case_id)
    if not rec or rec.get("status") != "active":
        return False
    rec = dict(rec, status="retired", retired_ts=round(time.time(), 3))
    _append(rec)
    export_candidates()
    return True


def list_rows() -> list[dict]:
    return sorted(_latest_by_id().values(), key=lambda r: r["id"])


def export_candidates():
    """reports/silver/: golden-compatible lines a human can hand-promote into
    maestro-core's evals/pr-review/golden.jsonl. Loop never does it itself."""
    config.SILVER_DIR.mkdir(parents=True, exist_ok=True)
    rows = [r for r in _latest_by_id().values() if r.get("status") == "active"]
    lines, md = [], ["# Silver eval cases — candidates for the real golden set", "",
                     "Each line below is golden.jsonl-compatible. Review, then append",
                     "the ones you trust to maestro-core/evals/pr-review/golden.jsonl",
                     "yourself (the loop never writes into maestro-core).", ""]
    for r in sorted(rows, key=lambda x: -(x.get("confidence") or 0)):
        lines.append(json.dumps({"id": r["id"], "pr": r["pr"], "path": r["path"],
                                 "bug": r["bug"], "must_match_any": r["must_match_any"],
                                 "severity": r["severity"]}, ensure_ascii=False))
        md.append(f"- `{r['id']}` (PR #{r['pr']}, {r['severity']}, "
                  f"conf {r.get('confidence')}): {r['bug']}")
        md.append(f"  - human said: {r.get('human_said', '')[:160]!r}")
    (config.SILVER_DIR / "candidates.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""))
    (config.SILVER_DIR / "SILVER.md").write_text("\n".join(md) + "\n")
