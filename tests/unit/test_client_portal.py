from datetime import UTC, datetime, timedelta

from app.web.client_portal import campaign_progress_payload, render_client_portal


def test_client_portal_shows_aggregate_progress_without_network_identifiers() -> None:
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)
    payload = campaign_progress_payload(
        {
            "name": "Client premiere",
            "status": "ACTIVE",
            "mode": "ROTATE",
            "start_at_utc": now - timedelta(hours=1),
            "current_end_at_utc": now + timedelta(hours=1),
            "archived_at": now,
            "updated_at": now,
        },
        {"SENT": 20, "PENDING": 4, "FAILED_PERMANENT": 1},
        {"planned": 3, "completed": 1},
        {"last_updated_at": now},
        live_posts=20,
        joined=7,
    )

    assert payload["campaign"]["name"] == "Client premiere"
    assert payload["delivery"] == {
        "complete": 21,
        "total": 25,
        "percent": 84,
        "sent": 20,
        "pending": 4,
        "failed": 1,
        "unknown": 0,
        "cancelled": 0,
    }
    assert payload["timeline"]["percent"] > 0
    assert "channel" not in str(payload).lower()


def test_client_portal_auto_refreshes_and_has_approval_gated_request_copy() -> None:
    payload = {
        "campaign": {"name": "<Client promotion>", "status": "Active", "mode": "Standard", "start_at": None, "end_at": None, "updated_at": None},
        "delivery": {"complete": 0, "total": 0, "percent": 0, "sent": 0, "pending": 0, "failed": 0, "unknown": 0, "cancelled": 0},
        "timeline": {"percent": 0, "status": "ACTIVE"},
        "cycles": {"completed": 0, "planned": 0},
        "cleanup": {"deleted": 0, "failed": 0, "live_posts": 0},
        "engagement": {"tracked_joins": 0},
    }

    page = render_client_portal("secret-token", payload)

    assert "setInterval(refresh,10000)" in page
    assert "approves every request" in page
    assert "&lt;Client promotion&gt;" in page
    assert "/client/c/${encodeURIComponent(token)}/progress" in page


def test_client_portal_includes_honest_audience_views_and_public_proof_only() -> None:
    payload = campaign_progress_payload(
        {"name": "Measured campaign", "status": "ACTIVE", "mode": "STANDARD"},
        {},
        {},
        {},
        live_posts=3,
        joined=0,
        audience={
            "target_channels": 4,
            "known_audience_channels": 3,
            "subscriber_slots": 12_345,
            "unknown_audience_channels": 1,
            "public_channels": 2,
        },
        views={"total_views": 456, "sampled_placements": 3, "views_per_placement": 152},
        benchmarks={
            "sampled_campaigns": 2,
            "sampled_placements": 6,
            "average_views_per_placement": 123,
            "hourly": [{"hour": 20, "sample_campaigns": 2, "views_per_placement_hour": 14.5}],
        },
        public_placements=[
            {"label": "Visible public placement", "url": "https://t.me/example/123"},
            {"label": "Do not expose", "url": "https://example.test/private"},
        ],
    )

    assert payload["audience"]["subscriber_slots"] == 12_345
    assert payload["views"]["total"] == 456
    assert payload["benchmarks"]["hourly"][0]["hour"] == 20
    assert payload["placements"] == [{"label": "Visible public placement", "url": "https://t.me/example/123"}]
    page = render_client_portal("secret-token", payload)
    assert "Subscriber slots across this campaign" in page
    assert "See public placements" in page
    assert "Private placements remain intentionally private" in page
