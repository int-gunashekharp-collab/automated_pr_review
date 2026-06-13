#!/usr/bin/env python3
"""Local dashboard server for the self-improving PR-reviewer loop.

Zero dependencies (stdlib only). Serves dashboard.html plus a JSON snapshot of
everything the loop emits (status.json heartbeat, thoughts.jsonl, ledger.jsonl,
state.json, history/, champion-skill/, reports/) and a Server-Sent-Events
stream that pushes a fresh snapshot whenever any of those files change.

Read-only by design: it never writes to the loop workspace (demo mode writes
only to its own temp directory) and never touches maestro-core.

Run:
    python3 dashboard_server.py            # watch the real loop workspace
    python3 dashboard_server.py --demo     # synthetic live data (no loop, no Vertex)
    python3 dashboard_server.py --port 9000 --no-open
"""

from __future__ import annotations

import argparse
import json
import random
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOOP_DIR = Path(__file__).resolve().parent

try:
    import config as loop_config
except Exception:
    loop_config = None

_corpus_total_cache = None


def _corpus_total():
    """Total mined human comments (cached once) — denominators for progress."""
    global _corpus_total_cache
    if _corpus_total_cache is None:
        if DEMO:
            _corpus_total_cache = 5349
        else:
            try:
                import corpus
                _corpus_total_cache = corpus.load_human_comments(0, 1)[1]
            except Exception:
                _corpus_total_cache = 0
    return _corpus_total_cache


# --- snapshot building --------------------------------------------------------
class Paths:
    def __init__(self, workspace: Path, reports: Path):
        self.workspace = workspace
        self.reports = reports


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _tail_jsonl(path: Path, n: int) -> list:
    try:
        lines = path.read_text().splitlines()[-n:]
    except Exception:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def _prune_state(state):
    if not isinstance(state, dict):
        return None
    ev = state.get("champion_eval") or {}
    missed = [{"id": m.get("id"), "pr": m.get("pr"), "path": m.get("path"),
               "bug": (m.get("bug") or "")[:200], "severity": m.get("severity")}
              for m in ev.get("missed", [])]
    return {
        "recall": ev.get("recall"), "noise": ev.get("noise"),
        "passed": ev.get("passed"), "scoreable": ev.get("scoreable"),
        "per_case": ev.get("per_case", []), "missed": missed,
        "champ_fp": state.get("champ_fp"), "champ_size": state.get("champ_size"),
        "corpus_offset": state.get("corpus_offset"), "totals": state.get("totals"),
        "train_ids": state.get("train_ids", []), "val_ids": state.get("val_ids", []),
        "model": state.get("model"), "project": state.get("project"),
        "shadow": state.get("shadow"), "consec_rejects": state.get("consec_rejects"),
        "calls_day": state.get("calls_day"),
    }


def _skill_breakdown(d: Path):
    if not d.exists():
        return None
    files, total = [], 0
    sk = d / "SKILL.md"
    if sk.exists():
        n = len(sk.read_text())
        files.append({"file": "SKILL.md", "chars": n})
        total += n
    refs = d / "references"
    if refs.exists():
        for f in sorted(refs.glob("*.md")):
            n = len(f.read_text())
            files.append({"file": f"references/{f.name}", "chars": n})
            total += n
    return {"total": total, "files": files}


def _latest_report(reports: Path):
    f = reports / "LATEST.md"
    try:
        return f.read_text()[:6000]
    except Exception:
        return None


