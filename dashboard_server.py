#!/usr/bin/env python3
"""Local dashboard server for the self-improving PR-reviewer loop.

Zero dependencies (stdlib only). Serves dashboard.html plus a JSON snapshot of
everything the loop emits (status.json heartbeat, thoughts.jsonl, ledger.jsonl,
state.json, history/, champion-skill/, reports/) and a Server-Sent-Events
stream that pushes a fresh snapshot whenever any of those files change.

Read-only by design: it never writes to the loop workspace and never touches
maestro-core. It only ever displays REAL data from the loop's workspace.

Run:
    python3 dashboard_server.py            # watch the real loop workspace
    python3 dashboard_server.py --port 9000 --no-open
"""

from __future__ import annotations

import argparse
import json
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
        "beyond_humans": state.get("beyond_humans", 0),
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
        "use_mcp": getattr(c, "USE_MCP", False),
        "use_workflow": getattr(c, "USE_WORKFLOW", False),
        "maestro_context": getattr(c, "MAESTRO_CONTEXT", False),
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


def _ralph(ws: Path):
    """Builder telemetry: roadmap snapshot (ralph_state.json) + the per-iteration
    build events with file diffs (ralph.jsonl). None when ralph hasn't run."""
    state = _read_json(ws / "ralph_state.json")
    events = _tail_jsonl(ws / "ralph.jsonl", 30)
    if not state and not events:
        return None
    return {"state": state, "events": events}


def _scorecard(ws: Path):
    """Lifetime scorecard trajectory (workspace/scorecard.jsonl): each line is one
    run's computed metrics, so the latest line is current and the file is the
    climbing-autonomy trend. None until the loop has recorded a run."""
    rows = _tail_jsonl(ws / "scorecard.jsonl", 500)
    if not rows:
        return None
    firsts = [r.get("autonomy_pct") for r in rows if r.get("autonomy_pct") is not None]
    return {**rows[-1], "runs": len(rows),
            "autonomy_first": firsts[0] if firsts else None,
            "trajectory": [{"ts": r.get("ts"), "autonomy_pct": r.get("autonomy_pct"),
                            "graduated": r.get("graduated"),
                            "beyond_humans": r.get("beyond_humans")} for r in rows[-60:]]}


def build_snapshot(P: Paths) -> dict:
    ws = P.workspace
    hist_dir = ws / "history"
    return {
        "now": round(time.time(), 3),
        "demo": DEMO,
        "workspace": str(ws),
        "corpus_total": _corpus_total(),
        "scorecard": _scorecard(ws),
        "ralph": _ralph(ws),
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
                 "attempts.jsonl", "silver.jsonl", "ralph.jsonl", "ralph_state.json",
                 "scorecard.jsonl"):
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


# --- legacy flags: demo mode was removed; kept False/None so the snapshot always
# reads as a live, real-data view ------------------------------------------------
DEMO = False
DEMO_CFG = None

# --- main -----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="PR-reviewer loop dashboard")
    ap.add_argument("--port", type=int, default=None,
                    help="default: LOOP_DASH_PORT from config (8123), else 8765")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--workspace", default=None, help="override workspace dir to watch")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

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
