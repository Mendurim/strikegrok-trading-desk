#!/usr/bin/env python3
"""Fixtures for opening_bell.py: depth bands, floors, and honest failures.

No network. Every case builds the Price Service shapes by hand so the band
arithmetic and the measured/floor distinction are pinned down exactly.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opening_bell  # noqa: E402


def exchange_info(symbol="ETH-USD", status="trading"):
    return {
        "serverTime": 1789047942828,
        "symbols": [
            {
                "symbol": symbol,
                "contractType": "PERPETUAL",
                "status": status,
                "baseAsset": symbol.split("-")[0],
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "liquidationFee": "0.0125",
                "orderType": ["LIMIT", "MARKET", "STOP_MARKET"],
                "timeInForce": ["GTC", "IOC", "FOK"],
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "10"},
                ],
            }
        ],
    }


PREMIUM = {
    "symbol": "ETH-USD",
    "markPrice": "2000.50",
    "indexPrice": "2000.40",
    "fundingRate": "0.0000238",
    "nextFundingTime": 1789048800000,
}

TICKER = {
    "symbol": "ETH-USD",
    "lastPrice": "2000.00",
    "openPrice": "2050.00",
    "highPrice": "2075.00",
    "lowPrice": "1990.00",
    "priceChangePercent": "-2.4390",
    "volume": "1234.5",
    "quoteVolume": "2469000.0",
}

OPEN_INTEREST = {"symbol": "ETH-USD", "openInterest": "500.0"}


def book(levels_per_side=10, step="0.40", size="1", count=None):
    """A two-sided book around a mid of 2000.00.

    `step` sets how far apart the levels are, which is what decides whether a
    given bps band is reached at all.
    """
    n = levels_per_side if count is None else count
    bids = [[str(Decimal("2000") - Decimal(i) * Decimal(step)), size] for i in range(n)]
    asks = [[str(Decimal("2000") + Decimal(i) * Decimal(step)), size] for i in range(n)]
    return {"lastUpdateId": 1462303322, "E": 1789047942828, "T": 1789047942451,
            "bids": bids, "asks": asks}


def snapshot(bk=None, info=None, symbol="ETH-USD"):
    return opening_bell.build_snapshot(
        info or exchange_info(symbol), PREMIUM, TICKER, OPEN_INTEREST,
        bk if bk is not None else book(), symbol, "fixture",
    )


class OpeningBellTest(unittest.TestCase):
    def test_snapshot_is_read_only_and_computes_depth(self):
        snap = snapshot()
        self.assertEqual(snap["mode"], "read-only")
        self.assertIn("No key requested", snap["safety"])
        self.assertIn("No MCP execution tool called", snap["safety"])
        self.assertEqual(snap["market"], "ETH-USD")
        self.assertEqual(Decimal(snap["prices"]["book_mid"]), Decimal("2000"))
        self.assertEqual(Decimal(snap["prices"]["mark"]), Decimal("2000.50"))
        # Funding is hourly on Strike; the annualisation is simple, not compounded.
        self.assertEqual(snap["funding"]["interval_hours"], 1)
        self.assertEqual(
            Decimal(snap["funding"]["annualized_simple_pct"]),
            Decimal("0.0000238") * Decimal(24 * 365 * 100),
        )
        self.assertEqual(snap["funding"]["next_funding_at"], "2026-09-10T14:00:00Z")
        self.assertEqual(
            Decimal(snap["activity"]["open_interest_usd"]),
            Decimal("500.0") * Decimal("2000.50"),
        )

    def test_bands_count_only_levels_inside_them(self):
        # Levels 0.40 apart on a 2000 mid: 2 bps a step. 5 bps is 1.00 in price,
        # so it takes the levels at 0.00, 0.40 and 0.80 - three of them. 10 bps
        # is 2.00 and takes six; 25 bps is 5.00 and takes thirteen.
        depth = snapshot(book(levels_per_side=20, step="0.40", size="1"))["book"]["depth"]
        self.assertEqual(Decimal(depth["5"]["bid_base"]), Decimal("3"))
        self.assertEqual(Decimal(depth["10"]["bid_base"]), Decimal("6"))
        self.assertEqual(Decimal(depth["25"]["bid_base"]), Decimal("13"))

    def test_deep_book_reports_every_band_as_measured(self):
        depth = snapshot(book(levels_per_side=40, step="0.40"))["book"]["depth"]
        for band in ("5", "10", "25"):
            self.assertTrue(depth[band]["bid_complete"], band)
            self.assertTrue(depth[band]["ask_complete"], band)

    def test_thin_book_marks_unreached_bands_as_floors(self):
        # Five levels 0.40 apart reach 1.60, which is 8 bps: 25 bps is not there.
        snap = snapshot(book(levels_per_side=5, step="0.40"))
        depth = snap["book"]["depth"]
        self.assertTrue(depth["5"]["bid_complete"])
        self.assertFalse(depth["25"]["bid_complete"])
        self.assertFalse(depth["25"]["ask_complete"])
        text = opening_bell.render(snap)
        self.assertIn(">=", text)
        self.assertIn("floors, not totals", text)
        self.assertIn("no resting order sits out there", text)

    def test_a_capped_response_is_a_floor_for_a_different_reason(self):
        # A side returned at exactly the cap may continue past the last level,
        # so the band is a floor even though the response reaches it.
        capped = book(levels_per_side=opening_bell.DEPTH_LIMIT, step="0.40", size="1")
        snap = opening_bell.build_snapshot(
            exchange_info(), PREMIUM, TICKER, OPEN_INTEREST, capped, "ETH-USD", "fixture")
        self.assertTrue(snap["book"]["levels_capped"]["bid"])
        self.assertFalse(snap["book"]["depth"]["25"]["bid_complete"])
        self.assertIn("cut off at the cap", opening_bell.render(snap))

    def test_render_labels_sources_and_says_it_is_not_a_signal(self):
        text = opening_bell.render(snapshot())
        self.assertIn("STRIKEGROK OPENING BELL", text)
        self.assertIn("READ ONLY", text)
        self.assertIn("Strike Price Service", text)
        self.assertIn("not a trading signal", text)
        self.assertIn("min notional $10", text)

    def test_unknown_symbol_is_named_before_any_further_request(self):
        with self.assertRaises(ValueError) as caught:
            opening_bell.market_config(exchange_info(), "NOTACOIN")
        self.assertIn("NOTACOIN", str(caught.exception))
        self.assertIn("not a Strike market", str(caught.exception))

    def test_untraded_market_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            snapshot(info=exchange_info(status="halted"))
        self.assertIn("not trading", str(caught.exception))

    def test_empty_side_is_unavailable_not_zero(self):
        one_sided = book()
        one_sided["asks"] = []
        with self.assertRaises(ValueError) as caught:
            snapshot(one_sided)
        message = str(caught.exception)
        self.assertIn("no resting asks", message)
        self.assertIn("unavailable", message)

    def test_rejects_crossed_book(self):
        crossed = book()
        crossed["bids"][0][0] = "2100"
        with self.assertRaises(ValueError):
            snapshot(crossed)

    def test_missing_event_time_is_refused(self):
        timeless = book()
        timeless["E"] = 0
        timeless["T"] = 0
        with self.assertRaises(ValueError) as caught:
            snapshot(timeless)
        self.assertIn("event time", str(caught.exception))

    def test_fixture_cli_path_emits_json(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, payload in (
                ("exchangeInfo.json", exchange_info()),
                ("premiumIndex-ETH-USD.json", PREMIUM),
                ("ticker24hr-ETH-USD.json", TICKER),
                ("openInterest-ETH-USD.json", OPEN_INTEREST),
                ("depth-ETH-USD.json", book()),
            ):
                with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
            out = io.StringIO()
            with redirect_stdout(out):
                code = opening_bell.main(
                    ["--fixture-dir", directory, "--symbol", "ETH-USD", "--json"])
            self.assertEqual(code, 0)
            parsed = json.loads(out.getvalue())
            self.assertEqual(parsed["network"], "fixture")
            self.assertEqual(parsed["market"], "ETH-USD")

    def test_bad_symbol_argument_is_rejected_by_the_parser(self):
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                opening_bell.main(["--symbol", "ETH/USD"])

    def test_testnet_flag_selects_the_testnet_base_url(self):
        self.assertIn("testnet", opening_bell.TESTNET_BASE_URL)
        self.assertNotIn("testnet", opening_bell.DEFAULT_BASE_URL)


if __name__ == "__main__":
    unittest.main()
