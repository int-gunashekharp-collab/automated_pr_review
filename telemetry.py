#!/usr/bin/env python3
"""Telemetry for the live dashboard — status heartbeat + Gemini thought capture.

Two small write streams, both under the loop's own workspace (isolation holds):

  * workspace/status.json    — atomic heartbeat: current phase, iteration, kind,
                               and (during evals) per-case/per-trial progress.
  * workspace/thoughts.jsonl — what Gemini was just asked and what it replied
                               (truncated heads), one record per proposer call.

Design rules:
  * CRASH-PROOF: every public function swallows ALL exceptions. The loop must
    never fail because of dashboard plumbing.
  * Paths are read from `config` at CALL time (not import time), so the smoke
    test's config redirection keeps working and writes stay in its sandbox.
  * Atomic status writes (tmp + os.replace) so readers never see torn JSON.
"""

from __future__ import annotations

import json
import os
import time

import config

_LOOP_STARTED = time.time()
_seq = 0
_state: dict = {}

_THOUGHT_PROMPT_HEAD = 1500
_THOUGHT_RESPONSE_HEAD = 6000
_THOUGHTS_MAX_LINES = 400
_THOUGHTS_KEEP_LINES = 200


def _write_status():
    global _seq
    _seq += 1
    _state.update(ts=round(time.time(), 3), seq=_seq, pid=os.getpid(),
                  loop_started=round(_LOOP_STARTED, 3),
                  model=getattr(config, "GEMINI_MODEL", ""))
    config.WORKSPACE.mkdir(parents=True, exist_ok=True)
    tmp = config.WORKSPACE / ".status.json.tmp"
    tmp.write_text(json.dumps(_state))
    os.replace(tmp, config.WORKSPACE / "status.json")


def phase(name: str, **info):
    """Record the loop's current phase (plan/proposing/screening/confirming/
    precision/deciding/decided/idle/...). Resets any stale eval progress."""
    try:
        _state.clear()
        _state.update(phase=name, **info)
        _write_status()
    except Exception:
        pass


def eval_progress(**kw):
    """Merge per-case/per-trial eval progress into the current status."""
    try:
        _state["eval"] = kw
        _write_status()
    except Exception:
        pass


def thought(kind: str, prompt: str, response: str, **meta):
    """Append one Gemini exchange (truncated) to thoughts.jsonl, bounded."""
    try:
        rec = {"ts": round(time.time(), 3), "kind": kind,
               "prompt_chars": len(prompt), "response_chars": len(response),
               "prompt_head": prompt[:_THOUGHT_PROMPT_HEAD],
               "response_head": response[:_THOUGHT_RESPONSE_HEAD]}
        if meta:
            rec.update(meta)
        config.WORKSPACE.mkdir(parents=True, exist_ok=True)
        f = config.WORKSPACE / "thoughts.jsonl"
        with f.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        lines = f.read_text().splitlines()
        if len(lines) > _THOUGHTS_MAX_LINES:
            tmp = config.WORKSPACE / ".thoughts.jsonl.tmp"
            tmp.write_text("\n".join(lines[-_THOUGHTS_KEEP_LINES:]) + "\n")
            os.replace(tmp, f)
    except Exception:
        pass
