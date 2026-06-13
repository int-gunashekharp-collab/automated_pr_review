#!/usr/bin/env python3
"""Counterfactual replay: champion vs live skill on recent merged PRs."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import config
import harness_bridge as hb

def _default_run_gh(args: list[str]) -> str:
    cmd = ["gh"] + args
    return subprocess.check_output(cmd, text=True)

def get_recent_merged_prs(n: int, run_gh=None) -> list[dict]:
    run_gh = run_gh or _default_run_gh
    out = run_gh(["pr", "list", "--state", "merged", "--json", "number,title,url", "--limit", str(n)])
    try:
        prs = json.loads(out)
    except Exception:
        prs = []

    cases = []
    for pr in prs:
        try:
            diff_out = run_gh(["pr", "diff", str(pr["number"])])
        except Exception:
            continue
        cases.append({
            "id": f"pr-{pr['number']}",
            "pr": pr["number"],
            "title": pr.get("title", ""),
            "url": pr.get("url", ""),
            "diff": diff_out
        })
    return cases

def evaluate_replay(skill_dir: Path, cases: list[dict], label: str, run_reviewer_fn=None) -> dict:
    run_reviewer_fn = run_reviewer_fn or hb.default_run_reviewer
    skill_text = hb.load_skill(base=skill_dir)
    fp = hashlib.sha256(skill_text.encode()).hexdigest()[:12]
    
    config.EVAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ck = config.EVAL_CACHE_DIR / f"replay-{label}.jsonl"
    done = {}
    if ck.exists():
        for line in ck.read_text().splitlines():
            try:
                row = json.loads(line)
                if row.get("skill") == fp:
                    done[row["id"]] = row
            except json.JSONDecodeError:
                pass

    results = {}
    passed_calls = 0
    
    for case in cases:
        if case["id"] in done:
            results[case["id"]] = done[case["id"]]["review"]
            continue
            
        try:
            prompt = hb.build_prompt(case["diff"], skill_text, config.USE_MCP, config.USE_WORKFLOW)
            pf = config.RUNS_DIR / f"replay-{label}-{case['id']}-prompt.txt"
            out = run_reviewer_fn(prompt, pf)
        except Exception as e:
            if "MAX_CALLS_PER_DAY" in str(e):
                raise
            print(f"Error reviewing {case['id']}: {e}", file=sys.stderr)
            continue
            
        passed_calls += 1
        results[case["id"]] = out
        
        with ck.open("a") as fh:
            fh.write(json.dumps({
                "id": case["id"],
                "skill": fp,
                "review": out,
                "ts": round(time.time(), 3)
            }) + "\n")
            
    return {"results": results, "calls": passed_calls}

def run_replay(n: int, run_gh=None, run_reviewer_fn=None) -> dict:
    cases = get_recent_merged_prs(n, run_gh=run_gh)
    if not cases:
        print("No PRs found for replay.")
        return {}

    if not config.CHAMPION_DIR.exists():
        print("Champion skill not found. Run the loop first to baseline.")
        return {}

    print(f"Replaying {len(cases)} PRs against live and champion skills...")
    
    live_dir = config.LIVE_SKILL_DIR if config.LIVE_SKILL_DIR.exists() else config.CHAMPION_DIR
    
    live_res = evaluate_replay(live_dir, cases, "live", run_reviewer_fn=run_reviewer_fn)
    champ_res = evaluate_replay(config.CHAMPION_DIR, cases, "champ", run_reviewer_fn=run_reviewer_fn)
    
    _generate_report(cases, live_res["results"], champ_res["results"])
    
    return {"cases": len(cases), "calls": live_res["calls"] + champ_res["calls"]}

def _generate_report(cases, live_results, champ_results):
    replay_dir = config.REPORTS_DIR / "replay"
    replay_dir.mkdir(parents=True, exist_ok=True)
    report_file = replay_dir / "REPLAY.md"
    
    lines = [
        "# Counterfactual Replay Report",
        f"Replayed **{len(cases)}** recent merged PRs.\n",
        "## Summary",
        "| PR | Live Skill | Champion Skill | diff |",
        "|---|---|---|---|"
    ]
    
    details = []
    
    for c in cases:
        cid = c["id"]
        live_out = live_results.get(cid, "")
        champ_out = champ_results.get(cid, "")
        
        live_len = len(live_out.strip())
        champ_len = len(champ_out.strip())
        
        diff_ind = "same" if live_out == champ_out else "DIFFERENT"
        
        lines.append(f"| [{c['pr']}]({c.get('url', '')}) | {live_len} chars | {champ_len} chars | {diff_ind} |")
        
        if diff_ind == "DIFFERENT":
            details.append(f"### PR {c['pr']}")
            details.append(f"**Live**:\n```\n{live_out}\n```\n")
            details.append(f"**Champion**:\n```\n{champ_out}\n```\n")
            
    lines.append("\n## Differences\n")
    lines.extend(details)
    
    report_file.write_text("\n".join(lines))
    print(f"Replay report written to {report_file}")
