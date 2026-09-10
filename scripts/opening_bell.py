#!/usr/bin/env python3
"""Print a zero-key Strike Finance market snapshot for a new StrikeGrok desk.

The script uses only the public Price Service (`/price/v2/...`). It never reads
environment variables, account state, API wallet material, the MCP execution
server or any authenticated endpoint.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

DEFAULT_BASE_URL = "https://api.strikefinance.org/price"
TESTNET_BASE_URL = "https://api-v2-testnet.strikefinance.org/price"
BANDS_BPS = (5, 10, 25)
# `/v2/depth` defaults to 20 levels a side, which stops well short of 25 bps on
# a liquid Strike perp, so the snapshot always asks for the documented maximum.
# At 1000 the whole resting book comes back in one response. Two different things
# can still leave a band unmeasured, and the snapshot keeps them apart: the book
# genuinely has no orders out that far (a fact about the market), or the response
# hit the level cap (a limit of the read). Either way the band is a floor, never
# a total.
DEPTH_LIMIT = 1000
FUNDING_INTERVAL_HOURS = 1  # `nextFundingTime` lands on the hour; funding accrues hourly.


def get_json(base_url: str, path: str, params: dict | None = None, timeout: float = 15.0):
    url = f"{base_url.rstrip('/')}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "strikegrok-opening-bell/1.0"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def read_json(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


FIXTURES = {
    "exchangeInfo": "exchangeInfo.json",
    "premiumIndex": "premiumIndex-{symbol}.json",
    "ticker24hr": "ticker24hr-{symbol}.json",
    "openInterest": "openInterest-{symbol}.json",
    "depth": "depth-{symbol}.json",
}


def load_fixture(directory: str, name: str, symbol: str = ""):
    return read_json(os.path.join(directory, FIXTURES[name].format(symbol=symbol)))


def decimal(value, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} is not numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{field} is not finite")
    return number


def levels_of(book: dict, side: str, symbol: str = "") -> list[tuple[Decimal, Decimal]]:
    raw = book.get(side)
    if not isinstance(raw, list) or not raw:
        # A one-sided or empty book is a real state, not a failed read - Strike's
        # testnet markets are routinely empty. Say which it is; never let it pass
        # as a quiet zero.
        raise ValueError(
            f"{symbol or 'this market'} has no resting {side}: the book is one-sided or empty,"
            " so mid, spread and depth are unavailable"
        )
    out = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            raise ValueError(f"malformed {side} level")
        out.append((decimal(entry[0], "book price"), decimal(entry[1], "book size")))
    return out


def depth_within(levels: list[tuple[Decimal, Decimal]], mid: Decimal, band_bps: int) -> Decimal:
    limit = Decimal(band_bps) / Decimal(10_000)
    return sum((size for px, size in levels if abs(px - mid) / mid <= limit), Decimal(0))


def reach_bps(levels: list[tuple[Decimal, Decimal]], mid: Decimal) -> Decimal:
    """How far from the mid the furthest resting level on this side sits."""
    return max(abs(px - mid) for px, _ in levels) / mid * Decimal(10_000)


def top_of_book(bids, asks) -> tuple[Decimal, Decimal, Decimal]:
    best_bid, best_ask = bids[0][0], asks[0][0]
    if best_bid <= 0 or best_ask <= 0 or best_bid > best_ask:
        raise ValueError("order book top is invalid")
    return best_bid, best_ask, (best_bid + best_ask) / Decimal(2)


def market_config(exchange_info, symbol: str) -> dict:
    symbols = exchange_info.get("symbols") if isinstance(exchange_info, dict) else None
    if not isinstance(symbols, list):
        raise ValueError("exchangeInfo returned an unexpected shape")
    config = next((s for s in symbols if s.get("symbol") == symbol), None)
    if config is None:
        known = ", ".join(sorted(s.get("symbol", "?") for s in symbols)[:8])
        raise ValueError(f"{symbol} is not a Strike market (known: {known}, ...)")
    return config


def filter_value(config: dict, filter_type: str, key: str):
    for entry in config.get("filters") or []:
        if entry.get("filterType") == filter_type:
            return entry.get(key)
    return None


def build_snapshot(exchange_info, premium, ticker, open_interest, book, symbol: str, network: str) -> dict:
    config = market_config(exchange_info, symbol)
    if config.get("status") != "trading":
        raise ValueError(f"{symbol} is not trading (status {config.get('status')!r})")

    bids, asks = levels_of(book, "bids", symbol), levels_of(book, "asks", symbol)
    best_bid, best_ask, book_mid = top_of_book(bids, asks)

    mark = decimal(premium.get("markPrice"), "markPrice")
    index = decimal(premium.get("indexPrice"), "indexPrice")
    funding = decimal(premium.get("fundingRate"), "fundingRate")
    oi_base = decimal(open_interest.get("openInterest"), "openInterest")

    timestamp_ms = int(book.get("E") or book.get("T") or 0)
    if timestamp_ms <= 0:
        raise ValueError("depth snapshot carries no event time")
    observed_at = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    next_funding_ms = int(premium.get("nextFundingTime") or 0)

    reach = {"bid": reach_bps(bids, book_mid), "ask": reach_bps(asks, book_mid)}
    # A side that came back exactly at the cap is the one case where the book may
    # continue past the furthest level returned.
    truncated = {"bid": len(bids) >= DEPTH_LIMIT, "ask": len(asks) >= DEPTH_LIMIT}
    depth = {}
    for band in BANDS_BPS:
        row = {}
        for name, levels in (("bid", bids), ("ask", asks)):
            size = depth_within(levels, book_mid, band)
            row[f"{name}_base"] = str(size)
            row[f"{name}_usd"] = str(size * book_mid)
            # Measured only when the response reaches the band and was not cut
            # off at the cap. Otherwise the number is a floor.
            row[f"{name}_complete"] = Decimal(band) <= reach[name] and not truncated[name]
        depth[str(band)] = row

    return {
        "mode": "read-only",
        "network": network,
        "source": {
            "service": "Strike Price Service",
            "requests": ["/v2/exchangeInfo", "/v2/premiumIndex", "/v2/ticker/24hr", "/v2/openInterest", "/v2/depth"],
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "last_update_id": book.get("lastUpdateId"),
            "depth_limit": DEPTH_LIMIT,
        },
        "market": symbol,
        "contract": {
            "type": config.get("contractType"),
            "base": config.get("baseAsset"),
            "quote": config.get("quoteAsset"),
            "margin": config.get("marginAsset"),
            "tick_size": filter_value(config, "PRICE_FILTER", "tickSize"),
            "step_size": filter_value(config, "LOT_SIZE", "stepSize"),
            "min_notional": filter_value(config, "MIN_NOTIONAL", "notional"),
            "liquidation_fee": config.get("liquidationFee"),
            "order_types": config.get("orderType"),
            "time_in_force": config.get("timeInForce"),
        },
        "prices": {
            "book_mid": str(book_mid),
            "mark": str(mark),
            "index": str(index),
            "last": str(decimal(ticker.get("lastPrice"), "lastPrice")),
            "open_24h": str(decimal(ticker.get("openPrice"), "openPrice")),
            "high_24h": str(decimal(ticker.get("highPrice"), "highPrice")),
            "low_24h": str(decimal(ticker.get("lowPrice"), "lowPrice")),
            "change_24h_pct": str(decimal(ticker.get("priceChangePercent"), "priceChangePercent")),
        },
        "funding": {
            "interval_hours": FUNDING_INTERVAL_HOURS,
            "rate": str(funding),
            "rate_pct": str(funding * Decimal(100)),
            "annualized_simple_pct": str(funding * Decimal(24 * 365 * 100 // FUNDING_INTERVAL_HOURS)),
            "next_funding_at": (
                datetime.fromtimestamp(next_funding_ms / 1000, tz=timezone.utc)
                .isoformat().replace("+00:00", "Z")
                if next_funding_ms > 0 else None
            ),
        },
        "activity": {
            "open_interest_base": str(oi_base),
            "open_interest_usd": str(oi_base * mark),
            "volume_24h_base": str(decimal(ticker.get("volume"), "volume")),
            "volume_24h_usd": str(decimal(ticker.get("quoteVolume"), "quoteVolume")),
        },
        "book": {
            "best_bid": str(best_bid),
            "best_ask": str(best_ask),
            "spread_bps": str((best_ask - best_bid) / book_mid * Decimal(10_000)),
            "levels_returned": {"bid": len(bids), "ask": len(asks)},
            "levels_capped": truncated,
            "reach_bps": {"bid": str(reach["bid"]), "ask": str(reach["ask"])},
            "depth": depth,
        },
        "safety": "No key requested. No account read. No MCP execution tool called. No order created or sent.",
    }


def fetch_snapshot(symbol: str, base_url: str, timeout: float, fixture_dir: str | None = None) -> dict:
    if fixture_dir:
        exchange_info = load_fixture(fixture_dir, "exchangeInfo")
        market_config(exchange_info, symbol)
        return build_snapshot(
            exchange_info,
            load_fixture(fixture_dir, "premiumIndex", symbol),
            load_fixture(fixture_dir, "ticker24hr", symbol),
            load_fixture(fixture_dir, "openInterest", symbol),
            load_fixture(fixture_dir, "depth", symbol),
            symbol,
            "fixture",
        )
    exchange_info = get_json(base_url, "/v2/exchangeInfo", timeout=timeout)
    market_config(exchange_info, symbol)  # an unknown symbol fails here, before any further request
    params = {"symbol": symbol}
    network = "testnet" if "testnet" in base_url.lower() else "mainnet"
    return build_snapshot(
        exchange_info,
        get_json(base_url, "/v2/premiumIndex", params, timeout),
        get_json(base_url, "/v2/ticker/24hr", params, timeout),
        get_json(base_url, "/v2/openInterest", params, timeout),
        get_json(base_url, "/v2/depth", {**params, "limit": DEPTH_LIMIT}, timeout),
        symbol,
        network,
    )


def compact(number: Decimal, prefix: str = "") -> str:
    absolute = abs(number)
    for threshold, suffix in ((Decimal("1e9"), "B"), (Decimal("1e6"), "M"), (Decimal("1e3"), "K")):
        if absolute >= threshold:
            return f"{prefix}{number / threshold:,.2f}{suffix}"
    return f"{prefix}{number:,.2f}"


def price(number: Decimal) -> str:
    """Strike lists sub-cent perps beside four-figure ones; keep both readable."""
    return f"{number:,.5f}" if abs(number) < 1 else f"{number:,.2f}"


def amount(number: Decimal, complete: bool) -> str:
    """A band the resting book does not reach is a floor, not a total."""
    return compact(number, "$") if complete else f">= {compact(number, '$')}"


def render(snapshot: dict) -> str:
    prices = snapshot["prices"]
    funding = snapshot["funding"]
    activity = snapshot["activity"]
    book = snapshot["book"]
    contract = snapshot["contract"]
    base = contract["base"] or snapshot["market"]
    lines = [
        f"STRIKEGROK OPENING BELL — {snapshot['market']}",
        f"READ ONLY · {snapshot['network'].upper()} · {snapshot['source']['observed_at']}",
        "Sources: Strike Price Service /v2 exchangeInfo + premiumIndex + ticker/24hr + openInterest + depth",
        "",
        f"Price       mid {price(Decimal(prices['book_mid']))} · mark {price(Decimal(prices['mark']))} · index {price(Decimal(prices['index']))}",
        f"24h         {Decimal(prices['change_24h_pct']):+,.2f}% · high {price(Decimal(prices['high_24h']))} · low {price(Decimal(prices['low_24h']))}",
        f"Funding     {Decimal(funding['rate_pct']):+,.5f}%/{funding['interval_hours']}h · {Decimal(funding['annualized_simple_pct']):+,.2f}% annualized (simple)",
    ]
    if funding["next_funding_at"]:
        lines.append(f"Next funding {funding['next_funding_at']}")
    lines.extend([
        f"Open int.   {compact(Decimal(activity['open_interest_base']))} {base} · {compact(Decimal(activity['open_interest_usd']), '$')}",
        f"24h volume  {compact(Decimal(activity['volume_24h_usd']), '$')}",
        f"Spread      {Decimal(book['spread_bps']):,.3f} bps",
        f"Contract    tick {contract['tick_size']} · step {contract['step_size']} · min notional ${contract['min_notional']}",
        "",
        "Visible depth from the book mid",
    ])
    for band in BANDS_BPS:
        row = book["depth"][str(band)]
        bid = amount(Decimal(row["bid_usd"]), row["bid_complete"])
        ask = amount(Decimal(row["ask_usd"]), row["ask_complete"])
        lines.append(f"  {band:>2} bps     bid {bid:>13} · ask {ask:>13}")
    capped = book.get("levels_capped") or {}
    lines.append(
        f"  Book read at limit {snapshot['source']['depth_limit']}:"
        f" {book['levels_returned']['bid']} bid levels, {book['levels_returned']['ask']} ask levels"
        + (" (cap reached)." if any(capped.values()) else " - the whole resting book.")
    )
    if any(not row[side] for row in book["depth"].values() for side in ("bid_complete", "ask_complete")):
        lines.append(
            f"  Resting orders stop at {Decimal(book['reach_bps']['bid']):,.1f} bps bid"
            f" and {Decimal(book['reach_bps']['ask']):,.1f} bps ask."
        )
        lines.append(
            "  Wider bands are floors, not totals: the response was cut off at the cap."
            if any(capped.values())
            else "  Wider bands are floors, not totals: no resting order sits out there to count."
        )
    lines.extend(["", snapshot["safety"], "Facts only. This snapshot is not a trading signal."])
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="ADA-USD", help="Strike perp symbol, e.g. ADA-USD or BTC-USD")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--testnet", action="store_true", help=f"shorthand for --base-url {TESTNET_BASE_URL}")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--fixture-dir", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)
    symbol = args.symbol.strip().upper()
    if not symbol or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in symbol):
        parser.error("symbol contains unsupported characters")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be positive")
    base_url = TESTNET_BASE_URL if args.testnet else args.base_url
    try:
        snapshot = fetch_snapshot(symbol, base_url, args.timeout, args.fixture_dir)
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"Opening Bell unavailable: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(snapshot, indent=2, sort_keys=True) if args.json else render(snapshot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
