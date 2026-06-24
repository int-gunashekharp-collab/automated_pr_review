#!/usr/bin/env python3
"""Hourly digest: 'what the loop did' since the last report.

Reports the held-out picture (train vs val recall), the rubric size, the
precision (false-positive) rate, how much of the human review corpus has been
read and incorporated, consolidation events, and rollback availability.
"""

from __future__ import annotations

import datetime
import json
import re
import time
import urllib.request
from pathlib import Path

import attempts
import config
import metrics


def _now() -> float:
    return time.time()


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


class Reporter:
    def __init__(self, baseline: dict, split: tuple[set, set]):
        self.start = _now()
        self.last_emit = _now()
        self.baseline = baseline
        self.champion = baseline
        self.train_ids, self.val_ids = split
        self.champ_size = 0
        self.champ_fp = None
        self.corpus_total = 0
        self.corpus_read = 0
        self.shadow = None   # corpus shadow eval result (human-parity; reported only)
        self.plateau = 0     # consecutive non-promotions (reflect mode trigger)
        self.window: list[dict] = []
        self.totals = {"iters": 0, "accepts": 0, "rejects": 0, "errors": 0,
                       "promotions": 0, "consolidations": 0, "corpus_adds": 0,
                       "model_calls": 0}
        config.HOURLY_DIR.mkdir(parents=True, exist_ok=True)

    # -- feed -------------------------------------------------------------
    def record_iteration(self, rec: dict):
        self.window.append(rec)
        self.totals["iters"] += 1
        self.totals["model_calls"] += rec.get("model_calls", 0)
        if rec["status"] == "accepted":
            self.totals["accepts"] += 1
            self.totals["promotions"] += 1
            if rec.get("kind") == "consolidate":
                self.totals["consolidations"] += 1
            elif rec.get("kind") == "corpus":
                self.totals["corpus_adds"] += 1
        elif rec["status"] == "rejected":
            self.totals["rejects"] += 1
        elif rec["status"] == "error":
            self.totals["errors"] += 1

    def set_champion(self, champ_eval: dict):
        self.champion = champ_eval

    # -- emit -------------------------------------------------------------
    def due(self) -> bool:
        return _now() - self.last_emit >= config.REPORT_INTERVAL_SEC

    def maybe_emit(self, force: bool = False) -> Path | None:
        if not force and not self.due():
            return None
        md = self._render()
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = config.HOURLY_DIR / f"{stamp}.md"
        path.write_text(md)
        (config.REPORTS_DIR / "LATEST.md").write_text(md)
        self._push(md)
        self.window.clear()
        self.last_emit = _now()
        return path

    # -- internals --------------------------------------------------------
    def _d(self, cur, base) -> str:
        if cur is None or base is None:
            return "n/a"
        delta = round(cur - base, 3)
        return f"{cur} ({'+' if delta >= 0 else ''}{delta} vs baseline)"

    def _kind_tag(self, r: dict) -> str:
        return {"consolidate": "🗜️ consolidation", "corpus": "📚 human-corpus rule"}.get(
            r.get("kind"), "🟢 golden-bug rule")

    def _trajectory(self) -> str:
        """Recall trajectory across promotion snapshots (history dir names)."""
        if not config.HISTORY_DIR.exists():
            return ""
        vals = []
        for d in sorted(config.HISTORY_DIR.glob("*")):
            m = re.search(r"-r([0-9.]+)$", d.name)
            if m:
                try:
                    vals.append(float(m.group(1)))
                except ValueError:
                    pass
        return " → ".join(f"{v:g}" for v in vals[-10:]) if vals else ""

    def _render(self) -> str:
        hrs = round((_now() - self.start) / 3600, 1)
        champ = self.champion
        promos = [r for r in self.window if r["status"] == "accepted"]
        tr = metrics.split_recall(champ, self.train_ids)
        vr = metrics.split_recall(champ, self.val_ids)
        missed = champ.get("missed", [])
        miss_val = [m["id"] for m in missed if m["id"] in self.val_ids]
        miss_train = [m["id"] for m in missed if m["id"] in self.train_ids]
        hist = sorted(p.name for p in config.HISTORY_DIR.glob("*")) if config.HISTORY_DIR.exists() else []
        corp = (f"{self.corpus_read}/{self.corpus_total} mined human comments read"
                if self.corpus_total else "corpus not present")

        L = [
            f"# PR-reviewer loop — hourly report ({_ts()})",
            "",
            f"Running {hrs}h · `{config.GEMINI_MODEL}` on Vertex (`{config.GCP_PROJECT}`) "
            f"· {config.CONFIRM_TRIALS}-trial promotions · "
            f"val held out ({len(self.val_ids)}/{len(self.train_ids) + len(self.val_ids)} cases).",
            "",
            "## This hour",
            f"- Iterations: {len(self.window)} "
            f"(✅ {sum(r['status']=='accepted' for r in self.window)} promoted, "
            f"❌ {sum(r['status']=='rejected' for r in self.window)} rejected, "
            f"⚠️ {sum(r['status']=='error' for r in self.window)} errored)",
        ]
        if promos:
            for r in promos:
                L.append(f"- {self._kind_tag(r)}: {r.get('rationale','(no rationale)')}")
                L.append(f"  - train {r.get('train_recall')} · val {r.get('val_recall')} "
                         f"· noise {r.get('cand_noise')} · size {r.get('size')} chars"
                         + (f" · FP {r.get('fp_rate')}" if r.get('fp_rate') is not None else ""))
                for c in r.get("changelog", []):
                    L.append(f"  - {c}")
        else:
            rej = [r for r in self.window if r["status"] == "rejected"]
            L.append(f"- No promotion. Last rejection: {rej[-1].get('reason','?')}" if rej
                     else "- No promotion this hour.")

        traj = self._trajectory()
        att = attempts.stats()
        val_ci = metrics.fmt_ci(metrics.split_ci(champ, self.val_ids))
        # weighted recall for champion
        state = {}
        if config.STATE_FILE.exists():
            try:
                state = json.loads(config.STATE_FILE.read_text())
            except:
                pass
        wr = state.get("weighted_recall")

        # routing tag
        routing_tag = " [routed]" if config.LOOP_ROUTING else ""

        L += [
            "",
            "## Champion now (best rubric found so far)",
            f"- Overall recall: {self._d(champ.get('recall'), self.baseline['recall'])} "
            f"({champ.get('passed')}/{champ.get('scoreable')})" + (f" · weighted {wr}" if wr is not None else ""),
            f"- Train recall {tr} · **held-out val recall {vr}** "
            f"{val_ci + ' ' if val_ci else ''}(the honest number)",
            f"- Noise {champ.get('noise')} findings/review (baseline {self.baseline.get('noise')}) "
            f"· precision FP-rate {self.champ_fp if self.champ_fp is not None else 'n/a'}",
            f"- Rubric size {self.champ_size} / {config.MAX_SKILL_CHARS} char budget{routing_tag}",
            f"- Learning from humans: {self.totals['corpus_adds']} mined patterns incorporated "
            f"· {corp}",
        ]
        if self.shadow and self.shadow.get("shadow_recall") is not None:
            L.append(
                f"- Corpus shadow recall **{self.shadow['shadow_recall']}** — matched "
                f"{self.shadow.get('caught')}/{self.shadow.get('checked')} real human-caught "
                f"bugs from the mined corpus (directional human-parity metric; never gates)")
        if traj:
            L.append(f"- Recall trajectory (promotions): {traj}")
        if self.plateau >= 1:
            note = (" — REFLECT MODE: wider beam + own-failure feedback"
                    if config.PLATEAU_ITERS > 0 and self.plateau >= config.PLATEAU_ITERS
                    else "")
            L.append(f"- Plateau: {self.plateau} consecutive non-promotions{note}")
        L += [
            f"- Still chasing — train {miss_train or 'none'}; "
            f"val (never trained on) {miss_val or 'none'}",
            "",
            "## Totals since start",
            f"- Iterations {self.totals['iters']} · promotions {self.totals['promotions']} "
            f"({self.totals['corpus_adds']} human-corpus, {self.totals['consolidations']} "
            f"consolidations) · rejected {self.totals['rejects']} · errors {self.totals['errors']}",
            f"- Gemini calls: ~{self.totals['model_calls']} · rollback points saved: {len(hist)}",
            f"- Attempt memory: {att['unique']} unique patches tried · "
            f"{att['duplicates_skipped']} duplicates skipped before spending evals",
            "",
            "_Isolated run — your live conventions skill is untouched. Promoted rubric is in "
            "this folder's `workspace/champion-skill/` (adoption bundle: `reports/proposal/`); "
            "roll back any promotion with `python3 loop.py --rollback <id>`._",
        ]
        return "\n".join(L)

    def _push(self, md: str):
        if not config.REPORT_WEBHOOK:
            return
        text = md if len(md) <= 3500 else md[:3490] + "\n…(truncated)"
        try:
            req = urllib.request.Request(
                config.REPORT_WEBHOOK, data=json.dumps({"text": text}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20)
        except Exception as e:
            (config.REPORTS_DIR / "last_send_error.txt").write_text(str(e))
