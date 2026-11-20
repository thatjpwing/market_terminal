# Market Terminal

A lightweight, interactive terminal dashboard that displays real-time market quotes alongside a live, deduplicated RSS news feed. Built with Python and [Rich](https://github.com/Textualize/rich).

## Features

* **Live Market Quotes:** Tracks price, daily change ($ and %), intraday range visualization, 52-week range, and trading volume via Yahoo Finance.
* **Merged RSS Feed:** Aggregates multiple RSS feeds concurrently, sorts headlines chronologically, and automatically removes duplicate stories.
* **Ticker-to-Headline Matching:** Highlights headlines that mention tracked tickers or company/index aliases.
* **Interactive Controls:** Open news articles directly in your default web browser, trigger manual refreshes, or run in single-snapshot mode for scripts and pipes.
* **Non-Blocking UI:** Background thread fetching keeps keyboard navigation responsive even while data updates.

## Prerequisites

* **Python 3.10 or higher** (uses modern union type hints such as `float | None`)

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/<your-username>/<your-repo-name>.git
   cd <your-repo-name>
   ```

2. **(Optional) Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install the required third-party dependencies:**
   ```bash
   pip install feedparser yfinance rich
   ```
   *(All other imports—`argparse`, `urllib`, `webbrowser`, `concurrent.futures`, `threading`, etc.—are part of the Python standard library.)*

## Usage

Run the dashboard with default symbols and Bloomberg RSS feeds:
```bash
python market_terminal.py
```

### Interactive Keyboard Controls

| Key | Action |
| :--- | :--- |
| `<number>` + `Enter` | Open the corresponding headline URL in your default browser |
| `r` | Force an immediate data refresh |
| `Backspace` / `Esc` | Edit or clear the typed headline number |
| `q` or `Ctrl+C` | Quit the application |

*(Note: While typing a headline number, incoming headline updates are temporarily held so list numbering does not shift beneath your cursor.)*

### Command-Line Options

| Flag | Description | Default |
| :--- | :--- | :--- |
| `--symbols` | Space-separated list of ticker symbols to track | `^GSPC ^DJI ^TNX GC=F AAPL MSFT NVDA AMZN TSLA` |
| `--feed NAME=URL` | Custom RSS feed (repeatable; replaces default feeds) | Bloomberg Markets, Business, Tech, Politics, Economics |
| `--refresh` | Auto-refresh interval in seconds (minimum `10`) | `300` |
| `--headlines` | Maximum number of headlines to display | `20` |
| `--once` | Print a single static snapshot and exit (auto-enabled if not in a TTY) | `False` |

#### Examples

Track specific stocks and crypto with a 60-second refresh:
```bash
python market_terminal.py --symbols AAPL NVDA BTC-USD ETH-USD --refresh 60
```

Use custom RSS feeds:
```bash
python market_terminal.py --feed Tech="https://feeds.arstechnica.com/arstechnica/index" --feed Finance="https://finance.yahoo.com/news/rssindex"
```

Print a one-time snapshot to the terminal:
```bash
python market_terminal.py --once --headlines 10
```

## Customization

To enable headline matching for custom tickers, add company or index keywords to the `ALIASES` dictionary near the top of `market_terminal.py`:

```python
ALIASES: dict[str, list[str]] = {
    "BTC-USD": ["Bitcoin", "BTC"],
    "GOOGL": ["Alphabet", "Google"],
}
```

## Disclaimer

Market data is retrieved via `yfinance` and news headlines via public RSS feeds. Data may be delayed and is intended for informational purposes only.