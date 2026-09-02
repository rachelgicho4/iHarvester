from bson import BSON

from app.campaigns.shuffle import cohort_map, dispatch_rank, variant_for


def test_dispatch_rank_is_repeatable_and_cycle_specific() -> None:
    seed = b"s" * 32
    assert dispatch_rank(seed, 4, -1001) == dispatch_rank(seed, 4, -1001)
    assert dispatch_rank(seed, 4, -1001) != dispatch_rank(seed, 5, -1001)


def test_dispatch_rank_is_always_a_mongo_safe_signed_int64() -> None:
    for channel_id in range(-1010, -990):
        rank = dispatch_rank(b"x" * 32, 0, channel_id)
        assert 0 <= rank < 2**63
        BSON.encode({"dispatch_rank": rank})


def test_registry_insertion_order_cannot_set_cohort_or_send_order() -> None:
    ids = [-1000, -1001, -1002, -1003, -1004, -1005, -1006]
    first = cohort_map(ids, 3, b"c" * 32)
    second = cohort_map(list(reversed(ids)), 3, b"c" * 32)
    assert first == second
    ranks = sorted(ids, key=lambda channel_id: dispatch_rank(b"x" * 32, 0, channel_id))
    assert ranks != ids


def test_mix_cohorts_are_balanced_and_rotate_every_variant() -> None:
    ids = list(range(20))
    cohorts = cohort_map(ids, 3, b"c" * 32)
    counts = [list(cohorts.values()).count(index) for index in range(3)]
    assert max(counts) - min(counts) <= 1
    for _channel_id, cohort in cohorts.items():
        assert {variant_for("MIX_ROTATE", cycle, cohort, 3) for cycle in range(3)} == {0, 1, 2}


def test_large_network_rotation_is_balanced_and_complete_for_many_variant_counts() -> None:
    ids = list(range(-10_607, -10_000))
    for variant_count in (2, 3, 7, 20):
        cohorts = cohort_map(ids, variant_count, b"large-network")
        sizes = [list(cohorts.values()).count(index) for index in range(variant_count)]
        assert max(sizes) - min(sizes) <= 1
        for cohort in cohorts.values():
            visited = {
                variant_for("MIX_ROTATE", cycle, cohort, variant_count)
                for cycle in range(variant_count)
            }
            assert visited == set(range(variant_count))


def test_standard_and_rotate_variant_selection() -> None:
    assert variant_for("STANDARD", 9, 1, 3) == 0
    assert [variant_for("ROTATE", cycle, 0, 3) for cycle in range(4)] == [0, 1, 2, 0]
