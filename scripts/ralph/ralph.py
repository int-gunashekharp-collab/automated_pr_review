#!/usr/bin/env python3
"""In-house Ralph — the roadmap builder, on the SAME Vertex Gemini brain the loop
already uses.

This REPLACES the old gemini-cli shell runner. No npm, no `gemini` CLI, no
git-baseline dance, no external agent process. Pure Python that shares the
project's `config`, `telemetry`, and the proposer's Vertex call, so the builder
and the self-improving loop are one system with one brain and one way to run.

What it is (and is NOT):
  * It is a *code* loop: each iteration implements ONE prd.json story by asking
    Gemini for a strict JSON file-patch, applying it, and gating it.
  * It is NOT a second rubric loop and it does NOT weaken loop.py's eval gates —
    the two stay distinct in WHAT they optimise (code vs. rubric) but share
    infrastructure. (See MEMORY.md turn 11b.)

Per iteration:
  1. maestro guard — abort (actionably) if maestro-core has uncommitted changes.
  2. pick the highest-priority story with passes=false.
  3. ask the Vertex brain for {files:[...], done:bool} implementing ONLY it.
  4. apply the patch under STRICT path safety (never escapes this folder, never
     touches maestro-core or .git).
  5. gate: py_compile every touched .py + offline smoke (LOOP_ALLOW_STUB=1).
  6. GREEN  -> mark the story passes=true, log GREEN, best-effort git commit.
     RED    -> after a bounded refine, roll the iteration's edits back and log
               RED so the next iteration fixes it first. The tree stays green.

Run:
  python3 scripts/ralph/ralph.py            # build until done (max 10 iters)
  python3 scripts/ralph/ralph.py 5          # cap iterations
  python3 scripts/ralph/ralph.py --selftest # offline proof — no Vertex, no network

Real runs need maestro-core present (the brain lives at
$MAESTRO_ROOT/scripts/gemini_vertex.py) and Vertex ADC. The selftest needs
neither — it stubs the brain and proves the harness end to end.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # .../scripts/ralph
ROOT = HERE.parent.parent                        # the loop folder (== config.LOOP_DIR)
sys.path.insert(0, str(ROOT))

import config      # noqa: E402  (project modules — same brain/telemetry as the loop)
import telemetry   # noqa: E402
import proposer    # noqa: E402


# --- env knobs (same _i/_f/_b spirit as config.py) --------------------------
def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


PRD_PATH = Path(os.environ.get("RALPH_PRD", HERE / "prd.json"))
PROGRESS_PATH = Path(os.environ.get("RALPH_PROGRESS", HERE / "progress.txt"))
PROMPT_PATH = HERE / "prompt.md"

MAX_ITERS_DEFAULT = _i("RALPH_MAX_ITERS", 10)
REFINE_TRIES = max(0, _i("RALPH_REFINE_TRIES", 1))   # extra fix attempts on a RED gate
CONTEXT_CHARS = _i("RALPH_CONTEXT_CHARS", 120_000)   # cap on included file context
SMOKE_TIMEOUT = _i("RALPH_SMOKE_TIMEOUT", 900)
SKIP_GUARD = _b("RALPH_SKIP_MAESTRO_GUARD", False)
DO_COMMIT = _b("RALPH_GIT_COMMIT", True)

# Files always worth showing the model: the knob/path source of truth and the
# offline test it must keep green.
_ALWAYS_INCLUDE = ("config.py", "smoke_test.py")

# Dashboard data streams the builder publishes (read by dashboard_server.py).
# Both live under the loop's gitignored workspace/ — isolation holds.
RALPH_EVENTS = "ralph.jsonl"      # append-only per-iteration build events (+ diffs)
RALPH_STATE = "ralph_state.json"  # roadmap snapshot: which stories done/building/red
_CURRENT_MAX_ITERS = [MAX_ITERS_DEFAULT]   # set by main(); read by _publish_state


# --- path safety: the cardinal rule, enforced in code -----------------------
def _safe_target(rel: str) -> Path:
    """Resolve a patch target and REFUSE anything that escapes the loop folder,
    lands in .git, or resolves under maestro-core. .resolve() collapses symlinks,
    so a symlink pointing out of the folder is caught by the containment check."""
    root = ROOT.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path escapes loop folder: {rel}")
    if ".git" in target.relative_to(root).parts:
        raise ValueError(f"refusing to write inside .git: {rel}")
    try:
        maestro = config.MAESTRO_ROOT.resolve()
        if target == maestro or maestro in target.parents:
            raise ValueError(f"refusing to write under maestro-core: {rel}")
    except (OSError, ValueError) as e:
        if "maestro-core" in str(e):
            raise
    return target


def _apply(files: list[dict]) -> tuple[list[str], dict]:
    """Apply file edits in order. Returns (changelog, snapshot) where snapshot
    maps abs-path -> prior bytes (or None if the file was created), enabling an
    exact rollback. Validation happens before any write of each file."""
    snapshot: dict[str, bytes | None] = {}
    changelog: list[str] = []
    for f in files:
        rel = (f.get("path") or "").strip()
        action = (f.get("action") or "create").strip().lower()
        content = f.get("content")
        if not rel or content is None:
            raise ValueError(f"malformed file edit: {f!r}")
        target = _safe_target(rel)
        if str(target) not in snapshot:
            snapshot[str(target)] = target.read_bytes() if target.exists() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        if action == "append" and target.exists():
            base = target.read_text()
            target.write_text(base.rstrip() + "\n\n" + content.strip() + "\n")
            changelog.append(f"append  -> {rel}")
        else:
            existed = snapshot[str(target)] is not None
            target.write_text(content if content.endswith("\n") else content + "\n")
            changelog.append(f"{'rewrite' if existed else 'create '} -> {rel}")
    return changelog, snapshot


def _safe_unlink(p: Path) -> None:
    """Remove a file, tolerating filesystems that forbid unlink: if delete is
    denied, neutralise the file by truncating it to empty (an empty .py is inert
    and still compiles) so a rolled-back creation never leaves broken content."""
    try:
        if p.exists():
            p.unlink()
    except OSError:
        try:
            p.write_bytes(b"")
        except OSError:
            pass


def _rollback(snapshot: dict) -> None:
    for path, data in snapshot.items():
        p = Path(path)
        if data is None:
            _safe_unlink(p)
        else:
            try:
                p.write_bytes(data)
            except OSError:
                pass


# --- quality gate -----------------------------------------------------------
def _gate(touched: list[str]) -> tuple[bool, str]:
    """py_compile every touched .py (fast fail), then the offline smoke test."""
    pys = sorted({p for p in touched if p.endswith(".py")})
    if pys:
        r = subprocess.run([sys.executable, "-m", "py_compile",
                            *[str(ROOT / p) for p in pys]],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False, "py_compile failed:\n" + r.stderr.strip()[-800:]
    env = {**os.environ, "LOOP_ALLOW_STUB": "1"}
    try:
        r = subprocess.run([sys.executable, "smoke_test.py"], cwd=str(ROOT),
                           capture_output=True, text=True, env=env,
                           timeout=SMOKE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"smoke timed out after {SMOKE_TIMEOUT}s"
    green = r.returncode == 0 and "FAIL" not in r.stdout
    tail = (r.stdout + "\n" + r.stderr).strip()[-1200:]
    return green, tail


# --- maestro guard (read-only rule, made actionable) ------------------------
def guard_maestro() -> None:
    if SKIP_GUARD:
        return
    maestro = config.MAESTRO_ROOT
    if not (maestro / ".git").exists():
        return  # maestro-core absent or not a checkout — nothing to guard here
    r = subprocess.run(["git", "-C", str(maestro), "status", "--porcelain"],
                       capture_output=True, text=True)
    if r.stdout.strip():
        print("FATAL: maestro-core has uncommitted changes — it is a READ-ONLY")
        print("input and ralph will not run while it is dirty. What changed:")
        print(subprocess.run(["git", "-C", str(maestro), "status", "--short"],
                             capture_output=True, text=True).stdout.rstrip())
        print(f'\nInspect:      git -C "{maestro}" status')
        print(f'If unwanted:  git -C "{maestro}" checkout -- .     # or: git stash')
        print("Bypass (only if those edits are unrelated to this project):")
        print("  RALPH_SKIP_MAESTRO_GUARD=1 python3 scripts/ralph/ralph.py")
        sys.exit(2)


# --- prd / progress / git ---------------------------------------------------
def load_prd() -> dict:
    return json.loads(PRD_PATH.read_text())


def remaining(prd: dict) -> int:
    return sum(1 for s in prd.get("userStories", []) if not s.get("passes"))


def next_story(prd: dict) -> dict | None:
    todo = [s for s in prd.get("userStories", []) if not s.get("passes")]
    todo.sort(key=lambda s: s.get("priority", 999))
    return todo[0] if todo else None


def mark_done(story_id: str) -> None:
    prd = load_prd()
    for s in prd.get("userStories", []):
        if s.get("id") == story_id:
            s["passes"] = True
    PRD_PATH.write_text(json.dumps(prd, indent=2) + "\n")


def log_progress(line: str) -> None:
    with PROGRESS_PATH.open("a") as fh:
        fh.write(line.rstrip() + "\n")


def smoke_marker(result: str, i: int) -> None:
    if result == "GREEN":
        log_progress(f"smoke: GREEN after iteration {i}")
    else:
        log_progress(f"smoke: RED after iteration {i} — "
                     "NEXT ITERATION MUST FIX THIS FIRST, before any new story")


def git_commit(msg: str) -> str | None:
    """Best-effort commit. Returns the short hash on success, else None."""
    if not DO_COMMIT or not (ROOT / ".git").exists():
        return None
    try:
        subprocess.run(["git", "-C", str(ROOT), "add", "-A"],
                       capture_output=True, text=True, check=True)
        r = subprocess.run(["git", "-C", str(ROOT), "commit", "-m", msg],
                           capture_output=True, text=True)
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            print(f"  (git commit skipped: {r.stderr.strip()[-160:] or r.stdout.strip()[-160:]})")
            return None
        h = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True)
        return h.stdout.strip() or None
    except Exception as e:  # noqa: BLE001 — commits are best-effort, never fatal
        print(f"  (git commit skipped: {e})")
        return None


# --- dashboard publishing (crash-proof, like telemetry) ---------------------
def _change_records(applied_meta: list[dict], snapshot: dict) -> list[dict]:
    """For each file the iteration touched, build a viewable change record:
    action, +/- line counts, and a (capped) unified diff of before->after. Call
    this BEFORE any rollback so a RED attempt's diff is still inspectable."""
    by_path: dict[str, str] = {}
    for m in applied_meta:                      # last action per path wins
        if m.get("path"):
            by_path[m["path"]] = m.get("action", "create")
    recs = []
    for rel, action in by_path.items():
        try:
            target = _safe_target(rel)
        except ValueError:
            continue
        before_b = snapshot.get(str(target))
        before = before_b.decode("utf-8", "replace") if before_b else ""
        after = target.read_text(errors="replace") if target.exists() else ""
        diff_lines = list(difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))
        added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
        diff_text = "\n".join(diff_lines)
        if len(diff_text) > 6000:
            diff_text = diff_text[:6000] + "\n… (diff truncated) …"
        recs.append({"path": rel, "action": action, "added": added,
                     "removed": removed, "diff": diff_text})
    return recs