def _loop_config_snapshot():
    if DEMO_CFG is not None:
        return DEMO_CFG
    if loop_config is None:
        return {}
    c = loop_config
    return {
        "max_skill_chars": c.MAX_SKILL_CHARS, "noise_tolerance": c.NOISE_TOLERANCE,
        "confirm_trials": c.CONFIRM_TRIALS, "screen_trials": c.SCREEN_TRIALS,
        "precision_every": c.PRECISION_EVERY, "precision_tol": c.PRECISION_TOL,
        "corpus_every": c.CORPUS_EVERY, "corpus_batch": c.CORPUS_BATCH,
        "consolidate_every": c.CONSOLIDATE_EVERY, "val_fraction": c.VAL_FRACTION,
        "model": c.GEMINI_MODEL, "project": c.GCP_PROJECT,
        "maestro_root": str(c.MAESTRO_ROOT),
        "plateau_iters": getattr(c, "PLATEAU_ITERS", 0),
        "beam_width": getattr(c, "BEAM_WIDTH", 1),
        "synth_every": getattr(c, "SYNTH_EVERY", 0),
        "max_calls_per_day": getattr(c, "MAX_CALLS_PER_DAY", 0),
    }


def _silver(ws: Path):
    """Silver-eval summary (workspace/silver.jsonl): last record per id wins."""
    rows = _tail_jsonl(ws / "silver.jsonl", 400)
    if not rows:
        return None
    latest = {}
    for r in rows:
        if r.get("id"):
            latest[r["id"]] = r
    active = [r for r in latest.values() if r.get("status") == "active"]
    return {
        "active": len(active),
        "retired": sum(r.get("status") == "retired" for r in latest.values()),
        "ids": sorted(r["id"] for r in active),
        "recent": [{"id": r["id"], "bug": (r.get("bug") or "")[:90],
                    "severity": r.get("severity"), "confidence": r.get("confidence"),
                    "human_said": (r.get("human_said") or "")[:90]}
                   for r in sorted(active, key=lambda x: -(x.get("added_ts") or 0))[:6]],
    }


def _attempts(ws: Path):
    """Attempt-memory summary (workspace/attempts.jsonl), if the loop emits it."""
    rows = _tail_jsonl(ws / "attempts.jsonl", 250)
    if not rows:
        return None
    return {
        "stats": {
            "total": len(rows),
            "unique": len({r.get("fp") for r in rows if r.get("fp")}),
            "duplicates_skipped": sum(r.get("status") == "duplicate" for r in rows),
            "accepted": sum(r.get("status") == "accepted" for r in rows),
            "rejected": sum(r.get("status") in ("rejected", "screened_out") for r in rows),
        },
        "recent": rows[-8:],
    }


def build_snapshot(P: Paths) -> dict:
    ws = P.workspace
    hist_dir = ws / "history"
    return {
        "now": round(time.time(), 3),
        "demo": DEMO,
        "workspace": str(ws),
        "corpus_total": _corpus_total(),
        "attempts": _attempts(ws),
        "silver": _silver(ws),
        "status": _read_json(ws / "status.json"),
        "state": _prune_state(_read_json(ws / "state.json")),
        "ledger": _tail_jsonl(ws / "ledger.jsonl", 150),
        "thoughts": _tail_jsonl(ws / "thoughts.jsonl", 25),
        "history": sorted(p.name for p in hist_dir.glob("*")) if hist_dir.exists() else [],
        "champion": _skill_breakdown(ws / "champion-skill"),
        "report": _latest_report(P.reports),
        "config": _loop_config_snapshot(),
    }


def _watch_signature(P: Paths):
    sig = []
    for name in ("status.json", "thoughts.jsonl", "ledger.jsonl", "state.json",
                 "attempts.jsonl", "silver.jsonl"):
        f = P.workspace / name
        try:
            st = f.stat()
            sig.append((name, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((name, 0, 0))
    return tuple(sig)


# --- HTTP ----------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    paths: Paths = None  # set at startup
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = self.path.split("?")[0]
        if route in ("/", "/index.html", "/dashboard.html"):
            f = LOOP_DIR / "dashboard.html"
            if f.exists():
                self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"dashboard.html not found next to dashboard_server.py",
                           "text/plain")
        elif route == "/api/snapshot":
            body = json.dumps(build_snapshot(self.paths)).encode()
            self._send(200, body, "application/json")
        elif route == "/api/stream":
            self._stream()
        elif route == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, b"not found", "text/plain")

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_sig, last_ping = None, time.time()
        try:
            while True:
                sig = _watch_signature(self.paths)
                if sig != last_sig:
                    payload = json.dumps(build_snapshot(self.paths))
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    last_sig, last_ping = sig, time.time()
                elif time.time() - last_ping > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


