#!/usr/bin/env python3
"""Proposal exporter — turn the champion into a human-reviewable bundle.

The hard rule stands: the loop NEVER writes into maestro-core and never opens
PRs. What it CAN do is make adoption a one-look decision. After every promotion
(and on demand via `python3 loop.py --export-proposal`) this writes, inside the
loop folder only:

  reports/proposal/
    PROPOSAL.md       — PR-ready body: metric deltas, evidence per promoted
                        change (from the ledger), apply instructions
    rubric.diff       — unified diff, live skill -> champion (review this!)
    champion-skill/   — the full improved rubric, ready to copy

A human reads PROPOSAL.md + rubric.diff, and if convinced copies the files (or
pastes the diff) into maestro-core themselves. Pure stdlib.
"""

from __future__ import annotations

import datetime
import difflib
import json
import shutil
from pathlib import Path

import config
import metrics


def _skill_files(d: Path) -> dict[str, str]:
    out = {}
    sk = d / "SKILL.md"
    if sk.exists():
        out["SKILL.md"] = sk.read_text()
    refs = d / "references"
    if refs.exists():
        for f in sorted(refs.glob("*.md")):
            out[f"references/{f.name}"] = f.read_text()
    return out


def _unified_diff(live: dict[str, str], champ: dict[str, str]) -> str:
    chunks = []
    for rel in sorted(set(live) | set(champ)):
        a, b = live.get(rel, ""), champ.get(rel, "")
        if a == b:
            continue
        chunks.append("".join(difflib.unified_diff(
            a.splitlines(keepends=True), b.splitlines(keepends=True),
            fromfile=f"live/{rel}", tofile=f"champion/{rel}")))
    return "\n".join(chunks) or "(no differences)\n"


def _accepted_ledger() -> list[dict]:
    if not config.LEDGER_FILE.exists():
        return []
    rows = []
    for line in config.LEDGER_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("status") == "accepted":
            rows.append(r)
    return rows


def export_proposal(champion_dir: Path | None = None) -> Path | None:
    """Write the proposal bundle. Returns the bundle dir, or None if no champion."""
    champion_dir = champion_dir or config.CHAMPION_DIR
    if not (champion_dir / "SKILL.md").exists():
        return None
    live = _skill_files(config.LIVE_SKILL_DIR) if config.LIVE_SKILL_DIR.exists() else {}
    champ = _skill_files(champion_dir)

    out = config.PROPOSAL_DIR
    if out.exists():
        shutil.rmtree(out)
    (out / "champion-skill").mkdir(parents=True)
    for rel, text in champ.items():
        dest = out / "champion-skill" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)

    diff_text = _unified_diff(live, champ)
    (out / "rubric.diff").write_text(diff_text)

    state = {}
    if config.STATE_FILE.exists():
        try:
            state = json.loads(config.STATE_FILE.read_text())
        except json.JSONDecodeError:
            state = {}
    ev = state.get("champion_eval") or {}
    val_ids = set(state.get("val_ids") or [])
    train_ids = set(state.get("train_ids") or [])
    accepted = _accepted_ledger()
    changed = [rel for rel in sorted(set(live) | set(champ))
               if live.get(rel, "") != champ.get(rel, "")]

    L = [
        f"# Proposal: improved PR-review conventions rubric ({datetime.date.today()})",
        "",
        "Produced by the self-improving loop (isolated; maestro-core untouched).",
        "Review `rubric.diff`, then copy `champion-skill/` over the live skill if convinced.",
        "",
        "## Measured result (golden eval, majority-voted)",
        "",
        f"- Overall recall: **{ev.get('recall', 'n/a')}** "
        f"({ev.get('passed', '?')}/{ev.get('scoreable', '?')})",
        f"- Train recall: {metrics.split_recall(ev, train_ids) if ev else 'n/a'} · "
        f"held-out val recall: **{metrics.split_recall(ev, val_ids) if ev else 'n/a'}**",
        f"- Noise: {ev.get('noise', 'n/a')} findings/review · "
        f"precision FP-rate: {state.get('champ_fp', 'n/a')}",
        f"- Rubric size: {state.get('champ_size', 'n/a')} chars "
        f"(budget {config.MAX_SKILL_CHARS})",
        "",
        f"## Promoted changes ({len(accepted)}) — each one passed every gate",
        "",
    ]
    if accepted:
        for r in accepted:
            L.append(f"- iter {r.get('iter')} · {r.get('kind')}: {r.get('rationale', '?')}")
            L.append(f"  - gate verdict: {r.get('reason', '?')}")
            for c in r.get("changelog", []):
                L.append(f"  - {c}")
    else:
        L.append("- (none yet — champion equals the seed skill)")
    L += [
        "",
        f"## Files changed ({len(changed)})",
        "",
        *([f"- `{rel}`" for rel in changed] if changed else ["- (none)"]),
        "",
        "## How to adopt (manual, by design)",
        "",
        "```bash",
        "# from maestro-core root — review rubric.diff first!",
        f"cp -r '{out / 'champion-skill'}'/* .claude/skills/maestro-review-conventions/",
        "git checkout -b pr-review-rubric-update && git add -A && git commit",
        "```",
        "",
        "_Every change above was screened, majority-vote confirmed on the full golden",
        "set, regression-guarded, noise-guarded, size-guarded and (periodically)",
        "precision-guarded before promotion. Held-out val recall is the honest number._",
    ]
    (out / "PROPOSAL.md").write_text("\n".join(L) + "\n")
    return out
