import datetime as dt

import pytest

from flowerstore_cli import dates, feed

FEED = b"""<?xml version="1.0"?>
<rss xmlns:g="http://base.google.com/ns/1.0"><channel>
<item><g:id>1</g:id><g:title>Beautiful You (12 Roses)</g:title>
  <g:description>Twelve red roses &amp; eucalyptus</g:description>
  <g:link>https://flowerstore.ph/product/beautiful-you?utm_source=feed</g:link>
  <g:image_link>https://img/1.webp</g:image_link><g:availability>in stock</g:availability>
  <g:custom_label_0>1</g:custom_label_0><g:price>3699.00 PHP</g:price><g:sale_price>1499 PHP</g:sale_price></item>
<item><g:id>2</g:id><g:title>Beautiful You (12 Roses)</g:title><g:description>x</g:description>
  <g:link>https://flowerstore.ph/product/beautiful-you</g:link><g:availability>in stock</g:availability>
  <g:custom_label_0>5</g:custom_label_0><g:custom_label_2>042101,042102</g:custom_label_2>
  <g:price>1999.00 PHP</g:price><g:sale_price>1999 PHP</g:sale_price></item>
<item><g:id>3</g:id><g:title>Sunny Tulips</g:title><g:description>Yellow tulips</g:description>
  <g:link>https://flowerstore.ph/product/sunny-tulips</g:link><g:availability>in stock</g:availability>
  <g:custom_label_0>2</g:custom_label_0><g:price>2500.00 PHP</g:price><g:sale_price>2600 PHP</g:sale_price></item>
</channel></rss>"""


@pytest.fixture
def products():
    return feed.parse(FEED)


def test_parse_groups_rows_by_product(products):
    assert [p["slug"] for p in products] == ["beautiful-you", "sunny-tulips"]
    by = products[0]["hubs"]
    assert by["1"]["price"] == 1499 and by["1"]["regular_price"] == 3699
    assert by["5"]["cities"] == ["042101", "042102"]
    assert products[0]["description"] == "Twelve red roses & eucalyptus"


def test_sale_price_above_price_is_ignored(products):
    assert products[1]["hubs"]["2"]["price"] == 2500


def test_search_needs_every_word_and_stems_plurals(products):
    assert [r["slug"] for r in feed.search(products, "red rose")] == ["beautiful-you"]
    assert feed.search(products, "roses tulips") == []
    assert [r["slug"] for r in feed.search(products, "tulip")] == ["sunny-tulips"]


def test_hub_filter(products):
    assert [r["slug"] for r in feed.search(products, "", hub_id=2)] == ["sunny-tulips"]
    r = feed.search(products, "roses", hub_id=1)[0]
    assert (r["price"], r["hub_id"]) == (1499, 1)


def test_city_limited_rows_count_only_for_listed_cities(products):
    # Hub 5 rows only deliver to the cities they list.
    r = feed.search(products, "roses", hub_id=5, city_id="042101")
    assert r and r[0]["hub_id"] == 5 and r[0]["price"] == 1999
    assert feed.search(products, "roses", hub_id=5, city_id="137602") == []


def test_price_bounds_and_sort(products):
    assert [r["slug"] for r in feed.search(products, "", max_price=2000)] == ["beautiful-you"]
    assert [r["slug"] for r in feed.search(products, "", sort="-price")] == ["sunny-tulips", "beautiful-you"]


def test_parse_date():
    today = dt.date(2026, 11, 23)
    assert dates.parse_date("tomorrow", today) == dt.date(2026, 11, 24)
    assert dates.parse_date("2026-12-24", today) == dt.date(2026, 12, 24)
    with pytest.raises(ValueError):
        dates.parse_date("2026-11-22", today)
    with pytest.raises(ValueError):
        dates.parse_date("next friday", today)


def test_order_by_same_day_and_day_before():
    tue = dt.date(2026, 11, 24)
    assert dates.order_by(tue, 13, 13) == "2026-11-24T13:00:00+08:00"
    # 30 hours = 6 AM the day before
    assert dates.order_by(tue, 30, 30) == "2026-11-23T06:00:00+08:00"


def test_order_by_uses_weekend_hour_on_weekends():
    sat = dt.date(2026, 11, 28)
    assert dates.order_by(sat, 9, 7) == "2026-11-28T07:00:00+08:00"


@pytest.mark.parametrize("word,stem", [("roses", "rose"), ("lilies", "lily"), ("glasses", "glass"),
                                       ("bunches", "bunch"), ("boxes", "box"), ("tulips", "tulip"),
                                       ("orchids", "orchid"), ("dress", "dress")])
def test_stem(word, stem):
    assert feed._stem(word) == stem


@pytest.mark.parametrize("name,key", [("Davao City", "davao"), ("City of Davao", "davao"),
                                      ("Cebu City", "cebu"), ("city of  Manila", "manila"),
                                      ("Lapu-Lapu City", "lapu-lapu"), ("Makati", "makati"),
                                      ("Quezon City", "quezon")])
def test_city_key(name, key):
    from flowerstore_cli import cli
    assert cli.city_key(name) == key


def test_city_lookup_retries_without_city_and_picks_the_exact_one(monkeypatch):
    from flowerstore_cli import cli

    class FakeClient:
        def __init__(self):
            self.asked = []

        def api(self, path, payload=None, *, label):
            if path == "city-hub":
                self.asked.append(payload["name"])
                if payload["name"] == "davao":
                    return [{"value": "112402", "city_name": "City of Davao", "label": "Region XI"},
                            {"value": "112403", "city_name": "Davao del Sur town", "label": "Region XI"}]
                return []
            if path == "change-hub":
                return {"hub_id": 3}
            if path == "hubs":
                return [{"value": 3, "label": "Davao City"}]
            raise AssertionError(path)

    c = FakeClient()
    loc = cli.resolve_location(c, None, "Davao City")
    assert c.asked == ["Davao City", "davao"]
    assert (loc["city_id"], loc["city"], loc["hub"]) == ("112402", "City of Davao", "Davao City")


@pytest.mark.parametrize("argv,query", [
    (["search", "crocheted", "sunflowers", "--city", "Davao City"], "crocheted sunflowers"),
    (["search", "red roses", "--max", "2000"], "red roses"),
    (["search"], ""),
    (["cities", "davao", "city"], "davao city"),
])
def test_unquoted_words_make_one_query(monkeypatch, argv, query):
    from flowerstore_cli import cli
    seen = {}

    def fake(c, a):
        seen["query"] = a.query

    monkeypatch.setattr(cli, "cmd_search", fake)
    monkeypatch.setattr(cli, "cmd_cities", fake)
    assert cli.main(argv) == 0
    assert seen["query"] == query
