#!/usr/bin/env python3
"""Golden-set loading, the train/validation split, and diff freezing.

The split is the overfitting defence: the proposer only ever sees TRAIN misses,
and a promotion must not regress the held-out VAL recall. The split is
deterministic (seedable) so a restart resumes the same partition.
"""

from __future__ import annotations

import hashlib
import json

import config


def load_cases() -> list[dict]:
    lines = config.GOLDEN_FILE.read_text().splitlines()
    cases = [json.loads(l) for l in lines if l.strip()]
    if config.MAX_CASES > 0:
        cases = cases[: config.MAX_CASES]
    return cases


def make_split(cases: list[dict]) -> tuple[set[str], set[str]]:
    """Return (train_ids, val_ids). FORCE_VAL_IDS pins ids into val for tests."""
    ids = [c["id"] for c in cases]
    if config.FORCE_VAL_IDS:
        val = {i for i in ids if i in config.FORCE_VAL_IDS}
    else:
        val = set()
        for i in ids:
            h = int(hashlib.sha256(f"{config.SPLIT_SEED}:{i}".encode()).hexdigest(), 16)
            if (h % 1000) / 1000.0 < config.VAL_FRACTION:
                val.add(i)
    # never let either side be empty when we have >=2 cases
    if len(ids) >= 2:
        if not val:
            val = {ids[-1]}
        if val == set(ids):
            val.discard(ids[0])
    train = {i for i in ids if i not in val}
    return train, val


def assign_new_ids(split: tuple[set[str], set[str]], ids: list[str]) -> tuple[set[str], set[str]]:
    """Deterministically place ids that aren't in the split yet (e.g. freshly
    harvested silver cases) into train/val with the same hash rule as
    make_split, so a resumed split stays stable while the eval set grows."""
    train, val = set(split[0]), set(split[1])
    for i in ids:
        if i in train or i in val:
            continue
        h = int(hashlib.sha256(f"{config.SPLIT_SEED}:{i}".encode()).hexdigest(), 16)
        (val if (h % 1000) / 1000.0 < config.VAL_FRACTION else train).add(i)
    return train, val


def prefetch_diffs(cases: list[dict], get_diff_fn) -> tuple[list[dict], list[str]]:
    """Fetch+validate every diff once so the loop runs on a frozen set.

    Returns (usable_cases, dropped_ids). A case whose historical diff can't be
    fetched (GC'd commit, etc.) is dropped rather than silently scoring 0.
    """
    usable, dropped = [], []
    for c in cases:
        try:
            get_diff_fn(c)
            usable.append(c)
        except Exception:
            dropped.append(c["id"])
    return usable, dropped
