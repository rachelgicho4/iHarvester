from app.db.repositories import Repositories


def test_view_tracking_payload_keeps_live_and_mature_evidence_distinct() -> None:
    payload = Repositories._view_tracking_payload(
        {
            "current_views": 1_200,
            "finalized_views": 8_000,
            "current_sampled_placements": 24,
            "finalized_placements": 60,
            "current_mature_views": 2_000,
            "finalized_mature_views": 6_000,
            "current_mature_placements": 12,
            "finalized_mature_placements": 40,
            "current_mature_placement_hours": 110.5,
            "finalized_mature_placement_hours": 300,
        }
    )

    assert payload["total_views"] == 9_200
    assert payload["current_views"] == 1_200
    assert payload["mature_views"] == 8_000
    assert payload["mature_placements"] == 52
