from app.web.media_kit import media_kit_payload, render_media_kit


def test_media_kit_uses_aggregate_inventory_and_sampled_benchmarks() -> None:
    payload = media_kit_payload(
        {
            "active_channels": 20,
            "known_audience_channels": 18,
            "subscriber_slots": 100_000,
            "unknown_audience_channels": 2,
        },
        {
            "sampled_campaigns": 5,
            "sampled_placements": 90,
            "average_views_per_placement": 220,
            "hourly": [{"hour": 19, "sample_campaigns": 4, "views_per_placement_hour": 18.25}],
        },
        contact_url="https://t.me/iharvester_bot",
    )

    assert payload["network"]["subscriber_slots"] == 100_000
    assert payload["benchmarks"]["hourly"] == [{"hour": 19, "sample_campaigns": 4, "views_per_placement_hour": 18.25}]
    page = render_media_kit(payload)
    assert "Indicative reach model" in page
    assert "not unique people" in page
    assert "/promote/insights" in page