# --- demo mode -----------------------------------------------------------------
DEMO = False
DEMO_CFG = None

_DEMO_PATTERNS = [
    ("missing idempotency keys on retried POST handlers",
     "Flag POST/PUT handlers wrapped in retry decorators that lack an idempotency key — "
     "duplicate side effects on retry.", "references/api-conventions.md", [4811, 4847, 4903]),
    ("enum imported but raw string literals still compared",
     "Flag comparisons against raw string literals where a domain enum exists for the value — "
     "silent breakage when the enum value changes.", "references/code-style.md", [4712, 4790]),
    ("unbounded queries feeding list endpoints",
     "Flag ORM queries without LIMIT/pagination that feed list endpoints — memory blowups on "
     "production-size tables.", "references/db.md", [4655, 4699, 4734]),
    ("timezone-naive datetime arithmetic",
     "Flag datetime.now() (naive) mixed with timezone-aware columns — off-by-5h30 bugs in "
     "scheduling paths.", "references/datetime.md", [4520, 4561]),
    ("exceptions swallowed inside worker tasks",
     "Flag bare except blocks in celery/queue workers that log nothing — silent job loss.",
     "references/workers.md", [4490, 4533, 4578]),
    ("N+1 lookups inside serializer loops",
     "Flag per-item relation access inside serializer loops without select_related/prefetch — "
     "N+1 under load.", "references/db.md", [4401, 4456]),
]


def _demo_thought(kind, topic, rule, file, prs, comments=None):
    if kind == "corpus":
        prompt_head = ("You maintain the maestro-core PR-review conventions skill.\n"
                       "Below: (1) the FULL current skill and (2) real human review comments "
                       f"mined from this team's merged PRs… [{comments} comments in batch]")
        resp = {"rationale": f"humans repeatedly flag {topic} "
                             f"(PRs {', '.join('#' + str(p) for p in prs)}; "
                             f"{len(prs)}/{len(prs)} resolved) — adding one generalised rule",
                "edits": [{"file": file, "action": "append", "content": f"**{rule}**"}]}
    elif kind == "consolidate":
        prompt_head = ("You maintain the maestro-core PR-review conventions skill. It has grown "
                       "and needs to be more concise WITHOUT losing any rule…")
        resp = {"rationale": "merged 3 overlapping db rules; tightened severity prose",
                "edits": [{"file": "references/db.md", "action": "rewrite",
                           "content": "(consolidated db rules…)"}]}
    else:
        prompt_head = ("You maintain the maestro-core PR-review conventions skill. An eval ran "
                       "the reviewer over historical PRs with known, human-verified bugs. "
                       "Below: the TRAIN bugs the reviewer MISSED…")
        resp = {"rationale": f"reviewer misses {topic} — naming the exact mechanism so it "
                             "transfers to unseen code",
                "edits": [{"file": file, "action": "append", "content": f"**{rule}**"}]}
    return prompt_head, json.dumps(resp, indent=2)


