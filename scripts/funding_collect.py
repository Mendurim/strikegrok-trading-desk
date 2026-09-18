#!/usr/bin/env python3
"""Accumulate the funding history a rule ranks against, one real observation at a time.

A rule that ranks funding against its own recent distribution needs a history to
rank against - the example rule wants 240 hourly samples, thirty days. This
script collects them.

    funding_collect.py --desk /workspace/trading-desk --markets BTC-USD ETH-USD
    funding_collect.py --status --desk /workspace/trading-desk
    funding_collect.py --rule template/rules/funding-fade-v1.json --status

Why this collects rather than backfills
---------------------------------------
There is no historical funding series in Strike's public API. `/v2/premiumIndex`
returns the rate now; `/v2/klines` offers `last`, `mark` and `index` and no
premium or funding series; there is no `fundingRate` history endpoint. The
account-scoped `/v2/history/funding` records what one account was charged on
markets it actually held, which is neither market-wide nor complete.

Funding could in principle be reconstructed from the premium, but not here:
Strike's published rate does not follow from the instantaneous premium, the
`averagePremiumIndex` that drives it is accumulated inside the funding interval
and is not available historically, and Binance's formula applied to Strike's own
numbers misses the published rate by orders of magnitude. A reconstructed series
would be a guess wearing the costume of an observation, and it would corrupt the
percentile a rule ranks against without anyone being able to see that it had.

So every row this writes is a rate Strike published, read at the time it was
published. The cost is that a thirty-day history takes thirty days. Run it
hourly, on the same clock as funding.

    5 * * * * cd /opt/strikegrok && python3 scripts/funding_collect.py \\
                --desk /workspace/trading-desk >> /var/log/funding_collect.log 2>&1

`autopilot.py scan` already appends one sample per market per run, in the same
format and the same files. This script does the same job for a chosen set of
markets, deduplicates by funding stamp so extra runs cannot inflate the sample
count, and reports how far the history has to go.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

MAINNET = "https://api.strikefinance.org/price"
TESTNET = "https://api-v2-testnet.strikefinance.org/price"
USER_AGENT = "strikegrok-funding-collect/1.0"

# The same three columns autopilot.py writes, so the two can share a file.
COLUMNS = ("at", "rate", "mark")


def fetch(base: str, path: str, timeout: float):
    request = urllib.request.Request(
        f"{base}{path}", headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle) if row.get("at")]


def append_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        if new:
            writer.writeheader()
        writer.writerow(row)


def funding_slot(payload: dict) -> dt.datetime | None:
    """The end of the funding interval this reading belongs to.

    `nextFundingTime` does not move within an interval, so two reads ten minutes
    apart report the same one. That makes it the natural key for "have we
    already sampled this hour".
    """
    stamp = payload.get("nextFundingTime")
    if not stamp:
        return None
    try:
        return dt.datetime.fromtimestamp(int(stamp) / 1000, tz=dt.timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def slot_of(moment: dt.datetime, interval: int = 3600) -> dt.datetime:
    """The funding slot a past reading fell in, recovered from its timestamp.

    Strike charges funding hourly on the hour, so the slot a sample belongs to
    is the next boundary strictly after it was taken. Deriving it this way means
    the stored format stays the three columns autopilot writes - no extra
    column, no chance of the two scripts disagreeing about the schema.
    """
    epoch = int(moment.timestamp())
    return dt.datetime.fromtimestamp(
        (epoch // interval + 1) * interval, tz=dt.timezone.utc)


def parse_at(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def collect(base: str, desk: Path, markets: list[str], timeout: float) -> list[str]:
    lines = []
    for symbol in markets:
        path = desk / "data" / "funding" / f"{symbol}.csv"
        try:
            payload = fetch(base, f"/v2/premiumIndex?symbol={symbol}", timeout)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            # Unavailable is a verdict, not a zero, and not a reason to write a row.
            lines.append(f"  {symbol:12} unavailable    {exc}")
            continue

        rate, mark = payload.get("fundingRate"), payload.get("markPrice")
        if rate in (None, "") or mark in (None, ""):
            lines.append(f"  {symbol:12} unavailable    premiumIndex carried no fundingRate")
            continue

        slot = funding_slot(payload)
        existing = read_history(path)
        if slot and existing:
            # One sample per funding interval, whatever the cron cadence. Extra
            # runs are harmless; they must not inflate the sample count, because
            # the rule reads that count as "thirty days of history".
            last_at = parse_at(existing[-1].get("at", ""))
            if last_at and slot_of(last_at) >= slot:
                lines.append(f"  {symbol:12} already have   this funding interval "
                             f"(to {iso(slot)})")
                continue

        append_row(path, {"at": iso(dt.datetime.now(dt.timezone.utc)),
                          "rate": str(rate), "mark": str(mark)})
        lines.append(f"  {symbol:12} collected      rate {rate} (now {len(existing) + 1} samples)")
    return lines


def status(desk: Path, markets: list[str], needed: int, days: float) -> list[str]:
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=days)
    lines = []
    for symbol in markets:
        rows = read_history(desk / "data" / "funding" / f"{symbol}.csv")
        inside = []
        for row in rows:
            stamp = parse_at(row.get("at", ""))
            if stamp and stamp >= cutoff and row.get("rate"):
                inside.append(stamp)
        have = len(inside)
        if have >= needed:
            lines.append(f"  {symbol:12} {have:>4}/{needed}  ready")
            continue
        short = needed - have
        eta = now + dt.timedelta(hours=short)
        oldest = iso(min(inside)) if inside else "-"
        lines.append(f"  {symbol:12} {have:>4}/{needed}  {short} more; hourly -> ~{iso(eta)}"
                     f"  (oldest {oldest})")
    return lines


def markets_from_rule(path: Path) -> tuple[list[str], int, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return (list(data.get("markets") or []),
            int(data.get("min_funding_samples", 240)),
            float(data.get("funding_history_days", 30)))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--desk", default="/workspace/trading-desk")
    parser.add_argument("--markets", nargs="+", help="symbols to collect, e.g. BTC-USD ETH-USD")
    parser.add_argument("--rule", help="take the markets and thresholds from a rule file")
    parser.add_argument("--network", choices=["mainnet", "testnet"], default="mainnet")
    parser.add_argument("--status", action="store_true", help="report progress, collect nothing")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)

    needed, days = 240, 30.0
    markets = args.markets or []
    if args.rule:
        try:
            from_rule, needed, days = markets_from_rule(Path(args.rule))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"cannot read {args.rule}: {exc}", file=sys.stderr)
            return 1
        markets = markets or from_rule
    if not markets:
        parser.error("give --markets or --rule")

    desk = Path(args.desk).expanduser()
    base = TESTNET if args.network == "testnet" else MAINNET

    if args.status:
        print(f"FUNDING HISTORY | {iso(dt.datetime.now(dt.timezone.utc))} | "
              f"need {needed} samples inside {days:g} days")
        for line in status(desk, markets, needed, days):
            print(line)
        return 0

    print(f"FUNDING COLLECT | {iso(dt.datetime.now(dt.timezone.utc))} | {args.network}")
    lines = collect(base, desk, markets, args.timeout)
    for line in lines:
        print(line)
    failed = sum(1 for line in lines if "unavailable" in line)
    if failed:
        print(f"{failed} of {len(markets)} unavailable this run", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
