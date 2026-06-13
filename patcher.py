#!/usr/bin/env python3
"""Apply a proposed rubric patch to the CANDIDATE skill copy (never the live one).

Three safe actions: append to / rewrite an existing markdown file, or create a
new references/*.md. Paths are validated to stay inside the candidate dir.
"""

from __future__ import annotations

from pathlib import Path


def _safe_target(candidate_dir: Path, rel: str) -> Path:
    target = (candidate_dir / rel).resolve()
    root = candidate_dir.resolve()
    if root not in target.parents and target != root:
        raise ValueError(f"edit escapes candidate dir: {rel}")
    if target.suffix != ".md":
        raise ValueError(f"only .md edits allowed: {rel}")
    if not (target.name == "SKILL.md" or target.parent.name == "references"):
        raise ValueError(f"edits must target SKILL.md or references/*.md: {rel}")
    return target


def apply_patch(candidate_dir: Path, patch: dict) -> list[str]:
    """Mutate the candidate skill in place. Returns a human-readable change log."""
    changelog: list[str] = []
    for edit in patch["edits"]:
        rel = edit.get("file", "")
        action = edit.get("action", "append")
        content = (edit.get("content") or "").strip()
        if not rel or not content:
            raise ValueError(f"malformed edit: {edit!r}")
        target = _safe_target(candidate_dir, rel)
        nlines = content.count("\n") + 1

        if action == "append":
            if not target.exists():
                raise ValueError(f"append target does not exist: {rel}")
            target.write_text(target.read_text().rstrip() + "\n\n" + content + "\n")
            changelog.append(f"append +{nlines} lines -> {rel}")
        elif action == "rewrite":
            if not target.exists():
                raise ValueError(f"rewrite target does not exist: {rel}")
            before = len(target.read_text())
            target.write_text(content + "\n")
            changelog.append(f"rewrite {rel} ({before}->{len(content)} chars)")
        elif action == "create":
            if target.exists():
                target.write_text(target.read_text().rstrip() + "\n\n" + content + "\n")
                changelog.append(f"append (existed) -> {rel}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content + "\n")
                changelog.append(f"create {target.name} ({nlines} lines) -> {rel}")
        else:
            raise ValueError(f"unknown action: {action!r}")
    return changelog
