"""Deterministic, crash-safe cohort allocation and per-cycle dispatch ordering."""

from __future__ import annotations

import hashlib
import hmac
import random
from collections.abc import Iterable, Mapping


def dispatch_rank(shuffle_seed: bytes, cycle_number: int, channel_id: int) -> int:
    digest = hmac.new(shuffle_seed, f"{cycle_number}:{channel_id}".encode(), hashlib.sha256).digest()
    # MongoDB BSON integers are signed 64-bit. Retain a deterministic 63-bit
    # rank instead of occasionally generating an unencodable uint64 value.
    return int.from_bytes(digest[:8], "big", signed=False) & ((1 << 63) - 1)


def cohort_map(channel_ids: Iterable[int], variant_count: int, cohort_seed: bytes) -> dict[int, int]:
    """Assign stable count-balanced cohorts; input/registration ordering cannot influence it."""
    if variant_count < 1:
        raise ValueError("variant_count must be positive")
    ids = sorted(set(channel_ids))
    random.Random(cohort_seed).shuffle(ids)
    return {channel_id: index % variant_count for index, channel_id in enumerate(ids)}


def extend_cohort_map(
    existing: Mapping[int | str, int],
    channel_ids: Iterable[int],
    variant_count: int,
    cohort_seed: bytes,
) -> dict[int, int]:
    """Assign only new channels to the least-populated stable cohorts.

    A running campaign must never reshuffle the cohorts already used by a
    completed or queued delivery cycle.  New channels are therefore ordered
    deterministically and added to the smallest cohort first.  This keeps
    existing variant rotations stable while keeping the network as balanced as
    possible from the next repost onward.
    """
    if variant_count < 1:
        raise ValueError("variant_count must be positive")

    assigned = {int(channel_id): int(cohort) for channel_id, cohort in existing.items()}
    counts = [0] * variant_count
    for cohort in assigned.values():
        if 0 <= cohort < variant_count:
            counts[cohort] += 1

    additions = sorted({int(channel_id) for channel_id in channel_ids} - set(assigned))
    additions.sort(
        key=lambda channel_id: hmac.new(cohort_seed, f"join:{channel_id}".encode(), hashlib.sha256).digest()
    )
    for channel_id in additions:
        smallest = min(counts)
        choices = [index for index, count in enumerate(counts) if count == smallest]
        digest = hmac.new(cohort_seed, f"cohort:{channel_id}".encode(), hashlib.sha256).digest()
        cohort = choices[int.from_bytes(digest[:4], "big") % len(choices)]
        assigned[channel_id] = cohort
        counts[cohort] += 1
    return assigned


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