def _publish_event(rec: dict) -> None:
    try:
        rec.setdefault("ts", round(time.time(), 3))
        config.WORKSPACE.mkdir(parents=True, exist_ok=True)
        f = config.WORKSPACE / RALPH_EVENTS
        with f.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        lines = f.read_text().splitlines()
        if len(lines) > 300:
            tmp = config.WORKSPACE / ("." + RALPH_EVENTS + ".tmp")
            tmp.write_text("\n".join(lines[-200:]) + "\n")
            os.replace(tmp, f)
    except Exception:
        pass


def _publish_state(prd: dict, *, iteration=None, running=True, phase=None,
                   current=None, override: dict | None = None) -> None:
    override = override or {}
    try:
        stories, done = [], 0
        for s in sorted(prd.get("userStories", []), key=lambda s: s.get("priority", 999)):
            sid = s.get("id")
            if s.get("passes"):
                done += 1
            status = override.get(sid) or ("done" if s.get("passes")
                     else "building" if current and sid == current else "pending")
            stories.append({"id": sid, "priority": s.get("priority"),
                            "title": s.get("title", ""), "status": status})
        state = {"ts": round(time.time(), 3), "model": config.GEMINI_MODEL,
                 "project": config.GCP_PROJECT, "running": running,
                 "iteration": iteration, "max_iters": _CURRENT_MAX_ITERS[0],
                 "phase": phase, "current": current, "done": done,
                 "total": len(stories), "stories": stories}
        config.WORKSPACE.mkdir(parents=True, exist_ok=True)
        tmp = config.WORKSPACE / ".ralph_state.json.tmp"
        tmp.write_text(json.dumps(state))
        os.replace(tmp, config.WORKSPACE / RALPH_STATE)
    except Exception:
        pass


