"""flowerstore-tui: find a gift, pick a delivery slot and follow an order, with mouse or keyboard.

It drives the same functions as the `flowerstore` CLI, so everything shown here is what the CLI
would print as JSON. Network calls run in worker threads behind one lock, because a single
HTTP session (cookies, CSRF token) is shared.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import threading
from types import SimpleNamespace as NS
from typing import Any, Callable

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.timer import Timer
from textual.widgets import Button, DataTable, Footer, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from . import __version__, cli, dates
from .client import Client, FlowerstoreError

# -- look ------------------------------------------------------------------------------------

LIGHT = Theme(
    name="flowerstore-light", dark=False,
    primary="#F78F7B",      # coral, fills only: text on it is indigo
    secondary="#302D6F",    # indigo
    accent="#302D6F",
    foreground="#302D6F",
    background="#FFEFF0",   # blush
    surface="#FFFFFF",
    panel="#FBD9D3",
    success="#2E7D5B", warning="#B26A00", error="#B3261E",
)
DARK = Theme(
    name="flowerstore-dark", dark=True,
    primary="#F79F8E",
    secondary="#B9B6F2",
    accent="#F79F8E",
    foreground="#FFEFF0",
    background="#16143A",
    surface="#211E52",
    panel="#2C2866",
    success="#7FD1A8", warning="#F5C26B", error="#FF8A80",
)

TULIP = r"""
   ▄█▄ ▄█▄ ▄█▄
   █████████████
   ▀███████████▀
     ▀███████▀
         █
    ▀▄   █   ▄▀
      ▀▄ █ ▄▀
        ▀█▀