def run_demo_writer(ws: Path, speed: float):
    """Simulate a running loop by writing the SAME files the real loop writes."""
    rng = random.Random(11)
    golden = [f"g{i:02d}" for i in range(1, 19)]
    sids = ["s4811", "s4655", "s4520"]
    ids = golden + sids
    val_ids = ["g03", "g07", "g09", "g12", "g15", "g17", "s4520"]
    train_ids = [i for i in ids if i not in val_ids]
    passing = set(rng.sample(golden, 11)) | {"s4811"}
    sil_meta = [
        ("s4811", 4811, "retried POST handler lacks an idempotency key — duplicate side effects",
         ["idempotency", "retry"], "high", .91),
        ("s4655", 4655, "list endpoint query has no LIMIT/pagination — unbounded result set",
         ["pagination", "unbounded"], "high", .87),
        ("s4520", 4520, "naive datetime mixed with tz-aware column in scheduler math",
         ["timezone", "naive datetime"], "medium", .82),
    ]
    (ws / "silver.jsonl").write_text("\n".join(json.dumps({
        "id": i, "pr": pr, "path": f"src/app/{i}.py", "bug": bug,
        "must_match_any": kws, "severity": sev, "confidence": conf,
        "diff": f"diff --git a/src/app/{i}.py b/src/app/{i}.py\n@@ demo hunk @@\n",
        "human_said": bug, "src_fp": f"{pr}|demo", "status": "active",
        "added_ts": round(time.time(), 3), "baseline_passed": i == "s4811"})
        for i, pr, bug, kws, sev, conf in sil_meta) + "\n")
    size, budget, fp, noise = 14600, 60000, 0.0, 4.6
    corpus_read, corpus_total = 0, 5349
    consec, shadow = 0, None
    totals = {"iters": 0, "accepts": 0, "rejects": 0, "errors": 0, "promotions": 0,
              "consolidations": 0, "corpus_adds": 0, "model_calls": 0}
    started = time.time()
    seq = 0
    (ws / "history").mkdir(parents=True, exist_ok=True)

    def s(x):
        time.sleep(max(0.05, x * speed))

    def write_champion():
        d = ws / "champion-skill"
        (d / "references").mkdir(parents=True, exist_ok=True)
        parts = {"SKILL.md": .18, "references/db.md": .27,
                 "references/api-conventions.md": .21, "references/datetime.md": .12,
                 "references/workers.md": .13, "references/do-not-flag.md": .09}
        for rel, frac in parts.items():
            (d / rel).write_text("# demo rubric (synthetic)\n" +
                                 "rule text " * max(1, int(size * frac) // 10))

    def status(phase, **info):
        nonlocal seq
        seq += 1
        d = {"phase": phase, **info, "ts": round(time.time(), 3), "seq": seq, "pid": 4242,
             "loop_started": round(started, 3), "model": "gemini-3.1-pro-preview"}
        tmp = ws / ".status.json.tmp"
        tmp.write_text(json.dumps(d))
        tmp.replace(ws / "status.json")
        return d

    def write_state(recall):
        per_case = [{"id": i, "passed": i in passing,
                     "findings_count": rng.randint(2, 7)} for i in ids]
        missed = [{"id": i, "pr": 4000 + int(i[1:]), "path": f"src/app/{i}.py",
                   "bug": "demo: known historical bug", "severity": "high"}
                  for i in ids if i not in passing]
        (ws / "state.json").write_text(json.dumps({
            "champion_eval": {"recall": recall, "noise": round(noise, 2),
                              "passed": len(passing), "scoreable": len(ids),
                              "per_case": per_case, "missed": missed},
            "champ_fp": fp, "champ_size": size, "corpus_offset": corpus_read,
            "totals": totals, "train_ids": train_ids, "val_ids": val_ids,
            "model": "gemini-3.1-pro-preview", "project": "snabbit-ai-productivity",
            "shadow": shadow, "consec_rejects": consec,
            "calls_day": {"day": time.strftime("%Y-%m-%d"),
                          "calls": totals["model_calls"]}}))

    def thought(kind, topic, rule, file, prs, comments=None):
        ph, rh = _demo_thought(kind, topic, rule, file, prs, comments)
        rec = {"ts": round(time.time(), 3), "kind": kind, "prompt_chars": rng.randint(38000, 52000),
               "response_chars": len(rh), "prompt_head": ph, "response_head": rh}
        if comments:
            rec["comments"] = comments
        with (ws / "thoughts.jsonl").open("a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def ledger(rec):
        rec.setdefault("ts", round(time.time(), 3))
        with (ws / "ledger.jsonl").open("a") as fh:
            fh.write(json.dumps(rec) + "\n")

    status("startup")
    write_champion()
    write_state(round(len(passing) / len(ids), 3))
    it = 0
    while True:
        it += 1
        kind = ("consolidate" if it % 5 == 0 else "corpus" if it % 2 == 0 else "propose")
        topic, rule, file, prs = _DEMO_PATTERNS[(it // 2) % len(_DEMO_PATTERNS)]
        if it == 2:
            ledger({"iter": it, "kind": "silver-harvest", "status": "info",
                    "reason": "eval set grew: +3 silver case(s)",
                    "rationale": ", ".join(sids),
                    "eval_set": {"golden": 18, "silver": 3},
                    "cases": {sid: (sid in passing) for sid in sids}})
        status("plan", iter=it, kind=kind); s(1.2)
        batch = 0
        if kind == "corpus":
            batch = min(120, corpus_total - corpus_read)
            status("extract", iter=it, kind=kind, offset=corpus_read, batch=batch); s(1.5)
            corpus_read += batch
        status("proposing", iter=it, kind=kind); s(rng.uniform(4, 7))
        thought(kind, topic, rule, file, prs, comments=batch or None)
        totals["model_calls"] += 1
        if kind == "propose":
            status("screening", iter=it, kind=kind, cases=8)
            for ci in range(1, 9):
                status("screening", iter=it, kind=kind, cases=8,
                       eval={"label": f"i{it}-screen", "case": ids[ci - 1], "case_idx": ci,
                             "n_cases": 8, "trial": 1, "trials": 1})
                s(0.35)
            totals["model_calls"] += 8
        status("confirming", iter=it, kind=kind, cases=18, trials=3)
        for ci, cid in enumerate(ids, 1):
            for t in range(1, 4):
                status("confirming", iter=it, kind=kind, cases=18, trials=3,
                       eval={"label": f"i{it}-confirm", "case": cid, "case_idx": ci,
                             "n_cases": 18, "trial": t, "trials": 3})
                s(0.16)
        totals["model_calls"] += 54
        if it % 3 == 0:
            status("precision", iter=it, kind=kind); s(2.0)
            totals["model_calls"] += 4
        status("deciding", iter=it, kind=kind); s(0.8)

        roll = rng.random()
        accept = roll < (0.45 if kind != "consolidate" else 0.6)
        cases_map = {i: (i in passing) for i in ids}
        delta_txt = ""
        if accept:
            if kind == "consolidate":
                size = max(11000, size - rng.randint(1500, 2600))
                delta_txt = "consolidated: shrunk, recall held"
            else:
                gain = [i for i in ids if i not in passing]
                if gain and rng.random() < 0.7:
                    won = rng.choice(gain)
                    passing.add(won)
                    cases_map[won] = True
                    delta_txt = f"caught ['{won}']"
                else:
                    delta_txt = "incorporated human-review pattern (no regression)"
                size += rng.randint(280, 520)
            totals["accepts"] += 1; totals["promotions"] += 1
            if kind == "corpus":
                totals["corpus_adds"] += 1
            if kind == "consolidate":
                totals["consolidations"] += 1
            recall = round(len(passing) / len(ids), 3)
            (ws / "history" / f"{it:04d}-r{recall}").mkdir(exist_ok=True)
        else:
            reasons = ["no new train bug caught", "regressed ['g05']",
                       "noise grew on already-passing cases",
                       f"precision worse (FP {round(fp + 0.05, 2)} > {fp})",
                       "no clear new pattern in this batch"]
            delta_txt = rng.choice(reasons)
            totals["rejects"] += 1
        totals["iters"] += 1
        recall = round(len(passing) / len(ids), 3)
        tr = round(sum(i in passing for i in train_ids) / len(train_ids), 3)
        vr = round(sum(i in passing for i in val_ids) / len(val_ids), 3)
        noise = max(3.0, min(7.0, noise + rng.uniform(-0.2, 0.25)))
        if it % 3 == 0:
            fp = round(max(0.0, min(0.25, fp + rng.uniform(-0.04, 0.05))), 3)
        ledger({"iter": it, "kind": kind, "status": "accepted" if accept else "rejected",
                "rationale": f"{topic} (PRs {', '.join('#' + str(p) for p in prs)})",
                "reason": delta_txt, "changelog": [f"append +6 lines -> {file}"],
                "size": size, "champ_recall": recall, "model_calls": 63,
                "cand_recall": recall, "cand_noise": round(noise, 2),
                "train_recall": tr, "val_recall": vr,
                "fp_rate": fp if it % 3 == 0 else None, "cases": cases_map})
        with (ws / "attempts.jsonl").open("a") as fh:
            fh.write(json.dumps({"ts": round(time.time(), 3), "iter": it, "kind": kind,
                                 "fp": f"{rng.getrandbits(64):016x}",
                                 "rationale": topic,
                                 "status": "accepted" if accept else "rejected",
                                 "reason": delta_txt}) + "\n")
        status("decided", iter=it, kind=kind,
               status="accepted" if accept else "rejected", reason=delta_txt)
        consec = 0 if accept else consec + 1
        if accept:
            status("exporting", iter=it); s(0.9)
            write_champion()
        if it % 4 == 0:
            status("shadow-eval", iter=it, cases=12); s(2.2)
            sh = round(min(0.72, 0.40 + 0.015 * it + rng.uniform(-0.03, 0.03)), 3)
            caught = round(sh * 12)
            shadow = {"shadow_recall": sh, "caught": caught, "checked": 12, "errored": 0,
                      "total_pool": 1487, "cursor": (it // 4) * 12,
                      "ts": round(time.time(), 3),
                      "misses": [{"id": f"syn-46{it % 90:02d}{i}", "pr": 4600 + i,
                                  "human_said": "race window between check and insert — "
                                                "use upsert with a unique constraint",
                                  "keywords": ["upsert", "unique_constraint"]}
                                 for i in range(min(3, 12 - caught))]}
        write_state(recall)
        s(1.0)
        status("idle", iter=it, resume_at=round(time.time() + 3 * speed, 1)); s(3.0)


# --- main -----------------------------------------------------------------------
def main():
    global DEMO, DEMO_CFG
    ap = argparse.ArgumentParser(description="PR-reviewer loop dashboard")
    ap.add_argument("--port", type=int, default=None,
                    help="default: LOOP_DASH_PORT from config (8123), else 8765")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--demo", action="store_true", help="synthetic live data (no loop needed)")
    ap.add_argument("--speed", type=float, default=1.0, help="demo pacing multiplier")
    ap.add_argument("--workspace", default=None, help="override workspace dir to watch")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    if args.demo:
        DEMO = True
        ws = Path(tempfile.mkdtemp(prefix="loop-dash-demo-"))
        DEMO_CFG = {"max_skill_chars": 60000, "noise_tolerance": 0.10, "confirm_trials": 3,
                    "screen_trials": 1, "precision_every": 3, "precision_tol": 0.0,
                    "corpus_every": 2, "corpus_batch": 120, "consolidate_every": 5,
                    "val_fraction": 0.33, "model": "gemini-3.1-pro-preview",
                    "project": "snabbit-ai-productivity", "maestro_root": "(demo)",
                    "plateau_iters": 6, "beam_width": 2, "synth_every": 4,
                    "max_calls_per_day": 0}
        P = Paths(ws, ws)
        threading.Thread(target=run_demo_writer, args=(ws, args.speed), daemon=True).start()
        print(f"DEMO mode — synthetic data in {ws}")
    else:
        if args.workspace:
            ws = Path(args.workspace).expanduser().resolve()
        elif loop_config is not None:
            ws = loop_config.WORKSPACE
        else:
            ws = LOOP_DIR / "workspace"
        reports = loop_config.REPORTS_DIR if loop_config is not None else LOOP_DIR / "reports"
        P = Paths(ws, reports)
        print(f"watching workspace: {ws}")

    Handler.paths = P
    port = args.port or (getattr(loop_config, "DASH_PORT", 8765) if loop_config else 8765)
    srv = ThreadingHTTPServer((args.host, port), Handler)
    url = f"http://{args.host}:{port}/"
    print(f"dashboard: {url}  (ctrl-c to stop)")
    if not args.no_open:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
