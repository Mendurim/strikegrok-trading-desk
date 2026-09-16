#!/usr/bin/env python3
"""autopilot.py - the desk's runbooks executed by a clock instead of a prompt.

`desk-signal-scan` describes an hourly universe scan, live rule monitors and
four clocks. `desk-standing-approvals` describes a Tier 1 send that happens at
03:00 with nobody awake. Both are written for agents. This script is the same
two runbooks with the language model taken out of the path: it reads public
market data, evaluates a frozen rule on closed bars, checks the four clocks,
sizes on a stressed stop, writes the proposal and its RISK PASS block, and asks
the signer to send exactly one bracketed order.

    autopilot.py scan                          the universe scan, hourly
    autopilot.py monitor --rule RULE.json      one frozen rule, at bar close
    autopilot.py sweep                         expire stale fires, cancel dead entries

What it deliberately does NOT do:

  * it never holds the API wallet. Every authenticated call - reads included -
    is handed to `strike_request.py` through `sudo -u <signer>`, so
    `desk_policy.py` gates every write exactly as it does for a human trader.
  * it never decides anything a frozen rule did not decide. There is no
    forecast here, no scoring, no model. A rule fires or it does not, and a
    read that failed is `could_not_tell`, never `not_fired`.
  * it never widens a bound. Every number it sends is the tightest of: the
    rule, the signed standing approval, and what the policy ceilings allow.

The trust model is the repo's, unchanged. This script runs as the Bot user; it
can read the desk and write proposals, signals, journals and suspensions, and
it cannot sign, cannot edit `policy-state/`, and cannot lift a suspension. If
this is running as the same OS user that owns the API wallet, then nothing here
is a boundary - see SETUP.md, "Two OS users".

Exit codes:
    0  something was done, or nothing needed doing
    1  an operating error (bad rule file, unreadable desk, no market data)
    2  held: a clock failed, a bound refused, a read was blind. Nothing sent.
    3  the policy layer refused a send. An incident is filed and NOTHING is
       retried - a refusal the desk did not predict is a defect in this script's
       preflight, not an obstacle to route around.

Standard library only. No network except Strike's public Price Service.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

VERSION = "3.1.0"
USER_AGENT = f"strikegrok-autopilot/{VERSION}"

DESK = Path(os.environ.get("STRIKEGROK_DESK", "/workspace/trading-desk"))
PRICE_MAINNET = "https://api.strikefinance.org/price"
PRICE_TESTNET = "https://api-v2-testnet.strikefinance.org/price"

# The only send path. `sudo -u strike-signer` is not decoration: it is the
# whole security model. The signer owns the API wallet and policy-state; this
# process owns neither and must not be able to.
SIGNER_CMD = os.environ.get("STRIKEGROK_SIGNER", "sudo -u strike-signer")
SIGNER_PYTHON = os.environ.get("STRIKEGROK_SIGNER_PYTHON", "/usr/bin/python3")
STRIKE_REQUEST = os.environ.get(
    "STRIKEGROK_STRIKE_REQUEST", str(Path(__file__).resolve().with_name("strike_request.py")))

# A fixture directory stands in for the Price Service so a rehearsal and the
# test suite can run a whole fire without touching the network. It is read-only
# and affects public data only; the signer path is never faked by this flag.
FIXTURES = os.environ.get("STRIKEGROK_PRICE_FIXTURES") or None

DEPTH_LIMIT = 1000            # `/v2/depth` defaults to 20 levels and understates every band
BAND_BPS = 10                 # the liquidity clock's band, from desk-signal-scan
BOOK_REACH_BPS = 25           # and how far the book must reach for the read to be a measurement
DEPTH_MULTIPLE = 5            # depth within the band must be >= 5x the ticket notional
FUNDING_CLEARANCE_SECONDS = 600     # >10 minutes to the charge if the position would pay it
CATALYST_CLEARANCE_SECONDS = 3600   # no calendar entry on this market inside the next hour
PACE_SECONDS = 90             # above the policy's 60s floor, so pacing is never the refusal
EQUITY_MAX_AGE_SECONDS = 600  # a snapshot older than this cannot size anything
SIGNAL_AGE_CEILING = 3600     # mirrors desk_policy.CEILING_MAX_SIGNAL_AGE_SECONDS
BLIND_BARS_BEFORE_SUSPEND = 2 # a monitor that cannot see for two bars is not a monitor
REQUEST_SPACING_SECONDS = 0.05  # 2400 reads/minute is the budget; a scan is nowhere near it

EQUITY_KEYS = ("equity", "accountEquity", "totalEquity", "marginBalance",
               "totalMarginBalance", "accountValue")
SIZE_KEYS = ("size", "positionAmt", "quantity", "amount", "positionSize")

ZERO = Decimal(0)


class Held(Exception):
    """A clock failed, a bound refused, a read was blind. Nothing was sent, and
    that is a normal outcome the desk records rather than an error."""


class Refused(Exception):
    """The policy layer said no. This is never retried and always journalled as
    an incident: the preflight below is supposed to predict every refusal, so
    one that got through means this script is wrong about the gate."""


class Blind(Exception):
    """A read failed or came back short. Never collapses into `not_fired`."""


# ---------------------------------------------------------------------------
# Time and numbers. Prices and sizes are Decimal from the moment they are read
# to the moment they are written, because the policy layer compares the ticket
# and the request as decimals and a float round-trip is how "0.43" stops
# equalling "0.43".
# ---------------------------------------------------------------------------
def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(stamp: dt.datetime) -> str:
    return stamp.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(text: str, what: str) -> dt.datetime:
    try:
        stamp = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise Blind(f"{what}: {text!r} is not an ISO-8601 timestamp")
    if stamp.tzinfo is None:
        raise Blind(f"{what}: {text!r} carries no timezone")
    return stamp.astimezone(dt.timezone.utc)


def dec(value: Any, what: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise Blind(f"{what}: {value!r} is not a number")
    if not number.is_finite():
        raise Blind(f"{what}: {value!r} is not finite")
    return number


def plain(value: Decimal) -> str:
    """A decimal as the exchange and the PASS block both want to read it: no
    exponent, no trailing noise, and the SAME string in both places."""
    text = format(value.normalize(), "f")
    return text if text not in ("-0", "") else "0"


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    """Round toward zero onto the venue's step. Sizes always round DOWN: a size
    rounded up is a risk budget rounded up."""
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def quantize_to_tick(value: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    """Round a price onto the tick, in the direction that keeps a stop outside
    the entry rather than drifting it inward."""
    if tick <= 0:
        return value
    mode = ROUND_CEILING if up else ROUND_FLOOR
    return (value / tick).to_integral_value(rounding=mode) * tick


def short(value: Any) -> str:
    """A number for a log line: readable, and never in exponent form. A funding
    percentile carries twenty-odd digits of division behind it, and none of them
    are what the Strategist is reading the line for."""
    if not isinstance(value, Decimal):
        return str(value)
    try:
        return plain(value.quantize(Decimal("0.00000001")))
    except InvalidOperation:
        return plain(value)


def sma(values: list[Decimal], length: int) -> Decimal:
    if len(values) < length:
        raise Blind(f"sma{length}: only {len(values)} closes available")
    window = values[-length:]
    return sum(window, ZERO) / Decimal(length)


def atr(bars: list[dict], length: int) -> Decimal:
    """Average true range over `length` completed bars, as a simple mean of the
    true ranges - not Wilder's smoothing. Stated rather than assumed, because a
    stop distance computed one way and backtested the other is a different rule.
    """
    if len(bars) < length + 1:
        raise Blind(f"atr{length}: needs {length + 1} bars, have {len(bars)}")
    ranges = []
    for previous, bar in zip(bars[-length - 1:-1], bars[-length:]):
        ranges.append(max(bar["high"] - bar["low"],
                          abs(bar["high"] - previous["close"]),
                          abs(bar["low"] - previous["close"])))
    return sum(ranges, ZERO) / Decimal(length)


def percentile_rank(history: list[Decimal], value: Decimal) -> Decimal:
    """Where `value` sits inside its own history, 0-100, counting ties as half.

    This is the definition the scan and the rule monitors share. A rule whose
    threshold is "95th percentile of its own 30 days" is only reproducible if
    both ends compute the percentile the same way, so there is exactly one
    implementation of it on the desk and this is it.
    """
    if not history:
        raise Blind("percentile: no history")
    below = sum(1 for h in history if h < value)
    equal = sum(1 for h in history if h == value)
    return (Decimal(below) + Decimal(equal) / 2) / Decimal(len(history)) * Decimal(100)


# ---------------------------------------------------------------------------
# The public Price Service. No key, no account, no signature - which is why a
# scan can run on a machine that holds nothing.
# ---------------------------------------------------------------------------
_last_request_at = 0.0
_cache: dict = {}

FIXTURE_NAMES = {
    "/v2/exchangeInfo": "exchangeInfo.json",
    "/v2/premiumIndex": "premiumIndex-{symbol}.json",
    "/v2/ticker/24hr": "ticker24hr-{symbol}.json",
    "/v2/openInterest": "openInterest-{symbol}.json",
    "/v2/depth": "depth-{symbol}.json",
    "/v2/klines": "klines-{symbol}-{interval}.json",
}


def price_base(network: str) -> str:
    return PRICE_TESTNET if network == "testnet" else PRICE_MAINNET


def price_get(base: str, path: str, params: Optional[dict] = None, timeout: float = 15.0):
    """One public read. A failed read raises `Blind`; it never returns a default,
    because a default is how a missing feed becomes "the condition did not fire".
    """
    params = params or {}
    if FIXTURES:
        name = FIXTURE_NAMES.get(path)
        if not name:
            raise Blind(f"fixtures: no fixture mapping for {path}")
        file = Path(FIXTURES) / name.format(symbol=params.get("symbol", ""),
                                            interval=params.get("interval", ""))
        try:
            return json.loads(file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise Blind(f"fixtures: {file.name} is unreadable ({exc})")

    global _last_request_at
    wait = REQUEST_SPACING_SECONDS - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()

    url = f"{base.rstrip('/')}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        raise Blind(f"price service: {path} {params.get('symbol', '')} failed ({exc})")


def exchange_info(base: str) -> dict:
    if "exchangeInfo" not in _cache:
        _cache["exchangeInfo"] = price_get(base, "/v2/exchangeInfo")
    return _cache["exchangeInfo"]


def market_config(base: str, symbol: str) -> dict:
    symbols = (exchange_info(base) or {}).get("symbols")
    if not isinstance(symbols, list):
        raise Blind("exchangeInfo returned an unexpected shape")
    config = next((s for s in symbols if s.get("symbol") == symbol), None)
    if config is None:
        raise Blind(f"{symbol} is not a Strike market")
    return config


def trading_markets(base: str) -> list[str]:
    symbols = (exchange_info(base) or {}).get("symbols") or []
    return sorted(s.get("symbol") for s in symbols
                  if isinstance(s, dict) and s.get("status") == "trading" and s.get("symbol"))


def filter_value(config: dict, filter_type: str, key: str):
    for entry in config.get("filters") or []:
        if entry.get("filterType") == filter_type:
            return entry.get(key)
    return None


def constraints(base: str, symbol: str) -> dict:
    """Everything the venue will reject an order for, read from exchangeInfo.

    A market entry is bounded by MARKET_LOT_SIZE, not LOT_SIZE: Strike publishes
    a smaller maximum for market orders than for resting ones, and sizing
    against the wrong filter is a rejection at best. Where the market filter is
    absent the lot filter is the only bound there is.
    """
    config = market_config(base, symbol)
    if config.get("status") != "trading":
        raise Blind(f"{symbol} is not trading (status {config.get('status')!r})")
    tick = filter_value(config, "PRICE_FILTER", "tickSize")
    market_step = filter_value(config, "MARKET_LOT_SIZE", "stepSize")
    step = market_step if market_step is not None else filter_value(config, "LOT_SIZE", "stepSize")
    which = "MARKET_LOT_SIZE" if market_step is not None else "LOT_SIZE"
    if tick is None or step is None:
        raise Blind(f"{symbol}: exchangeInfo carries no tickSize/stepSize")
    order_types = [str(t).upper() for t in (config.get("orderType") or [])]
    if not order_types:
        raise Blind(f"{symbol}: exchangeInfo lists no orderType, so the protective leg's type "
                    "cannot be checked against the venue")
    return {
        "tick": dec(tick, "tickSize"),
        "step": dec(step, "stepSize"),
        "lot_filter": which,
        "min_qty": dec(filter_value(config, which, "minQty") or 0, "minQty"),
        "max_qty": dec(filter_value(config, which, "maxQty") or 0, "maxQty"),
        "min_price": dec(filter_value(config, "PRICE_FILTER", "minPrice") or 0, "minPrice"),
        "max_price": dec(filter_value(config, "PRICE_FILTER", "maxPrice") or 0, "maxPrice"),
        "min_notional": dec(filter_value(config, "MIN_NOTIONAL", "notional") or 0, "minNotional"),
        "order_types": order_types,
    }


def klines(base: str, symbol: str, interval: str, limit: int) -> list[dict]:
    """Candles, Binance-shaped: [openTime, open, high, low, close, volume, closeTime, ...].

    Returns bars oldest-first with Decimal prices and a UTC close time. A dict
    shape is accepted too, because a price service that changes shape should
    stop the desk, not quietly feed it zeros.
    """
    raw = price_get(base, "/v2/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    rows = raw.get("klines") if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not rows:
        raise Blind(f"klines {symbol} {interval}: empty response")
    bars = []
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) >= 7:
            open_ms, o, h, l, c, _v, close_ms = row[0], row[1], row[2], row[3], row[4], row[5], row[6]
        elif isinstance(row, dict):
            open_ms = row.get("openTime", row.get("t"))
            close_ms = row.get("closeTime", row.get("T"))
            o, h, l, c = row.get("open"), row.get("high"), row.get("low"), row.get("close")
        else:
            raise Blind(f"klines {symbol}: unexpected candle shape")
        bars.append({
            "open_at": dt.datetime.fromtimestamp(int(open_ms) / 1000, tz=dt.timezone.utc),
            "close_at": dt.datetime.fromtimestamp(int(close_ms) / 1000, tz=dt.timezone.utc),
            "open": dec(o, "kline open"), "high": dec(h, "kline high"),
            "low": dec(l, "kline low"), "close": dec(c, "kline close"),
        })
    bars.sort(key=lambda b: b["open_at"])
    return bars


def closed_bars(bars: list[dict], at: dt.datetime) -> list[dict]:
    """Only bars that have finished. A rule that reads the bar it is standing in
    is a rule that was never backtested: the last print moves after the read."""
    return [bar for bar in bars if bar["close_at"] <= at]


def book(base: str, symbol: str) -> dict:
    """The resting book, with the whole depth asked for rather than the default
    20 levels. A one-sided or empty book is `unavailable`, never a zero."""
    raw = price_get(base, "/v2/depth", {"symbol": symbol, "limit": DEPTH_LIMIT})
    sides = {}
    for side in ("bids", "asks"):
        levels = raw.get(side)
        if not isinstance(levels, list) or not levels:
            raise Blind(f"{symbol}: the book has no resting {side}; mid, spread and depth "
                        "are unavailable")
        sides[side] = [(dec(entry[0], "book price"), dec(entry[1], "book size"))
                       for entry in levels
                       if isinstance(entry, (list, tuple)) and len(entry) >= 2]
        if not sides[side]:
            raise Blind(f"{symbol}: malformed {side} levels")
    best_bid, best_ask = sides["bids"][0][0], sides["asks"][0][0]
    if best_bid <= 0 or best_ask <= 0 or best_bid > best_ask:
        raise Blind(f"{symbol}: the top of the book is crossed or invalid")
    mid = (best_bid + best_ask) / 2
    return {"bids": sides["bids"], "asks": sides["asks"], "best_bid": best_bid,
            "best_ask": best_ask, "mid": mid,
            "spread_bps": (best_ask - best_bid) / mid * Decimal(10_000),
            "capped": max(len(sides["bids"]), len(sides["asks"])) >= DEPTH_LIMIT}


def depth_within(levels: list[tuple[Decimal, Decimal]], mid: Decimal, band_bps: int) -> Decimal:
    limit = Decimal(band_bps) / Decimal(10_000)
    return sum((size for price, size in levels if abs(price - mid) / mid <= limit), ZERO)


def reach_bps(levels: list[tuple[Decimal, Decimal]], mid: Decimal) -> Decimal:
    return max(abs(price - mid) for price, _ in levels) / mid * Decimal(10_000)


def premium_index(base: str, symbol: str) -> dict:
    raw = price_get(base, "/v2/premiumIndex", {"symbol": symbol})
    if isinstance(raw, list):
        raw = next((r for r in raw if r.get("symbol") == symbol), None) or {}
    return {"mark": dec(raw.get("markPrice"), f"{symbol} markPrice"),
            "index": dec(raw.get("indexPrice"), f"{symbol} indexPrice"),
            "funding": dec(raw.get("fundingRate"), f"{symbol} fundingRate"),
            "next_funding_at": (
                dt.datetime.fromtimestamp(int(raw["nextFundingTime"]) / 1000, tz=dt.timezone.utc)
                if raw.get("nextFundingTime") else None)}


def ticker_24h(base: str, symbol: str) -> dict:
    raw = price_get(base, "/v2/ticker/24hr", {"symbol": symbol})
    if isinstance(raw, list):
        raw = next((r for r in raw if r.get("symbol") == symbol), None) or {}
    return {"last": dec(raw.get("lastPrice"), "lastPrice"),
            "open": dec(raw.get("openPrice"), "openPrice"),
            "high": dec(raw.get("highPrice"), "highPrice"),
            "low": dec(raw.get("lowPrice"), "lowPrice"),
            "change_pct": dec(raw.get("priceChangePercent"), "priceChangePercent"),
            "quote_volume": dec(raw.get("quoteVolume", 0), "quoteVolume")}


def open_interest(base: str, symbol: str) -> Decimal:
    raw = price_get(base, "/v2/openInterest", {"symbol": symbol})
    if isinstance(raw, list):
        raw = next((r for r in raw if r.get("symbol") == symbol), None) or {}
    return dec(raw.get("openInterest"), f"{symbol} openInterest")


# ---------------------------------------------------------------------------
# The desk on disk. Everything this script writes is append-only or additive:
# signals, journals, proposals, incidents and suspensions. It never edits a
# record it wrote earlier, because a desk that can rewrite its own history
# cannot be reviewed.
# ---------------------------------------------------------------------------
class Desk:
    def __init__(self, root: Path):
        self.root = Path(root)

    # -- paths -------------------------------------------------------------
    @property
    def signals_dir(self) -> Path: return self.root / "signals"
    @property
    def proposals_dir(self) -> Path: return self.root / "proposals"
    @property
    def journal_dir(self) -> Path: return self.root / "journal"
    @property
    def incidents_dir(self) -> Path: return self.root / "journal" / "incidents" / "open"
    @property
    def data_dir(self) -> Path: return self.root / "data"
    @property
    def state_dir(self) -> Path: return self.root / "watch" / "autopilot"
    @property
    def equity_file(self) -> Path: return self.root / "desk" / "equity.json"
    @property
    def register_file(self) -> Path: return self.root / "desk" / "standing-approvals.json"
    @property
    def suspend_file(self) -> Path: return self.root / "desk" / "standing-approvals.suspended.json"
    @property
    def blackouts_file(self) -> Path: return self.root / "desk" / "blackouts.json"

    def watch_dir(self, rule_name: str) -> Path:
        return self.root / "watch" / f"rule-{rule_name}"

    # -- append-only writers ----------------------------------------------
    @staticmethod
    def _append(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text if text.endswith("\n") else text + "\n")

    def signal(self, block: str) -> None:
        self._append(self.signals_dir / f"{now().date().isoformat()}.md", block + "\n")

    def journal(self, line: str) -> None:
        self._append(self.journal_dir / f"{now().date().isoformat()}.md", line)

    def watch_log(self, rule_name: str, line: str) -> None:
        self._append(self.watch_dir(rule_name) / "log", f"{iso(now())} {line}")

    def incident(self, slug: str, body: str) -> Path:
        """An incident blocks every opening order until the user clears it with a
        re-signed register. That is the point: the desk stops rather than
        retrying into a gate it does not understand."""
        self.incidents_dir.mkdir(parents=True, exist_ok=True)
        path = self.incidents_dir / f"{now().strftime('%Y%m%dT%H%M%SZ')}-{slug}.md"
        path.write_text(f"# incident {slug}\nopened: {iso(now())}\nby: autopilot {VERSION}\n\n{body}\n")
        self.journal(f"{iso(now())} INCIDENT {path.name} {slug}")
        return path

    def suspend_sa(self, sa_id: str, reason: str) -> None:
        """A Bot may only ever REMOVE permission. The policy layer ingests this
        one way and a re-signed register is the only thing that lifts it."""
        entries = []
        if self.suspend_file.exists():
            try:
                loaded = json.loads(self.suspend_file.read_text())
                entries = loaded if isinstance(loaded, list) else []
            except (OSError, json.JSONDecodeError):
                entries = []
        if any(isinstance(e, dict) and e.get("id") == sa_id and e.get("reason") == reason
               for e in entries):
            return
        entries.append({"id": sa_id, "reason": reason, "at": iso(now()), "by": "autopilot"})
        self.suspend_file.parent.mkdir(parents=True, exist_ok=True)
        self.suspend_file.write_text(json.dumps(entries, indent=1) + "\n")
        self.journal(f"{iso(now())} SUSPEND {sa_id} {reason}")

    # -- the script's own state (pace, fired bars, blind counters) ---------
    def state(self) -> dict:
        path = self.state_dir / "state.json"
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def save_state(self, data: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "state.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        tmp.replace(path)

    # -- append-only metric history ---------------------------------------
    def history(self, kind: str, symbol: str) -> list[dict]:
        path = self.data_dir / kind / f"{symbol}.csv"
        if not path.exists():
            return []
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                return [row for row in csv.DictReader(handle) if row.get("at")]
        except OSError as exc:
            raise Blind(f"history: {path} is unreadable ({exc})")

    def append_history(self, kind: str, symbol: str, row: dict) -> None:
        path = self.data_dir / kind / f"{symbol}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            if new:
                writer.writeheader()
            writer.writerow(row)


def recent(rows: list[dict], field: str, days: float, at: dt.datetime) -> list[Decimal]:
    """The numeric column of every sample inside the window, oldest first."""
    cutoff = at - dt.timedelta(days=days)
    out = []
    for row in rows:
        try:
            stamp = parse_iso(row["at"], "history at")
        except Blind:
            continue
        if stamp < cutoff or row.get(field) in (None, ""):
            continue
        try:
            out.append(Decimal(str(row[field])))
        except InvalidOperation:
            continue
    return out


def median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if not ordered:
        raise Blind("median: no samples")
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


# ---------------------------------------------------------------------------
# The signer. This process asks; it never signs. Reads go through the same door
# as writes so the account read that opens the trading day is performed by the
# user that owns policy-state, which is the user whose start-of-day equity the
# daily-loss stop is measured against.
# ---------------------------------------------------------------------------
class Signer:
    def __init__(self, network: str = "testnet", command: Optional[str] = None,
                 timeout: float = 60.0):
        self.network = network
        self.command = shlex.split(command if command is not None else SIGNER_CMD)
        self.timeout = timeout
        self.sent: list[dict] = []

    def _argv(self, method: str, path: str, query: Iterable[str], body: Optional[str]) -> list[str]:
        argv = list(self.command) + [SIGNER_PYTHON, STRIKE_REQUEST, method.upper(), path]
        for item in query:
            argv += ["--query", item]
        if self.network == "testnet":
            argv.append("--testnet")
        if body is not None:
            argv += ["--body-file", "-"]
        return argv

    def call(self, method: str, path: str, *, query: Iterable[str] = (),
             body: Optional[dict] = None) -> Any:
        """Run one signed request. A policy refusal (exit 3) becomes `Refused`
        carrying the layer's own words, which are the authoritative name for
        whatever this script got wrong."""
        payload = json.dumps(body, separators=(",", ":")) if body is not None else None
        argv = self._argv(method, path, query, payload)
        try:
            done = subprocess.run(argv, input=payload, capture_output=True, text=True,
                                  timeout=self.timeout)
        except FileNotFoundError as exc:
            raise Held(f"signer: cannot run {argv[0]} ({exc})")
        except subprocess.TimeoutExpired:
            # This path carries reads and Tier 0 cancels. A read that timed out
            # tells the desk nothing, so it holds; a cancel that timed out may
            # have landed, and the next sweep looks it up rather than assuming.
            # The entry path does not come through here - see send_entry, where
            # a timeout is an UNKNOWN result and files an incident.
            raise Held(f"signer: {method} {path} did not answer within {self.timeout:.0f}s")
        self.sent.append({"argv": argv, "code": done.returncode, "stdout": done.stdout,
                          "stderr": done.stderr})
        if done.returncode == 3:
            raise Refused((done.stderr or "").strip() or "policy refused with no reason given")
        if done.returncode != 0:
            raise Held(f"signer: {method} {path} exited {done.returncode}: "
                       f"{(done.stderr or done.stdout or '').strip()[:400]}")
        try:
            return json.loads(done.stdout) if done.stdout.strip() else {}
        except json.JSONDecodeError:
            raise Held(f"signer: {method} {path} did not return JSON")

    # -- typed reads -------------------------------------------------------
    @staticmethod
    def _dig(payload: Any, keys: tuple) -> Any:
        for layer in (payload, (payload or {}).get("data") if isinstance(payload, dict) else None,
                      (payload or {}).get("result") if isinstance(payload, dict) else None):
            if isinstance(layer, dict):
                for key in keys:
                    if layer.get(key) not in (None, ""):
                        return layer[key]
        return None

    def equity(self) -> Decimal:
        value = self._dig(self.call("GET", "/v2/account"), EQUITY_KEYS)
        if value is None:
            raise Held("account: no equity field in GET /v2/account")
        return dec(value, "equity")

    def positions(self) -> list[dict]:
        payload = self.call("GET", "/v2/positions")
        rows = payload if isinstance(payload, list) else self._dig(payload, ("positions", "data", "result"))
        out = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            size = next((row[k] for k in SIZE_KEYS if row.get(k) not in (None, "")), None)
            if size is None or dec(size, "position size") == 0:
                continue
            out.append({"symbol": row.get("symbol"), "size": dec(size, "position size")})
        return out

    def open_orders(self) -> list[dict]:
        payload = self.call("GET", "/v2/openOrders")
        rows = payload if isinstance(payload, list) else self._dig(payload, ("orders", "data", "result"))
        out = []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict):
                out.append({"symbol": row.get("symbol"),
                            "client_order_id": row.get("client_order_id") or row.get("clientOrderId"),
                            "order_id": row.get("order_id") or row.get("orderId"),
                            "reduce_only": bool(row.get("reduce_only", row.get("reduceOnly")))})
        return out

    def find_order(self, coid: str) -> Optional[dict]:
        try:
            payload = self.call("GET", "/v2/order", query=[f"client_order_id={coid}"])
        except Held:
            return None
        if isinstance(payload, list):
            return payload[0] if payload else None
        return payload or None


# ---------------------------------------------------------------------------
# The frozen rule. A rule file is data, not code: every key is known, every
# unknown key is an error, and nothing in it is interpreted loosely. A rule the
# script half-understands is a rule nobody backtested.
# ---------------------------------------------------------------------------
RULE_REQUIRED = ("name", "version", "markets", "timeframe_seconds", "klines_interval",
                 "condition", "direction", "entry", "stop_atr_multiple", "slippage_bps",
                 "fee_bps_round_turn", "max_spread_bps")
RULE_OPTIONAL = ("take_profit_atr_multiple", "funding_position", "atr_period", "sma_period",
                 "min_funding_samples", "funding_history_days", "tp_leg", "notes",
                 "sl_order_type", "tp_order_type")
CONDITION_KEYS = ("funding_pct30d", "price_vs_sma20", "range_expansion_atr20")
NAME_RE = re.compile(r"^[\w.\-]+$")


class Rule:
    """One frozen, hashed, backtested rule, as the monitor reads it."""

    def __init__(self, data: dict, path: Optional[Path] = None):
        self.path = path
        unknown = set(data) - set(RULE_REQUIRED) - set(RULE_OPTIONAL)
        if unknown:
            raise ValueError(f"rule: unknown keys {sorted(unknown)}; a key this script does "
                             "not understand is a rule it cannot evaluate")
        missing = [k for k in RULE_REQUIRED if k not in data]
        if missing:
            raise ValueError(f"rule: missing keys {missing}")

        self.name = str(data["name"])
        if not NAME_RE.match(self.name):
            raise ValueError(f"rule: name {self.name!r} must match {NAME_RE.pattern}; the policy "
                             "layer parses 'signal: <rule>@<version>' with the same character set")
        self.version = int(data["version"])
        self.markets = [str(m) for m in data["markets"]]
        if not self.markets:
            raise ValueError("rule: markets is empty")
        self.timeframe_seconds = int(data["timeframe_seconds"])
        if not 60 <= self.timeframe_seconds <= 86_400:
            raise ValueError("rule: timeframe_seconds must be between one minute and one day")
        self.interval = str(data["klines_interval"])
        self.direction = str(data["direction"]).lower()
        if self.direction not in ("fade", "follow", "long", "short"):
            raise ValueError("rule: direction must be fade, follow, long or short")
        self.entry = str(data["entry"]).lower()
        if self.entry != "market":
            raise ValueError("rule: entry must be 'market'. A resting limit entry fills when the "
                             "signal may no longer be true, and this script has no bar-by-bar "
                             "supervisor for it; that is a Tier 2 ticket a human prices.")
        self.stop_atr_multiple = dec(data["stop_atr_multiple"], "stop_atr_multiple")
        self.tp_atr_multiple = (dec(data["take_profit_atr_multiple"], "take_profit_atr_multiple")
                                if data.get("take_profit_atr_multiple") is not None else None)
        if self.stop_atr_multiple <= 0:
            raise ValueError("rule: stop_atr_multiple must be positive")
        self.slippage_bps = dec(data["slippage_bps"], "slippage_bps")
        self.fee_bps_round_turn = dec(data["fee_bps_round_turn"], "fee_bps_round_turn")
        self.max_spread_bps = dec(data["max_spread_bps"], "max_spread_bps")
        if min(self.slippage_bps, self.fee_bps_round_turn, self.max_spread_bps) < 0:
            raise ValueError("rule: slippage, fees and spread bounds cannot be negative")
        self.funding_position = str(data.get("funding_position", "either")).lower()
        if self.funding_position not in ("pays", "earns", "either"):
            raise ValueError("rule: funding_position must be pays, earns or either")
        self.atr_period = int(data.get("atr_period", 20))
        self.sma_period = int(data.get("sma_period", 20))
        self.min_funding_samples = int(data.get("min_funding_samples", 240))
        self.funding_history_days = float(data.get("funding_history_days", 30))
        self.tp_leg = bool(data.get("tp_leg", True))
        # Strike lists STOP, STOP_MARKET, TAKE_PROFIT and TAKE_PROFIT_MARKET. On the
        # Binance scheme this API follows, the plain forms are the *limit* ones:
        # they rest at a price after triggering and can fail to fill on the move
        # that triggered them. The desk's stop must fill, so the default is the
        # market-on-trigger form, and whichever is chosen is checked against the
        # market's own orderType list before anything is sent.
        self.sl_order_type = str(data.get("sl_order_type", "stop_market")).lower()
        self.tp_order_type = str(data.get("tp_order_type", "take_profit_market")).lower()
        for name, value in (("sl_order_type", self.sl_order_type),
                            ("tp_order_type", self.tp_order_type)):
            if not NAME_RE.match(value):
                raise ValueError(f"rule: {name} {value!r} is not an order type")

        condition = data["condition"]
        if not isinstance(condition, dict) or not condition:
            raise ValueError("rule: condition must be a non-empty object")
        unknown = set(condition) - set(CONDITION_KEYS)
        if unknown:
            raise ValueError(f"rule: unknown condition keys {sorted(unknown)}")
        self.condition = condition
        band = condition.get("funding_pct30d")
        if band is not None:
            if (not isinstance(band, (list, tuple)) or len(band) != 2
                    or not 0 <= float(band[0]) <= float(band[1]) <= 100):
                raise ValueError("rule: funding_pct30d must be [low, high] inside 0..100")
        if condition.get("price_vs_sma20") not in (None, "above", "below"):
            raise ValueError("rule: price_vs_sma20 must be 'above' or 'below'")
        expansion = condition.get("range_expansion_atr20")
        if expansion is not None and (not isinstance(expansion, (list, tuple)) or len(expansion) != 2):
            raise ValueError("rule: range_expansion_atr20 must be [low, high]")

    @property
    def signal(self) -> str:
        """Exactly what the policy layer will compare against the register."""
        return f"{self.name}@{self.version}"

    @classmethod
    def load(cls, path: Path) -> "Rule":
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"rule: {path} is unreadable ({exc})")
        return cls(data, Path(path))

    # -- evaluation --------------------------------------------------------
    def evaluate(self, bars: list[dict], funding_now: Decimal,
                 funding_history: list[Decimal]) -> dict:
        """Evaluate on CLOSED bars. Returns a reading; `fired` is only ever True
        when every stated condition is true and every input was measured."""
        closes = [bar["close"] for bar in bars]
        reading: dict = {"bar_close_at": bars[-1]["close_at"], "close": closes[-1]}
        fired = True

        band = self.condition.get("funding_pct30d")
        if band is not None:
            if len(funding_history) < self.min_funding_samples:
                raise Blind(f"funding history has {len(funding_history)} samples, "
                            f"needs {self.min_funding_samples}")
            rank = percentile_rank(funding_history, funding_now)
            reading["funding"] = funding_now
            reading["funding_pct30d"] = rank
            reading["funding_band"] = band
            fired = fired and (Decimal(str(band[0])) <= rank <= Decimal(str(band[1])))

        want = self.condition.get("price_vs_sma20")
        if want is not None:
            mean = sma(closes, self.sma_period)
            reading[f"sma{self.sma_period}"] = mean
            fired = fired and (closes[-1] > mean if want == "above" else closes[-1] < mean)

        expansion = self.condition.get("range_expansion_atr20")
        if expansion is not None:
            average = atr(bars, self.atr_period)
            if average <= 0:
                raise Blind("atr is zero; range expansion cannot be measured")
            ratio = (bars[-1]["high"] - bars[-1]["low"]) / average
            reading["range_expansion"] = ratio
            fired = fired and (Decimal(str(expansion[0])) <= ratio <= Decimal(str(expansion[1])))

        reading["fired"] = fired
        return reading

    def side(self, reading: dict) -> str:
        """`buy` or `sell`. A fade takes the other side of whoever is paying."""
        if self.direction in ("long", "short"):
            return "buy" if self.direction == "long" else "sell"
        band = self.condition.get("funding_pct30d")
        if band is None:
            raise ValueError("rule: fade/follow needs funding_pct30d to know which side is crowded")
        crowded_long = (Decimal(str(band[0])) + Decimal(str(band[1]))) / 2 >= 50
        if self.direction == "fade":
            return "sell" if crowded_long else "buy"
        return "buy" if crowded_long else "sell"


# ---------------------------------------------------------------------------
# The signed register, read for its bounds. The signature is verified by the
# policy layer at send time and by `desk_policy.py verify` on demand; this
# script reads the file only so its preflight can refuse for the same reason
# the gate would, before anything is sent.
# ---------------------------------------------------------------------------
def standing_approval(desk: Desk, rule: Rule, symbol: str) -> dict:
    try:
        register = json.loads(desk.register_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise Held(f"register: {desk.register_file} is unreadable ({exc}); Tier 1 needs a signed "
                   "standing-approval register")
    candidates = [a for a in (register.get("approvals") or [])
                  if isinstance(a, dict) and a.get("rule") == rule.name
                  and int(a.get("rule_version", -1)) == rule.version
                  and symbol in (a.get("markets") or [])]
    if not candidates:
        raise Held(f"register: no standing approval covers {rule.signal} on {symbol}")
    if len(candidates) > 1:
        raise Held(f"register: {len(candidates)} approvals cover {rule.signal} on {symbol}; "
                   "an ambiguous register is not an approval")
    sa = candidates[0]
    today = now().date()
    if dt.date.fromisoformat(str(sa["expires"])) < today:
        raise Held(f"register: {sa['id']} expired {sa['expires']}")
    if dt.date.fromisoformat(str(sa["granted"])) > today:
        raise Held(f"register: {sa['id']} is not granted until {sa['granted']}")
    hours = sa.get("hours_utc")
    if hours:
        low, high = int(hours[0]), int(hours[1])
        hour = now().hour
        inside = (low <= hour < high) if low < high else (hour >= low or hour < high)
        if low == high or not inside:
            raise Held(f"register: {hour:02d}:00 UTC is outside {sa['id']}'s hours {hours}")
    return sa


def suspended_ids(desk: Desk) -> set:
    """What this side of the desk can see of the suspension list. The policy
    layer's copy is authoritative and includes suspensions this script cannot
    read; this is only here so a suspended SA is not evaluated at all."""
    if not desk.suspend_file.exists():
        return set()
    try:
        entries = json.loads(desk.suspend_file.read_text())
    except (OSError, json.JSONDecodeError):
        # An unreadable suspension file is not permission to trade.
        raise Held("suspensions: standing-approvals.suspended.json is unreadable")
    return {e.get("id") for e in entries if isinstance(e, dict) and e.get("id")}


def rules_hash_ok(desk: Desk, sa: dict) -> bool:
    """The register names the sha256 of the rule's frozen RULES.md. A mismatch
    would suspend the SA at the gate, so the monitor holds instead of spending
    a standing approval on a rule that changed underneath it."""
    path = desk.root / "strategies" / str(sa.get("rule")) / "RULES.md"
    if not path.exists():
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == sa.get("rules_sha256")


# ---------------------------------------------------------------------------
# Equity. Sizing against a number nobody checked is how a 0.25% risk becomes a
# 2% one, so there are exactly two sources and both are the signer's.
# ---------------------------------------------------------------------------
def equity_now(desk: Desk, signer: Optional[Signer]) -> tuple[Decimal, str]:
    """The live account read first, because the policy layer will compare the
    PASS block against its own read and refuse a 2% drift - and because that
    read is what captures the day's opening equity in the signer's state.

    The 5-minute snapshot is the fallback, and it is a fallback with an expiry:
    a snapshot older than `EQUITY_MAX_AGE_SECONDS` means the signer's cron has
    stopped, and a desk that cannot see the account does not open a position.
    """
    if signer is not None:
        try:
            return signer.equity(), "account read"
        except (Held, Blind) as exc:
            reason = str(exc)
    else:
        reason = "no signer"
    try:
        snapshot = json.loads(desk.equity_file.read_text())
        stamp = parse_iso(snapshot["at"], "equity.json at")
        value = dec(snapshot["equity"], "equity.json equity")
    except (OSError, json.JSONDecodeError, KeyError, Blind) as exc:
        raise Held(f"equity: the account read failed ({reason}) and the snapshot is unusable ({exc})")
    age = (now() - stamp).total_seconds()
    if age > EQUITY_MAX_AGE_SECONDS:
        raise Held(f"equity: the account read failed ({reason}) and the snapshot is {age:.0f}s old, "
                   f"past {EQUITY_MAX_AGE_SECONDS}s. The signer's snapshot cron is not running.")
    if value <= 0:
        raise Held("equity: the snapshot is zero or negative")
    return value, f"snapshot {age:.0f}s old"


# ---------------------------------------------------------------------------
# The four clocks. Three of them live here; the fourth - the rule clock - is
# the monitor itself. Each returns its readings whether it passes or fails,
# because a ticket carries all four and a held signal is worth reading.
# ---------------------------------------------------------------------------
def liquidity_clock(desk: Desk, symbol: str, side: str, notional: Decimal,
                    rule: Rule, book_now: dict) -> dict:
    """depth within 10 bps on the side the entry takes >= 5x the ticket, spread
    no wider than its own 7-day median, and a book that actually reaches 25 bps.

    A market order that takes the bid needs resting BIDS; measuring the other
    side would pass a book that cannot fill this ticket at all.
    """
    levels = book_now["bids"] if side == "sell" else book_now["asks"]
    depth_base = depth_within(levels, book_now["mid"], BAND_BPS)
    depth_usd = depth_base * book_now["mid"]
    reach = reach_bps(levels, book_now["mid"])
    spread = book_now["spread_bps"]

    history = recent(desk.history("depth", symbol), "spread_bps", 7, now())
    spread_median = median(history) if len(history) >= 24 else None

    reasons = []
    if depth_usd < notional * Decimal(DEPTH_MULTIPLE):
        reasons.append(f"depth within {BAND_BPS}bps ${depth_usd:,.0f} < {DEPTH_MULTIPLE}x "
                       f"notional ${notional * Decimal(DEPTH_MULTIPLE):,.0f}")
    if spread > rule.max_spread_bps:
        reasons.append(f"spread {spread:.1f}bps > rule max {rule.max_spread_bps}bps")
    if spread_median is not None and spread > spread_median:
        reasons.append(f"spread {spread:.1f}bps > 7d median {spread_median:.1f}bps")
    if reach < BOOK_REACH_BPS:
        reasons.append(f"book reaches only {reach:.1f}bps, short of {BOOK_REACH_BPS}bps")
    return {"pass": not reasons, "why": "; ".join(reasons) or "depth, spread and reach all clear",
            "depth_usd": depth_usd, "spread_bps": spread, "reach_bps": reach,
            "spread_median_bps": spread_median, "side_taken": "bids" if side == "sell" else "asks"}


def funding_clock(premium: dict, side: str, rule: Rule) -> dict:
    """Whether the position pays the next charge, and whether it is close."""
    rate = premium["funding"]
    pays = (rate > 0 and side == "buy") or (rate < 0 and side == "sell")
    seconds = None
    if premium["next_funding_at"] is not None:
        seconds = (premium["next_funding_at"] - now()).total_seconds()
    if not pays:
        return {"pass": True, "why": f"position receives funding at {rate}", "pays": False,
                "seconds_to_charge": seconds}
    if rule.funding_position == "earns":
        return {"pass": False, "why": f"the rule is written to earn funding but a {side} pays "
                f"{rate} here", "pays": True, "seconds_to_charge": seconds}
    if rule.funding_position == "pays":
        return {"pass": True, "why": f"the rule pays funding by design ({rate})", "pays": True,
                "seconds_to_charge": seconds}
    if seconds is None:
        return {"pass": False, "why": "the position pays funding and nextFundingTime is unknown",
                "pays": True, "seconds_to_charge": None}
    if seconds < FUNDING_CLEARANCE_SECONDS:
        return {"pass": False, "why": f"the position pays funding in {seconds:.0f}s, inside the "
                f"{FUNDING_CLEARANCE_SECONDS}s clearance", "pays": True, "seconds_to_charge": seconds}
    return {"pass": True, "why": f"pays funding, {seconds:.0f}s away", "pays": True,
            "seconds_to_charge": seconds}


def catalyst_clock(desk: Desk, symbol: str) -> dict:
    """`desk/blackouts.json` is the Research Analyst's calendar with teeth - the
    same file the policy layer ingests one way. If it is in force, or an entry
    opens inside the hour, the signal is `soon`, not `now`."""
    if not desk.blackouts_file.exists():
        return {"pass": True, "why": "no blackout file; no calendar entry known"}
    try:
        entries = json.loads(desk.blackouts_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise Held(f"catalyst: blackouts.json is unreadable ({exc})")
    at = now()
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("symbol") not in ("*", symbol):
            continue
        try:
            start = parse_iso(entry["start"], "blackout start")
            end = parse_iso(entry["end"], "blackout end")
        except (KeyError, Blind):
            raise Held(f"catalyst: a blackout entry on {entry.get('symbol')} has no usable window")
        if start <= at <= end:
            return {"pass": False, "why": f"{entry.get('reason') or 'scheduled'} window in force "
                    f"until {iso(end)}"}
        if at < start <= at + dt.timedelta(seconds=CATALYST_CLEARANCE_SECONDS):
            return {"pass": False, "why": f"{entry.get('reason') or 'scheduled'} window opens "
                    f"{iso(start)}, inside the hour"}
    return {"pass": True, "why": "no calendar entry inside the hour"}


# ---------------------------------------------------------------------------
# Sizing, on a stressed stop. The arithmetic is the Risk Manager's, written out
# so the ticket can show it: the stop the desk would actually get is worse than
# the stop it asked for, and the fees are paid on both legs.
# ---------------------------------------------------------------------------
def size_ticket(*, equity: Decimal, risk_fraction: Decimal, entry: Decimal, side: str,
                atr_value: Decimal, rule: Rule, tick: Decimal, step: Decimal) -> dict:
    if entry <= 0 or atr_value <= 0:
        raise Held("sizing: entry price and ATR must both be positive")
    risk_usd = equity * risk_fraction
    distance = atr_value * rule.stop_atr_multiple
    short = side == "sell"
    raw_stop = entry + distance if short else entry - distance
    # A stop rounds AWAY from the entry: onto the tick in the direction that
    # cannot walk it inside the price it is protecting.
    stop = quantize_to_tick(raw_stop, tick, up=short)
    if short and stop <= entry or not short and stop >= entry:
        raise Held("sizing: the stop rounded onto the wrong side of the entry; the tick is wider "
                   "than the stop distance")
    slip = entry * rule.slippage_bps / Decimal(10_000)
    stressed_stop = stop + slip if short else stop - slip
    fee_rate = rule.fee_bps_round_turn / 2 / Decimal(10_000)
    fee_per_unit = (entry + stressed_stop) * fee_rate
    per_unit = abs(entry - stressed_stop) + fee_per_unit
    if per_unit <= 0:
        raise Held("sizing: the stressed loss per unit is not positive")
    size = quantize_down(risk_usd / per_unit, step)
    if size <= 0:
        raise Held(f"sizing: risk ${risk_usd:,.2f} buys less than one step ({step}) at a stressed "
                   f"loss of ${per_unit:,.4f} per unit")
    take_profit = None
    if rule.tp_atr_multiple is not None:
        raw_tp = entry - atr_value * rule.tp_atr_multiple if short else entry + atr_value * rule.tp_atr_multiple
        take_profit = quantize_to_tick(raw_tp, tick, up=not short)
        if take_profit <= 0:
            raise Held("sizing: the take-profit rounded to zero or below")
    nominal = abs(entry - stop) * size
    return {"risk_usd": risk_usd, "size": size, "stop": stop, "stressed_stop": stressed_stop,
            "per_unit": per_unit, "slip": slip, "fee_per_unit": fee_per_unit,
            "notional": size * entry, "take_profit": take_profit, "nominal_loss": nominal,
            "stop_distance": distance}


def venue_limits(limits: dict, sizing: dict, rule: Rule, entry: Decimal) -> list[str]:
    """What the venue itself would reject. Checked before the send, because a
    rejection burns a client order id and tells the desk nothing it could not
    have read out of exchangeInfo first."""
    reasons = []
    size, stop = sizing["size"], sizing["stop"]
    if limits["min_qty"] > 0 and size < limits["min_qty"]:
        reasons.append(f"size {plain(size)} is under {limits['lot_filter']} minQty "
                       f"{plain(limits['min_qty'])}")
    if limits["max_qty"] > 0 and size > limits["max_qty"]:
        reasons.append(f"size {plain(size)} is over {limits['lot_filter']} maxQty "
                       f"{plain(limits['max_qty'])}")
    for name, price in (("stop", stop), ("take profit", sizing["take_profit"]), ("entry", entry)):
        if price is None:
            continue
        if limits["min_price"] > 0 and price < limits["min_price"]:
            reasons.append(f"the {name} {plain(price)} is below PRICE_FILTER minPrice "
                           f"{plain(limits['min_price'])}")
        if limits["max_price"] > 0 and price > limits["max_price"]:
            reasons.append(f"the {name} {plain(price)} is above PRICE_FILTER maxPrice "
                           f"{plain(limits['max_price'])}")
    needed = [rule.sl_order_type.upper()]
    if rule.tp_leg and sizing["take_profit"] is not None:
        needed.append(rule.tp_order_type.upper())
    for order_type in needed:
        if order_type not in limits["order_types"]:
            reasons.append(f"the venue does not list order type {order_type} for this market "
                           f"(it lists {', '.join(limits['order_types'])})")
    return reasons


# ---------------------------------------------------------------------------
# Tickets, proposals and the PASS block.
#
# The block below is not decoration. `desk_policy.pass_block_fields()` reads the
# LAST `RISK | <ticket> |` block in the file, requires it to be a PASS, and
# compares its `key: value` lines against the request body as decimals. Every
# number here is therefore rendered ONCE, as a string, and the same string goes
# into the body. Anything appended to this file later must open a markdown
# heading first, which is what closes the block.
# ---------------------------------------------------------------------------
TICKET_FILE_RE = re.compile(r"^SG-(\d{8})-(\d{2})\.md$")


def next_ticket(desk: Desk, state: Optional[dict] = None) -> str:
    """The next free `SG-YYYYMMDD-NN` for today, counting every proposal already
    on the desk - the humans' as well as this script's.

    The proposals directory is the ledger, and a deleted proposal would hand its
    id back. The venue and the policy layer both remember a spent client order
    id, so the only thing that would come of it is a replay refusal and an
    incident - which is why the ids this script has issued are kept in its own
    state as well, and the two are unioned.
    """
    day = now().strftime("%Y%m%d")
    used = set()
    if desk.proposals_dir.is_dir():
        for path in desk.proposals_dir.iterdir():
            match = TICKET_FILE_RE.match(path.name)
            if match and match.group(1) == day:
                used.add(int(match.group(2)))
    for issued in (state or {}).get("issued") or []:
        match = re.fullmatch(r"SG-(\d{8})-(\d{2})", str(issued))
        if match and match.group(1) == day:
            used.add(int(match.group(2)))
    nxt = next((n for n in range(1, 100) if n not in used), None)
    if nxt is None:
        raise Held("tickets: all 99 ticket ids for today are used")
    return f"SG-{day}-{nxt:02d}"


def build_body(ticket: str, symbol: str, side: str, sizing: dict, rule: Rule,
               strings: dict) -> dict:
    """The exact bytes that will be sent. A bracket on `/v2/order/strategy`, so
    the stop arms with the fill; a market entry with a slippage bound, because
    an unbounded market order on a thin book is how a desk loses more than its
    ticket said."""
    body = {
        "strategy_id": ticket,
        "client_order_id": f"{ticket}-entry",
        "symbol": symbol,
        "side": side,
        "type": "market",
        "size": strings["size"],
        "slippage": strings["slippage"],
        "sl_order": {
            "client_order_id": f"{ticket}-sl",
            "type": rule.sl_order_type,
            "size": strings["size"],
            "stop_price": strings["stop_price"],
            "working_type": "mark_price",
            "reduce_only": True,
        },
    }
    if rule.tp_leg and sizing["take_profit"] is not None:
        body["tp_order"] = {
            "client_order_id": f"{ticket}-tp",
            "type": rule.tp_order_type,
            "size": strings["size"],
            "stop_price": strings["take_profit"],
            "working_type": "mark_price",
            "reduce_only": True,
        }
    return body


def pass_block(ticket: str, sa_id: str, fields: dict, lines: list[str]) -> str:
    head = f"RISK | {ticket} | PASS | {sa_id} | {iso(now())}"
    body = "\n".join(lines)
    rendered = "".join(f"  {key}: {value}\n" for key, value in fields.items())
    return f"{head}\n{body}\nfields\n{rendered}"


def write_proposal(desk: Desk, ticket: str, text: str) -> Path:
    desk.proposals_dir.mkdir(parents=True, exist_ok=True)
    path = desk.proposals_dir / f"{ticket}.md"
    if path.exists():
        raise Held(f"proposal: {path.name} already exists; this ticket id is taken")
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# Preflight: every refusal the gate can produce, predicted on this side of it.
#
# This is the difference between a desk that stops and a desk that machine-guns
# a gate. A refusal means this function was wrong, which is why a refusal files
# an incident instead of being retried.
# ---------------------------------------------------------------------------
CEILING_NOTIONAL = Decimal(5_000)
CEILING_RISK_FRACTION = Decimal("0.005")
CEILING_OPEN_POSITIONS = 3
CEILING_DAILY_LOSS = Decimal("0.03")
LOCAL_DAILY_LOSS_HOLD = Decimal("0.024")   # hold before the gate has to refuse


def preflight(desk: Desk, signer: Optional[Signer], *, sa: dict, symbol: str,
              notional: Decimal, risk_usd: Decimal, equity: Decimal, sizing: dict,
              state: dict, min_notional: Decimal) -> list[str]:
    """Returns the reasons to hold. Empty means every bound the gate will check
    has been checked here first, against the same sources."""
    held = []

    if desk.incidents_dir.is_dir() and any(desk.incidents_dir.iterdir()):
        names = sorted(p.name for p in desk.incidents_dir.iterdir())
        held.append(f"{len(names)} open incident(s) ({', '.join(names[:3])}); opening exposure is "
                    "blocked until the user clears them with a re-signed register")
    if sa["id"] in suspended_ids(desk):
        held.append(f"{sa['id']} is suspended")
    if not rules_hash_ok(desk, sa):
        held.append(f"RULES.md for {sa['rule']} does not match the hash {sa['id']} was signed "
                    "against; sending would suspend the approval")

    cap = min(dec(sa["max_notional"], "sa max_notional"), CEILING_NOTIONAL, equity)
    if notional > cap:
        held.append(f"notional ${notional:,.0f} > the tightest cap ${cap:,.0f}")
    if min_notional > 0 and notional < min_notional:
        held.append(f"notional ${notional:,.2f} is under the venue minimum ${min_notional:,.2f}")
    risk_cap = min(dec(sa["risk_per_trade"], "sa risk_per_trade"), CEILING_RISK_FRACTION)
    if equity > 0 and risk_usd / equity > risk_cap:
        held.append(f"risk {risk_usd / equity:.3%} > cap {risk_cap:.3%}")
    if sizing["nominal_loss"] > risk_usd * Decimal("1.05"):
        held.append(f"the stop loses ${sizing['nominal_loss']:,.2f} against a stated risk of "
                    f"${risk_usd:,.2f}; the stop and the sizing disagree")

    last = state.get("last_send_at")
    if last:
        since = (now() - parse_iso(last, "state last_send_at")).total_seconds()
        if since < PACE_SECONDS:
            held.append(f"{since:.0f}s since the last opening order, pacing floor {PACE_SECONDS}s")

    sod = state.get("sod") or {}
    if sod.get("date") == now().date().isoformat():
        start = dec(sod.get("equity", 0), "state sod equity")
        if start > 0 and (start - equity) / start >= LOCAL_DAILY_LOSS_HOLD:
            held.append(f"day is {(start - equity) / start:.2%} down against this script's own "
                        f"opening mark; holding before the {CEILING_DAILY_LOSS:.0%} stop refuses")

    if signer is not None:
        positions = signer.positions()
        if len(positions) >= CEILING_OPEN_POSITIONS:
            held.append(f"{len(positions)} positions already open across the book "
                        f"(ceiling {CEILING_OPEN_POSITIONS})")
        resting = [o for o in signer.open_orders() if not o["reduce_only"]
                   and re.search(r"SG-\d{8}-\d{2}", str(o.get("client_order_id") or ""))]
        occupied = ({p["symbol"] for p in positions} | {o["symbol"] for o in resting}) & set(
            sa.get("markets") or [])
        max_open = min(int(sa["max_open"]), CEILING_OPEN_POSITIONS)
        if symbol in occupied:
            held.append(f"{sa['id']} already has exposure or a working order on {symbol}")
        elif len(occupied) >= max_open:
            held.append(f"{sa['id']} holds {len(occupied)}/{max_open} markets "
                        f"({', '.join(sorted(occupied))})")
    return held


# ---------------------------------------------------------------------------
# The last thing before the send: run the gate's own comparisons on this side
# of it.
#
# `desk_policy._match_ticket` and `_check_protection` are the two functions that
# decide whether a ticket and a request are the same trade. Everything they
# compare is comparable here, so it is compared here. A failure is not a market
# condition and not a bound - it means the block and the body this script just
# wrote disagree, which is a defect in this script. It stops, loudly, and sends
# nothing.
# ---------------------------------------------------------------------------
TICKET_IN_COID = re.compile(r"SG-\d{8}-\d{2}")


def self_check(fields: dict, body: dict) -> None:
    problems = []

    def same_number(left, right, what):
        try:
            if dec(left, what) != dec(right, what):
                problems.append(f"{what}: block {left!r} != body {right!r}")
        except Blind as exc:
            problems.append(str(exc))

    for key in ("symbol", "side"):
        if str(body.get(key)).strip().lower() != str(fields[key]).strip().lower():
            problems.append(f"{key}: block {fields[key]!r} != body {body.get(key)!r}")
    same_number(fields["size"], body.get("size"), "size")
    if not TICKET_IN_COID.search(str(body.get("client_order_id", ""))):
        problems.append("client_order_id carries no SG- ticket id")
    if fields.get("order_type") == "market":
        if body.get("price") not in (None, ""):
            problems.append("a market ticket must carry no price")
        if body.get("slippage") in (None, ""):
            problems.append("a market ticket needs a slippage bound")
        if fields.get("ref_price") is None:
            problems.append("a market ticket needs ref_price so its notional is bounded")
    if body.get("leverage") not in (None, ""):
        same_number(fields.get("leverage"), body.get("leverage"), "leverage")

    leg = body.get("sl_order")
    if not isinstance(leg, dict):
        problems.append("no sl_order: an opening order carries its own stop")
    else:
        if not leg.get("reduce_only") and not leg.get("close_position"):
            problems.append("sl_order is not reduce-only; a stop that can open a position "
                            "is not a stop")
        if str(leg.get("working_type", "")).lower() != "mark_price":
            problems.append("sl_order must trigger on mark_price")
        same_number(fields["size"], leg.get("size"), "sl_order size")
        same_number(fields["stop_price"], leg.get("stop_price"), "sl_order stop_price")
        try:
            entry = dec(fields.get("price") or fields.get("ref_price"), "entry")
            stop = dec(leg.get("stop_price"), "stop")
            side = str(body.get("side", "")).lower()
            if side in ("buy", "long") and stop >= entry:
                problems.append("a long with its stop at or above the entry triggers immediately")
            if side in ("sell", "short") and stop <= entry:
                problems.append("a short with its stop at or below the entry triggers immediately")
            nominal = abs(entry - stop) * dec(fields["size"], "size")
            stated = dec(fields["risk_usd"], "risk_usd")
            if nominal > stated * Decimal("1.05"):
                problems.append(f"the stop loses ${nominal:,.2f} against a stated risk_usd of "
                                f"${stated:,.2f}")
        except Blind as exc:
            problems.append(str(exc))

    if problems:
        raise Held("autopilot defect - the PASS block and the request body disagree, so nothing "
                   "was sent: " + "; ".join(problems))


# ---------------------------------------------------------------------------
# The send, and what each answer means.
#
# `strike_request.py` exits 0 when the venue accepted, 3 when the policy layer
# refused, 1 when the venue answered with an error, and raises on a transport
# failure. Those are four different facts about the world and the desk treats
# them as four, because "resend" is the wrong answer to three of them.
# ---------------------------------------------------------------------------
def send_entry(signer: Signer, body: dict, path: str = "/v2/order/strategy") -> dict:
    payload = json.dumps(body, separators=(",", ":"))
    argv = signer._argv("POST", path, (), payload)
    try:
        done = subprocess.run(argv, input=payload, capture_output=True, text=True,
                              timeout=signer.timeout)
    except FileNotFoundError as exc:
        raise Held(f"signer: cannot run {argv[0]} ({exc}); nothing was sent")
    except subprocess.TimeoutExpired:
        return {"state": "unknown", "detail": f"the signer did not answer within "
                f"{signer.timeout:.0f}s", "payload": None}
    if done.returncode == 3:
        raise Refused((done.stderr or "").strip() or "policy refused with no reason given")
    try:
        payload_json = json.loads(done.stdout) if done.stdout.strip() else None
    except json.JSONDecodeError:
        payload_json = None
    if done.returncode == 0:
        return {"state": "accepted", "detail": "the venue accepted the order",
                "payload": payload_json}
    if payload_json is not None or "HTTP " in (done.stderr or ""):
        return {"state": "rejected", "detail": (done.stderr or "").strip()[:300],
                "payload": payload_json}
    return {"state": "unknown", "detail": (done.stderr or done.stdout or "").strip()[:300],
            "payload": payload_json}


def reconcile(signer: Optional[Signer], coid: str) -> str:
    """Read the order back by its client order id. This is the desk's answer to
    an unknown result: a lookup, never a guess and never a resend."""
    if signer is None:
        return "not reconciled (no signer)"
    found = signer.find_order(coid)
    if found is None:
        return "the venue does not know this client order id"
    status = found.get("status") or found.get("state") or "present"
    return f"the venue has {coid}: {status}"


# ---------------------------------------------------------------------------
# monitor - one frozen rule, evaluated at bar close, for each of its markets.
# ---------------------------------------------------------------------------
def read_market(desk: Desk, base: str, rule: Rule, symbol: str) -> dict:
    """Every input the rule needs, or `Blind`. Nothing here has a default."""
    bars = closed_bars(klines(base, symbol, rule.interval,
                              max(rule.sma_period, rule.atr_period) + 60), now())
    needed = max(rule.sma_period, rule.atr_period + 1)
    if len(bars) < needed:
        raise Blind(f"klines returned {len(bars)} closed bars, needs {needed}")
    age = (now() - bars[-1]["close_at"]).total_seconds()
    if age > rule.timeframe_seconds + SIGNAL_AGE_CEILING:
        raise Blind(f"the last closed bar is {age / 3600:.1f}h old; the feed is behind")
    premium = premium_index(base, symbol)
    history = recent(desk.history("funding", symbol), "rate", rule.funding_history_days, now())
    return {"bars": bars, "premium": premium, "funding_history": history}


def attempt_fire(desk: Desk, base: str, rule: Rule, symbol: str, market: dict, reading: dict,
                 signer: Optional[Signer], state: dict, dry: bool) -> dict:
    """One fire, from the signed register to the read-back. Returns a record;
    raises `Held` when a bound or a clock says no, `Refused` when the gate did."""
    sa = standing_approval(desk, rule, symbol)
    if sa["id"] in suspended_ids(desk):
        raise Held(f"{sa['id']} is suspended")
    if int(sa.get("bar_seconds", 0)) != rule.timeframe_seconds:
        raise Held(f"{sa['id']} was signed for bar_seconds {sa.get('bar_seconds')} but the rule's "
                   f"timeframe is {rule.timeframe_seconds}; the register and the rule disagree")

    side = rule.side(reading)
    sides = [str(s).lower() for s in (sa.get("sides") or ["long", "short"])]
    if ("long" if side == "buy" else "short") not in sides:
        raise Held(f"{sa['id']} does not permit a {side}")

    fired_at = reading["bar_close_at"]
    age = (now() - fired_at).total_seconds()
    stale_at = min(rule.timeframe_seconds, SIGNAL_AGE_CEILING)
    if age > stale_at:
        raise Held(f"the fire is {age:.0f}s old, past {stale_at:.0f}s; stale")

    equity, equity_source = equity_now(desk, signer)
    limits = constraints(base, symbol)
    entry = market["premium"]["mark"]
    average = atr(market["bars"], rule.atr_period)
    risk_fraction = min(dec(sa["risk_per_trade"], "sa risk_per_trade"), CEILING_RISK_FRACTION)
    sizing = size_ticket(equity=equity, risk_fraction=risk_fraction, entry=entry, side=side,
                         atr_value=average, rule=rule, tick=limits["tick"], step=limits["step"])

    rejected = venue_limits(limits, sizing, rule, entry)
    if rejected:
        raise Held("; ".join(rejected))

    book_now = book(base, symbol)
    liquidity = liquidity_clock(desk, symbol, side, sizing["notional"], rule, book_now)
    funding = funding_clock(market["premium"], side, rule)
    catalyst = catalyst_clock(desk, symbol)
    failed = [name for name, clock in (("liquidity", liquidity), ("funding", funding),
                                       ("catalyst", catalyst)) if not clock["pass"]]
    if failed:
        raise Held("; ".join(f"{name} clock: "
                             f"{ {'liquidity': liquidity, 'funding': funding, 'catalyst': catalyst}[name]['why'] }"
                             for name in failed))

    reasons = preflight(desk, signer, sa=sa, symbol=symbol, notional=sizing["notional"],
                        risk_usd=sizing["risk_usd"], equity=equity, sizing=sizing, state=state,
                        min_notional=limits["min_notional"])
    if reasons:
        raise Held("; ".join(reasons))

    # ---- the ticket. One rendering of every number, shared by block and body.
    ticket = next_ticket(desk, state)
    yesterday = (now() - dt.timedelta(days=1)).strftime("%Y%m%d")
    state["issued"] = [t for t in (state.get("issued") or [])
                       if str(t)[3:11] >= yesterday] + [ticket]
    desk.save_state(state)
    strings = {
        "size": plain(sizing["size"]),
        "stop_price": plain(sizing["stop"]),
        "ref_price": plain(entry),
        "slippage": plain((rule.slippage_bps / Decimal(10_000)).quantize(Decimal("0.000001"))),
        "take_profit": plain(sizing["take_profit"]) if sizing["take_profit"] is not None else "none",
        "risk_usd": plain(sizing["risk_usd"].quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
        "equity": plain(equity.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
    }
    expires_at = fired_at + dt.timedelta(seconds=rule.timeframe_seconds)
    fields = {
        "symbol": symbol, "side": side, "size": strings["size"], "order_type": "market",
        "ref_price": strings["ref_price"], "stop_price": strings["stop_price"],
        "take_profit": strings["take_profit"], "risk_usd": strings["risk_usd"],
        "equity": strings["equity"], "liquidity": "pass", "sa": sa["id"],
        "signal": rule.signal, "fired_at": iso(fired_at), "expires_at": iso(expires_at),
    }
    lines = [
        f"inputs   equity {strings['equity']} ({equity_source}) | entry {strings['ref_price']} "
        f"(mark) | atr{rule.atr_period} {short(average)}",
        f"sizing   risk {strings['risk_usd']} / stressed {short(sizing['per_unit'])} "
        f"= {short(sizing['risk_usd'] / sizing['per_unit'])} "
        f"-> {strings['size']} (step {plain(limits['step'])} from {limits['lot_filter']}, down) = notional "
        f"{short(sizing['notional'])}",
        f"stress   stop {strings['stop_price']} + slip {short(sizing['slip'])} "
        f"({plain(rule.slippage_bps)} bps) = {short(sizing['stressed_stop'])} | "
        f"fees {short(sizing['fee_per_unit'])}/unit "
        f"({plain(rule.fee_bps_round_turn)} bps round turn)",
        f"clocks   rule fired {iso(fired_at)} | liquidity {liquidity['why']} | "
        f"funding {funding['why']} | catalyst {catalyst['why']}",
        f"bounds   {sa['id']} risk {plain(risk_fraction * 100)}% max_notional {sa['max_notional']} "
        f"max_open {sa['max_open']} | ceilings notional 5000, leverage 5, positions 3",
    ]
    body = build_body(ticket, symbol, side, sizing, rule, strings)
    self_check(fields, body)

    header = (f"# {ticket}\n"
              f"opened: {iso(now())}\n"
              f"source: autopilot {VERSION} monitor {rule.signal}"
              f"{' (DRY REHEARSAL - nothing was sent)' if dry else ''}\n"
              f"market: {symbol}\n\n")
    path = write_proposal(desk, ticket, header + pass_block(ticket, sa["id"], fields, lines))
    body_path = desk.proposals_dir / f"{ticket}-entry.json"
    body_path.write_text(json.dumps(body, indent=2) + "\n")

    desk.signal(
        f"RULE FIRED | {iso(now())} | {rule.signal} | {symbol} | {side} {strings['size']} @ mark "
        f"{strings['ref_price']} stop {strings['stop_price']}\n"
        f"  clocks: rule pass | liquidity {'pass' if liquidity['pass'] else 'fail'} | "
        f"funding {'pass' if funding['pass'] else 'fail'} | "
        f"catalyst {'pass' if catalyst['pass'] else 'fail'}\n"
        f"  next: {ticket} ({sa['id']}, {'dry rehearsal' if dry else 'sent'})")

    record = {"ticket": ticket, "symbol": symbol, "side": side, "sa": sa["id"],
              "proposal": str(path), "body": str(body_path), "size": strings["size"],
              "coid": body["client_order_id"], "expires_at": iso(expires_at),
              "notional": short(sizing["notional"])}

    if dry:
        record["state"] = "rehearsed"
        desk.journal(f"{iso(now())} DRY {ticket} {rule.signal} {symbol} {side} {strings['size']} "
                     f"notional {record['notional']} - preview written, nothing sent")
        return record

    if signer is None:
        raise Held("no signer is configured, so nothing can be sent")
    state["last_send_at"] = iso(now())
    desk.save_state(state)
    desk.journal(f"{iso(now())} SEND {ticket} {rule.signal} {symbol} {side} {strings['size']} "
                 f"notional {record['notional']} sl {strings['stop_price']}")
    answer = send_entry(signer, body)
    record["state"] = answer["state"]
    record["detail"] = answer["detail"]
    record["read_back"] = reconcile(signer, body["client_order_id"])
    desk.journal(f"{iso(now())} {answer['state'].upper()} {ticket} {answer['detail']} | "
                 f"{record['read_back']}")
    if answer["state"] == "unknown":
        desk.incident("unknown-send-result",
                      f"{ticket} {symbol} {side} {strings['size']}\n"
                      f"The send did not return a readable answer: {answer['detail']}\n"
                      f"Read-back: {record['read_back']}\n\n"
                      "Nothing was resent. Reconcile by hand before the desk opens exposure again.")
    return record


def mark_day(desk: Desk, state: dict) -> None:
    """This script's own opening mark for the day, taken from the signer's
    snapshot. It is advisory - the daily-loss stop that matters is the policy
    layer's, measured against a baseline this process cannot write - and it
    exists so the desk holds before the gate has to refuse."""
    today = now().date().isoformat()
    if (state.get("sod") or {}).get("date") == today:
        return
    try:
        snapshot = json.loads(desk.equity_file.read_text())
        state["sod"] = {"date": today, "equity": str(dec(snapshot["equity"], "snapshot equity"))}
    except (OSError, json.JSONDecodeError, KeyError, Blind):
        pass


def prune(mapping: dict, days: int = 7) -> dict:
    cutoff = now() - dt.timedelta(days=days)
    out = {}
    for key, value in mapping.items():
        try:
            if parse_iso(value.get("at"), "state at") >= cutoff:
                out[key] = value
        except (AttributeError, Blind):
            continue
    return out


def cmd_monitor(args) -> int:
    desk = Desk(DESK)
    try:
        rule = Rule.load(Path(args.rule))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    base = price_base(args.network)
    signer = None if args.no_signer else Signer(args.network, timeout=args.timeout)
    state = desk.state()
    state.setdefault("fired", {})
    state.setdefault("blind", {})
    state.setdefault("open_tickets", [])
    mark_day(desk, state)

    markets = [m for m in rule.markets if not args.market or m in args.market]
    if not markets:
        print(f"none of {args.market} is in {rule.signal}", file=sys.stderr)
        return 1

    fires, worst = [], 0
    for symbol in markets:
        try:
            market = read_market(desk, base, rule, symbol)
            reading = rule.evaluate(market["bars"], market["premium"]["funding"],
                                    market["funding_history"])
        except Blind as exc:
            # `could_not_tell` is a verdict of its own. It never collapses into
            # `not_fired`, and two of them in a row stop the rule.
            key = f"{symbol}"
            count = int((state["blind"].get(key) or {}).get("count", 0)) + 1
            state["blind"][key] = {"count": count, "at": iso(now()), "why": str(exc)}
            desk.watch_log(rule.name, f"{rule.signal} {symbol:10} could_not_tell  {exc}")
            desk.signal(f"WATCH COULD NOT TELL | {iso(now())} | {rule.signal} | {symbol} | {exc}\n"
                        f"  next: @Strategist (consecutive {count})")
            if count >= BLIND_BARS_BEFORE_SUSPEND:
                try:
                    sa = standing_approval(desk, rule, symbol)
                    desk.suspend_sa(sa["id"], f"monitor blind for {count} bars on {symbol}: {exc}")
                except Held:
                    pass
            worst = max(worst, 2)
            continue

        state["blind"].pop(symbol, None)
        detail = " ".join(f"{k}={short(v)}" for k, v in reading.items()
                          if k not in ("fired", "bar_close_at"))
        verdict = "fired" if reading["fired"] else "not_fired"
        desk.watch_log(rule.name, f"{rule.signal} {symbol:10} {verdict:10} bar "
                                  f"{iso(reading['bar_close_at'])} {detail}")
        if reading["fired"]:
            fires.append((symbol, market, reading))

    sent_this_run = False
    for symbol, market, reading in fires:
        key = f"{rule.name}|{symbol}|{iso(reading['bar_close_at'])}"
        previous = state["fired"].get(key)
        if previous and previous.get("state") != "held":
            # Sent, rehearsed or refused: that bar is decided and is never
            # revisited. A HOLD is different - the book thins and recovers, a
            # blackout window closes, the pace floor passes - so a held fire is
            # tried again on the next run and the staleness check is what ends
            # it, one bar or one hour after it fired, whichever is tighter.
            continue
        if sent_this_run:
            # One opening order per run. The policy floor is 60 seconds between
            # opens and the next cron tick is well inside the signal's life.
            desk.journal(f"{iso(now())} DEFER {rule.signal} {symbol} - one send per run; the next "
                         "run picks this fire up if it is still fresh")
            continue
        try:
            record = attempt_fire(desk, base, rule, symbol, market, reading, signer, state,
                                  args.dry)
        except (Held, Blind) as exc:
            # A read that fails after the fire - a book that went one-sided, an
            # exchangeInfo that changed shape - is the same outcome as a bound
            # refusing: nothing was sent, and the bar is retried while it is fresh.
            state["fired"][key] = {"at": iso(now()), "state": "held", "why": str(exc)}
            desk.watch_log(rule.name, f"{rule.signal} {symbol:10} held       {exc}")
            desk.journal(f"{iso(now())} HELD {rule.signal} {symbol} - {exc}")
            desk.signal(f"RULE FIRED | {iso(now())} | {rule.signal} | {symbol} | held\n"
                        f"  held: {exc}\n  next: @Desk Lead (no ticket raised)")
            worst = max(worst, 2)
            continue
        except Refused as exc:
            state["fired"][key] = {"at": iso(now()), "state": "refused", "why": str(exc)}
            desk.incident("policy-refusal",
                          f"{rule.signal} {symbol}\n\nThe policy layer refused a send this script's "
                          f"preflight expected to pass:\n\n    {exc}\n\n"
                          "Nothing was retried. The refusal names the bound; fix the preflight in "
                          "autopilot.py to predict it, then clear this incident with a re-signed "
                          "register.")
            desk.signal(f"RULE FIRED | {iso(now())} | {rule.signal} | {symbol} | REFUSED\n"
                        f"  policy: {exc}\n  next: @Desk Lead incident filed")
            print(f"POLICY REFUSED: {exc}", file=sys.stderr)
            state["fired"] = prune(state["fired"])
            desk.save_state(state)
            return 3
        state["fired"][key] = {"at": iso(now()), "state": record["state"],
                               "ticket": record["ticket"]}
        if record["state"] == "accepted":
            state["open_tickets"].append(
                {"ticket": record["ticket"], "coid": record["coid"], "symbol": symbol,
                 "expires_at": record["expires_at"], "at": iso(now())})
        sent_this_run = record["state"] != "rehearsed"
        print(json.dumps(record, indent=2))

    state["fired"] = prune(state["fired"])
    desk.save_state(state)
    return worst


# ---------------------------------------------------------------------------
# scan - the hourly universe scan. Descriptive, never directional: it says what
# is unusual, and a frozen rule decides whether unusual is tradeable.
# ---------------------------------------------------------------------------
def scan_market(desk: Desk, base: str, symbol: str, at: dt.datetime) -> dict:
    """Three cheap metrics per market. Depth is a fourth read and is taken only
    for the markets these three already flagged."""
    premium = premium_index(base, symbol)
    ticker = ticker_24h(base, symbol)
    interest = open_interest(base, symbol)

    funding_rows = desk.history("funding", symbol)
    history = recent(funding_rows, "rate", 30, at)
    rank = percentile_rank(history, premium["funding"]) if len(history) >= 24 else None
    desk.append_history("funding", symbol,
                        {"at": iso(at), "rate": plain(premium["funding"]),
                         "mark": plain(premium["mark"])})

    oi_rows = desk.history("oi", symbol)
    desk.append_history("oi", symbol, {"at": iso(at), "oi_base": plain(interest),
                                       "mark": plain(premium["mark"])})
    oi_change = None
    for row in reversed(oi_rows):
        try:
            stamp = parse_iso(row["at"], "oi at")
        except Blind:
            continue
        gap = (at - stamp).total_seconds()
        if 22 * 3600 <= gap <= 26 * 3600:
            then = dec(row["oi_base"], "oi_base")
            if then > 0:
                oi_change = (interest - then) / then * 100
            break

    daily = closed_bars(klines(base, symbol, "1d", 40), at)
    average = atr(daily, 20) if len(daily) >= 21 else None
    expansion = ((ticker["high"] - ticker["low"]) / average) if average and average > 0 else None

    flags = []
    if rank is not None and (rank >= 95 or rank <= 5):
        flags.append(f"funding {plain(rank.quantize(Decimal('0.1')))}th pct of 30d")
    if oi_change is not None:
        if abs(oi_change) >= 8 and abs(ticker["change_pct"]) <= 1:
            flags.append(f"OI {plain(oi_change.quantize(Decimal('0.1')))}% with price flat")
        elif abs(oi_change) >= 10 and (oi_change > 0) != (ticker["change_pct"] > 0):
            flags.append(f"OI {plain(oi_change.quantize(Decimal('0.1')))}% against price "
                         f"{plain(ticker['change_pct'])}%")
    if expansion is not None and expansion >= 2:
        flags.append(f"range {plain(expansion.quantize(Decimal('0.01')))}x ATR20")

    deviation = abs(rank - 50) if rank is not None else Decimal(0)
    return {"symbol": symbol, "flags": flags, "funding_pct": rank, "oi_change_pct": oi_change,
            "range_expansion": expansion, "deviation": deviation, "mark": premium["mark"],
            "change_pct": ticker["change_pct"]}


def scan_depth(desk: Desk, base: str, symbol: str, at: dt.datetime) -> Optional[str]:
    book_now = book(base, symbol)
    depth_usd = depth_within(book_now["bids"], book_now["mid"], BAND_BPS) * book_now["mid"]
    history = recent(desk.history("depth", symbol), "bid_10bps_usd", 7, at)
    desk.append_history("depth", symbol, {
        "at": iso(at), "bid_10bps_usd": plain(depth_usd.quantize(Decimal("0.01"))),
        "spread_bps": plain(book_now["spread_bps"].quantize(Decimal("0.01")))})
    if len(history) < 24:
        return None
    middle = median(history)
    if middle <= 0:
        return None
    ratio = depth_usd / middle
    if ratio <= Decimal("0.6") or ratio >= Decimal("1.8"):
        return f"depth {plain(ratio.quantize(Decimal('0.01')))}x its 7d median"
    return None


def cmd_scan(args) -> int:
    desk = Desk(DESK)
    base = price_base(args.network)
    at = now()
    state = desk.state()
    mark_day(desk, state)
    desk.save_state(state)

    try:
        universe = args.market or trading_markets(base)
    except Blind as exc:
        print(f"scan: {exc}", file=sys.stderr)
        return 1
    rows, unavailable = [], []
    for symbol in universe:
        try:
            rows.append(scan_market(desk, base, symbol, at))
        except Blind as exc:
            unavailable.append(f"{symbol} ({exc})")

    rows.sort(key=lambda r: (len(r["flags"]), r["deviation"]), reverse=True)
    for row in rows[:10]:
        try:
            shift = scan_depth(desk, base, row["symbol"], at)
        except Blind as exc:
            unavailable.append(f"{row['symbol']} depth ({exc})")
            continue
        if shift:
            row["flags"].append(shift)
    rows.sort(key=lambda r: (len(r["flags"]), r["deviation"]), reverse=True)

    flagged = [r for r in rows if r["flags"]][:5]
    if not flagged:
        block = (f"SCAN | {iso(at)} | {len(rows)} markets | nothing flagged")
    else:
        lines = [f"SCAN | {iso(at)} | {len(rows)} markets | {len(flagged)} flagged"]
        for row in flagged:
            lines.append(f"  {row['symbol']:10} mark {plain(row['mark'])} "
                         f"24h {plain(row['change_pct'].quantize(Decimal('0.01')))}% | "
                         + "; ".join(row["flags"]))
        lines.append("  next: @Strategist (a frozen rule decides whether any of this is tradeable)")
        block = "\n".join(lines)
    if unavailable:
        block += "\n  unavailable: " + "; ".join(unavailable)
    desk.signal(block)
    print(block)
    return 0


# ---------------------------------------------------------------------------
# sweep - Tier 0 housekeeping. Expire a fire nobody filled, cancel the entry it
# left resting. Cancels and reduce-only orders need no approval, by design.
# ---------------------------------------------------------------------------
def cmd_sweep(args) -> int:
    desk = Desk(DESK)
    signer = None if args.no_signer else Signer(args.network, timeout=args.timeout)
    state = desk.state()
    state.setdefault("open_tickets", [])
    if signer is None:
        print("sweep needs the signer to read the book", file=sys.stderr)
        return 1

    resting = {o["client_order_id"]: o for o in signer.open_orders() if o.get("client_order_id")}
    held = {p["symbol"] for p in signer.positions()}
    keep, acted = [], 0
    for entry in state["open_tickets"]:
        coid, symbol = entry.get("coid"), entry.get("symbol")
        try:
            expires = parse_iso(entry.get("expires_at"), "open ticket expires_at")
        except Blind:
            expires = now()
        order = resting.get(coid)
        if symbol in held:
            desk.journal(f"{iso(now())} FILLED {entry['ticket']} {symbol} - position open, "
                         "the ticket leaves the sweep list")
            close_proposal(desk, entry["ticket"], "filled; the position is live")
            acted += 1
            continue
        if order is None:
            if now() > expires:
                desk.journal(f"{iso(now())} GONE {entry['ticket']} {symbol} - no resting order, "
                             "no position, past expiry")
                close_proposal(desk, entry["ticket"], "expired with nothing resting and no position")
                acted += 1
                continue
            keep.append(entry)
            continue
        if now() <= expires:
            keep.append(entry)
            continue
        # An entry still working past its bar is the trade the backtest never
        # modelled. Cancelling it is Tier 0 and needs no approval.
        order_id = order.get("order_id")
        if order_id in (None, ""):
            desk.journal(f"{iso(now())} SWEEP {entry['ticket']} {symbol} - resting past expiry but "
                         "the venue returned no order_id to cancel")
            keep.append(entry)
            continue
        try:
            signer.call("DELETE", "/v2/order/cancel", body={"order_id": order_id, "symbol": symbol})
        except (Held, Refused) as exc:
            desk.journal(f"{iso(now())} SWEEP-FAILED {entry['ticket']} {symbol} - {exc}")
            keep.append(entry)
            continue
        desk.journal(f"{iso(now())} CANCELLED {entry['ticket']} {symbol} {coid} - expired unfilled")
        desk.signal(f"RULE STALE | {iso(now())} | {entry['ticket']} | {symbol} | entry expired "
                    f"unfilled and was cancelled\n  next: @Trade Reviewer")
        close_proposal(desk, entry["ticket"], "expired unfilled; the resting entry was cancelled")
        acted += 1

    state["open_tickets"] = keep
    desk.save_state(state)
    print(f"sweep: {acted} closed, {len(keep)} still open")
    return 0


def close_proposal(desk: Desk, ticket: str, outcome: str) -> None:
    """Append the outcome UNDER A HEADING. The heading is what closes the PASS
    block: `desk_policy.pass_block_fields()` stops reading there, so nothing
    written after a ticket is decided can become a field of it."""
    path = desk.proposals_dir / f"{ticket}.md"
    if not path.exists():
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## outcome\n\nclosed: {iso(now())}\n{outcome}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    global DESK, SIGNER_CMD
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    def options(target, suppress: bool):
        """The same options before and after the subcommand.

        A cron line that reads `monitor --rule R --no-signer` must not fail
        because the flag came after the verb. The subcommand's copies default
        to SUPPRESS so they never overwrite a value given before it.
        """
        blank = argparse.SUPPRESS if suppress else None
        target.add_argument("--desk", default=blank if suppress else str(DESK),
                            help="the trading desk directory")
        target.add_argument("--network", choices=["testnet", "mainnet"],
                            default=blank if suppress
                            else os.environ.get("STRIKEGROK_NETWORK", "testnet"),
                            help="testnet by default. A rule earns mainnet by forward testing, "
                                 "not by a default.")
        target.add_argument("--signer", default=blank,
                            help="the command that becomes the signer, e.g. "
                                 "'sudo -u strike-signer'")
        target.add_argument("--no-signer", action="store_true", default=blank if suppress else False,
                            help="never invoke the signer: public reads only")
        target.add_argument("--timeout", type=float, default=blank if suppress else 60.0)

    options(parser, suppress=False)
    parser.add_argument("--version", action="version", version=f"autopilot {VERSION}")
    common = argparse.ArgumentParser(add_help=False)
    options(common, suppress=True)
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", parents=[common], help="the hourly universe scan")
    scan.add_argument("--market", action="append", help="limit the scan to these markets")

    monitor = sub.add_parser("monitor", parents=[common],
                             help="evaluate one frozen rule at bar close")
    monitor.add_argument("--rule", required=True, help="path to the frozen rule JSON")
    monitor.add_argument("--market", action="append", help="limit the run to these markets")
    monitor.add_argument("--dry", action="store_true",
                         help="rehearse: write the proposal, the PASS block and the exact request "
                              "body, and stop before the send")

    sweep = sub.add_parser("sweep", parents=[common],
                           help="expire stale fires and cancel dead entries (Tier 0)")
    sweep.add_argument("--market", action="append", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)

    DESK = Path(args.desk)
    if args.signer:
        SIGNER_CMD = args.signer

    try:
        if args.cmd == "scan":
            return cmd_scan(args)
        if args.cmd == "monitor":
            return cmd_monitor(args)
        if args.cmd == "sweep":
            return cmd_sweep(args)
    except Refused as exc:
        print(f"POLICY REFUSED: {exc}", file=sys.stderr)
        return 3
    except Held as exc:
        print(f"held: {exc}", file=sys.stderr)
        return 2
    except Blind as exc:
        print(f"could not tell: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:                       # noqa: BLE001 - deliberate
        # A crash is not a hold and not a refusal. Say which it was, and exit on
        # the operating-error code so a cron line's failure is visible as one.
        import traceback
        traceback.print_exc()
        print(f"autopilot: unhandled {exc!r}; nothing further was attempted", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
