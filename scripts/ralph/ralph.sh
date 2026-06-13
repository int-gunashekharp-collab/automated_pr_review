#!/usr/bin/env bash
# Ralph runner — thin wrapper around the in-house Python builder.
#
# As of the in-house integration, ralph no longer shells out to the `gemini`
# CLI. It runs ENTIRELY in Python on the SAME Vertex Gemini brain the loop uses
# (scripts/gemini_vertex.py via proposer.call_vertex). That means:
#   * no `npm install -g @google/gemini-cli`  (the EACCES you hit is gone)
#   * no manual "baseline before ralph" commit (ralph.py makes it if missing)
#   * the maestro-core read-only guard now prints exactly what's dirty + how to fix
#
# Usage:
#   ./scripts/ralph/ralph.sh            # build until done (max 10 iterations)
#   ./scripts/ralph/ralph.sh 5          # cap iterations
#   ./scripts/ralph/ralph.sh --selftest # offline proof — no Vertex, no network
#
# Real runs need maestro-core present and Vertex ADC
# (gcloud auth application-default login). The selftest needs neither.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/ralph.py" "$@"
