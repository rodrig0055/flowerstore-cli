"""flowerstore: FlowerStore.ph gifts, delivery slots and order status as JSON."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import webbrowser
from typing import Any

from . import __version__, dates, feed
from .client import Client, FlowerstoreError, NotFoundError

DEFAULT_HUB = 1  # Metro Manila, the storefront's default


def default_location() -> dict[str, Any]:
    return {"hub_id": DEFAULT_HUB, "hub": "Metro Manila", "city_id": None, "city": None}


class UsageError(FlowerstoreError):
    code = 2


def emit(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def emit_line(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# -- locations -------------------------------------------------------------------------------

def get_hubs(c: Client) -> list[dict[str, Any]]:
    return [{"id": h["value"], "name": h["label"]} for h in c.api("hubs", label="hubs")]


def find_cities(c: Client, query: str) -> list[dict[str, Any]]:
    rows = c.api("city-hub", {"name": query}, label="city lookup")
    return [{"city_id": r["value"], "name": r["city_name"], "area": r["label"]} for r in rows]


def city_hub(c: Client, city_id: str) -> int | None:
    r = c.api("change-hub", {"city_id": city_id}, label="city hub")
    return r.get("hub_id") if isinstance(r, dict) else None


def resolve_location(c: Client, hub: str | None, city: str | None) -> dict[str, Any] | None:
    """--hub (id or name) and/or --city (name or PSGC code) into ids the API takes."""
    if not hub and not city:
        return None
    loc: dict[str, Any] = {"hub_id": None, "hub": None, "city_id": None, "city": None}
    hubs = get_hubs(c)
    if city:
        if city.isdigit():
            loc["city_id"] = city
        else:
            matches = find_cities(c, city)
            exact = [m for m in matches if feed.fold(m["name"]) == feed.fold(city)]
            pick = exact if exact else matches
            if not pick:
                raise UsageError(f"No city matches '{city}'. Try `flowerstore cities {city[:4]}`")
            if len(pick) > 1 and not exact:
                raise UsageError(f"'{city}' matches several cities; pass its city_id",
                                 matches=matches[:8])
            loc["city_id"], loc["city"] = pick[0]["city_id"], pick[0]["name"]
        loc["hub_id"] = city_hub(c, loc["city_id"])
        if loc["hub_id"] is None:
            raise UsageError(f"FlowerStore has no delivery hub for city {loc['city_id']}")
    if hub and loc["hub_id"] is None:
        h = next((h for h in hubs if str(h["id"]) == hub or feed.fold(h["name"]) == feed.fold(hub)), None)
        if h is None:
            h = next((h for h in hubs if feed.fold(hub) in feed.fold(h["name"])), None)
        if h is None:
            raise UsageError(f"No hub matches '{hub}'", hubs=hubs)
        loc["hub_id"] = h["id"]
    loc["hub"] = next((h["name"] for h in hubs if h["id"] == loc["hub_id"]), None)
    return loc


# -- commands --------------------------------------------------------------------------------

def cmd_hubs(c: Client, a) -> dict[str, Any]:
    hubs = get_hubs(c)
    return {"count": len(hubs), "hubs": hubs}


def cmd_cities(c: Client, a) -> dict[str, Any]:
    cities = find_cities(c, a.query)[: a.limit]
    hubs = {h["id"]: h["name"] for h in get_hubs(c)}
    for city in cities:
        hid = city_hub(c, city["city_id"])
        city["hub"] = {"id": hid, "name": hubs.get(hid)} if hid else None
    return {"query": a.query, "count": len(cities), "cities": cities}


def cmd_search(c: Client, a) -> dict[str, Any]:
    loc = resolve_location(c, a.hub, a.city)
    products, meta = feed.load(c, refresh=a.refresh)
    results = feed.search(products, a.query or "", hub_id=loc and loc["hub_id"],
                          city_id=loc and loc["city_id"], min_price=a.min, max_price=a.max,
                          sort=a.sort)
    notice = meta.get("notice")
    if not results:
        notice = notice or "Nothing matched. Try fewer words, or drop --min/--max/--city."
    return {
        "query": a.query,
        "location": loc,
        "total": len(results),
        "count": min(len(results), a.limit),
        "products": results[: a.limit],
        "catalog": {"source": "product feed",
                    "fetched_at": dt.datetime.fromtimestamp(meta["fetched_at"], dates.MANILA).isoformat(),
                    "stale": meta["stale"]},
        "notice": notice,
    }


def _date(text: str) -> dt.date:
    try:
        return dates.parse_date(text)
    except ValueError as e:
        raise UsageError(str(e))


def _listing(item: dict[str, Any]) -> dict[str, Any]:
    regular, price = item.get("unit_price"), item.get("purchase_price")
    return {
        "id": item.get("id"),
        "slug": item.get("slug"),
        "name": item.get("name"),
        "variant": item.get("variant"),
        "price": price,
        "regular_price": regular if regular and price and regular > price else None,
        "currency": "PHP",
        "rating": float(item["ratings"]) if item.get("ratings") else None,
        "reviews": item.get("reviews") or 0,
        "advance_days": item.get("advance_days"),
        "url": f"https://flowerstore.ph/product/{item.get('slug')}",
    }


def cmd_bestsellers(c: Client, a) -> dict[str, Any]:
    loc = resolve_location(c, a.hub, a.city) or default_location()
    r = c.api("search/best-sellers", {"hub_id": loc["hub_id"], "city_id": loc["city_id"],
                                      "per_page": a.limit}, label="best sellers")
    items = [_listing(i) for i in (r.get("data") or [])][: a.limit]
    return {"location": loc, "count": len(items), "products": items}


def _product_props(c: Client, slug: str) -> dict[str, Any]:
    slug = slug.rstrip("/").split("/product/")[-1].split("?")[0]
    props = c.page_props(f"/product/{slug}", label="product page")
    if not props.get("product"):
        raise NotFoundError(f"No product '{slug}' on flowerstore.ph")
    return props


def _variants(c: Client, product_id: int, hub_id: int, day: dt.date) -> list[dict[str, Any]]:
    rows = c.api("product-variants", {"product_id": product_id, "hub_id": hub_id,
                                      "delivery_date": day.isoformat()}, label="variants")
    out = []
    for v in rows:
        if not v.get("published"):
            continue
        # purchase_price is already the price for that date and hub: the server applies
        # campaign, flash deal and flash prices before answering.
        regular, price = v.get("unit_price"), v.get("purchase_price")
        out.append({
            "variant_id": v.get("id"),
            "name": v.get("name"),
            "price": price,
            "regular_price": regular if regular and price and regular > price else None,
            "currency": "PHP",
            "in_stock": (v.get("current_stock") or 0) > 0,
            "price_type": v.get("price_type"),
            "note": v.get("message"),
        })
    return out


def _blocked_dates(c: Client, vendor_id: int, hub_id: int, product_id: int) -> list[str]:
    rows = c.api("calendar-rules", {"vendor_id": vendor_id, "hub_id": hub_id,
                                    "product_ids": [product_id]}, label="calendar")
    today = dates.now().date().isoformat()
    return sorted({d for d in rows if isinstance(d, str) and d >= today})


def cmd_product(c: Client, a) -> dict[str, Any]:
    day = _date(a.date)
    props = _product_props(c, a.slug)
    p = props["product"]
    loc = resolve_location(c, a.hub, a.city) or default_location()
    ratings = props.get("product_ratings") or []
    return {
        "id": p["id"],
        "slug": p["slug"],
        "name": p["name"],
        "description": feed._clean(props.get("html_description") or p.get("description")),
        "tags": [t.strip() for t in (p.get("tags") or "").split(",") if t.strip()],
        "advance_days": p.get("advance_days") or 0,
        "vendor": (props.get("vendor") or {}).get("name"),
        "images": [i["image"] for i in props.get("product_images") or []],
        "location": loc,
        "date": day.isoformat(),
        "variants": _variants(c, p["id"], loc["hub_id"], day),
        "blocked_dates": _blocked_dates(c, p["vendor_id"], loc["hub_id"], p["id"]),
        "recent_reviews": [{"rating": r.get("rate"), "comment": r.get("comment"),
                            "date": (r.get("created_at") or "")[:10]} for r in ratings[:5]],
        "url": f"https://flowerstore.ph/product/{p['slug']}",
    }


def cmd_slots(c: Client, a) -> dict[str, Any]:
    day = _date(a.date)
    props = _product_props(c, a.slug)
    p = props["product"]
    loc = resolve_location(c, a.hub, a.city) or default_location()
    out: dict[str, Any] = {"product": {"id": p["id"], "slug": p["slug"], "name": p["name"]},
                           "location": loc, "date": day.isoformat(), "count": 0, "slots": [],
                           "notice": None}
    today = dates.now().date()
    advance = int(p.get("advance_days") or 0)
    if advance and day < today + dt.timedelta(days=advance):
        earliest = today + dt.timedelta(days=advance)
        out["notice"] = f"This product needs {advance} day(s) notice: the earliest date is {earliest}"
        return out
    if day.isoformat() in _blocked_dates(c, p["vendor_id"], loc["hub_id"], p["id"]):
        out["notice"] = ("Same-day delivery is closed for today" if day == today
                         else f"{day} is blocked for this product in this hub")
        return out
    rows = c.api("delivery-time", {"vendor_id": p["vendor_id"], "vendor_ids": [p["vendor_id"]],
                                   "delivery_date": day.isoformat(), "hub_id": loc["hub_id"],
                                   "product_ids": [p["id"]], "no_express": False},
                 label="delivery slots")
    for s in rows:
        out["slots"].append({
            "id": s.get("id"),
            "name": s.get("name"),
            "start": (s.get("start_time") or "")[:5] or None,
            "end": (s.get("end_time") or "")[:5] or None,
            "order_by": dates.order_by(day, s.get("closes_at"), s.get("weekend_closes_at")),
            "fee": s.get("fee") or s.get("express_fee"),
            "description": feed._clean(s.get("description")) or None,
        })
    out["count"] = len(out["slots"])
    if not out["slots"]:
        out["notice"] = "No slot is open for that date any more" if day == today else \
            "No delivery slots for that date"
    return out


def _track(c: Client, order: str, email: str) -> dict[str, Any]:
    r = c.post("track-order", {"contact_email": email, "order_name": order,
                               "show_shipping_address": False}, label="track order", allow=(422,))
    if r.status_code == 422:
        raise NotFoundError("No order with that number and email")
    o = r.json()
    return {
        "order": o.get("order_name"),
        "status": o.get("order_status"),
        "delivery_type": o.get("delivery_type"),
        "delivery_date": o.get("delivery_date"),
        "delivery_time": o.get("delivery_time"),
        "payment_type": o.get("payment_type"),
        "total": o.get("total_price"),
        "currency": "PHP",
        "items": [{"name": i.get("product_name"), "quantity": i.get("quantity"),
                   "price": i.get("price"), "slug": i.get("slug")}
                  for i in o.get("new_order_items") or []],
    }


def cmd_track(c: Client, a) -> dict[str, Any]:
    return _track(c, a.order, a.email)


def cmd_watch(c: Client, a) -> None:
    every = max(60, a.every)
    keys = ("status", "delivery_date", "delivery_time", "items")
    last = None
    while True:
        try:
            t = _track(c, a.order, a.email)
        except NotFoundError:
            raise
        except FlowerstoreError as e:
            emit_line({"at": dates.now().isoformat(timespec="seconds"), "error": str(e)})
        else:
            snap = {k: t[k] for k in keys}
            if snap != last:
                emit_line({"at": dates.now().isoformat(timespec="seconds"), **t})
                last = snap
            if a.until_delivered and str(t.get("status") or "").lower() in ("delivered", "completed"):
                return
        time.sleep(every)


def cmd_open(c: Client, a) -> dict[str, Any]:
    slug = a.slug.rstrip("/").split("/product/")[-1].split("?")[0]
    url = f"https://flowerstore.ph/product/{slug}"
    opened = False if a.print_only else webbrowser.open(url)
    return {"url": url, "opened": opened}


# -- entry point -----------------------------------------------------------------------------

def _loc_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--hub", help="hub id or name, e.g. 1 or 'Metro Cebu' (default: Metro Manila)")
    p.add_argument("--city", help="delivery city name or PSGC code, e.g. Makati; sets the hub")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="flowerstore", description=__doc__)
    ap.add_argument("--version", action="version", version=f"flowerstore-cli {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    p = sub.add_parser("hubs", help="delivery hubs")
    p.set_defaults(fn=cmd_hubs)

    p = sub.add_parser("cities", help="cities matching a name, with the hub that delivers there")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(fn=cmd_cities)

    p = sub.add_parser("search", help="products matching words, from the cached catalog")
    p.add_argument("query", nargs="?", default="", help="e.g. 'red roses' (empty lists everything)")
    _loc_args(p)
    p.add_argument("--min", type=float, help="lowest price, PHP")
    p.add_argument("--max", type=float, help="highest price, PHP")
    p.add_argument("--sort", choices=["relevance", "price", "-price"], default="relevance")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--refresh", action="store_true", help="download the catalog again now")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("bestsellers", help="the store's best sellers for a hub or city")
    _loc_args(p)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(fn=cmd_bestsellers)

    p = sub.add_parser("product", help="one product: variants and prices for a date, blocked dates")
    p.add_argument("slug", help="product slug or URL, e.g. love-blossom")
    _loc_args(p)
    p.add_argument("-d", "--date", default="today", help="YYYY-MM-DD, today or tomorrow")
    p.set_defaults(fn=cmd_product)

    p = sub.add_parser("slots", help="delivery slots still open for a product on a date")
    p.add_argument("slug")
    _loc_args(p)
    p.add_argument("-d", "--date", default="today")
    p.set_defaults(fn=cmd_slots)

    p = sub.add_parser("track", help="an order's status (needs the order's contact email)")
    p.add_argument("order", help="order number, e.g. FS1234567")
    p.add_argument("--email", required=True)
    p.set_defaults(fn=cmd_track)

    p = sub.add_parser("watch", help="a JSON line each time an order's status changes")
    p.add_argument("order")
    p.add_argument("--email", required=True)
    p.add_argument("--every", type=int, default=300, help="seconds between checks (min 60)")
    p.add_argument("--until-delivered", action="store_true")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("open", help="open a product page in the browser")
    p.add_argument("slug")
    p.add_argument("--print", dest="print_only", action="store_true", help="only print the link")
    p.set_defaults(fn=cmd_open)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "limit", 1) < 1:
        return _fail(UsageError("--limit must be at least 1"))
    client = Client()
    try:
        result = args.fn(client, args)
    except FlowerstoreError as e:
        return _fail(e, client)
    except KeyboardInterrupt:
        return 130
    if result is not None:
        if client.retries:
            result["retries"] = client.retries
        emit(result)
    return 0


def _fail(e: FlowerstoreError, client: Client | None = None) -> int:
    err: dict[str, Any] = {"error": str(e), **e.extra}
    if client and client.retries:
        err["retries"] = client.retries
    sys.stderr.write(json.dumps(err, ensure_ascii=False) + "\n")
    return e.code


if __name__ == "__main__":
    sys.exit(main())
