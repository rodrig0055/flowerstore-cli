# flowerstore-cli

Search FlowerStore.ph gifts, check delivery slots and follow an order from the terminal. Every
command prints JSON, so scripts and AI agents can use it as easily as people. No account and no
API key.

> **Side project, not an official FlowerStore product.** It talks to flowerstore.ph the way the
> site's own pages do. Those endpoints are undocumented and can change without notice.

## Install

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install "flowerstore-cli[tui] @ git+https://github.com/rodrig0055/flowerstore-cli"
flowerstore --help
flowerstore-tui
```

Drop `[tui]` for the CLI alone.

## Quick start

```sh
flowerstore hubs                                         # delivery hubs
flowerstore cities makati                                # a city's code and the hub that delivers there
flowerstore search "red roses" --city Makati --max 3000  # products, best match first
flowerstore search tulips --hub "Metro Cebu" --sort price --limit 5
flowerstore bestsellers --city "Cebu City"
flowerstore product love-blossom -d tomorrow             # variants and prices for that date, blocked dates
flowerstore slots love-blossom -d tomorrow --city Makati # delivery slots still open, with order-by times
flowerstore track FS1234567 --email you@example.com      # an order's status
flowerstore watch FS1234567 --email you@example.com      # a JSON line each time it changes
flowerstore open love-blossom                            # buy it on the website
```

It pipes into other tools:

```sh
flowerstore search "red roses" --city Makati --max 2000 --limit 3 \
  | jq -r '.products[] | "\(.price) PHP  \(.name)"'
# 1299.0 PHP  Red Ecuadorian Rose
# 699.0 PHP  Bright Red Eternal Heart Box'
# 699.0 PHP  Rose Diffuser.
```

## TUI

A full-screen app over the same functions, with mouse or keyboard:

```sh
flowerstore-tui
```

- **Home:** type what you want, then a city (pick it from the suggestions), the delivery day
  and a budget.
- **Results:** a table of gifts. On terminals 110+ columns wide, a side pane shows each gift's
  variant prices and the slots still open that day, with the time orders close. `enter` opens
  the gift, `o` buys it on flowerstore.ph, `p` sorts by price.
- **Gift page:** the full description, blocked dates and recent reviews. `[` and `]` change the day.
- **Photos:** the side pane and the gift page show the product photo. Kitty and Ghostty
  (Kitty graphics) and iTerm2, WezTerm, foot and Windows Terminal (Sixel) draw the real photo; other
  terminals, such as Apple Terminal and VS Code, get coloured half-blocks.
- **Track my order:** order number and email. Where the store only says "Processing", the TUI adds
  the step behind it, such as "Processing · Rider assigned". `f` follows it: it checks every 5 minutes while the
  screen is open, and rings and notifies when the status, date or slot changes.

FlowerStore coral, blush and indigo, light or dark to match macOS (`ctrl+t` toggles). `f1` for help.

## Commands

| Command | What it does |
|---|---|
| `hubs` | The delivery hubs (Metro Manila, Metro Cebu, Davao City, Rest of the Philippines) |
| `cities QUERY` | Cities matching a name, with their PSGC code and delivering hub |
| `search [QUERY]` | Products matching every word, with the price where you deliver |
| `bestsellers` | The store's best sellers for a hub or city |
| `product SLUG` | Description, tags, variants with prices for a date, blocked dates, recent reviews |
| `slots SLUG` | Delivery slots still open for a date, each with the time orders close |
| `track ORDER --email` | Status, the step behind it (`stage`), delivery date and slot, items and total of your order |
| `watch ORDER --email` | JSON Lines: one line per change, checked every 5 minutes (`--every`, min 60 s) |
| `open SLUG` | Opens the product page (`--print` only prints the link) |

Location options on `search`, `bestsellers`, `product` and `slots`:
- `--city` takes a name (`Makati`, `"Cebu City"`) or a PSGC code (`137602`) and sets the hub. A name
  that matches several cities fails with the list, so you can pass the code instead.
- `--hub` takes an id or a name. Without either, the hub is Metro Manila, like the site.

Dates are `YYYY-MM-DD`, `today` or `tomorrow`, in Manila time, up to a year ahead.

## Where the data comes from

- **`search`** reads the public product feed (`/data/feed/`, the one Google Shopping uses), cached
  for 12 hours in `~/.cache/flowerstore-cli` (or `$XDG_CACHE_HOME`, or `$FLOWERSTORE_CACHE_DIR`).
  `--refresh` downloads it again. If a download fails, the cached copy is used and `catalog.stale`
  is `true`. Search runs on the cached copy rather than the site's search box on purpose: on
  FlowerStore PH the search box reports each query to Meta as a "Search" event, and a CLI should
  not add to the store's ad data.
- **Everything else** is live, from the same JSON endpoints the storefront calls: `/api/hubs`,
  `/api/city-hub`, `/api/change-hub`, `/api/search/best-sellers`, `/api/product-variants`,
  `/api/calendar-rules`, `/api/delivery-time` and `/api/track-order`. Product details come from
  the JSON the product page embeds.

Prices:
- Search prices are the feed's sale price when it is lower than the regular price, which is what
  the product page shows.
- `product` prices are already the price for that date and hub, after any campaign, flash deal or
  flash price.

Delivery slots:
- `slots` lists only the slots the site would still offer right now.
- `order_by` is computed from each slot's cutoff hour. A cutoff of 24 or more reaches back whole
  days, so 30 means 6 AM the day before.

## Being gentle with the site

- Cloudflare challenges clients that don't look like a browser, so requests go out with a Chrome
  TLS fingerprint (`curl_cffi`).
- It sends at most two requests a second.
- It retries a dropped connection or a 500/502 once.
- It stops on a Cloudflare challenge or a 429.
- Retries that happened are listed in the output's `retries`.

## Output and errors

stdout is JSON (JSON Lines for `watch`). Errors go to stderr as `{"error": ...}`, with these exit
codes:

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | flowerstore.ph failed or answered something unexpected |
| 2 | Bad input (date, city, hub, limit) |
| 3 | Not found (product, or no order with that number and email) |
| 4 | Blocked by Cloudflare or rate limited |

## Development

```sh
uv sync --all-extras --group dev
uv run pytest                   # offline: feed parsing, search, cutoffs, and the TUI against fake data
uv run flowerstore hubs         # live
```