def ensure_git_baseline() -> None:
    """If this is a git repo with no commits yet, make the baseline commit the
    user kept failing to make by hand (the `# one-time` comment swallowed it).
    No git? Fine — ralph does not require it."""
    if not DO_COMMIT or not (ROOT / ".git").exists():
        return
    has = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"],
                         capture_output=True, text=True)
    if has.returncode != 0:
        print("ralph(py): no commits yet — creating baseline commit.")
        git_commit("baseline before ralph")


# --- prompt assembly --------------------------------------------------------
def _repo_map() -> str:
    rows = []
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if any(part in (".git", "__pycache__", "workspace", "reports")
               for part in rel.parts):
            continue
        rows.append(f"  {rel}  ({p.stat().st_size} B)")
    return "\n".join(rows)


def _relevant_files(story: dict) -> dict[str, str]:
    """Always include config.py + smoke_test.py; add any tracked file whose stem
    is named in the story; cap total characters."""
    text = json.dumps(story).lower()
    picked: list[Path] = [ROOT / n for n in _ALWAYS_INCLUDE]
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if any(part in (".git", "__pycache__", "workspace", "reports")
               for part in rel.parts):
            continue
        if p.stem.lower() in text and p not in picked:
            picked.append(p)
    out: dict[str, str] = {}
    budget = CONTEXT_CHARS
    for p in picked:
        if not p.exists() or budget <= 0:
            continue
        body = p.read_text(errors="ignore")
        if len(body) > budget:
            body = body[:budget] + "\n# … (truncated for context budget) …\n"
        out[str(p.relative_to(ROOT))] = body
        budget -= len(body)
    return out


