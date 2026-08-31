from app.campaigns.shuffle import cohort_map, dispatch_rank, variant_for


def test_dispatch_rank_is_repeatable_and_cycle_specific() -> None:
    seed = b"s" * 32
    assert dispatch_rank(seed, 4, -1001) == dispatch_rank(seed, 4, -1001)
    assert dispatch_rank(seed, 4, -1001) != dispatch_rank(seed, 5, -1001)


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


def test_standard_and_rotate_variant_selection() -> None:
    assert variant_for("STANDARD", 9, 1, 3) == 0
    assert [variant_for("ROTATE", cycle, 0, 3) for cycle in range(4)] == [0, 1, 2, 0]
