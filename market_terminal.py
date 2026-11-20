#!/usr/bin/env python3
"""Terminal dashboard: market quotes plus a live, merged RSS headline feed.

Keys:  <number> + Enter  open that headline    r  refresh now    q  quit
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, Lock, Thread

import feedparser
import yfinance as yf
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

DEFAULT_FEEDS = {
    "Markets": "https://feeds.bloomberg.com/markets/news.rss",
    "Business": "https://feeds.bloomberg.com/business/news.rss",
    "Technology": "https://feeds.bloomberg.com/technology/news.rss",
    "Politics": "https://feeds.bloomberg.com/politics/news.rss",
    "Economics": "https://feeds.bloomberg.com/economics/news.rss",
}

# ^TNX: 10Yr Treasury yield, GC=F: Gold futures
DEFAULT_SYMBOLS = [
    "^GSPC", "^DJI", "^TNX", "GC=F",
    "AAPL", "MSFT", "NVDA", "AMZN", "TSLA",
]

# Words that link a headline to a symbol. Headlines rarely contain tickers, so
# company/index names do most of the work. Plain-letter tickers (AAPL) are
# also matched automatically, case-sensitively. Add your own symbols here.
ALIASES: dict[str, list[str]] = {
    "^GSPC": ["S&P 500", "S&P"],
    "^DJI": ["Dow Jones", "Dow"],
    "^TNX": ["Treasury", "Treasuries"],
    "GC=F": ["Gold"],
    "AAPL": ["Apple"],
    "MSFT": ["Microsoft"],
    "NVDA": ["Nvidia"],
    "AMZN": ["Amazon"],
    "TSLA": ["Tesla"],
}

USER_AGENT = "Mozilla/5.0 (compatible; market-terminal/2.0)"
HTTP_TIMEOUT = 10  # seconds; feedparser has no timeout of its own
MAX_WORKERS = 8
EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #

@dataclass
class Headline:
    title: str
    url: str
    category: str
    published: datetime | None  # age is computed at render time so it stays live


@dataclass
class Quote:
    symbol: str
    price: float | None = None
    change: float | None = None
    change_percent: float | None = None
    volume: int | None = None
    day_low: float | None = None
    day_high: float | None = None
    high_52wk: float | None = None
    low_52wk: float | None = None
    error: str | None = None


@dataclass
class Snapshot:
    quotes: list[Quote]
    headlines: list[Headline]
    last_updated: datetime | None
    refreshing: bool
    warnings: list[str]
    input_buffer: str
    message: str
    held: bool
    next_refresh: float


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def entry_datetime(entry) -> datetime | None:
    """feedparser already normalises dates to a UTC struct_time."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def fetch_feed(category: str, url: str) -> list[Headline]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        parsed = feedparser.parse(response.read())

    if not parsed.entries and parsed.get("bozo"):
        raise ValueError(f"unreadable feed ({parsed.get('bozo_exception')})")

    headlines = []
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if title and link:
            headlines.append(Headline(title, link, category, entry_datetime(entry)))
    return headlines


def merge_headlines(headlines: list[Headline]) -> list[Headline]:
    """Newest first across ALL feeds, with duplicates removed."""
    seen: set[str] = set()
    merged = []
    for headline in sorted(headlines, key=lambda h: h.published or EPOCH, reverse=True):
        title_key = headline.title.casefold()
        if headline.url in seen or title_key in seen:
            continue
        seen.update((headline.url, title_key))
        merged.append(headline)
    return merged