_CONTRACT = """Reply with ONLY this JSON object, no prose, no markdown fence:
{
  "rationale": "one sentence: how this implements the story",
  "done": true,
  "files": [
    {"path": "outcomes.py", "action": "create", "content": "<full file text>"},
    {"path": "smoke_test.py", "action": "append", "content": "<new offline checks>"}
  ],
  "progress_note": "concise learnings/gotchas for the next iteration"
}
Rules for the patch:
- "action": "create" (new file or full overwrite), "rewrite" (replace a whole
  existing file), or "append" (add to the end of an existing file).
- "content" is the LITERAL file text (for create/rewrite, the ENTIRE file).
- Only touch files inside THIS folder. NEVER write under maestro-core or .git.
- You MUST extend smoke_test.py with offline (LOOP_ALLOW_STUB=1) checks for the
  new behaviour; the gate runs it and rejects you if it goes red.
- Set "done": false if you could only land a coherent partial slice (then the
  story stays open for the next iteration) — keep smoke green either way."""


def build_prompt(story: dict, refine_note: str = "") -> str:
    rules = PROMPT_PATH.read_text() if PROMPT_PATH.exists() else ""
    progress = PROGRESS_PATH.read_text() if PROGRESS_PATH.exists() else "(none)"
    files = _relevant_files(story)
    files_block = "\n".join(
        f"\n--- {name} ---\n{body}" for name, body in files.items())
    parts = [
        rules,
        "\n## YOUR STORY THIS ITERATION (implement ONLY this one)\n",
        json.dumps(story, indent=2),
        "\n## PROGRESS LOG (durable memory; if the last smoke line is RED, FIX IT FIRST)\n",
        progress[-4000:],
        "\n## REPOSITORY (python files)\n",
        _repo_map(),
        "\n## RELEVANT FILE CONTENTS\n",
        files_block,
    ]
    if refine_note:
        parts += ["\n## YOUR PREVIOUS ATTEMPT FAILED THE GATE — FIX IT\n", refine_note]
    parts += ["\n## OUTPUT CONTRACT\n", _CONTRACT]
    return "\n".join(parts)


