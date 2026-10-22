from app.web.media_kit import media_kit_payload, render_media_kit


def test_media_kit_uses_aggregate_inventory_and_public_directory() -> None:
    payload = media_kit_payload(
        {
            "active_channels": 20,
            "known_audience_channels": 18,
            "subscriber_slots": 100_000,
            "unknown_audience_channels": 2,
            "public_channels": 12,
        },
        {
            "items": [{"label": "Large public channel", "url": "https://t.me/example", "subscriber_slots": 50_000}],
            "total": 12,
            "page": 0,
            "page_size": 24,
        },
        contact_url="tg://user?id=123456",
        insights_url="/promote/clone_abc/insights",
        placements_url="/promote/clone_abc/placements",
    )

    assert payload["network"]["subscriber_slots"] == 100_000
    assert payload["placements"]["total"] == 12
    page = render_media_kit(payload)
    assert "Live public channel browse" in page
    assert "Average measured post views" not in page
    assert "Indicative post-view model" not in page
    assert "not a deduplicated audience" in page
    assert "/promote/clone_abc/insights" in page
    assert "/promote/clone_abc/placements" in page