def _num(value) -> float | None:
    """float() that maps None/NaN/garbage to None (pandas gives NaN, not None)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def fetch_quote(symbol: str) -> Quote:
    """Never raises. One 1y history call supplies price, change, day and 52w range."""
    try:
        history = yf.Ticker(symbol).history(
            period="1y", interval="1d", auto_adjust=False
        )
        history = history.dropna(subset=["Close"])
        if history.empty:
            return Quote(symbol, error="No data")

        last = history.iloc[-1]
        price = _num(last["Close"])
        previous = _num(history["Close"].iloc[-2]) if len(history) > 1 else None

        change = change_percent = None
        if price is not None and previous is not None:
            change = price - previous
            if previous:
                change_percent = change / previous * 100

        volume = _num(last.get("Volume"))

        return Quote(
            symbol=symbol,
            price=price,
            change=change,
            change_percent=change_percent,
            volume=int(volume) if volume else None,
            day_low=_num(last.get("Low")),
            day_high=_num(last.get("High")),
            high_52wk=_num(history["High"].max()),
            low_52wk=_num(history["Low"].min()),
        )
    except Exception as exc:
        return Quote(symbol, error=str(exc) or type(exc).__name__)


def refresh_all(
    symbols: list[str], feeds: dict[str, str]
) -> tuple[list[Quote], list[Headline], list[str]]:
    """Fetch every quote and feed concurrently instead of one after another."""
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        feed_futures = {name: pool.submit(fetch_feed, name, url) for name, url in feeds.items()}
        quote_futures = [pool.submit(fetch_quote, symbol) for symbol in symbols]

        quotes = [future.result() for future in quote_futures]
        headlines: list[Headline] = []
        warnings: list[str] = []
        for name, future in feed_futures.items():
            try:
                headlines.extend(future.result())
            except Exception as exc:
                warnings.append(f"{name} feed: {exc}")

    return quotes, merge_headlines(headlines), warnings


# --------------------------------------------------------------------------- #
# Shared state (written by the refresh thread, read by the UI thread)
# --------------------------------------------------------------------------- #

class AppState:
    def __init__(self) -> None:
        self._lock = Lock()
        self._quotes: list[Quote] = []
        self._headlines: list[Headline] = []
        self._pending_headlines: list[Headline] | None = None
        self._last_updated: datetime | None = None
        self._refreshing = False
        self._warnings: list[str] = []
        self._input = ""
        self._message = ""
        self._message_until = 0.0
        self._next_refresh = time.monotonic()

    # -- refresh side -------------------------------------------------------
    def begin_refresh(self) -> None:
        with self._lock:
            self._refreshing = True

    def apply_update(self, quotes, headlines, warnings) -> None:
        with self._lock:
            self._quotes = quotes
            self._warnings = list(warnings)
            self._last_updated = datetime.now().astimezone()
            if headlines:
                self._pending_headlines = headlines
            elif self._headlines:
                self._warnings.append("showing cached headlines")
            self._flush_headlines_locked()

    def fail_refresh(self, message: str) -> None:
        with self._lock:
            self._warnings = [f"refresh failed: {message}"]

    def finish_refresh(self, interval: int) -> None:
        with self._lock:
            self._refreshing = False
            self._next_refresh = time.monotonic() + interval

    def _flush_headlines_locked(self) -> None:
        # Don't reshuffle the numbering while the user is mid-way through
        # typing a headline number, or Enter would open the wrong story.
        if self._pending_headlines is not None and not self._input:
            self._headlines = self._pending_headlines
            self._pending_headlines = None

    # -- keyboard side ------------------------------------------------------
    def add_digit(self, digit: str) -> None:
        with self._lock:
            if len(self._input) < 3:
                self._input += digit

    def backspace(self) -> None:
        with self._lock:
            self._input = self._input[:-1]
            self._flush_headlines_locked()

    def clear_input(self) -> None:
        with self._lock:
            self._input = ""
            self._flush_headlines_locked()

    def take_selection(self) -> tuple[int | None, list[Headline]]:
        with self._lock:
            number = int(self._input) if self._input else None
            headlines = self._headlines  # the list the user was looking at
            self._input = ""
            self._flush_headlines_locked()
        return number, headlines

    def flash(self, message: str, seconds: float = 4.0) -> None:
        with self._lock:
            self._message = message
            self._message_until = time.monotonic() + seconds

    # -- render side --------------------------------------------------------
    def snapshot(self) -> Snapshot:
        with self._lock:
            now = time.monotonic()
            return Snapshot(
                quotes=self._quotes,
                headlines=self._headlines,
                last_updated=self._last_updated,
                refreshing=self._refreshing,
                warnings=list(self._warnings),
                input_buffer=self._input,
                message=self._message if now < self._message_until else "",
                held=self._pending_headlines is not None,
                next_refresh=self._next_refresh,
            )


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

def format_age(published: datetime | None, now: datetime) -> str:
    if published is None:
        return "—"
    seconds = (now - published).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def format_number(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def format_price(symbol: str, price: float | None) -> str:
    # Indices (^GSPC) and yields (^TNX) are not dollar amounts.
    prefix = "" if symbol.startswith("^") else "$"
    return "—" if price is None else f"{prefix}{price:,.2f}"


def format_range(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return "—"
    digits = 0 if high >= 100 else 2  # "3–5" is useless for a 4.25% yield
    return f"{low:,.{digits}f}–{high:,.{digits}f}"


def format_volume(value: int | None) -> str:
    if not value:
        return "—"
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= threshold:
            return f"{value / threshold:.2f}{suffix}"
    return f"{value:,}"


def render_day_range(
    price: float | None, low: float | None, high: float | None, width: int = 8
) -> str:
    """Marker showing where price sits within today's low-high span."""
    if price is None or low is None or high is None or low == high:
        return "—"
    position = max(0.0, min(1.0, (price - low) / (high - low)))
    bar = ["░"] * width
    bar[int(position * (width - 1))] = "█"
    return "".join(bar)