# --- one iteration ----------------------------------------------------------
def run_iteration(i: int, call_fn, *, commit: bool = True) -> str:
    """Returns: 'promoted' | 'partial' | 'red' | 'done'."""
    prd = load_prd()
    story = next_story(prd)
    if story is None:
        return "done"
    sid = story.get("id", "?")
    title = story.get("title", "")
    telemetry.phase("ralph-build", iteration=i, story=sid, title=title)
    _publish_state(prd, iteration=i, running=True, phase="ralph-build", current=sid)
    guard_maestro()

    raw = call_fn(build_prompt(story))
    telemetry.thought("ralph", "(story " + sid + ")", raw, story=sid)
    tries = 0

    def emit(result, *, done=False, changes=None, note="", commit_hash=None, detail=""):
        _publish_event({"iteration": i, "story": sid, "title": title,
                        "result": result, "done": done, "changes": changes or [],
                        "note": note, "commit": commit_hash, "refines": tries,
                        "detail": detail[:400]})
        override = {sid: "red"} if result in ("red", "rejected") else None
        _publish_state(load_prd(), iteration=i, running=True,
                       phase="ralph-idle", current=None, override=override)

    try:
        patch = proposer._extract_json(raw)
    except ValueError as e:
        log_progress(f"iter {i} {sid}: brain returned no JSON ({e}) — skipped")
        emit("rejected", detail=str(e))
        return "red"
    files = patch.get("files") or []
    if not files:
        log_progress(f"iter {i} {sid}: brain proposed no files — skipped")
        emit("rejected", detail="no files in patch")
        return "red"

    applied_meta: list[dict] = []
    snapshot: dict = {}
    try:
        changelog, snapshot = _apply(files)
    except ValueError as e:
        if snapshot:
            _rollback(snapshot)
        log_progress(f"iter {i} {sid}: REJECTED unsafe patch — {e}")
        telemetry.phase("ralph-idle", iteration=i, story=sid, result="rejected")
        emit("rejected", detail=str(e))
        return "red"
    applied_meta += [{"path": f.get("path", ""), "action": f.get("action", "create")}
                     for f in files]
    print("  applied:\n    " + "\n    ".join(changelog))

    ok, detail = _gate([m["path"] for m in applied_meta])
    while not ok and tries < REFINE_TRIES:
        tries += 1
        print(f"  gate RED — refine attempt {tries}/{REFINE_TRIES}")
        telemetry.phase("ralph-refine", iteration=i, story=sid, attempt=tries)
        raw = call_fn(build_prompt(story, refine_note=detail))
        try:
            patch = proposer._extract_json(raw)
        except ValueError:
            break
        files2 = patch.get("files") or []
        try:
            cl2, snap2 = _apply(files2)
        except ValueError as e:
            log_progress(f"iter {i} {sid}: refine produced unsafe patch — {e}")
            break
        for k, v in snap2.items():          # keep the EARLIEST prior state per file
            snapshot.setdefault(k, v)
        applied_meta += [{"path": f.get("path", ""), "action": f.get("action", "create")}
                         for f in files2]
        ok, detail = _gate([m["path"] for m in applied_meta])

    changes = _change_records(applied_meta, snapshot)   # capture BEFORE any rollback

    if not ok:
        _rollback(snapshot)
        smoke_marker("RED", i)
        log_progress(f"iter {i} {sid}: smoke RED after {tries} refine(s) — "
                     f"rolled back. tail: {detail[:200]!r}")
        telemetry.phase("ralph-idle", iteration=i, story=sid, result="red")
        emit("red", changes=changes, detail=detail)
        return "red"

    done = bool(patch.get("done", True))
    if done:
        mark_done(sid)
    smoke_marker("GREEN", i)
    note = (patch.get("progress_note") or "").strip()
    log_progress(f"iter {i} {sid}: GREEN — {'story DONE' if done else 'partial slice kept'}"
                 + (f". {note}" if note else ""))
    commit_hash = git_commit(f"ralph: {sid} {title[:60]}".rstrip()) if commit else None
    telemetry.phase("ralph-idle", iteration=i, story=sid, result="green", done=done)
    emit("green" if done else "partial", done=done, changes=changes,
         note=note, commit_hash=commit_hash)
    return "promoted" if done else "partial"


