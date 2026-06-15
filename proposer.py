#!/usr/bin/env python3
"""The 'think deep' step: Gemini 3.1 Pro proposes rubric changes.

Two mutation kinds:
  * propose_edit       — close TRAIN misses (and stop known false positives)
  * propose_consolidation — shrink the rubric losslessly when it bloats

Both use the same Vertex path as the reviewer (scripts/gemini_vertex.py) and
return a strict JSON patch the patcher applies mechanically.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import config
import maestro_context
import telemetry

EDIT_PROMPT = """You maintain the maestro-core PR-review conventions skill. An
eval ran the reviewer (Gemini 3.1 Pro) over historical PRs with known,
human-verified bugs. Below: (1) the FULL current skill, (2) the TRAIN bugs the
reviewer MISSED (with what it said instead), (3) optionally, FALSE POSITIVES
it raised on already-fixed code, and (4) DISMISSED FINDINGS from real PRs where the author pushed back.

Propose the SMALLEST edit that makes the reviewer catch the misses, stop the
false positives, and stop flagging the dismissed patterns.

Hard rules:
- Be mechanism-specific: name the exact pattern, the failure, and the bug class.
- Ground it in the MAESTRO-CORE CONTEXT below — cite real modules/files/patterns
  from THIS codebase, not generic advice.
- Do NOT write a rule that just names a specific PR/file from a miss — that is
  memorisation. Generalise to the underlying pattern so it transfers to unseen
  code.
- Do NOT add broad "scrutinise everything" guidance — it raises false positives
  and the eval penalises noise (current mean {noise} findings/review).
- Prefer STRENGTHENING ONE existing reference file; create a new one only if
  nothing fits.
- Edit only SKILL.md or files under references/.

Reply with ONLY this JSON, no prose around it:
{{
  "rationale": "one sentence: what gap this closes",
  "edits": [
    {{"file": "references/<name>.md", "action": "append", "content": "<markdown>"}}
  ]
}}
"append" appends to an existing file; "create" makes references/<name>.md.

=== MAESTRO-CORE CONTEXT (read-only — how this codebase works; ground your rule in it) ===
{maestro_context}

=== CURRENT SKILL ===
{skill}

=== MISSED TRAIN BUGS ===
{misses}

=== FALSE POSITIVES (reviewer flagged these already-fixed issues) ===
{fps}

=== DISMISSED FINDINGS (reviewer flagged these but author rebutted/ignored) ===
{dismissed}
"""

CORPUS_PROMPT = """You maintain the maestro-core PR-review conventions skill.
Below: (1) the FULL current skill, (2) real human review comments mined from
this team's merged PRs, with resolution status, and (3) ACTED-ON AI FINDINGS where the author accepted the AI's suggestion.

UNDERSTAND these comments and findings and find ONE recurring pattern (backed by >= 2
comments, preferring RESOLVED ones) that the current skill does NOT already
cover. Turn it into a single rubric improvement.

Hard rules:
- It must be a genuine, generalisable rule — name the mechanism and bug class,
  not one specific PR. Cite the PR numbers as evidence.
- If the strongest signal is authors PUSHING BACK on a comment class
  (unresolved + rebuttal), add a do-not-flag entry instead (append to
  references/do-not-flag.md) — that raises precision.
- If nothing recurs clearly, return a single edit whose content is the exact
  string "NO-NEW-PATTERN" appended to references/do-not-flag.md is NOT allowed;
  instead return {{"rationale":"no clear new pattern","edits":[]}} and nothing else.
- Edit only SKILL.md or files under references/.

Reply with ONLY this JSON, no prose:
{{
  "rationale": "the human pattern you incorporated + PRs",
  "edits": [
    {{"file": "references/<name>.md", "action": "append", "content": "<markdown rule>"}}
  ]
}}

=== MAESTRO-CORE CONTEXT (read-only — how this codebase works; ground your rule in it) ===
{maestro_context}

=== CURRENT SKILL ===
{skill}

=== HUMAN REVIEW COMMENTS (JSONL: pr, path, resolved, body, hunk) ===
{comments}

=== ACTED-ON AI FINDINGS (AI found these, author accepted; codify if not covered) ===
{acted_on}
"""

CONSOLIDATE_PROMPT = """You maintain the maestro-core PR-review conventions
skill. It has grown to {size} characters and needs to be more concise WITHOUT
losing any rule or weakening any guidance — every bug class currently covered
must remain covered.

