#!/usr/bin/env python3
"""Fixtures for funding_collect.py.

No network: the Price Service read is replaced with crafted payloads. What
matters here is that a sample is only ever a rate Strike published, that extra
runs cannot inflate the count, and that a failed read writes nothing.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import funding_collect as fc  # noqa: E402

HOUR = 3600


def payload(rate="0.00001", mark="81000", next_funding=None, **extra):
    if next_funding is None:
        now = dt.datetime.now(dt.timezone.utc)
        next_funding = fc.slot_of(now)
    body = {
        "symbol": "BTC-USD",
        "fundingRate": rate,
        "markPrice": mark,
        "nextFundingTime": int(next_funding.timestamp() * 1000),
    }
    body.update(extra)
    return body


class StubFetch:
    """Stands in for the Price Service. Records what was asked for."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, base, path, timeout):
        self.calls.append(path)
        result = self.responses.pop(0) if isinstance(self.responses, list) else self.responses
        if isinstance(result, Exception):
            raise result
        return result


class SlotArithmetic(unittest.TestCase):
    def test_slot_is_the_next_hour_boundary(self):
        at = dt.datetime(2026, 9, 18, 14, 23, 11, tzinfo=dt.timezone.utc)
        self.assertEqual(fc.slot_of(at),
                         dt.datetime(2026, 9, 18, 15, 0, tzinfo=dt.timezone.utc))

    def test_a_reading_exactly_on_the_hour_belongs_to_the_next_slot(self):
        at = dt.datetime(2026, 9, 18, 14, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(fc.slot_of(at),
                         dt.datetime(2026, 9, 18, 15, 0, tzinfo=dt.timezone.utc))

    def test_two_readings_in_the_same_hour_share_a_slot(self):
        a = dt.datetime(2026, 9, 18, 14, 1, tzinfo=dt.timezone.utc)
        b = dt.datetime(2026, 9, 18, 14, 59, tzinfo=dt.timezone.utc)
        self.assertEqual(fc.slot_of(a), fc.slot_of(b))

    def test_readings_in_different_hours_do_not(self):
        a = dt.datetime(2026, 9, 18, 14, 59, tzinfo=dt.timezone.utc)
        b = dt.datetime(2026, 9, 18, 15, 1, tzinfo=dt.timezone.utc)
        self.assertNotEqual(fc.slot_of(a), fc.slot_of(b))


class Collecting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.desk = Path(self.tmp) / "desk"
        self.original = fc.fetch

    def tearDown(self):
        fc.fetch = self.original

    def csv_path(self, symbol="BTC-USD"):
        return self.desk / "data" / "funding" / f"{symbol}.csv"

    def test_a_reading_is_written_in_the_shared_three_column_format(self):
        fc.fetch = StubFetch(payload(rate="-0.000031", mark="81149.7"))
        fc.collect("base", self.desk, ["BTC-USD"], 5)
        text = self.csv_path().read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(text[0], "at,rate,mark")
        self.assertIn("-0.000031", text[1])
        self.assertIn("81149.7", text[1])
        self.assertEqual(len(text), 2)

    def test_a_second_run_in_the_same_interval_adds_nothing(self):
        fc.fetch = StubFetch(payload())
        fc.collect("base", self.desk, ["BTC-USD"], 5)
        lines = fc.collect("base", self.desk, ["BTC-USD"], 5)
        self.assertIn("already have", lines[0])
        self.assertEqual(len(fc.read_history(self.csv_path())), 1)

    def test_the_next_interval_is_collected(self):
        now = dt.datetime.now(dt.timezone.utc)
        fc.fetch = StubFetch(payload(next_funding=fc.slot_of(now)))
        fc.collect("base", self.desk, ["BTC-USD"], 5)
        # An hour later the venue reports the following slot.
        fc.fetch = StubFetch(payload(rate="0.00002",
                                     next_funding=fc.slot_of(now) + dt.timedelta(hours=1)))
        lines = fc.collect("base", self.desk, ["BTC-USD"], 5)
        self.assertIn("collected", lines[0])
        self.assertEqual(len(fc.read_history(self.csv_path())), 2)

    def test_a_failed_read_writes_no_row(self):
        fc.fetch = StubFetch(OSError("connection reset"))
        lines = fc.collect("base", self.desk, ["BTC-USD"], 5)
        self.assertIn("unavailable", lines[0])
        self.assertFalse(self.csv_path().exists())

    def test_a_payload_with_no_funding_rate_writes_no_row(self):
        body = payload()
        del body["fundingRate"]
        fc.fetch = StubFetch(body)
        lines = fc.collect("base", self.desk, ["BTC-USD"], 5)
        self.assertIn("unavailable", lines[0])
        self.assertFalse(self.csv_path().exists())

    def test_a_missing_next_funding_time_still_collects(self):
        # Without a slot there is nothing to deduplicate against; a sample is
        # better than a gap, and the hourly cron is what bounds the count.
        body = payload()
        del body["nextFundingTime"]
        fc.fetch = StubFetch(body)
        lines = fc.collect("base", self.desk, ["BTC-USD"], 5)
        self.assertIn("collected", lines[0])

    def test_one_market_failing_does_not_stop_the_others(self):
        fc.fetch = StubFetch([OSError("down"), payload(), payload()])
        lines = fc.collect("base", self.desk, ["BTC-USD", "ETH-USD", "SOL-USD"], 5)
        self.assertIn("unavailable", lines[0])
        self.assertIn("collected", lines[1])
        self.assertIn("collected", lines[2])


class Status(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.desk = Path(self.tmp) / "desk"

    def write(self, symbol, stamps):
        path = self.desk / "data" / "funding" / f"{symbol}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            handle.write("at,rate,mark\n")
            for stamp in stamps:
                handle.write(f"{fc.iso(stamp)},0.00001,81000\n")

    def test_counts_only_samples_inside_the_window(self):
        now = dt.datetime.now(dt.timezone.utc)
        self.write("BTC-USD", [now - dt.timedelta(days=40),      # outside 30 days
                               now - dt.timedelta(hours=2),
                               now - dt.timedelta(hours=1)])
        line = fc.status(self.desk, ["BTC-USD"], 240, 30)[0]
        self.assertIn("2/240", line)

    def test_reports_ready_once_the_threshold_is_met(self):
        now = dt.datetime.now(dt.timezone.utc)
        self.write("BTC-USD", [now - dt.timedelta(hours=i) for i in range(5)])
        line = fc.status(self.desk, ["BTC-USD"], 5, 30)[0]
        self.assertIn("ready", line)

    def test_a_market_with_no_history_reports_zero_rather_than_failing(self):
        line = fc.status(self.desk, ["NOPE-USD"], 240, 30)[0]
        self.assertIn("0/240", line)

    def test_an_unparseable_row_is_skipped_not_counted(self):
        path = self.desk / "data" / "funding" / "BTC-USD.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("at,rate,mark\nnot-a-date,0.1,1\n", encoding="utf-8")
        self.assertIn("0/240", fc.status(self.desk, ["BTC-USD"], 240, 30)[0])


class RuleReading(unittest.TestCase):
    def test_markets_and_thresholds_come_from_the_rule(self):
        root = Path(__file__).resolve().parents[1]
        markets, needed, days = fc.markets_from_rule(root / "template/rules/funding-fade-v1.json")
        self.assertEqual(markets, ["BTC-USD", "ETH-USD", "SOL-USD"])
        self.assertEqual(needed, 240)
        self.assertEqual(days, 30)


class Cli(unittest.TestCase):
    def test_status_needs_no_network_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                code = fc.main(["--desk", tmp, "--markets", "BTC-USD", "--status"])
            self.assertEqual(code, 0)
            self.assertIn("0/240", out.getvalue())

    def test_neither_markets_nor_rule_is_an_error(self):
        with self.assertRaises(SystemExit):
            fc.main(["--desk", "/tmp"])

    def test_an_unreadable_rule_exits_one(self):
        self.assertEqual(fc.main(["--desk", "/tmp", "--rule", "/nonexistent.json", "--status"]), 1)


if __name__ == "__main__":
    unittest.main()