def _real_brain(prompt: str) -> str:
    return proposer.call_vertex(prompt, log_name="ralph-prompt.txt", prefix="ralph-")


def main() -> int:
    ap = argparse.ArgumentParser(description="In-house Ralph builder (Vertex Gemini).")
    ap.add_argument("max_iters", nargs="?", type=int, default=MAX_ITERS_DEFAULT,
                    help=f"max iterations (default {MAX_ITERS_DEFAULT})")
    ap.add_argument("--selftest", action="store_true",
                    help="offline proof of the harness — no Vertex, no network")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    _CURRENT_MAX_ITERS[0] = args.max_iters
    prd = load_prd()
    print(f"ralph(py): {remaining(prd)} story(ies) remaining · max {args.max_iters} iter(s)")
    print(f"ralph(py): brain = Vertex {config.GEMINI_MODEL} · project "
          f"{config.GCP_PROJECT} · in-process (no gemini-cli)")
    print(f"ralph(py): live dashboard → run  python3 dashboard_server.py")
    ensure_git_baseline()
    _publish_state(prd, iteration=0, running=True, phase="ralph-build", current=None)

    for i in range(1, args.max_iters + 1):
        prd = load_prd()
        if next_story(prd) is None:
            print("ralph(py): all stories pass — done.")
            break
        print(f"\n=== ralph(py) iteration {i}/{args.max_iters} — "
              f"{remaining(prd)} left ===")
        t0 = time.time()
        try:
            res = run_iteration(i, _real_brain)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001 — one bad iter shouldn't kill the run
            print(f"  iteration error: {e}")
            log_progress(f"iter {i}: iteration crashed — {e}")
            res = "red"
        print(f"  -> {res}  ({time.time() - t0:.0f}s)")
        if res == "done":
            break

    final = load_prd()
    _publish_state(final, running=False, phase=None, current=None)
    print(f"\nralph(py): finished — {remaining(final)} story(ies) remaining. "
          f"See {PROGRESS_PATH.name} and git log.")
    return 0


