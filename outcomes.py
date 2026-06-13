#!/usr/bin/env python3
"""Outcome mining: harvest production shadow-review outcomes.

Mines the fate of the AI reviewer's comments on recent merged PRs to
differentiate between acted_on and dismissed findings.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import config


def _get_outcomes_file() -> Path:
    config.WORKSPACE.mkdir(parents=True, exist_ok=True)
    return config.WORKSPACE / "outcomes.jsonl"


def _default_run_gh(args: list[str]) -> str:
    res = subprocess.run(["gh"] + args, capture_output=True, text=True, check=True)
    return res.stdout


def _fetch_merged_prs(limit: int, run_gh) -> list[int]:
    out = run_gh(["pr", "list", "--state", "merged", "--limit", str(limit), "--json", "number"])
    return [pr["number"] for pr in json.loads(out)]


def _fetch_pr_data(pr: int, run_gh) -> dict:
    out = run_gh(["pr", "view", str(pr), "--json", "reviewThreads"])
    return json.loads(out)


def classify_thread(thread: dict, bot_login: str) -> dict | None:
    """
    Classify a review thread started by the AI bot.
    Returns None if not started by the bot.
    acted_on: resolved
    dismissed: unresolved + has rebuttal (a reply from someone else)
    unknown: otherwise
    """
    comments = thread.get("comments", [])
    if not comments:
        return None
    
    first = comments[0]
    author = first.get("author", {}).get("login", "")
    if author != bot_login:
        return None
        
    is_resolved = thread.get("isResolved", False)
    has_reply = len(comments) > 1
    
    if is_resolved:
        outcome = "acted_on"
    elif has_reply:
        outcome = "dismissed"
    else:
        outcome = "unknown"
        
    return {
        "body": first.get("body", ""),
        "path": thread.get("path", ""),
        "outcome": outcome,
        "replies": len(comments) - 1
    }


def mine_outcomes(run_gh=None) -> dict:
    limit = config._i("LOOP_OUTCOMES_PRS", 10)
    bot_login = os.environ.get("LOOP_AI_REVIEWER_LOGIN", "snabbit-bot").strip()
    
    run_gh = run_gh or _default_run_gh
    outcomes_file = _get_outcomes_file()
    
    seen = set()
    if outcomes_file.exists():
        for line in outcomes_file.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                seen.add((rec.get("pr"), rec.get("body")))
            except json.JSONDecodeError:
                continue

    try:
        prs = _fetch_merged_prs(limit, run_gh)
    except Exception as e:
        print(f"Failed to fetch PRs: {e}")
        return {"scanned_prs": 0, "new_acted_on": 0, "new_dismissed": 0, "new_unknown": 0}

    stats = {"scanned_prs": len(prs), "new_acted_on": 0, "new_dismissed": 0, "new_unknown": 0}
    
    with outcomes_file.open("a") as f:
        for pr in prs:
            try:
                data = _fetch_pr_data(pr, run_gh)
            except Exception:
                continue
                
            threads = data.get("reviewThreads", [])
            for thread in threads:
                clf = classify_thread(thread, bot_login)
                if not clf:
                    continue
                    
                key = (pr, clf["body"])
                if key in seen:
                    continue
                    
                seen.add(key)
                rec = {
                    "pr": pr,
                    "path": clf["path"],
                    "body": clf["body"],
                    "outcome": clf["outcome"],
                    "replies": clf["replies"]
                }
                f.write(json.dumps(rec) + "\n")
                
                stats[f"new_{clf['outcome']}"] += 1

    return stats
