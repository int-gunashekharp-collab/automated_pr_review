#!/usr/bin/env bash
# keep_running.sh — keep the self-improving PR-review loop alive on this Mac.
#
# What it does, forever:
#   1. Makes sure Vertex (gcloud ADC) auth is valid BEFORE each run. If it has
#      expired — the exact failure that silently killed iterations 121-132 —
#      it waits and re-checks instead of burning cycles on dead auth.
#   2. Runs `python3 loop.py` (the loop, which itself runs forever + resumes).
#   3. If the loop ever exits/crashes, restarts it automatically with capped
#      backoff so a fast crash-loop can't spin the CPU.
#   4. Logs everything to workspace/loop_supervisor.log.
#
# The ONE thing it can't do for you: the interactive re-auth. When it says auth
# is invalid, run this once in your own terminal (it opens a browser):
#       gcloud auth application-default login
#
set -uo pipefail

# --- resolve the loop dir (this script lives in <loop>/keepalive/) ----------
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOOP_DIR="$(cd "$SELF/.." && pwd)"
cd "$LOOP_DIR" || { echo "cannot cd to loop dir"; exit 1; }

# --- make gcloud / python3 / gh findable even under launchd's bare PATH ------
for p in /opt/homebrew/bin /usr/local/bin "$HOME/google-cloud-sdk/bin" /usr/bin /bin; do
  [[ -d "$p" ]] && case ":$PATH:" in *":$p:"*) ;; *) PATH="$p:$PATH";; esac
done
# Inherit anything else from the login shell (gcloud can live in odd spots).
[[ -r "$HOME/.zprofile" ]] && source "$HOME/.zprofile" 2>/dev/null || true
[[ -r "$HOME/.zshrc"   ]] && source "$HOME/.zshrc"   2>/dev/null || true
export PATH

LOG="$LOOP_DIR/workspace/loop_supervisor.log"
mkdir -p "$LOOP_DIR/workspace"
say(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }

auth_ok(){ gcloud auth application-default print-access-token >/dev/null 2>&1; }

say "supervisor start — loop dir: $LOOP_DIR"
command -v gcloud >/dev/null 2>&1 || say "WARNING: gcloud not on PATH — fix PATH or install the Cloud SDK."
command -v python3 >/dev/null 2>&1 || say "WARNING: python3 not on PATH."
command -v gh >/dev/null 2>&1 || say "NOTE: gh not on PATH — precision/fixed-diff checks will be skipped (non-fatal)."

backoff=5
while true; do
  # 1) Vertex auth must be live, or every iteration just errors.
  if ! auth_ok; then
    say "Vertex/gcloud ADC auth INVALID or expired."
    say "  -> Run this once in YOUR terminal (opens a browser):"
    say "       gcloud auth application-default login"
    say "  Re-checking in 60s; the loop will start automatically once auth is valid."
    sleep 60
    continue
  fi

  # 2) Run the loop (it runs forever and resumes from workspace/state.json).
  say "auth OK — launching: python3 loop.py"
  start=$(date +%s)
  python3 loop.py
  code=$?
  dur=$(( $(date +%s) - start ))
  say "loop.py exited (code=$code) after ${dur}s."

  # 3) Backoff only if it died fast (likely a real problem), else restart promptly.
  if (( dur < 30 )); then
    backoff=$(( backoff*2 > 300 ? 300 : backoff*2 ))
    say "fast exit — backing off ${backoff}s (check the log above for the cause)."
  else
    backoff=5
  fi
  sleep "$backoff"
done
