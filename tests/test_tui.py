"""Drives the TUI end to end against a fake data layer, so it runs offline (and in CI)."""

import asyncio
import datetime as dt

import pytest

pytest.importorskip("textual")

from flowerstore_cli import dates, tui  # noqa: E402

TOMORROW = (dates.now().date() + dt.timedelta(days=1)).isoformat()
LOC = {"hub_id": 1, "hub": "Metro Manila", "city_id": "137602", "city": None}
PRODUCTS = [
    {"slug": "beautiful-you", "name": "Beautiful You (12 Roses)", "price": 1499, "regular_price": 3699},
    {"slug": "sunny-tulips", "name": "Sunny Tulips", "price": 2500, "regular_price": None},
]


@pytest.fixture(autouse=True)
def no_photo_downloads(monkeypatch):
    # Photos are fetched outside the store; keep every test offline unless it fakes them itself.
    monkeypatch.setattr(tui, "fetch_photo", lambda url: None)


class FakeStore:
    def __init__(self):
        self.calls = []
        self.status = "Processing"
        self.stage = "Ready for production"

    def call(self, fn, *args):
        name = fn.__name__
        self.calls.append(name)
        a = args[0] if args else None
        if name == "find_cities":
            return [{"city_id": "137602", "name": "Makati", "area": "NCR"}]
        if name == "resolve_location":
            return dict(LOC)
        if name == "cmd_search":
            assert a.loc["city_id"] == "137602" and a.query == "roses"
            return {"products": list(PRODUCTS)}
        if name == "cmd_bestsellers":
            return {"products": [dict(PRODUCTS[1], variant="Standard")]}
        if name == "_product_props":
            return {"product": {"id": 1, "slug": a, "name": "Beautiful You", "vendor_id": 1}}
        if name == "cmd_product":
            return {"name": "Beautiful You (12 Roses)", "images": ["https://img.example/beautiful-you.webp"], "location": a.loc, "advance_days": 0,
                    "description": "Twelve red roses.", "tags": ["Romance"], "blocked_dates": [],
                    "recent_reviews": [{"rating": 5, "comment": "Lovely"}],
                    "variants": [{"name": "Standard", "price": 1499, "regular_price": 3699,
                                  "in_stock": True, "note": None}]}
        if name == "cmd_slots":
            return {"date": a.date, "notice": None,
                    "slots": [{"name": "10AM - 1PM", "order_by": f"{a.date}T09:00:00+08:00"}]}
        if name == "_track":
            return {"order": args[0], "status": self.status, "stage": self.stage, "delivery_type": "DeliveryTimeFrame",
                    "delivery_date": TOMORROW, "delivery_time": "10AM - 1PM", "total": 1499,
                    "items": [{"name": "Beautiful You", "quantity": 1, "price": 1499}]}
        raise AssertionError(f"unexpected call {name}")


async def until(pilot, cond, secs=5.0):
    for _ in range(int(secs * 20)):
        if cond():
            return True
        await pilot.pause(0.05)
    return False


def text(app, selector):
    return str(app.screen.query_one(selector).render())


def test_search_results_product_and_back():
    async def run():
        app = tui.FlowerstoreApp()
        app.store = FakeStore()
        async with app.run_test(size=(130, 42)) as pilot:
            await pilot.press(*"roses", "tab", *"makati")
            assert await until(pilot, lambda: app.screen.query_one("#city-list").has_class("open"))
            await pilot.press("down", "enter")
            assert app.screen.city["city_id"] == "137602"
            await pilot.press("enter")  # focus is on Find gifts after picking a city
            assert await until(pilot, lambda: isinstance(app.screen, tui.ResultsScreen) and app.screen.rows)
            assert "Makati" in text(app, "#summary")
            assert await until(pilot, lambda: "10AM - 1PM  " in text(app, "#detail-body"))
            assert "by 9:00 AM" in text(app, "#detail-body")
            assert "₱1,499" in text(app, "#detail-body")
            await pilot.press("p")  # cheapest first, then dearest first
            await pilot.press("p")
            assert app.screen.sort == "-price"
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, tui.ProductScreen)
                               and "Lovely" in text(app, "#product-body"))
            await pilot.press("right_square_bracket")
            assert await until(pilot, lambda: "Lovely" in text(app, "#product-body"))
            await pilot.press("escape", "escape")
            assert isinstance(app.screen, tui.HomeScreen)
    asyncio.run(run())


def test_bestsellers_on_a_narrow_terminal_hides_the_side_pane():
    async def run():
        app = tui.FlowerstoreApp()
        app.store = FakeStore()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+b")
            assert await until(pilot, lambda: isinstance(app.screen, tui.ResultsScreen) and app.screen.rows)
            assert "Best sellers" in text(app, "#summary")
            assert app.screen.query_one("#detail").display is False
    asyncio.run(run())


def test_track_and_follow_notices_a_change():
    async def run():
        app = tui.FlowerstoreApp()
        store = app.store = FakeStore()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+o")
            await pilot.press(*"fs-1234567", "enter", *"me@example.com", "enter")
            assert await until(pilot, lambda: "Processing · Ready for production" in text(app, "#track-status"))
            assert "Total ₱1,499" in text(app, "#track-status")
            await pilot.press("f")
            assert await until(pilot, lambda: "Following since" in text(app, "#track-status"))
            store.stage = "Rider assigned"  # the store still says Processing
            await pilot.press("r")
            assert await until(pilot, lambda: "Processing · Rider assigned" in text(app, "#track-status"))
            assert any("changed" in n.title for n in app._notifications)
    asyncio.run(run())


def test_photo_shows_in_side_pane_and_gift_page_and_hides_when_missing(monkeypatch):
    if tui.Photo is None:
        pytest.skip("textual-image not installed")
    from PIL import Image as PILImage

    asked = []

    def fake_fetch(url):
        asked.append(url)
        return PILImage.new("RGB", (40, 40), "#F78F7B") if url else None

    monkeypatch.setattr(tui, "fetch_photo", fake_fetch)

    async def run():
        app = tui.FlowerstoreApp()
        app.store = FakeStore()
        async with app.run_test(size=(130, 42)) as pilot:
            await pilot.press(*"roses", "tab", *"makati")
            assert await until(pilot, lambda: app.screen.query_one("#city-list").has_class("open"))
            await pilot.press("down", "enter", "enter")
            assert await until(pilot, lambda: isinstance(app.screen, tui.ResultsScreen)
                               and "Lovely" not in text(app, "#detail-body")
                               and "₱1,499" in text(app, "#detail-body"))
            assert app.screen.query_one("#detail-img").display is True
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, tui.ProductScreen)
                               and "Lovely" in text(app, "#product-body"))
            assert app.screen.query_one("#product-img").display is True
            assert asked and all(u == "https://img.example/beautiful-you.webp" for u in asked)
    asyncio.run(run())


def test_no_photo_url_hides_the_photo():
    assert tui.photo_url({"product": {"images": []}}) is None
    assert tui.fetch_photo(None) is None
