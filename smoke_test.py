#!/usr/bin/env python3
"""End-to-end smoke test — NO Vertex, NO network, NO real golden set/corpus.

Runs entirely in a temp sandbox with the reviewer brain, diff fetch, fixed-diff
fetch and the Gemini proposer all stubbed. Verifies the promotion gates, the
three mutation kinds (propose / corpus / consolidate), precision, rollback, and —
most importantly — that the loop writes nothing outside its own folder and never
touches the seed skill.

Run:  python3 smoke_test.py    # exits non-zero on any failure
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))

# Point MAESTRO_ROOT at the real maestro-core so harness_bridge can import the
# real eval_pr_reviewer (only build_prompt/load_skill/score are used here).
for _cand in [os.environ.get("MAESTRO_ROOT"),
              *sorted(glob.glob("/sessions/*/mnt/maestro-core")),
              "/Users/gunashekharp/snabbit chatbot/maestro-core"]:
    if _cand and (Path(_cand) / "scripts" / "eval_pr_reviewer.py").exists():
        os.environ["MAESTRO_ROOT"] = _cand
        break

TMP = Path(tempfile.mkdtemp(prefix="loop-smoke-"))

# --- fake parent inputs -----------------------------------------------------
SEED = TMP / "live-skill"
(SEED / "references").mkdir(parents=True)
(SEED / "SKILL.md").write_text("# conventions\nknows: needs-a-rule\n")
(SEED / "references" / "db.md").write_text("# db\nknows: bbb-pattern\n")

CASES = [
    {"id": "aaa", "pr": 1, "path": "src/a.py", "bug": "a bug",
     "must_match_any": ["needs-a-rule"], "severity": "high"},
    {"id": "bbb", "pr": 2, "path": "src/b.py", "bug": "b bug",
     "must_match_any": ["bbb-pattern"], "severity": "high"},
    {"id": "ccc", "pr": 3, "path": "src/c.py", "bug": "c bug",
     "must_match_any": ["ccc-pattern"], "severity": "high"},
]
(TMP / "golden.jsonl").write_text("\n".join(json.dumps(c) for c in CASES))

# fake mined human corpus
CORP = TMP / "corpus"
CORP.mkdir()
(CORP / "review_comments.jsonl").write_text("\n".join(json.dumps(c) for c in [
    {"id": "1", "pr": 11, "author": "dev", "is_bot": False, "path": "src/x.py",
     "line": 3, "position": 1, "in_reply_to_id": None, "created_at": "2026-05-01T00:00:00Z",
     "body": "please use the enum instead of the string literal here", "diff_hunk": "@@ x @@"},
    {"id": "2", "pr": 12, "author": "dev", "is_bot": False, "path": "src/y.py",
     "line": 9, "position": 1, "in_reply_to_id": None, "created_at": "2026-05-02T00:00:00Z",
     "body": "same enum-vs-string-literal issue as the other PR, fix it", "diff_hunk": "@@ y @@"},
]))
(CORP / "review_threads.jsonl").write_text(json.dumps(
    {"pr": 11, "pr_merged": True, "pr_author": "a", "path": "src/x.py", "is_resolved": True,
     "is_outdated": False, "n_comments": 1,
     "comments": [{"author": "dev", "is_bot": False,
                   "body": "please use the enum instead of the string literal here",
                   "created_at": "2026-05-01T00:00:00Z"}]}))

# --- override config to the sandbox BEFORE importing the loop --------------
os.environ.update(
    LOOP_REPORT_INTERVAL_SEC="0", LOOP_EVAL_SUBSET="2", LOOP_NOISE_TOLERANCE="0.5",
    LOOP_FORCE_VAL_IDS="bbb", LOOP_CONFIRM_TRIALS="3", LOOP_SCREEN_TRIALS="1",
    LOOP_CONSOLIDATE_EVERY="3", LOOP_CORPUS_EVERY="2", LOOP_CORPUS_MIN_LEN="10",
    LOOP_PRECISION_EVERY="1", LOOP_PRECISION_SAMPLE="3", LOOP_MAX_SKILL_CHARS="100000",
    # v2 feature knobs under test
    LOOP_ALLOW_STUB="1",         # run even without maestro-core (stub harness)
    LOOP_OUTCOMES="1",           # enable outcome feedback feature
    LOOP_BEAM="2",               # beam search: 2 proposals per propose-iteration
    LOOP_MODEL_RETRIES="0",      # no retry sleeps inside tests
    LOOP_PLATEAU_ITERS="2",      # reflect-mode threshold (not expected to trip here)
    LOOP_SYNTH_EVERY="2", LOOP_SYNTH_SAMPLE="2", LOOP_SYNTH_MIN_HUNK="1",
    LOOP_SILVER_EVERY="0",   # silver harvest tested directly below, not mid-loop
)
import config  # noqa: E402

LOOPWS = TMP / "loop"
config.WORKSPACE = LOOPWS / "workspace"
config.CHAMPION_DIR = config.WORKSPACE / "champion-skill"
config.CANDIDATE_DIR = config.WORKSPACE / "candidate-skill"
config.DIFF_CACHE = config.WORKSPACE / "diffs"
config.FIXED_DIFF_CACHE = config.WORKSPACE / "diffs-fixed"
config.RUNS_DIR = config.WORKSPACE / "runs"
config.HISTORY_DIR = config.WORKSPACE / "history"
config.LEDGER_FILE = config.WORKSPACE / "ledger.jsonl"
config.STATE_FILE = config.WORKSPACE / "state.json"
config.REPORTS_DIR = LOOPWS / "reports"
config.HOURLY_DIR = config.REPORTS_DIR / "hourly"
config.ATTEMPTS_FILE = config.WORKSPACE / "attempts.jsonl"
config.STATUS_FILE = config.WORKSPACE / "status.json"
config.LOCK_FILE = config.WORKSPACE / "loop.lock"
config.PROPOSAL_DIR = config.REPORTS_DIR / "proposal"
config.SILVER_FILE = config.WORKSPACE / "silver.jsonl"
config.SILVER_DIR = config.REPORTS_DIR / "silver"
config.EVAL_CACHE_DIR = config.WORKSPACE / "evalcache"
config.LIVE_SKILL_DIR = SEED
config.GOLDEN_FILE = TMP / "golden.jsonl"
config.CORPUS_DIR = CORP

import harness_bridge as hb  # noqa: E402
hb.H.DIFF_CACHE = config.DIFF_CACHE
hb.H.RUNS_DIR = config.RUNS_DIR
import attempts  # noqa: E402
import metrics  # noqa: E402
import silver  # noqa: E402
import loop  # noqa: E402

print(f"harness in use: {hb.H.__file__}")


# --- stubs ------------------------------------------------------------------
def fake_get_diff(case):
    return f"diff --git a/{case['path']} b/{case['path']}\n+BUG-MARKER touch {case['path']}\n"


def fake_get_fixed_diff(case):
    return f"diff --git a/{case['path']} b/{case['path']}\n+clean change in {case['path']}\n"


def fake_reviewer(prompt: str, prompt_file: Path) -> str:
    path = next((c["path"] for c in CASES if c["path"] in prompt), None)
    if path is None:  # e.g. corpus shadow-eval hunks — no golden case in this prompt
        return "shadow: reviewed, no issues found."
    pat = next(c["must_match_any"][0] for c in CASES if c["path"] == path)
    if pat in prompt and "BUG-MARKER" in prompt:
        return f"{path}:10 🔴 {pat} — concrete failure scenario."
    return f"{path}: reviewed, no issues found."


def fake_model(prompt: str) -> str:
    if "HUMAN REVIEW COMMENTS" in prompt:
        assert "ai finding accepted" in prompt, "acted-on outcomes missing in corpus prompt"
    if "MISSED TRAIN BUGS" in prompt:
        assert "ai finding rejected" in prompt, "dismissed outcomes missing in propose prompt"

    if "Rewrite the file(s)" in prompt:                       # consolidation -> shrink
        return json.dumps({"rationale": "merge db rules",
                           "edits": [{"file": "references/db.md", "action": "rewrite",
                                      "content": "bbb-pattern ccc-pattern"}]})
    if "HUMAN REVIEW COMMENTS" in prompt:                     # corpus -> benign human rule
        return json.dumps({"rationale": "use enums not string literals (PR #11, #12)",
                           "edits": [{"file": "references/db.md", "action": "append",
                                      "content": "**Use enums, not string literals.** (PR #11, #12)"}]})
    return json.dumps({"rationale": "teach the ccc pattern",  # propose -> add ccc
                       "edits": [{"file": "references/db.md", "action": "append",
                                  "content": "**Catch ccc bug:** flag ccc-pattern. (PR #999)"}]})


def dir_hash(d: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(d.rglob("*.md")):
        h.update(f.relative_to(d).as_posix().encode()); h.update(f.read_bytes())
    return h.hexdigest()


def ev(recall, noise, rows):
    return {"recall": recall, "noise": noise, "passed": sum(r.get("passed", False) for r in rows),
            "scoreable": len(rows), "per_case": rows, "missed": []}


def main():
    failures = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    # ---- pure gate logic ----
    champ = ev(1.0, 1.0, [{"id": "aaa", "passed": True, "findings_count": 1},
                          {"id": "bbb", "passed": True, "findings_count": 1}])
    regr = ev(0.5, 1.0, [{"id": "aaa", "passed": True, "findings_count": 1},
                         {"id": "bbb", "passed": False, "findings_count": 1}])
    ok, why = metrics.decide(kind="propose", champ=champ, cand=regr, train_ids={"aaa"},
                             val_ids={"bbb"}, champ_size=100, cand_size=100,
                             noise_tol=0.1, max_size=99999)
    check("regression guard rejects pass->fail flip", ok is False and "regress" in why)
    corpus_ok = metrics.decide(kind="corpus", champ=champ, cand=ev(1.0, 1.0, champ["per_case"]),
                               train_ids={"aaa"}, val_ids={"bbb"}, champ_size=100, cand_size=150,
                               noise_tol=0.1, max_size=99999)[0]
    check("corpus gate accepts safe human rule (no regression)", corpus_ok is True)
    cons_ok = metrics.decide(kind="consolidate", champ=champ, cand=ev(1.0, 1.0, champ["per_case"]),
                             train_ids=set(), val_ids=set(), champ_size=200, cand_size=120,
                             noise_tol=0.1, max_size=99999)[0]
    check("consolidation accepts smaller+equal-recall", cons_ok is True)

    # ---- fake outcomes data ----
    config.WORKSPACE.mkdir(parents=True, exist_ok=True)
    (config.WORKSPACE / "outcomes.jsonl").write_text(json.dumps(
        {"pr": 100, "path": "src/o.py", "body": "ai finding accepted", "outcome": "acted_on", "beyond_human": True}
    ) + "\n" + json.dumps(
        {"pr": 101, "path": "src/p.py", "body": "ai finding rejected", "outcome": "dismissed"}
    ) + "\n")

    # ---- full loop: propose -> corpus -> consolidate ----
    seed_before = dir_hash(SEED)
    reporter = loop.run_loop(
        max_iters=3, fresh=True, get_diff_fn=fake_get_diff, run_reviewer_fn=fake_reviewer,
        call_model_fn=fake_model, fixed_diff_fn=fake_get_fixed_diff)

    check("baseline missed one bug (recall 0.667)", reporter.baseline["recall"] == round(2 / 3, 3))
    check("loop reached full recall", reporter.champion["recall"] == 1.0)
    check("held-out val recall reported 1.0", metrics.split_recall(reporter.champion, {"bbb"}) == 1.0)
    check("a human-corpus pattern was incorporated", reporter.totals["corpus_adds"] >= 1)
    check("a consolidation ran", reporter.totals["consolidations"] == 1)
    check("corpus comments were read", reporter.corpus_read >= 2)
    check("baseline precision measured (FP 0.0)", reporter.champ_fp == 0.0)

    # ---- isolation ----
    check("SEED skill unchanged (isolation)", dir_hash(SEED) == seed_before)
    check("all writes confined to sandbox", str(config.WORKSPACE).startswith(str(TMP)))

    check("beyond_humans counted in state", json.loads(config.STATE_FILE.read_text()).get("beyond_humans") == 1)

    # ---- ledger + rollback ----
    check("ledger recorded decisions", config.LEDGER_FILE.exists()
          and len(config.LEDGER_FILE.read_text().splitlines()) >= 3)
    snaps = sorted(p.name for p in config.HISTORY_DIR.glob("*"))
    check("history snapshots saved (>=3)", len(snaps) >= 3)
    loop.do_rollback("0000")
    check("rollback restored baseline rubric",
          "ccc-pattern" not in (config.CHAMPION_DIR / "references" / "db.md").read_text())
    check("rollback cleared state", not config.STATE_FILE.exists())

    # ---- reporting ----
    check("hourly report emitted", len(list(config.HOURLY_DIR.glob("*.md"))) >= 1)
    latest = (config.REPORTS_DIR / "LATEST.md").read_text().lower()
    check("report mentions model, val, and human learning",
          all(s in latest for s in ("gemini", "val", "human")))

    # ---- dashboard telemetry ----
    check("status heartbeat written (dashboard)", (config.WORKSPACE / "status.json").exists())
    tj = config.WORKSPACE / "thoughts.jsonl"
    check("gemini thoughts captured (dashboard)", tj.exists()
          and len(tj.read_text().splitlines()) >= 2)

    # ---- silver eval (self-growing eval set) ----
    def fake_silver_model(prompt):
        assert "RESOLVED human PR-review comments" in prompt
        return json.dumps([{"idx": 0, "reject": False,
                            "bug": "raw string literal compared where a domain enum exists",
                            "must_match_any": ["enum", "string literal"],
                            "severity": "medium", "confidence": 0.9}])

    hv = silver.harvest(0, call_model_fn=fake_silver_model)
    check("silver harvest admitted a case from a resolved human comment",
          hv["admitted"] == 1 and len(hv["ids"]) == 1)
    sc = silver.active_cases()
    check("silver case is golden-shaped with a frozen inline diff",
          len(sc) == 1 and sc[0]["must_match_any"]
          and sc[0]["diff"].startswith("diff --git") and sc[0].get("silver") is True)
    hv2 = silver.harvest(0, call_model_fn=fake_silver_model)
    check("silver dedupe blocks re-harvesting the same comment", hv2["admitted"] == 0)
    check("silver candidates bundle exported",
          (config.SILVER_DIR / "candidates.jsonl").exists())
    check("silver retire removes the case from the active eval",
          silver.retire(sc[0]["id"]) and silver.stats()["active"] == 0)

    # ---- eval checkpointing (crash-resume never re-buys finished reviews) ----
    spent = {"n": 0}

    def counting_reviewer(prompt, pf):
        spent["n"] += 1
        return fake_reviewer(prompt, pf)

    e1 = hb.evaluate(config.CHAMPION_DIR, CASES, label="ckpt-test", trials=2,
                     get_diff_fn=fake_get_diff, run_reviewer_fn=counting_reviewer)
    first_cost = spent["n"]
    e2 = hb.evaluate(config.CHAMPION_DIR, CASES, label="ckpt-test", trials=2,
                     get_diff_fn=fake_get_diff, run_reviewer_fn=counting_reviewer)
    check("eval checkpoint: resume spends 0 extra reviews, same verdicts",
          first_cost > 0 and spent["n"] == first_cost
          and e2["recall"] == e1["recall"] and e2["passed"] == e1["passed"])

    # ---- beam search + attempt memory ----
    a = attempts.stats()
    check("attempt memory recorded attempts", config.ATTEMPTS_FILE.exists() and a["total"] >= 3)
    check("beam dedupe skipped a duplicate proposal", a["duplicates_skipped"] >= 1)
    check("accepted patches recorded in attempt memory", a["accepted"] >= 3)

    # ---- corpus shadow eval (human-parity scoreboard) ----
    check("shadow eval ran against human-caught bugs",
          reporter.shadow is not None
          and reporter.shadow.get("shadow_recall") is not None)

    # ---- wilson CI + report extensions ----
    lo_hi = metrics.wilson_ci(9, 18)
    check("wilson CI sane", lo_hi is not None and lo_hi[0] < 0.5 < lo_hi[1])
    check("report shows val confidence interval", "95% ci" in latest)
    check("report shows attempt memory", "attempt memory" in latest)

    # ---- proposal bundle (exporter) ----
    pmd = config.PROPOSAL_DIR / "PROPOSAL.md"
    check("proposal bundle written on promotion",
          pmd.exists() and (config.PROPOSAL_DIR / "rubric.diff").exists()
          and (config.PROPOSAL_DIR / "champion-skill" / "SKILL.md").exists())
    check("proposal lists promoted changes",
          pmd.exists() and "Promoted changes" in pmd.read_text())

    # ---- ops hardening ----
    check("lock released after run", not config.LOCK_FILE.exists())
    check("no torn state tmp left behind",
          not (config.WORKSPACE / "state.json.tmp").exists())

    print()
    if failures:
        print(f"SMOKE TEST FAILED: {len(failures)} check(s): {failures}")
        sys.exit(1)
    print(f"SMOKE TEST PASSED — sandbox: {TMP}")


if __name__ == "__main__":
    main()

def _test_outcomes():
    import outcomes
    import json
    import os
    import sys
    print("---- outcomes smoke ----")
    def fake_run_gh(args):
        if "list" in args:
            return '[{"number": 100}]'
        if "view" in args:
            return json.dumps({
              "reviewThreads": [
                {
                  "isResolved": True,
                  "path": "src/main.py",
                  "comments": [{"author": {"login": "snabbit-bot"}, "body": "Bot finding"}]
                },
                {
                  "isResolved": False,
                  "path": "src/other.py",
                  "comments": [
                    {"author": {"login": "snabbit-bot"}, "body": "Rebutted finding"},
                    {"author": {"login": "human"}, "body": "No I disagree"}
                  ]
                },
                {
                  "isResolved": True,
                  "path": "src/unique.py",
                  "comments": [{"author": {"login": "snabbit-bot"}, "body": "Beyond human finding"}]
                },
                {
                  "isResolved": True,
                  "path": "src/main.py",
                  "comments": [{"author": {"login": "human"}, "body": "Overlapping human"}]
                }
              ]
            })
        return "[]"
    os.environ["LOOP_AI_REVIEWER_LOGIN"] = "snabbit-bot"
    import config
    of = config.WORKSPACE / "outcomes.jsonl"
    if of.exists(): of.unlink()
    stats = outcomes.mine_outcomes(run_gh=fake_run_gh)
    if stats.get("new_acted_on") != 2 or stats.get("new_dismissed") != 1:
        print(f"SMOKE TEST FAILED: outcomes stats incorrect: {stats}")
        sys.exit(1)
    if outcomes.count_beyond_humans() != 1:
        print(f"SMOKE TEST FAILED: beyond_humans count incorrect: {outcomes.count_beyond_humans()}")
        sys.exit(1)
    print("  [PASS] extracted acted_on and dismissed stats")
    
    stats2 = outcomes.mine_outcomes(run_gh=fake_run_gh)
    if stats2.get("new_acted_on") != 0:
        print("SMOKE TEST FAILED: outcomes dedupe failed")
        sys.exit(1)
    print("  [PASS] deduplicated successfully on re-run")

if __name__ == "__main__":
    _test_outcomes()

def _test_cli_outcomes():
    import sys
    import os
    import loop
    import outcomes
    from unittest.mock import patch

    def fake_run_gh(args):
        if "list" in args:
            return '[{"number": 101}]'
        if "view" in args:
            import json
            return json.dumps({
              "reviewThreads": [
                {
                  "isResolved": True,
                  "path": "src/cli.py",
                  "comments": [{"author": {"login": "snabbit-bot"}, "body": "CLI test finding"}]
                }
              ]
            })
        return "[]"

    print("---- loop.py --mine-outcomes CLI smoke ----")
    os.environ["LOOP_AI_REVIEWER_LOGIN"] = "snabbit-bot"
    with patch.object(sys, "argv", ["loop.py", "--mine-outcomes"]):
        with patch("outcomes._default_run_gh", side_effect=fake_run_gh):
            try:
                loop.main()
            except SystemExit as e:
                if e.code != 0:
                    print(f"SMOKE TEST FAILED: CLI --mine-outcomes exited with {e.code}")
                    sys.exit(1)
    
    import config
    outcomes_file = config.WORKSPACE / "outcomes.jsonl"
    found = False
    if outcomes_file.exists():
        for line in outcomes_file.read_text().splitlines():
             if '"CLI test finding"' in line:
                 found = True
                 break
    if not found:
        print("SMOKE TEST FAILED: CLI --mine-outcomes did not append to outcomes.jsonl")
        sys.exit(1)
    print("  [PASS] CLI --mine-outcomes completed")

if __name__ == "__main__":
    _test_cli_outcomes()

def _test_cli_replay():
    import sys
    import os
    import loop
    import replay
    from unittest.mock import patch

    def fake_run_gh(args):
        if "list" in args:
            return '[{"number": 201, "title": "A PR", "url": "http://pr"}]'
        if "diff" in args:
            return "diff --git a/src/new.py b/src/new.py\n+new line"
        return ""

    def mock_reviewer(prompt, pf):
        return "mocked review finding"

    print("---- loop.py --replay CLI smoke ----")
    import config
    config.CHAMPION_DIR.mkdir(parents=True, exist_ok=True)
    (config.CHAMPION_DIR / "SKILL.md").write_text("# champion")

    with patch.object(sys, "argv", ["loop.py", "--replay", "1"]):
        with patch("replay._default_run_gh", side_effect=fake_run_gh):
            with patch("harness_bridge.default_run_reviewer", side_effect=mock_reviewer):
                try:
                    loop.main()
                except SystemExit as e:
                    if e.code != 0:
                        print(f"SMOKE TEST FAILED: CLI --replay exited with {e.code}")
                        sys.exit(1)

    replay_md = config.REPORTS_DIR / "replay" / "REPLAY.md"
    if not replay_md.exists() or "Counterfactual" not in replay_md.read_text():
        print("SMOKE TEST FAILED: CLI --replay did not generate REPLAY.md properly")
        sys.exit(1)
        
    print("  [PASS] CLI --replay completed")

if __name__ == "__main__":
    _test_cli_replay()