Rewrite the file(s) below to be shorter: merge duplicate rules, tighten prose,
drop redundancy. Do NOT remove any distinct rule, severity calibration, or
do-not-flag pattern.

Reply with ONLY this JSON, no prose:
{{
  "rationale": "what you merged/tightened",
  "edits": [
    {{"file": "references/<name>.md", "action": "rewrite", "content": "<full new file content>"}}
  ]
}}
Use action "rewrite" to replace a whole file. Only include files you changed.

=== CURRENT SKILL FILES ===
{skill}
"""

# Reflection: appended to EDIT_PROMPT when the loop has memory of failed
# attempts — the proposer sees what it already tried and WHY each was rejected,
# so it stops circling the same dead end and changes strategy.
REFLECT_ADDENDUM = """

=== YOUR OWN RECENT FAILED ATTEMPTS (memory — do NOT repeat these) ===
{failures}

Every attempt above was tried and REJECTED for the stated reason. Propose a
FUNDAMENTALLY DIFFERENT edit this time: a different reference file, a different
rule shape (e.g. a do-not-flag subtraction instead of an addition, or a
severity recalibration), or a different mechanism for the same bug class.
"""

# Beam diversity: appended to candidates after the first within one iteration,
# so K proposals explore K different angles instead of paraphrasing each other.
DIVERSITY_ADDENDUM = """

=== ALREADY PROPOSED THIS ITERATION (yours must differ materially) ===
{others}

Take a different angle than the proposal(s) above — a different file, a
different mechanism, or a different missed bug as the primary target.
"""

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _format_failures(failures: list[dict]) -> str:
    return "\n".join(
        f"- [{f.get('kind', '?')}] tried: {f.get('rationale', '?')!r} -> "
        f"rejected: {f.get('reason', '?')}" for f in failures) or "(none)"


def _full_skill_text(skill_dir: Path) -> str:
    parts = [f"--- SKILL.md ---\n{(skill_dir / 'SKILL.md').read_text()}"]
    for f in sorted((skill_dir / "references").glob("*.md")):
        parts.append(f"\n--- references/{f.name} ---\n{f.read_text()}")
    return "\n".join(parts)


def _format_misses(missed: list[dict]) -> str:
    if not missed:
        return "(none)"
    # Highest-severity misses first — the proposer's attention follows order,
    # so a critical miss should never hide behind a low one.
    missed = sorted(missed, key=lambda m: _SEVERITY_ORDER.get(
        str(m.get("severity", "high")).lower(), 1))
    out = []
    for m in missed:
        out.append(
            f"- id {m['id']} (PR #{m['pr']}, {m['severity']}) file {m['path']}\n"
            f"  bug: {m['bug']}\n"
            f"  must mention any of: {m['must_match_any']}\n"
            f"  reviewer said: {m['excerpt'][:500].strip()!r}")
    return "\n".join(out)


def _format_fps(fps: list[dict]) -> str:
    if not fps:
        return "(none observed this round)"
    return "\n".join(
        f"- id {f['id']} file {f['path']}: re-flagged the already-fixed "
        f"'{f['bug'][:80]}' — said {f['excerpt'][:300].strip()!r}" for f in fps)


def _format_outcomes(outcomes: list[dict] | None) -> str:
    if not outcomes:
        return "(none)"
    return "\n".join(f"- PR #{o.get('pr')} {o.get('path')}: {o.get('body', '')[:300]}" for o in outcomes)


def call_vertex(prompt: str, *, log_name: str = "proposer-prompt.txt",
                prefix: str = "proposer-") -> str:
    """One Vertex call (the SAME brain as the reviewer, scripts/gemini_vertex.py)
    with bounded retry/backoff — a transient 429/500 should cost a short sleep,
    not a whole iteration.

    Shared by the proposer and the in-house builder (scripts/ralph/ralph.py) so
    both speak to the identical Gemini-on-Vertex endpoint. `log_name` lets each
    caller keep its own prompt log under RUNS_DIR without clobbering the other."""
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    pf = config.RUNS_DIR / log_name
    pf.write_text(prompt)
    env = {**os.environ, "GEMINI_MODEL": config.GEMINI_MODEL,
           "GOOGLE_CLOUD_PROJECT": config.GCP_PROJECT}
    last_err = ""
    for attempt in range(1 + max(0, config.MODEL_RETRIES)):
        if attempt:
            time.sleep(config.RETRY_BACKOFF_SEC * attempt)
        with tempfile.TemporaryDirectory(prefix=prefix) as sandbox:
            r = subprocess.run(
                [sys.executable, str(config.SCRIPTS_DIR / "gemini_vertex.py"), str(pf)],
                capture_output=True, text=True, timeout=1200, cwd=sandbox, env=env)
        if r.returncode == 0:
            return r.stdout
        # keep the TAIL of stderr — warnings print first, the real traceback last
        last_err = r.stderr.strip()[-600:]
    raise RuntimeError(
        f"gemini_vertex failed after {1 + config.MODEL_RETRIES} tries: {last_err}")


def _default_call_model(prompt: str) -> str:
    """Back-compat shim: the loop's proposer path uses the shared Vertex call."""
    return call_vertex(prompt)