def format_countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


# --------------------------------------------------------------------------- #
# Symbol <-> headline matching
# --------------------------------------------------------------------------- #

def build_matchers(symbols: list[str]) -> list[tuple[str, list[re.Pattern]]]:
    matchers = []
    for symbol in symbols:
        patterns = []
        names = ALIASES.get(symbol, [])
        if names:
            joined = "|".join(rf"\b{re.escape(name)}\b" for name in names)
            patterns.append(re.compile(joined, re.IGNORECASE))
        if symbol.isalpha():  # AAPL yes; ^GSPC / GC=F no (would match "GC", "GSPC" noise)
            patterns.append(re.compile(rf"\b{re.escape(symbol)}\b"))
        if patterns:
            label = symbol.lstrip("^").split("=")[0]
            matchers.append((label, patterns))
    return matchers


def match_symbols(title: str, matchers) -> str:
    hits = [label for label, patterns in matchers if any(p.search(title) for p in patterns)]
    return ",".join(hits[:2])


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def build_quotes_panel(quotes: list[Quote]) -> Panel:
    if not quotes:
        body = Text("Loading market data…", style="dim")
    else:
        body = Table(expand=True, box=box.SIMPLE_HEAVY, header_style="bold cyan")
        body.add_column("Symbol", style="bold white")
        body.add_column("Price", justify="right")
        body.add_column("Change", justify="right")
        body.add_column("%", justify="right")
        body.add_column("Day Range", justify="center")
        body.add_column("52W Range", justify="right", style="dim")
        body.add_column("Volume", justify="right")

        for q in quotes:
            if q.error:
                body.add_row(
                    Text(q.symbol),
                    Text("Error", style="red"),
                    Text(q.error[:30], style="red"),
                    "", "", "", "",
                )
                continue

            if q.change is None:
                color = "dim"
            else:
                color = "green" if q.change >= 0 else "red"
            change = "—" if q.change is None else f"{q.change:+,.2f}"
            percent = "—" if q.change_percent is None else f"{q.change_percent:+.2f}%"

            body.add_row(
                Text(q.symbol),
                format_price(q.symbol, q.price),
                Text(change, style=color),
                Text(percent, style=color),
                Text(render_day_range(q.price, q.day_low, q.day_high), style="cyan"),
                format_range(q.low_52wk, q.high_52wk),
                format_volume(q.volume),
            )

    return Panel(body, title="Market Data & Benchmarks", border_style="blue")


