"""The public product feed (flowerstore.ph/data/feed/), cached locally and searched offline.

Free-text search runs here rather than on the site's search endpoint: on FlowerStore PH that
endpoint reports every query to Meta as a "Search" conversion event, so a CLI calling it would
add noise to the store's ad data. The feed is what Google Shopping reads, so it lists every
product on sale with its price per delivery hub.

Feed rows are one per product per hub. `custom_label_0` is the hub id and `custom_label_2`,
when present, the PSGC codes of the only cities that row delivers to.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .client import Client, FlowerstoreError

TTL = 12 * 3600
G = "{http://base.google.com/ns/1.0}"


def cache_dir() -> Path:
    if os.environ.get("FLOWERSTORE_CACHE_DIR"):
        return Path(os.environ["FLOWERSTORE_CACHE_DIR"])
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "flowerstore-cli"


def _money(text: str | None) -> float | None:
    if not text:
        return None
    m = re.match(r"\s*([\d.]+)", text)
    return float(m.group(1)) if m else None


def _clean(text: str | None) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def parse(xml_bytes: bytes) -> list[dict[str, Any]]:
    """Group feed rows into one product each, with a price block per hub."""
    products: dict[str, dict[str, Any]] = {}
    for item in ET.fromstring(xml_bytes).iter("item"):
        def g(tag: str) -> str | None:
            el = item.find(G + tag)
            if el is None:
                el = item.find(tag)
            return el.text if el is not None else None

        link = (g("link") or "").split("?")[0]
        m = re.search(r"/product/([^/]+)", link)
        if not m:
            continue
        slug = m.group(1)
        price, sale = _money(g("price")), _money(g("sale_price"))
        hub = g("custom_label_0")
        cities = [c for c in (g("custom_label_2") or "").split(",") if c.strip()]
        p = products.setdefault(slug, {
            "slug": slug,
            "name": _clean(g("title")),
            "description": _clean(g("description")),
            "url": f"https://flowerstore.ph/product/{slug}",
            "image": g("image_link"),
            "hubs": {},
        })
        if hub:
            p["hubs"][hub] = {
                "price": sale if sale is not None and price is not None and sale < price else price,
                "regular_price": price,
                "in_stock": (g("availability") or "").strip() == "in stock",
                "cities": cities or None,
            }
    return list(products.values())


def load(client: Client, refresh: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The cached catalog, refreshed when older than TTL. Returns (products, meta)."""
    path = cache_dir() / "feed.json"
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text())
        except (OSError, ValueError):
            cached = None
    fresh = cached and time.time() - cached.get("fetched_at", 0) < TTL
    if cached and fresh and not refresh:
        return cached["products"], {"fetched_at": cached["fetched_at"], "stale": False}
    try:
        products = parse(client.get_bytes("/data/feed/", label="product feed"))
    except (FlowerstoreError, ET.ParseError) as e:
        if cached:
            return cached["products"], {"fetched_at": cached["fetched_at"], "stale": True,
                                        "notice": f"Using the cached catalog: {e}"}
        raise FlowerstoreError(f"product feed: {e}")
    data = {"fetched_at": int(time.time()), "products": products}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)
    return products, {"fetched_at": data["fetched_at"], "stale": False}


# -- search ----------------------------------------------------------------------------------

def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text))


def _stem(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 4 and tok.endswith(("sses", "shes", "ches", "xes", "zes")):
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def score(product: dict[str, Any], query: str) -> float:
    """0 when any query word is missing; otherwise higher for name hits and whole phrases."""
    words = [_stem(t) for t in _tokens(query)]
    if not words:
        return 1.0
    name = {_stem(t) for t in _tokens(product["name"])}
    desc = {_stem(t) for t in _tokens(product["description"])}
    total = 0.0
    for w in words:
        if w in name:
            total += 3
        elif w in desc:
            total += 1
        elif any(n.startswith(w) for n in name) and len(w) >= 3:
            total += 2
        else:
            return 0.0
    if fold(query).strip() in fold(product["name"]):
        total += 2
    return total


def offer_for(product: dict[str, Any], hub_id: int | None, city_id: str | None) -> dict[str, Any] | None:
    """The product's price block where it can be delivered, or None if it can't.

    With a city, a hub row restricted to a list of cities counts only if the city is on it.
    """
    hubs = product["hubs"]
    if hub_id is None and city_id is None:
        prices = [h["price"] for h in hubs.values() if h["price"] is not None]
        return {"hub_id": None, "price": min(prices) if prices else None, "regular_price": None}
    for hid, h in sorted(hubs.items(), key=lambda kv: kv[0] != str(hub_id)):
        if not h["in_stock"]:
            continue
        if h["cities"]:
            if city_id and city_id in h["cities"]:
                return {"hub_id": int(hid), **h}
            continue
        if hub_id is not None and hid == str(hub_id):
            return {"hub_id": int(hid), **h}
    return None


def search(products: list[dict[str, Any]], query: str, *, hub_id: int | None = None,
           city_id: str | None = None, min_price: float | None = None,
           max_price: float | None = None, sort: str = "relevance") -> list[dict[str, Any]]:
    results = []
    for p in products:
        s = score(p, query)
        if s <= 0:
            continue
        offer = offer_for(p, hub_id, city_id)
        if offer is None:
            continue
        price = offer["price"]
        if min_price is not None and (price is None or price < min_price):
            continue
        if max_price is not None and (price is None or price > max_price):
            continue
        results.append({
            "slug": p["slug"],
            "name": p["name"],
            "price": price,
            "regular_price": offer.get("regular_price") if offer.get("regular_price") != price else None,
            "currency": "PHP",
            "hub_id": offer["hub_id"],
            "url": p["url"],
            "image": p["image"],
            "_score": s,
        })
    if sort == "price":
        results.sort(key=lambda r: (r["price"] is None, r["price"] or 0, -r["_score"]))
    elif sort == "-price":
        results.sort(key=lambda r: (r["price"] is None, -(r["price"] or 0), -r["_score"]))
    else:
        results.sort(key=lambda r: (-r["_score"], r["price"] or 0))
    for r in results:
        del r["_score"]
    return results