def _extract_json(text: str) -> dict:
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                esc = (c == "\\" and not esc)
                if c == '"' and not esc:
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise ValueError("no valid JSON object found in proposer output")


def _finish(raw: str) -> dict:
    patch = _extract_json(raw)
    if not isinstance(patch.get("edits"), list) or not patch["edits"]:
        raise ValueError("proposer returned no edits")
    return patch


def _ctx() -> str:
    """maestro-core grounding for the proposer prompt (config-gated, crash-proof)."""
    if not getattr(config, "MAESTRO_CONTEXT", False):
        return "(maestro-core grounding disabled — set LOOP_MAESTRO_CONTEXT=1)"
    try:
        return maestro_context.load_context()
    except Exception:  # noqa: BLE001 — grounding must never break a proposal
        return "(maestro-core grounding unavailable)"


def propose_edit(champion_dir: Path, eval_result: dict, *,
                 false_positives=None, dismissed_outcomes=None, call_model_fn=None,
                 failed_attempts: list[dict] | None = None,
                 avoid: list[str] | None = None) -> dict:
    """Propose one rubric edit targeting the train misses.

    `failed_attempts` (from attempt memory) makes the proposer reflect on its
    own rejected ideas; `avoid` lists rationales already proposed THIS iteration
    so beam candidates diversify instead of paraphrasing each other.
    """
    call_model_fn = call_model_fn or _default_call_model
    prompt = EDIT_PROMPT.format(
        noise=eval_result.get("noise"),
        maestro_context=_ctx(),
        skill=_full_skill_text(champion_dir),
        misses=_format_misses(eval_result.get("missed_train", eval_result.get("missed", []))),
        fps=_format_fps(false_positives or []),
        dismissed=_format_outcomes(dismissed_outcomes))
    if failed_attempts:
        prompt += REFLECT_ADDENDUM.format(failures=_format_failures(failed_attempts))
    if avoid:
        prompt += DIVERSITY_ADDENDUM.format(
            others="\n".join(f"- {a}" for a in avoid if a) or "(none)")
    raw = call_model_fn(prompt)
    telemetry.thought("propose", prompt, raw)
    return _finish(raw)


def propose_consolidation(champion_dir: Path, *, call_model_fn=None) -> dict:
    call_model_fn = call_model_fn or _default_call_model
    text = _full_skill_text(champion_dir)
    prompt = CONSOLIDATE_PROMPT.format(size=len(text), skill=text)
    raw = call_model_fn(prompt)
    telemetry.thought("consolidate", prompt, raw)
    return _finish(raw)


def _format_corpus(comments: list[dict]) -> str:
    return "\n".join(json.dumps(c, ensure_ascii=False) for c in comments)


def propose_from_corpus(champion_dir: Path, comments: list[dict], *,
                        acted_on_outcomes=None, call_model_fn=None) -> dict:
    """Understand a batch of human review comments and propose ONE rubric rule.
    May return {"edits": []} when nothing new recurs — the loop then skips."""
    call_model_fn = call_model_fn or _default_call_model
    prompt = CORPUS_PROMPT.format(
        maestro_context=_ctx(),
        skill=_full_skill_text(champion_dir), comments=_format_corpus(comments),
        acted_on=_format_outcomes(acted_on_outcomes))
    raw = call_model_fn(prompt)
    telemetry.thought("corpus", prompt, raw, comments=len(comments))
    patch = _extract_json(raw)
    patch.setdefault("edits", [])
    if not isinstance(patch["edits"], list):
        patch["edits"] = []
    return patch