def build_headlines_panel(headlines: list[Headline], matchers) -> Panel:
    if not headlines:
        body = Text("Loading headlines…", style="dim")
    else:
        body = Table(expand=True, box=box.SIMPLE, header_style="bold cyan")
        body.add_column("#", width=4, justify="right")
        body.add_column("Cat", width=10, style="yellow", no_wrap=True, overflow="ellipsis")
        body.add_column("Headline", ratio=1, no_wrap=True, overflow="ellipsis")
        body.add_column("Match", width=10, style="bold magenta", no_wrap=True)
        body.add_column("Age", width=9, justify="right", style="dim")

        now = datetime.now(timezone.utc)
        for index, h in enumerate(headlines, start=1):
            body.add_row(
                str(index),
                Text(h.category),
                Text(h.title, style="white"),  # Text, not str: titles may contain [brackets]
                match_symbols(h.title, matchers),
                format_age(h.published, now),
            )

    return Panel(body, title="Live News Feed", border_style="magenta")


def build_footer(snap: Snapshot) -> Group:
    lines = []
    if snap.input_buffer:
        line = Text(f"Open headline: {snap.input_buffer}▌", style="bold")
        if snap.held:
            line.append("   (new headlines held until you finish)", style="dim yellow")
        lines.append(line)
    elif snap.message:
        lines.append(Text(snap.message, style="green"))

    status = Text(style="dim")
    status.append("<number>+Enter open | r refresh | q quit | ")
    if snap.refreshing:
        status.append("refreshing…", style="yellow")
    else:
        status.append(f"next refresh in {format_countdown(snap.next_refresh - time.monotonic())}")
    lines.append(status)

    for warning in snap.warnings:
        lines.append(Text(f"⚠ {warning}", style="yellow"))
    return Group(*lines)


def build_dashboard(snap: Snapshot, matchers, interactive: bool = True) -> Group:
    title = Text()
    title.append(" Market Terminal ", style="bold white on blue")
    if snap.last_updated:
        title.append(f" Updated: {snap.last_updated:%Y-%m-%d %H:%M:%S}", style="dim")

    parts = [
        Panel(title, border_style="blue"),
        build_quotes_panel(snap.quotes),
        build_headlines_panel(snap.headlines, matchers),
    ]
    if interactive:
        parts.append(build_footer(snap))
    else:
        parts.extend(Text(f"⚠ {w}", style="yellow") for w in snap.warnings)
    return Group(*parts)


# --------------------------------------------------------------------------- #
# Keyboard (runs on the main thread; terminal mode is always restored)
# --------------------------------------------------------------------------- #

@contextmanager
def raw_terminal():
    if os.name == "nt":
        yield
        return

    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def read_key(timeout: float = 0.1) -> str | None:
    if os.name == "nt":
        import msvcrt

        if not msvcrt.kbhit():
            time.sleep(timeout)
            return None
        key = msvcrt.getwch()
        if key in ("\x00", "\xe0"):  # arrow/function key prefix: swallow the 2nd code
            msvcrt.getwch()
            return None
        return key

    import select

    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return None
    # os.read, not sys.stdin.read: stdin's own buffer can hide pending keys from select().
    data = os.read(sys.stdin.fileno(), 1)
    return data.decode("utf-8", "ignore") or None


def open_selected(state: AppState) -> None:
    number, headlines = state.take_selection()
    if number is None:
        return
    if not 1 <= number <= len(headlines):
        state.flash(f"No headline #{number}")
        return
    headline = headlines[number - 1]
    if not headline.url.startswith(("http://", "https://")):
        state.flash(f"Refusing to open non-web link for #{number}")
        return
    webbrowser.open(headline.url)
    state.flash(f"Opened #{number}: {headline.title[:60]}")


