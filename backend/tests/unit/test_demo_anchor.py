"""The demo anchor: the database seed and the mock shipping provider count back from the
same instant, so an order's delivery date never disagrees with its tracking page."""

from datetime import UTC, date, datetime, timedelta

import httpx

from mock_providers.common.clock import anchored_clock
from mock_providers.shipping.app import create_app
from mock_providers.shipping.config import ShippingSettings
from support_agent.core.config import Settings
from support_agent.core.demo_anchor import resolve_demo_now
from support_agent.db.seed import load_scenarios

ANCHOR = date(2026, 11, 10)
STARTED = datetime(2026, 11, 13, 9, 30, tzinfo=UTC)


def test_a_pinned_date_means_noon_utc_on_that_date() -> None:
    assert resolve_demo_now(ANCHOR, STARTED) == datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def test_without_a_pinned_date_it_is_the_moment_given() -> None:
    assert resolve_demo_now(None, STARTED) == STARTED


def test_the_mock_provider_copy_of_the_rule_agrees_with_the_app() -> None:
    for anchor in (ANCHOR, None):
        assert anchored_clock(anchor, STARTED)() == resolve_demo_now(anchor, STARTED)


def test_a_blank_setting_means_unset_in_both_places() -> None:
    assert ShippingSettings(_env_file=None, demo_anchor_date="").demo_anchor_date is None  # type: ignore[arg-type]
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost/x",  # type: ignore[arg-type]
        jwt_secret="x" * 32,  # type: ignore[arg-type]
        demo_anchor_date="",  # type: ignore[arg-type]
    )
    assert settings.demo_anchor_date is None


async def test_shipping_dates_match_the_seed_dates_for_every_scenario_order() -> None:
    now = resolve_demo_now(ANCHOR, STARTED)  # what scripts/seed_db.py would use
    shipping = create_app(ShippingSettings(_env_file=None, demo_anchor_date=ANCHOR))
    transport = httpx.ASGITransport(app=shipping)
    checked = 0
    async with httpx.AsyncClient(transport=transport, base_url="http://shipping") as http:
        for customer in load_scenarios().customers:
            for order in customer.orders:
                response = await http.get(f"/shipments/{order.order_number}")
                body = response.json()
                if order.delivered_days_ago is None:
                    assert body["delivered_at"] is None
                    continue
                seeded = now - timedelta(days=order.delivered_days_ago)
                assert body["delivered_at"][:10] == seeded.date().isoformat(), order.order_number
                checked += 1
    assert checked >= 8
