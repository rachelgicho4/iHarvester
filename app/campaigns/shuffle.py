"""Deterministic, crash-safe cohort allocation and per-cycle dispatch ordering."""

from __future__ import annotations

import hashlib
import hmac
import random
from collections.abc import Iterable


def dispatch_rank(shuffle_seed: bytes, cycle_number: int, channel_id: int) -> int:
    digest = hmac.new(shuffle_seed, f"{cycle_number}:{channel_id}".encode(), hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def cohort_map(channel_ids: Iterable[int], variant_count: int, cohort_seed: bytes) -> dict[int, int]:
    """Assign stable count-balanced cohorts; input/registration ordering cannot influence it."""
    if variant_count < 1:
        raise ValueError("variant_count must be positive")
    ids = sorted(set(channel_ids))
    random.Random(cohort_seed).shuffle(ids)
    return {channel_id: index % variant_count for index, channel_id in enumerate(ids)}


def variant_for(mode: str, cycle_number: int, cohort_index: int, variant_count: int) -> int:
    if variant_count < 1:
        raise ValueError("variant_count must be positive")
    if mode == "STANDARD":
        return 0
    if mode == "ROTATE":
        return cycle_number % variant_count
    if mode == "MIX_ROTATE":
        return (cohort_index + cycle_number) % variant_count
    raise ValueError(f"unknown campaign mode: {mode}")