""".strip("\n")

CSS = """
Screen { background: $background; color: $foreground; }
#logo { color: $primary; width: auto; }
#brand { text-style: bold; color: $secondary; width: auto; }
#tagline { color: $text-muted; width: auto; margin-bottom: 1; }
.form { width: 72; height: auto; padding: 1 2; background: $surface; border: round $primary; }
.row { height: auto; margin-bottom: 1; }
.row > Label { width: 12; padding-top: 1; color: $text-muted; }
.row > Input, .row > Select { width: 1fr; }
#city-list { height: auto; max-height: 8; display: none; margin-left: 12; border: round $panel; }
#city-list.open { display: block; }
#find { width: 100%; margin-top: 1; }
#find, .primary-btn { background: $primary; color: #302D6F; text-style: bold; }
.links { height: auto; width: auto; margin-top: 1; }
.links Button { margin: 0 1; min-width: 16; }
.title { text-style: bold; padding: 0 1; color: $secondary; }
.muted { color: $text-muted; padding: 0 1; }
#split { height: 1fr; }
#table { width: 3fr; }
#detail { width: 2fr; padding: 0 1; background: $surface; border-left: tall $panel; }
DataTable > .datatable--cursor { background: $primary; color: #302D6F; }
DataTable > .datatable--header { color: $text-muted; text-style: bold; background: $surface; }
#product { padding: 1 2; }
.panel { background: $surface; padding: 1 2; margin: 0 1 1 1; height: auto; border: round $panel; }
#track-form { height: auto; padding: 1 2; }
#track-form Input { width: 1fr; margin-right: 1; }
#track-status { padding: 1 2; }
HelpScreen { align: center middle; }
#help { width: 64; height: auto; padding: 1 2; background: $surface; border: round $primary; }
"""


def peso(v: float | int | None) -> str:
    return "" if v is None else f"₱{v:,.0f}"


def nice_day(d: dt.date) -> str:
    today = dates.now().date()
    label = d.strftime("%a %-d %b")
    if d == today:
        return f"Today · {label}"
    if d == today + dt.timedelta(days=1):
        return f"Tomorrow · {label}"
    return label


def by_time(iso: str | None) -> str:
    """When orders close, short enough for the side pane: "by 1:00 PM" today, else with the day."""
    if not iso:
        return ""
    t = dt.datetime.fromisoformat(iso)
    clock = t.strftime("%-I:%M %p")
    return f"by {clock}" if t.date() == dates.now().date() else f"by {clock} {t.strftime('%a')}"


# The storefront's own words for an order's delivery type (Order.vue on the tracking page).
DELIVERY_LABEL = {
    "DeliveryTimeFrame": "Delivery", "ExpressDelivery": "Express delivery",
    "ShipOutSetting": "Ships", "CampaignShipOutSetting": "Ships",
    "PickupLocation": "Pickup", "DigitalGift": "Digital gift",
}


def nice_time(iso: str | None) -> str:
    if not iso:
        return ""
    t = dt.datetime.fromisoformat(iso)
    day = t.date()
    today = dates.now().date()
    when = "today" if day == today else "tomorrow" if day == today + dt.timedelta(days=1) \
        else day.strftime("%a %-d %b")
    return f"{when} {t.strftime('%-I:%M %p')}"


def system_is_dark() -> bool:
    if sys.platform != "darwin":
        return True
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                             capture_output=True, text=True, timeout=2)
        return out.stdout.strip() == "Dark"
    except (OSError, subprocess.SubprocessError):
        return False


# -- data ------------------------------------------------------------------------------------

class Store:
    """One HTTP session for the whole app, used by one worker at a time."""

    def __init__(self) -> None:
        self.client = Client()
        self.lock = threading.Lock()

    def call(self, fn: Callable[..., Any], *args: Any) -> Any:
        with self.lock:
            return fn(self.client, *args)


# -- home ------------------------------------------------------------------------------------

class HomeScreen(Screen):
    BINDINGS = [
        Binding("ctrl+b", "bestsellers", "Best sellers"),
        Binding("ctrl+o", "track", "Track order"),
        Binding("f1", "app.help", "Help"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.city: dict[str, Any] | None = None
        self._city_timer: Timer | None = None
        self._matches: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        today = dates.now().date()
        days = [(nice_day(today + dt.timedelta(days=i)), (today + dt.timedelta(days=i)).isoformat())
                for i in range(14)]
        budgets = [("Any budget", 0), ("Up to ₱1,000", 1000), ("Up to ₱2,000", 2000),
                   ("Up to ₱3,000", 3000), ("Up to ₱5,000", 5000)]
        with VerticalScroll():
            with Center():
                yield Static(TULIP, id="logo")
            with Center():
                yield Static("flowerstore.ph", id="brand")
            with Center():
                yield Static("Gifts, delivery slots and orders, from your terminal", id="tagline")
            with Center():
                with Vertical(classes="form"):
                    with Horizontal(classes="row"):
                        yield Label("Looking for")
                        yield Input(placeholder="red roses, birthday, sunflowers…",
                                    id="what")
                    with Horizontal(classes="row"):
                        yield Label("Deliver to")
                        yield Input(placeholder="city, e.g. Makati (blank = Metro Manila)", id="city")
                    yield OptionList(id="city-list")
                    with Horizontal(classes="row"):
                        yield Label("Delivery")
                        yield Select(days, value=days[0][1], allow_blank=False, id="day")
                    with Horizontal(classes="row"):
                        yield Label("Budget")
                        yield Select(budgets, value=0, allow_blank=False, id="budget")
                    yield Button("Find gifts", id="find")
            with Center():
                with Horizontal(classes="links"):
                    yield Button("Best sellers", id="go-best", variant="default")
                    yield Button("Track my order", id="go-track", variant="default")
                    yield Button("Help", id="go-help", variant="default")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#what", Input).focus()

    def on_resize(self, event) -> None:
        # The tulip needs about 10 rows; on a short terminal the form matters more.
        self.query_one("#logo").display = event.size.height >= 34

    # city autocomplete ---------------------------------------------------------------------

    @on(Input.Changed, "#city")
    def city_changed(self, event: Input.Changed) -> None:
        if self.city and event.value == self.city["name"]:
            return
        self.city = None
        if self._city_timer:
            self._city_timer.stop()
        lst = self.query_one("#city-list", OptionList)
        if len(event.value.strip()) < 2:
            lst.remove_class("open")
            return
        self._city_timer = self.set_timer(0.35, lambda: self.lookup_cities(event.value.strip()))

    @work(thread=True, exclusive=True, group="cities")
    def lookup_cities(self, text: str) -> None:
        try:
            matches = self.app.store.call(cli.find_cities, text)
        except FlowerstoreError as e:
            self.app.call_from_thread(self.app.notify, str(e), severity="error")
            return
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.show_cities, matches[:8])

    def show_cities(self, matches: list[dict[str, Any]]) -> None:
        self._matches = matches
        lst = self.query_one("#city-list", OptionList)
        lst.clear_options()
        for i, m in enumerate(matches):
            lst.add_option(Option(f"{escape(m['name'])}  [dim]{escape(m['area'])}[/]", id=str(i)))
        lst.set_class(bool(matches), "open")
        if not matches:
            self.app.notify("No city by that name", severity="warning")

    @on(OptionList.OptionSelected, "#city-list")
    def city_picked(self, event: OptionList.OptionSelected) -> None:
        self.pick_city(self._matches[int(event.option.id or 0)])

    def pick_city(self, m: dict[str, Any]) -> None:
        self.city = m
        self.query_one("#city", Input).value = m["name"]
        self.query_one("#city-list", OptionList).remove_class("open")
        # Enter again searches with the default day and budget; shift+tab reaches them.
        self.query_one("#find", Button).focus()

    def on_key(self, event) -> None:
        lst = self.query_one("#city-list", OptionList)
        if event.key == "down" and self.focused and self.focused.id == "city" and lst.has_class("open"):
            lst.focus()
            lst.highlighted = 0
            event.stop()

    # actions -------------------------------------------------------------------------------

    @on(Input.Submitted, "#what")
    def what_submitted(self) -> None:
        self.query_one("#city", Input).focus()

    @on(Input.Submitted, "#city")
    def city_submitted(self) -> None:
        if not self.city and self._matches and self.query_one("#city", Input).value.strip():
            self.pick_city(self._matches[0])
        else:
            self.find()

    @on(Button.Pressed, "#find")
    def find(self) -> None:
        typed = self.query_one("#city", Input).value.strip()
        if typed and not self.city:
            self.app.notify("Pick the city from the list first", severity="warning")
            return
        self.app.push_screen(ResultsScreen(
            query=self.query_one("#what", Input).value.strip(),
            city=self.city,
            day=dt.date.fromisoformat(str(self.query_one("#day", Select).value)),
            budget=int(self.query_one("#budget", Select).value or 0),
        ))

    @on(Button.Pressed, "#go-best")
    def action_bestsellers(self) -> None:
        self.app.push_screen(ResultsScreen(query=None, city=self.city,
                                           day=dt.date.fromisoformat(str(self.query_one("#day", Select).value)),
                                           budget=0, bestsellers=True))

    @on(Button.Pressed, "#go-track")
    def action_track(self) -> None:
        self.app.push_screen(TrackScreen())

    @on(Button.Pressed, "#go-help")
    def go_help(self) -> None:
        self.app.action_help()


# -- results ---------------------------------------------------------------------------------

class ResultsScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("enter", "details", "Details", show=True),
        Binding("o", "open", "Open on site"),
        Binding("p", "sort", "Sort by price"),
        Binding("r", "reload", "Refresh"),
    ]

    def __init__(self, *, query: str | None, city: dict[str, Any] | None, day: dt.date,
                 budget: int, bestsellers: bool = False) -> None:
        super().__init__()
        self.q, self.city, self.day, self.budget, self.best = query, city, day, budget, bestsellers
        self.loc: dict[str, Any] | None = None
        self.rows: list[dict[str, Any]] = []
        self.sort = "relevance"
        self._timer: Timer | None = None
        self._detail_cache: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="summary", classes="title")
        yield Static("Loading…", id="hint", classes="muted")
        with Horizontal(id="split"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="detail"):
                yield Static("", id="detail-body")
        yield Footer()

    def on_resize(self, event) -> None:
        # Below ~110 columns the side pane squeezes the prices off the table: enter still
        # opens the full gift page.
        self.query_one("#detail").display = event.size.width >= 110

    def on_mount(self) -> None:
        t = self.query_one("#table", DataTable)
        t.add_columns("Gift", "Price", "Was")
        self.load()

    @work(thread=True, exclusive=True, group="results")
    def load(self, refresh: bool = False) -> None:
        store = self.app.store
        try:
            loc = store.call(cli.resolve_location, None, self.city["city_id"] if self.city else None) \
                or cli.default_location()
            if self.city:
                loc["city"] = self.city["name"]
            if self.best:
                r = store.call(cli.cmd_bestsellers, NS(loc=loc, limit=40))
                rows = r["products"]
            else:
                r = store.call(cli.cmd_search, NS(query=self.q or "", hub=None, city=None,
                                                  loc=loc, min=None, max=self.budget or None,
                                                  sort="relevance", limit=300, refresh=refresh))
                rows = r["products"]
        except FlowerstoreError as e:
            self.app.call_from_thread(self.failed, str(e))
            return
        self.app.call_from_thread(self.show, loc, rows)

    def failed(self, msg: str) -> None:
        self.query_one("#hint", Static).update(f"[b]Couldn't load:[/] {escape(msg)}  ·  r to retry")

    def show(self, loc: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        self.loc, self.rows = loc, rows
        where = loc.get("city") or loc.get("hub") or "Metro Manila"
        what = "Best sellers" if self.best else (f"“{self.q}”" if self.q else "Everything")
        bits = [what, where, nice_day(self.day)]
        if self.budget:
            bits.append(f"up to {peso(self.budget)}")
        self.query_one("#summary", Static).update(escape("  ·  ".join(bits)))
        self.query_one("#hint", Static).update(
            f"{len(rows)} gifts. Arrows or mouse to browse, enter for details, o to buy on the site."
            if rows else "Nothing matched. Try fewer words, another budget or another city.")
        self.fill()

    def fill(self) -> None:
        rows = self.rows
        if self.sort == "price":
            rows = sorted(rows, key=lambda r: r.get("price") or 0)
        elif self.sort == "-price":
            rows = sorted(rows, key=lambda r: -(r.get("price") or 0))
        t = self.query_one("#table", DataTable)
        t.clear()
        for r in rows:
            name = r["name"] + (f"  ({r['variant']})" if r.get("variant") not in (None, "Standard") else "")
            t.add_row(escape(name), peso(r.get("price")), f"[dim strike]{peso(r.get('regular_price'))}[/]",
                      key=r["slug"])
        if rows:
            t.focus()

    @on(DataTable.RowHighlighted, "#table")
    def highlighted(self, event: DataTable.RowHighlighted) -> None:
        slug = event.row_key.value
        if not slug or not self.loc:
            return
        if slug in self._detail_cache:
            self.render_detail(self._detail_cache[slug])
            return
        self.query_one("#detail-body", Static).update("[dim]Loading prices and slots…[/]")
        if self._timer:
            self._timer.stop()
        self._timer = self.set_timer(0.4, lambda: self.load_detail(slug))

    @work(thread=True, exclusive=True, group="detail")
    def load_detail(self, slug: str) -> None:
        try:
            data = load_product(self.app.store, slug, self.loc, self.day)
        except FlowerstoreError as e:
            data = {"error": str(e), "slug": slug}
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self.detail_loaded, slug, data)

    def detail_loaded(self, slug: str, data: dict[str, Any]) -> None:
        if "error" not in data:
            self._detail_cache[slug] = data
        t = self.query_one("#table", DataTable)
        try:
            current = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            current = None
        if current == slug:
            self.render_detail(data)

    def render_detail(self, data: dict[str, Any]) -> None:
        self.query_one("#detail-body", Static).update(detail_markup(data, compact=True))

    def current_slug(self) -> str | None:
        t = self.query_one("#table", DataTable)
        if not t.row_count:
            return None
        return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value

    def action_details(self) -> None:
        slug = self.current_slug()
        if slug and self.loc:
            self.app.push_screen(ProductScreen(slug, self.loc, self.day))

    @on(DataTable.RowSelected, "#table")
    def row_selected(self) -> None:
        self.action_details()

    def action_open(self) -> None:
        slug = self.current_slug()
        if slug:
            self.app.open_url(f"https://flowerstore.ph/product/{slug}")

    def action_sort(self) -> None:
        self.sort = {"relevance": "price", "price": "-price", "-price": "relevance"}[self.sort]
        self.app.notify({"price": "Cheapest first", "-price": "Dearest first",
                         "relevance": "Best match first"}[self.sort], timeout=2)
        self.fill()

    def action_reload(self) -> None:
        self._detail_cache.clear()
        self.query_one("#hint", Static).update("Loading…")
        self.load(refresh=True)


def load_product(store: Store, slug: str, loc: dict[str, Any], day: dt.date) -> dict[str, Any]:
    props = store.call(cli._product_props, slug)
    args = NS(slug=slug, props=props, loc=loc, date=day.isoformat(), hub=None, city=None)
    product = store.call(cli.cmd_product, args)
    slots = store.call(cli.cmd_slots, args)
    return {"product": product, "slots": slots}


def detail_markup(data: dict[str, Any], compact: bool) -> str:
    if "error" in data:
        return f"[b]Couldn't load this gift[/]\n{escape(data['error'])}"
    p, s = data["product"], data["slots"]
    where = p["location"].get("city") or p["location"].get("hub") or "Metro Manila"
    out = [f"[b]{escape(p['name'])}[/]", f"[dim]Prices for delivery to {escape(where)}[/]", ""]
    for v in p["variants"]:
        was = f"  [dim strike]{peso(v['regular_price'])}[/]" if v.get("regular_price") else ""
        stock = "" if v["in_stock"] else "  [dim](sold out)[/]"
        note = f"  [i]{escape(v['note'])}[/]" if v.get("note") else ""
        out.append(f"  {escape(v['name']):<26} [b]{peso(v['price'])}[/]{was}{stock}{note}")
    out += ["", f"[b]Delivery {escape(nice_day(dt.date.fromisoformat(s['date'])))}[/]"]
    if s["slots"]:
        width = max(len(slot["name"] or "") for slot in s["slots"])
        out.append("  [dim]Slot".ljust(width + 9) + "Order[/]")
        for slot in s["slots"]:
            by = by_time(slot.get("order_by"))
            out.append(f"  {escape((slot['name'] or '').ljust(width))}  [dim]{by}[/]")
    else:
        out.append(f"  [dim]{escape(s.get('notice') or 'No slots')}[/]")
    if p.get("advance_days"):
        out.append(f"  [dim]Needs {p['advance_days']} day(s) notice[/]")
    desc = p.get("description") or ""
    if compact:
        desc = desc[:280] + ("…" if len(desc) > 280 else "")
    out += ["", escape(desc)]
    if not compact:
        if p.get("tags"):
            out += ["", "[dim]" + escape(" · ".join(p["tags"])) + "[/]"]
        upcoming = [d for d in p.get("blocked_dates", []) if d > s["date"]][:6]
        if upcoming:
            out += ["", "[b]No delivery on[/] " + ", ".join(
                dt.date.fromisoformat(d).strftime("%a %-d %b") for d in upcoming)]
        if p.get("recent_reviews"):
            out += ["", "[b]Recent reviews[/]"]
            for r in p["recent_reviews"]:
                out.append(f"  {'★' * int(r.get('rating') or 0)}  {escape(r.get('comment') or '')}")
    else:
        out += ["", "[dim]enter for more · o to buy on flowerstore.ph[/]"]
    return "\n".join(out)


# -- product ---------------------------------------------------------------------------------

class ProductScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("o", "open", "Open on site"),
        Binding("left_square_bracket", "day(-1)", "Day before"),
        Binding("right_square_bracket", "day(1)", "Day after"),
    ]

    def __init__(self, slug: str, loc: dict[str, Any], day: dt.date) -> None:
        super().__init__()
        self.slug, self.loc, self.day = slug, loc, day

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="product"):
            with Horizontal(classes="links"):
                yield Button("Buy on flowerstore.ph", id="buy", classes="primary-btn")
                yield Button("Day before  [", id="prev")
                yield Button("Day after  ]", id="next")
            yield Static("[dim]Loading…[/]", id="product-body", classes="panel")
        yield Footer()

    def on_mount(self) -> None:
        self.load()

    @work(thread=True, exclusive=True, group="product")
    def load(self) -> None:
        try:
            data = load_product(self.app.store, self.slug, self.loc, self.day)
        except FlowerstoreError as e:
            data = {"error": str(e)}
        self.app.call_from_thread(self.query_one("#product-body", Static).update,
                                  detail_markup(data, compact=False))

    def action_day(self, step: int) -> None:
        new = self.day + dt.timedelta(days=step)
        if new < dates.now().date():
            self.app.notify("That day has passed", severity="warning", timeout=2)
            return
        self.day = new
        self.query_one("#product-body", Static).update(f"[dim]Loading {nice_day(new)}…[/]")
        self.load()

    @on(Button.Pressed, "#prev")
    def prev(self) -> None:
        self.action_day(-1)

    @on(Button.Pressed, "#next")
    def next(self) -> None:
        self.action_day(1)

    @on(Button.Pressed, "#buy")
    def action_open(self) -> None:
        self.app.open_url(f"https://flowerstore.ph/product/{self.slug}")
        self.app.notify("Opened in your browser. Pick the variant, date and slot there.", timeout=4)


# -- tracking --------------------------------------------------------------------------------

class TrackScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("f", "follow", "Follow / stop"),
        Binding("r", "check", "Check now"),
    ]
    EVERY = 300

    def __init__(self) -> None:
        super().__init__()
        self.order = self.email = ""
        self.last: dict[str, Any] | None = None
        self.following: Timer | None = None
        self.since: str | None = None
        self.checked: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("Track my order", classes="title")
        yield Static("The order number is in your confirmation email (FS…). Use the email you ordered with.",
                     classes="muted")
        with Horizontal(id="track-form"):
            yield Input(placeholder="Order number", id="order")
            yield Input(placeholder="Email", id="email")
            yield Button("Track", id="go", classes="primary-btn")
        yield Static("", id="track-status", classes="panel")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#order", Input).focus()

    @on(Input.Submitted, "#order")
    def to_email(self) -> None:
        self.query_one("#email", Input).focus()

    @on(Input.Submitted, "#email")
    @on(Button.Pressed, "#go")
    def go(self) -> None:
        self.order = self.query_one("#order", Input).value.strip().upper()
        self.email = self.query_one("#email", Input).value.strip()
        if not self.order or not self.email:
            self.app.notify("Enter both the order number and the email", severity="warning")
            return
        self.last = None
        # Leave the inputs so f (follow) and r (check now) act instead of being typed.
        self.set_focus(None)
        self.action_check()

    def action_check(self) -> None:
        if self.order and self.email:
            self.fetch()

    @work(thread=True, exclusive=True, group="track")
    def fetch(self) -> None:
        try:
            t = self.app.store.call(cli._track, self.order, self.email)
        except FlowerstoreError as e:
            self.app.call_from_thread(self.failed, str(e))
            return
        self.app.call_from_thread(self.show, t)

    def failed(self, msg: str) -> None:
        self.query_one("#track-status", Static).update(f"[b]{escape(msg)}[/]")

    def show(self, t: dict[str, Any]) -> None:
        self.checked = dates.now().strftime("%-I:%M %p")
        keys = ("status", "delivery_date", "delivery_time")
        snap = {k: t.get(k) for k in keys}
        if self.last is not None and snap != self.last:
            self.app.bell()
            self.app.notify(f"{t['order']}: {t.get('status')}", title="Your order changed", timeout=15)
        self.last = snap
        lines = [f"[b]{escape(t['order'] or self.order)}[/]", "",
                 f"[b]{escape(str(t.get('status') or 'Unknown'))}[/]"]
        when = " ".join(str(x) for x in (t.get("delivery_date"), t.get("delivery_time")) if x)
        if when:
            label = DELIVERY_LABEL.get(t.get("delivery_type") or "", "Delivery")
            lines.append(f"{label}: {escape(when)}")
        lines += [""] + [f"  {i['quantity']} × {escape(i['name'] or '')}  [dim]{peso(i.get('price'))}[/]"
                         for i in t["items"]]
        if t.get("total") is not None:
            lines.append(f"  [b]Total {peso(t['total'])}[/]")
        lines.append("")
        if self.following:
            lines.append(f"[dim]Following since {self.since}. Last check {self.checked}. "
                         f"Checks every {self.EVERY // 60} min and rings when anything changes. f to stop.[/]")
        else:
            lines.append(f"[dim]Checked {self.checked}. f to follow it: checks every "
                         f"{self.EVERY // 60} min while this screen is open.[/]")
        self.query_one("#track-status", Static).update("\n".join(lines))

    def action_follow(self) -> None:
        if not self.order:
            self.app.notify("Track an order first", severity="warning")
            return
        if self.following:
            self.following.stop()
            self.following = None
            self.app.notify("Stopped following", timeout=2)
        else:
            self.since = dates.now().strftime("%-I:%M %p")
            self.following = self.set_interval(self.EVERY, self.action_check)
            self.app.notify("Following. You'll hear a bell when it changes.", timeout=3)
        self.action_check()


# -- help ------------------------------------------------------------------------------------

class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Close"), Binding("f1", "app.pop_screen", "Close")]

    def compose(self) -> ComposeResult:
        yield Static(
            "[b]flowerstore-tui[/] " + __version__ + "\n\n"
            "[b]Find a gift[/]  type what you want, pick the city from the list,\n"
            "  the delivery day and a budget, then [b]Find gifts[/].\n"
            "[b]Results[/]  arrows or mouse to browse; the right pane shows prices\n"
            "  and open delivery slots for your day. [b]enter[/] details,\n"
            "  [b]o[/] buy on the site, [b]p[/] sort by price, [b]esc[/] back.\n"
            "[b]Gift page[/]  [b][[/] and [b]][/] change the day.\n"
            "[b]Track[/]  order number + email; [b]f[/] follows it and rings on changes.\n\n"
            "[b]ctrl+b[/] best sellers   [b]ctrl+o[/] track   [b]ctrl+t[/] light/dark   [b]ctrl+q[/] quit\n\n"
            "[dim]Side project, not an official FlowerStore app. Payment always happens\n"
            "on flowerstore.ph. Search uses the store's product feed (refreshed every 12 h).[/]",
            id="help")


# -- app -------------------------------------------------------------------------------------

class FlowerstoreApp(App):
    TITLE = "flowerstore"
    CSS = CSS
    BINDINGS = [
        Binding("ctrl+t", "toggle_theme", "Light/dark"),
        Binding("f1", "help", "Help", show=False),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.store = Store()

    def on_mount(self) -> None:
        self.register_theme(LIGHT)
        self.register_theme(DARK)
        self.theme = DARK.name if system_is_dark() else LIGHT.name
        self.push_screen(HomeScreen())

    def action_toggle_theme(self) -> None:
        self.theme = LIGHT.name if self.theme == DARK.name else DARK.name

    def action_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())


def main() -> None:
    FlowerstoreApp().run()


if __name__ == "__main__":
    main()