# --- offline selftest (the 'lets see') --------------------------------------
def selftest() -> int:
    """Prove the harness end-to-end with NO Vertex and NO network: a stubbed
    brain drives a real story pick, a real (path-validated) patch apply, the real
    py_compile + offline smoke gate, real progress/PRD/telemetry writes, and the
    rollback-on-red path. Also proves unsafe paths are rejected. Mutates only a
    temp PRD/progress and two clearly-named artifacts it deletes afterwards."""
    global PRD_PATH, PROGRESS_PATH
    print("ralph selftest — offline (no Vertex, no network)\n")
    checks: list[tuple[str, bool]] = []

    def chk(name: str, cond: bool) -> None:
        checks.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    # 1) path safety — the cardinal rule
    for bad in ("../maestro-core/evil.py", "/etc/evil.py",
                ".git/hooks/pre-commit", "../../../../tmp/evil.py"):
        rejected = False
        try:
            _safe_target(bad)
        except ValueError:
            rejected = True
        chk(f"reject unsafe path {bad!r}", rejected)
    chk("accept in-folder path", _safe_target("scripts/ralph/_x.py").name == "_x.py")

    # 2) full mechanics on a sandboxed PRD with a stubbed brain
    saved_prd, saved_prog, saved_ws = PRD_PATH, PROGRESS_PATH, config.WORKSPACE
    tmp = Path(tempfile.mkdtemp(prefix="ralph-selftest-"))
    artifact = "workspace/_ralph_selftest_artifact.py"   # inside ROOT, gitignored scratch
    broken = "workspace/_ralph_selftest_broken.py"
    try:
        PRD_PATH = tmp / "prd.json"
        PROGRESS_PATH = tmp / "progress.txt"
        # Redirect telemetry + ralph_state/ralph.jsonl into the sandbox so the
        # selftest never pollutes the real workspace with fake build events.
        config.WORKSPACE = tmp / "workspace"
        config.WORKSPACE.mkdir(parents=True, exist_ok=True)
        PROGRESS_PATH.write_text("seeded selftest\n")
        PRD_PATH.write_text(json.dumps({"project": "selftest", "userStories": [
            {"id": "T1", "priority": 1, "passes": False,
             "title": "trivial marker module"}]}))

        def stub_green(_prompt: str) -> str:
            return json.dumps({"rationale": "trivial", "done": True,
                "progress_note": "selftest ok",
                "files": [{"path": artifact, "action": "create",
                           "content": "# ralph selftest artifact (safe to delete)\nVALUE = 42\n"}]})

        res = run_iteration(1, stub_green, commit=False)
        chk("iteration promoted the story", res == "promoted")
        chk("artifact written inside the loop folder", (ROOT / artifact).exists())
        chk("PRD story marked passes=true",
            json.loads(PRD_PATH.read_text())["userStories"][0]["passes"] is True)
        chk("progress logged GREEN", "GREEN" in PROGRESS_PATH.read_text())
        chk("telemetry heartbeat written", (config.WORKSPACE / "status.json").exists())
        rstate = json.loads((config.WORKSPACE / RALPH_STATE).read_text())
        chk("ralph_state.json published the roadmap (dashboard)",
            any(s.get("id") == "T1" for s in rstate.get("stories", [])))
        revents = [json.loads(l) for l in
                   (config.WORKSPACE / RALPH_EVENTS).read_text().splitlines() if l.strip()]
        green_ev = [e for e in revents if e.get("story") == "T1" and e.get("result") == "green"]
        chk("ralph.jsonl logged a GREEN build event", bool(green_ev))
        chk("build event carries a unified diff (view-what-changed)",
            bool(green_ev) and any(c.get("diff") for c in green_ev[-1].get("changes", [])))

        # 3) a RED iteration must roll back and NOT mark the story done
        PRD_PATH.write_text(json.dumps({"project": "selftest", "userStories": [
            {"id": "T2", "priority": 1, "passes": False, "title": "broken"}]}))

        def stub_red(_prompt: str) -> str:
            return json.dumps({"rationale": "oops", "done": True,
                "files": [{"path": broken, "action": "create",
                           "content": "def (:\n    not python\n"}]})

        res2 = run_iteration(2, stub_red, commit=False)
        chk("broken iteration reported red", res2 == "red")
        chk("broken artifact rolled back (gone from disk)", not (ROOT / broken).exists())
        chk("red story NOT marked done",
            json.loads(PRD_PATH.read_text())["userStories"][0]["passes"] in (False, None))
        chk("progress logged RED", "RED" in PROGRESS_PATH.read_text())
    finally:
        for rel in (artifact, broken):
            _safe_unlink(ROOT / rel)
        shutil.rmtree(tmp, ignore_errors=True)
        PRD_PATH, PROGRESS_PATH, config.WORKSPACE = saved_prd, saved_prog, saved_ws

    failed = [n for n, c in checks if not c]
    print(f"\nralph selftest {'PASSED' if not failed else 'FAILED'} "
          f"({len(checks) - len(failed)}/{len(checks)} checks)")
    if failed:
        print("  failing: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