def handle_key(key: str, state: AppState, stop: Event, wake: Event) -> None:
    lowered = key.lower()
    if lowered == "q" or key == "\x03":
        stop.set()
        wake.set()
    elif lowered == "r":
        state.clear_input()
        wake.set()
    elif key.isascii() and key.isdigit():
        state.add_digit(key)
    elif key in ("\x7f", "\x08"):
        state.backspace()
    elif key == "\x1b":
        state.clear_input()
    elif key in ("\r", "\n"):
        open_selected(state)


# --------------------------------------------------------------------------- #
# Background refresh
# --------------------------------------------------------------------------- #

def refresh_worker(
    state: AppState,
    symbols: list[str],
    feeds: dict[str, str],
    limit: int,
    interval: int,
    stop: Event,
    wake: Event,
) -> None:
    """Fetches off the UI thread, so the screen and keys stay responsive."""
    while not stop.is_set():
        wake.clear()
        state.begin_refresh()
        try:
            quotes, headlines, warnings = refresh_all(symbols, feeds)
            state.apply_update(quotes, headlines[:limit], warnings)
        except Exception as exc:  # the worker must never die silently
            state.fail_refresh(str(exc))
        finally:
            state.finish_refresh(interval)
        wake.wait(timeout=interval)  # returns early on 'r' or quit


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def bounded_int(minimum: int):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not an integer")
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}")
        return value
    return parse


def parse_feed(text: str) -> tuple[str, str]:
    name, _, url = text.partition("=")
    if not name or not url.startswith(("http://", "https://")):
        raise argparse.ArgumentTypeError("expected NAME=URL with an http(s) URL")
    return name, url


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Terminal market dashboard with live headlines"
    )
    parser.add_argument(
        "--symbols", nargs="+", type=str.upper, default=DEFAULT_SYMBOLS,
        help="Symbols, e.g. AAPL MSFT ^TNX GC=F",
    )
    parser.add_argument(
        "--feed", action="append", type=parse_feed, metavar="NAME=URL",
        help="RSS feed to use (repeatable). If given, replaces the default feeds.",
    )
    parser.add_argument(
        "--refresh", type=bounded_int(10), default=300,
        help="Auto-refresh interval in seconds (min 10). Default: 300",
    )
    parser.add_argument(
        "--headlines", type=bounded_int(1), default=20,
        help="Maximum headlines to display. Default: 20",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Print one snapshot and exit (also used automatically when not on a TTY)",
    )
    return parser.parse_args(argv)


def run_once(args, feeds, matchers) -> None:
    console = Console()
    with console.status("Fetching…"):
        quotes, headlines, warnings = refresh_all(args.symbols, feeds)
    state = AppState()
    state.apply_update(quotes, headlines[: args.headlines], warnings)
    console.print(build_dashboard(state.snapshot(), matchers, interactive=False))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    feeds = dict(args.feed) if args.feed else dict(DEFAULT_FEEDS)
    matchers = build_matchers(args.symbols)

    if args.once or not (sys.stdin.isatty() and sys.stdout.isatty()):
        run_once(args, feeds, matchers)
        return 0

    state = AppState()
    stop, wake = Event(), Event()
    Thread(
        target=refresh_worker,
        args=(state, args.symbols, feeds, args.headlines, args.refresh, stop, wake),
        daemon=True,
    ).start()

    try:
        with raw_terminal(), Live(
            get_renderable=lambda: build_dashboard(state.snapshot(), matchers),
            refresh_per_second=4,
            screen=True,
        ):
            while not stop.is_set():
                key = read_key(0.1)
                if key:
                    handle_key(key, state, stop, wake)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        wake.set()

    print("Exited.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
