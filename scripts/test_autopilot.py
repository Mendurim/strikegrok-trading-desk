#!/usr/bin/env python3
"""Fixtures for autopilot.py - the desk's runbooks without a language model.

Two kinds of test live here and both matter:

  * the arithmetic, in isolation: rounding onto a tick and a step, ATR and SMA,
    the funding percentile, the liquidity clock's bands, and the stressed-stop
    sizing that decides how much of the account is at risk. These are the
    numbers that reach the exchange, so each one is pinned to a worked example
    that can be checked by hand.

  * the whole fire, against the REAL policy layer. The headline test builds a
    desk with a genuine Ed25519 key and a genuinely signed register, runs
    `monitor` end to end against a fake price service, and then hands the
    proposal and the request body autopilot wrote to `desk_policy.Policy` - the
    same object `strike_request.py` calls before it signs. If the PASS block
    autopilot writes and the gate that reads it ever drift apart, that is the
    test that fails, and it fails here rather than at 03:00 with money on it.

No network: public data comes from fixture files, and the signer is a stub that
runs the real policy and answers like the venue would.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot  # noqa: E402
import desk_policy  # noqa: E402

D = Decimal
RULES_MD = "# funding-fade-v1 v3\nfade funding above the 95th percentile of its own 30 days\n"

RULE = {
    "name": "funding-fade-v1",
    "version": 3,
    "markets": ["ETH-USD"],
    "timeframe_seconds": 14400,
    "klines_interval": "4h",
    "condition": {"funding_pct30d": [95, 100], "price_vs_sma20": "above"},
    "direction": "fade",
    "entry": "market",
    "stop_atr_multiple": 1.5,
    "take_profit_atr_multiple": 3.0,
    "slippage_bps": 10,
    "fee_bps_round_turn": 10,
    "max_spread_bps": 10,
    "funding_position": "pays",
}


def quiet(fn, *a, **kw):
    with redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


def ms(stamp: dt.datetime) -> int:
    return int(stamp.timestamp() * 1000)


def bars_fixture(count: int, last_close_at: dt.datetime, *, step_seconds: int = 14400,
                 start: Decimal = D("2400"), drift: Decimal = D("1"),
                 half_range: Decimal = D("20")) -> list:
    """Candles whose true range is exactly 2 x half_range on every bar, so ATR20
    is a number the test can state rather than discover."""
    rows = []
    for i in range(count):
        close_at = last_close_at - dt.timedelta(seconds=step_seconds * (count - 1 - i))
        close = start + drift * i
        rows.append([ms(close_at - dt.timedelta(seconds=step_seconds)),
                     str(close), str(close + half_range), str(close - half_range), str(close),
                     "100", ms(close_at), "0", 0, "0", "0", "0"])
    return rows


class Fixtures:
    """A desk on disk, a fake price service, and a stub signer that runs the
    real policy layer."""

    def __init__(self, root: str, *, mark=D("2450"), funding=D("0.0088"), equity=D("10000"),
                 positions=None, open_orders=None, bar_age_seconds: int = 60,
                 funding_samples: int = 300, spread: Decimal = D("0.5")):
        self.root = Path(root)
        self.desk = self.root / "desk-root"
        self.prices = self.root / "prices"
        self.state = self.root / "policy-state"
        self.mark = mark
        self.equity = equity
        self.positions = positions if positions is not None else []
        self.open_orders = open_orders if open_orders is not None else []
        for sub in ("desk", "proposals", "signals", "journal/incidents/open", "data",
                    "watch", "strategies/funding-fade-v1"):
            (self.desk / sub).mkdir(parents=True, exist_ok=True)
        self.prices.mkdir(parents=True, exist_ok=True)
        (self.desk / "strategies/funding-fade-v1/RULES.md").write_text(RULES_MD)

        self.key = self.root / "user-signing"
        quiet(desk_policy.keygen, self.key)
        (self.desk / "desk/user-signing.pub").write_bytes(
            Path(str(self.key) + ".pub").read_bytes())
        self.write_register()
        self.write_equity()

        last_close = autopilot.now() - dt.timedelta(seconds=bar_age_seconds)
        self.last_close_at = last_close
        self.write_price_fixtures(last_close, funding=funding, spread=spread)
        self.write_funding_history(funding_samples)

        self.rule_path = self.root / "funding-fade-v1.json"
        self.rule_path.write_text(json.dumps(RULE))
        self.stub = self.root / "stub_signer.py"
        self.stub.write_text(STUB)
        self.sent_log = self.root / "sent.jsonl"

    # -- desk files --------------------------------------------------------
    def write_register(self, version=1, **overrides):
        approval = {
            "id": "SA-03", "rule": "funding-fade-v1", "rule_version": 3,
            "rules_sha256": hashlib.sha256(RULES_MD.encode()).hexdigest(),
            "markets": ["ETH-USD"], "sides": ["long", "short"],
            "risk_per_trade": 0.0025, "max_notional": 1500, "max_open": 2,
            "max_leverage": 5, "bar_seconds": 14400, "hours_utc": None,
            "granted": dt.date.today().isoformat(),
            "expires": (dt.date.today() + dt.timedelta(days=30)).isoformat(),
            "kill": {"consecutive_losses": 3},
        }
        approval.update(overrides)
        path = self.desk / "desk/standing-approvals.json"
        path.write_text(json.dumps({"version": version, "signed_at": autopilot.iso(autopilot.now()),
                                    "approvals": [approval]}))
        quiet(desk_policy.sign_file, self.key, path)

    def write_equity(self, equity=None, age_seconds: int = 30):
        stamp = autopilot.now() - dt.timedelta(seconds=age_seconds)
        (self.desk / "desk/equity.json").write_text(json.dumps(
            {"equity": str(equity if equity is not None else self.equity),
             "at": autopilot.iso(stamp)}))

    def write_funding_history(self, samples: int, rate: str = "0.0001",
                              symbol: str = "ETH-USD"):
        at = autopilot.now()
        rows = ["at,rate,mark"]
        for i in range(samples):
            stamp = at - dt.timedelta(hours=samples - i)
            rows.append(f"{autopilot.iso(stamp)},{rate},2400")
        path = self.desk / f"data/funding/{symbol}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(rows) + "\n")

    # -- the fake price service -------------------------------------------
    def write_price_fixtures(self, last_close, *, funding=D("0.0088"), spread=D("0.5")):
        write = lambda name, payload: (self.prices / name).write_text(json.dumps(payload))
        write("exchangeInfo.json", {"symbols": [
            {"symbol": "ETH-USD", "status": "trading", "contractType": "PERPETUAL",
             "baseAsset": "ETH", "quoteAsset": "USD",
             "orderType": ["LIMIT", "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT",
                           "TAKE_PROFIT_MARKET"],
             "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01",
                          "minPrice": "39.9", "maxPrice": "306177"},
                         {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001",
                          "maxQty": "10000"},
                         {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.01",
                          "minQty": "0.01", "maxQty": "2000"},
                         {"filterType": "MIN_NOTIONAL", "notional": "10"}]},
            # SOL publishes no MARKET_LOT_SIZE, so the lot filter is the only bound.
            {"symbol": "SOL-USD", "status": "trading",
             "orderType": ["LIMIT", "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT",
                           "TAKE_PROFIT_MARKET"],
             "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
                {"filterType": "MIN_NOTIONAL", "notional": "10"}]}]})
        write("klines-ETH-USD-4h.json", bars_fixture(60, last_close))
        write("klines-ETH-USD-1d.json", bars_fixture(40, last_close, step_seconds=86400))
        write("klines-SOL-USD-1d.json", bars_fixture(40, last_close, step_seconds=86400))
        write("klines-SOL-USD-4h.json",
              bars_fixture(60, last_close, start=D("140"), drift=D("0.1"), half_range=D("2")))
        write("premiumIndex-ETH-USD.json", {
            "symbol": "ETH-USD", "markPrice": str(self.mark), "indexPrice": str(self.mark),
            "fundingRate": str(funding),
            "nextFundingTime": ms(autopilot.now() + dt.timedelta(minutes=30))})
        write("premiumIndex-SOL-USD.json", {
            "symbol": "SOL-USD", "markPrice": "150", "indexPrice": "150",
            "fundingRate": "0.00001",
            "nextFundingTime": ms(autopilot.now() + dt.timedelta(minutes=30))})
        for symbol, last in (("ETH-USD", self.mark), ("SOL-USD", D("150"))):
            write(f"ticker24hr-{symbol}.json", {
                "symbol": symbol, "lastPrice": str(last), "openPrice": str(last),
                "highPrice": str(last + 10), "lowPrice": str(last - 10),
                "priceChangePercent": "0.4", "volume": "1000", "quoteVolume": "1000000"})
            write(f"openInterest-{symbol}.json", {"symbol": symbol, "openInterest": "5000"})
        half = spread / 2
        write("depth-ETH-USD.json", {
            "lastUpdateId": 1, "E": ms(autopilot.now()),
            "bids": [[str(self.mark - half), "5"], [str(self.mark - 2), "5"],
                     [str(self.mark - 3), "5"], [str(self.mark - 10), "5"]],
            "asks": [[str(self.mark + half), "5"], [str(self.mark + 2), "5"],
                     [str(self.mark + 3), "5"], [str(self.mark + 10), "5"]]})
        write("depth-SOL-USD.json", {
            "lastUpdateId": 1, "E": ms(autopilot.now()),
            "bids": [["149.95", "50"], ["149", "50"]], "asks": [["150.05", "50"], ["151", "50"]]})

    # -- wiring ------------------------------------------------------------
    def env(self):
        return {"STRIKEGROK_DESK": str(self.desk),
                "STRIKEGROK_POLICY_STATE": str(self.state),
                "STRIKEGROK_STATE_TRUSTED": "1",
                "STUB_EQUITY": str(self.equity),
                "STUB_POSITIONS": json.dumps(self.positions),
                "STUB_OPEN_ORDERS": json.dumps(self.open_orders),
                "STUB_LOG": str(self.sent_log)}

    def install(self, test: unittest.TestCase):
        """Point autopilot at this desk, this price service and this signer."""
        autopilot._cache.clear()
        autopilot.DESK = self.desk
        autopilot.FIXTURES = str(self.prices)
        autopilot.SIGNER_CMD = ""
        autopilot.STRIKE_REQUEST = str(self.stub)
        autopilot.SIGNER_PYTHON = sys.executable
        for key, value in self.env().items():
            os.environ[key] = value
        test.addCleanup(self.restore)

    def restore(self):
        autopilot._cache.clear()
        autopilot.FIXTURES = None
        for key in self.env():
            os.environ.pop(key, None)

    def run_cli(self, *argv):
        return autopilot.main(["--desk", str(self.desk), "--network", "testnet", *argv])

    # -- reading the result -----------------------------------------------
    def proposals(self):
        return sorted(p for p in (self.desk / "proposals").iterdir() if p.suffix == ".md")

    def bodies(self):
        return sorted(p for p in (self.desk / "proposals").iterdir() if p.suffix == ".json")

    def sends(self):
        if not self.sent_log.exists():
            return []
        return [json.loads(line) for line in self.sent_log.read_text().splitlines() if line.strip()]

    def signals(self):
        path = self.desk / "signals" / f"{autopilot.now().date().isoformat()}.md"
        return path.read_text() if path.exists() else ""

    def journal(self):
        path = self.desk / "journal" / f"{autopilot.now().date().isoformat()}.md"
        return path.read_text() if path.exists() else ""

    def incidents(self):
        return sorted(p.name for p in (self.desk / "journal/incidents/open").iterdir())

    def policy(self, equity=None, positions=None, resting=None, fills=None):
        return desk_policy.Policy(
            account_reader=lambda: float(equity if equity is not None else self.equity),
            positions_reader=lambda: list(positions or []),
            open_orders_reader=lambda: list(resting or []),
            fills_reader=(lambda symbol, n: list(fills)) if fills is not None else None,
            desk=self.desk)


# The stub signer. It is `strike_request.py` with the network removed: it runs
# the REAL policy layer, so a body that would be refused in production is
# refused here, and only the exchange's answer is invented.
STUB = '''#!/usr/bin/env python3
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, %r)
from desk_policy import Policy, PolicyRefusal

method, path = sys.argv[1], sys.argv[2]
body = {}
if "--body-file" in sys.argv:
    body = json.loads(sys.stdin.read() or "{}")

def policy():
    return Policy(account_reader=lambda: float(os.environ["STUB_EQUITY"]),
                  positions_reader=lambda: json.loads(os.environ.get("STUB_POSITIONS", "[]")),
                  open_orders_reader=lambda: json.loads(os.environ.get("STUB_OPEN_ORDERS", "[]")),
                  fills_reader=lambda symbol, n: [])

if method == "GET":
    policy().check("GET", path, {})
    if path == "/v2/account":
        print(json.dumps({"equity": os.environ["STUB_EQUITY"]}))
    elif path == "/v2/positions":
        print(os.environ.get("STUB_POSITIONS", "[]"))
    elif path == "/v2/openOrders":
        print(os.environ.get("STUB_OPEN_ORDERS", "[]"))
    else:
        print("{}")
    sys.exit(0)

pol = policy()
try:
    decision = pol.check(method, path, body)
except PolicyRefusal as exc:
    print("POLICY REFUSED: %%s" %% exc, file=sys.stderr)
    sys.exit(3)
pol.commit(body)
with open(os.environ["STUB_LOG"], "a") as handle:
    handle.write(json.dumps({"method": method, "path": path, "body": body,
                             "tier": decision.get("tier")}) + "\\n")
if os.environ.get("STUB_VENUE") == "unknown":
    sys.exit(2)
if os.environ.get("STUB_VENUE") == "reject":
    print(json.dumps({"code": -2010, "msg": "rejected by the venue"}))
    print("HTTP 400", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"order_id": 991, "client_order_id": body.get("client_order_id"),
                  "status": "NEW"}))
''' % os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# The arithmetic. Every number below can be checked with a pencil.
# ---------------------------------------------------------------------------
class TestRounding(unittest.TestCase):
    def test_a_size_rounds_down_onto_the_step(self):
        self.assertEqual(autopilot.quantize_down(D("0.38502"), D("0.01")), D("0.38"))
        self.assertEqual(autopilot.quantize_down(D("4899.9"), D("1")), D("4899"))

    def test_a_size_already_on_the_step_is_unchanged(self):
        self.assertEqual(autopilot.quantize_down(D("0.38"), D("0.01")), D("0.38"))

    def test_a_size_under_one_step_rounds_to_zero_rather_than_up(self):
        self.assertEqual(autopilot.quantize_down(D("0.9"), D("1")), D("0"))

    def test_a_short_stop_rounds_up_and_a_long_stop_rounds_down(self):
        self.assertEqual(autopilot.quantize_to_tick(D("2510.004"), D("0.01"), up=True), D("2510.01"))
        self.assertEqual(autopilot.quantize_to_tick(D("2510.004"), D("0.01"), up=False), D("2510.00"))

    def test_a_zero_tick_leaves_the_price_alone(self):
        self.assertEqual(autopilot.quantize_to_tick(D("2510.004"), D("0"), up=True), D("2510.004"))

    def test_plain_never_renders_an_exponent(self):
        self.assertEqual(autopilot.plain(D("0.00010")), "0.0001")
        self.assertEqual(autopilot.plain(D("1E+3")), "1000")
        self.assertEqual(autopilot.plain(D("0.000000")), "0")

    def test_plain_survives_the_round_trip_the_policy_layer_makes(self):
        for value in ("0.38", "2510.01", "1000", "0.0001"):
            self.assertEqual(D(autopilot.plain(D(value))), D(value))


class TestIndicators(unittest.TestCase):
    def test_sma_is_the_mean_of_the_last_n_closes(self):
        closes = [D(x) for x in range(1, 41)]
        self.assertEqual(autopilot.sma(closes, 20), D("30.5"))

    def test_sma_refuses_rather_than_averaging_what_it_has(self):
        with self.assertRaises(autopilot.Blind):
            autopilot.sma([D(1), D(2)], 20)

    def test_atr_is_the_mean_true_range_over_completed_bars(self):
        bars = [{"high": D(110), "low": D(90), "close": D(100)} for _ in range(21)]
        self.assertEqual(autopilot.atr(bars, 20), D(20))

    def test_atr_uses_the_previous_close_when_the_bar_gaps(self):
        bars = [{"high": D(100), "low": D(100), "close": D(100)},
                {"high": D(150), "low": D(140), "close": D(145)}]
        self.assertEqual(autopilot.atr(bars, 1), D(50))   # 150 - 100, not 150 - 140

    def test_atr_refuses_without_one_bar_more_than_the_period(self):
        with self.assertRaises(autopilot.Blind):
            autopilot.atr([{"high": D(1), "low": D(0), "close": D(1)}] * 20, 20)

    def test_percentile_counts_ties_as_half(self):
        history = [D(1), D(2), D(3), D(4)]
        self.assertEqual(autopilot.percentile_rank(history, D(5)), D(100))
        self.assertEqual(autopilot.percentile_rank(history, D(0)), D(0))
        self.assertEqual(autopilot.percentile_rank(history, D(2)), D("37.5"))

    def test_percentile_refuses_on_an_empty_history(self):
        with self.assertRaises(autopilot.Blind):
            autopilot.percentile_rank([], D(1))

    def test_median_handles_both_parities_and_refuses_on_nothing(self):
        self.assertEqual(autopilot.median([D(3), D(1), D(2)]), D(2))
        self.assertEqual(autopilot.median([D(4), D(1), D(2), D(3)]), D("2.5"))
        with self.assertRaises(autopilot.Blind):
            autopilot.median([])


class TestTimeAndCandles(unittest.TestCase):
    def test_a_timestamp_without_a_timezone_is_refused(self):
        with self.assertRaises(autopilot.Blind):
            autopilot.parse_iso("2026-09-16T12:00:00", "test")

    def test_z_and_offset_forms_both_parse(self):
        self.assertEqual(autopilot.parse_iso("2026-09-16T12:00:00Z", "t"),
                         autopilot.parse_iso("2026-09-16T14:00:00+02:00", "t"))

    def test_the_bar_being_printed_is_not_a_closed_bar(self):
        at = autopilot.now()
        bars = [{"close_at": at - dt.timedelta(hours=8)}, {"close_at": at - dt.timedelta(hours=4)},
                {"close_at": at + dt.timedelta(hours=1)}]
        self.assertEqual(len(autopilot.closed_bars(bars, at)), 2)

    def test_a_dict_shaped_candle_parses_like_an_array_shaped_one(self):
        at = autopilot.now().replace(microsecond=0)
        row = {"openTime": ms(at - dt.timedelta(hours=4)), "closeTime": ms(at),
               "open": "1", "high": "2", "low": "0.5", "close": "1.5"}
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "klines-X-4h.json").write_text(json.dumps([row]))
            autopilot.FIXTURES = tmp
            try:
                bars = autopilot.klines("base", "X", "4h", 1)
            finally:
                autopilot.FIXTURES = None
        self.assertEqual(bars[0]["high"], D("2"))
        self.assertEqual(bars[0]["close_at"], at)

    def test_a_candle_shape_the_script_does_not_know_is_blind(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "klines-X-4h.json").write_text(json.dumps([["only", "two"]]))
            autopilot.FIXTURES = tmp
            try:
                with self.assertRaises(autopilot.Blind):
                    autopilot.klines("base", "X", "4h", 1)
            finally:
                autopilot.FIXTURES = None


class TestBook(unittest.TestCase):
    def book(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "depth-X.json").write_text(json.dumps(payload))
            autopilot.FIXTURES = tmp
            try:
                return autopilot.book("base", "X")
            finally:
                autopilot.FIXTURES = None

    def test_a_one_sided_book_is_unavailable_not_a_zero(self):
        with self.assertRaises(autopilot.Blind) as caught:
            self.book({"bids": [["100", "1"]], "asks": []})
        self.assertIn("no resting asks", str(caught.exception))

    def test_a_crossed_book_is_refused(self):
        with self.assertRaises(autopilot.Blind):
            self.book({"bids": [["101", "1"]], "asks": [["100", "1"]]})

    def test_depth_within_a_band_counts_only_the_levels_inside_it(self):
        book = self.book({"bids": [["99.95", "5"], ["99.00", "7"]],
                          "asks": [["100.05", "5"], ["101.00", "7"]]})
        self.assertEqual(book["mid"], D("100"))
        self.assertEqual(autopilot.depth_within(book["bids"], book["mid"], 10), D(5))
        self.assertEqual(autopilot.depth_within(book["bids"], book["mid"], 200), D(12))
        self.assertEqual(autopilot.reach_bps(book["bids"], book["mid"]), D(100))
        self.assertEqual(book["spread_bps"], D(10))


class TestRuleValidation(unittest.TestCase):
    def rule(self, **overrides):
        data = dict(RULE)
        data.update(overrides)
        return autopilot.Rule(data)

    def test_the_example_rule_loads(self):
        self.assertEqual(self.rule().signal, "funding-fade-v1@3")

    def test_an_unknown_key_is_an_error_not_a_shrug(self):
        with self.assertRaises(ValueError) as caught:
            self.rule(leverage=20)
        self.assertIn("unknown keys", str(caught.exception))

    def test_an_unknown_condition_key_is_an_error(self):
        with self.assertRaises(ValueError):
            self.rule(condition={"rsi14": [70, 100]})

    def test_a_missing_key_is_an_error(self):
        data = dict(RULE)
        del data["stop_atr_multiple"]
        with self.assertRaises(ValueError):
            autopilot.Rule(data)

    def test_a_limit_entry_is_refused_until_something_supervises_it(self):
        with self.assertRaises(ValueError) as caught:
            self.rule(entry="limit")
        self.assertIn("Tier 2", str(caught.exception))

    def test_a_name_the_policy_layer_cannot_parse_is_refused(self):
        with self.assertRaises(ValueError):
            self.rule(name="funding fade v1")

    def test_a_percentile_band_outside_zero_to_a_hundred_is_refused(self):
        with self.assertRaises(ValueError):
            self.rule(condition={"funding_pct30d": [95, 120]})

    def test_a_backwards_percentile_band_is_refused(self):
        with self.assertRaises(ValueError):
            self.rule(condition={"funding_pct30d": [100, 95]})

    def test_a_timeframe_longer_than_a_day_is_refused(self):
        with self.assertRaises(ValueError):
            self.rule(timeframe_seconds=172800)

    def test_a_negative_stop_multiple_is_refused(self):
        with self.assertRaises(ValueError):
            self.rule(stop_atr_multiple=0)


class TestRuleEvaluation(unittest.TestCase):
    def setUp(self):
        self.rule = autopilot.Rule(dict(RULE))
        self.bars = []
        rows = bars_fixture(40, autopilot.now())
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "klines-X-4h.json").write_text(json.dumps(rows))
            autopilot.FIXTURES = tmp
            try:
                self.bars = autopilot.klines("base", "X", "4h", 40)
            finally:
                autopilot.FIXTURES = None
        self.history = [D("0.0001")] * 300

    def test_a_fire_needs_every_condition(self):
        reading = self.rule.evaluate(self.bars, D("0.0088"), self.history)
        self.assertTrue(reading["fired"])
        self.assertEqual(reading["funding_pct30d"], D(100))

    def test_funding_inside_its_own_range_does_not_fire(self):
        reading = self.rule.evaluate(self.bars, D("0.0001"), self.history)
        self.assertFalse(reading["fired"])

    def test_price_below_the_mean_does_not_fire(self):
        falling = list(reversed([dict(bar) for bar in self.bars]))
        for index, bar in enumerate(falling):
            bar["close_at"] = self.bars[index]["close_at"]
        reading = self.rule.evaluate(falling, D("0.0088"), self.history)
        self.assertFalse(reading["fired"])

    def test_too_little_funding_history_is_blind_not_not_fired(self):
        with self.assertRaises(autopilot.Blind):
            self.rule.evaluate(self.bars, D("0.0088"), [D("0.0001")] * 10)

    def test_a_fade_of_crowded_longs_is_a_short(self):
        self.assertEqual(self.rule.side({}), "sell")

    def test_a_fade_of_crowded_shorts_is_a_long(self):
        rule = autopilot.Rule({**RULE, "condition": {"funding_pct30d": [0, 5]}})
        self.assertEqual(rule.side({}), "buy")

    def test_following_takes_the_crowded_side(self):
        rule = autopilot.Rule({**RULE, "direction": "follow"})
        self.assertEqual(rule.side({}), "buy")

    def test_an_explicit_direction_ignores_the_crowd(self):
        self.assertEqual(autopilot.Rule({**RULE, "direction": "long"}).side({}), "buy")
        self.assertEqual(autopilot.Rule({**RULE, "direction": "short"}).side({}), "sell")


class TestSizing(unittest.TestCase):
    """The worked example, by hand:

        risk      10000 x 0.25%                       = 25.00
        stop      2450 + 1.5 x 40                     = 2510
        slip      2450 x 10bps                        = 2.45      -> stressed 2512.45
        fees      (2450 + 2512.45) x 5bps             = 2.481225
        per unit  62.45 + 2.481225                    = 64.931225
        size      25 / 64.931225 = 0.38502 -> step    = 0.38
        notional  0.38 x 2450                         = 931
        nominal   60 x 0.38                           = 22.80  (under the stated 25)
    """

    def size(self, **overrides):
        args = dict(equity=D("10000"), risk_fraction=D("0.0025"), entry=D("2450"), side="sell",
                    atr_value=D("40"), rule=autopilot.Rule(dict(RULE)), tick=D("0.01"),
                    step=D("0.01"))
        args.update(overrides)
        return autopilot.size_ticket(**args)

    def test_the_worked_example(self):
        out = self.size()
        self.assertEqual(out["risk_usd"], D("25.0000"))
        self.assertEqual(out["stop"], D("2510"))
        self.assertEqual(out["slip"], D("2.45"))
        self.assertEqual(out["stressed_stop"], D("2512.45"))
        self.assertEqual(out["fee_per_unit"], D("2.481225"))
        self.assertEqual(out["per_unit"], D("64.931225"))
        self.assertEqual(out["size"], D("0.38"))
        self.assertEqual(out["notional"], D("931.00"))
        self.assertEqual(out["take_profit"], D("2330"))
        self.assertEqual(out["nominal_loss"], D("22.80"))

    def test_the_stressed_stop_always_sizes_smaller_than_the_nominal_one(self):
        out = self.size()
        self.assertLess(out["nominal_loss"], out["risk_usd"])

    def test_a_long_puts_the_stop_below_the_entry_and_the_target_above(self):
        out = self.size(side="buy")
        self.assertEqual(out["stop"], D("2390"))
        self.assertEqual(out["stressed_stop"], D("2387.55"))
        self.assertEqual(out["take_profit"], D("2570"))

    def test_a_coarse_step_rounds_the_size_down_and_never_up(self):
        out = self.size(step=D("1"), atr_value=D("0.4"), entry=D("10"), tick=D("0.001"))
        self.assertEqual(out["size"], out["size"].to_integral_value())
        self.assertLessEqual(out["nominal_loss"], out["risk_usd"])

    def test_an_account_too_small_for_one_step_refuses_rather_than_rounding_up(self):
        with self.assertRaises(autopilot.Held) as caught:
            self.size(equity=D("100"), step=D("1"))
        self.assertIn("less than one step", str(caught.exception))

    def test_a_tick_wider_than_the_stop_distance_pushes_the_stop_out_not_in(self):
        """And the position that comes back is enormous, which is the point.

        When the stop distance is smaller than one tick the stop lands a whole
        tick away, the stressed loss per unit collapses, and the risk budget
        buys a position worth far more than the account should hold. Sizing
        does not refuse that - it is arithmetically correct - so the notional
        caps are what catch it. This test states both halves.
        """
        out = self.size(atr_value=D("0.001"), tick=D("1"))
        self.assertEqual(out["stop"], D("2451"))
        self.assertGreater(out["stop"], D("2450"))
        self.assertLessEqual(out["nominal_loss"], out["risk_usd"])
        self.assertGreater(out["notional"], D("1500"))   # the SA cap refuses it
        self.assertGreater(out["notional"], autopilot.CEILING_NOTIONAL)

    def test_a_zero_atr_refuses(self):
        with self.assertRaises(autopilot.Held):
            self.size(atr_value=D("0"))

    def test_a_rule_without_a_target_leaves_the_take_profit_unset(self):
        rule = dict(RULE)
        del rule["take_profit_atr_multiple"]
        self.assertIsNone(self.size(rule=autopilot.Rule(rule))["take_profit"])


class TestClocks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.desk = autopilot.Desk(Path(self._tmp.name))
        self.rule = autopilot.Rule(dict(RULE))

    # -- funding -----------------------------------------------------------
    def premium(self, rate, minutes=30):
        return {"funding": D(rate), "mark": D("2450"), "index": D("2450"),
                "next_funding_at": autopilot.now() + dt.timedelta(minutes=minutes)}

    def test_a_position_that_receives_funding_always_passes(self):
        clock = autopilot.funding_clock(self.premium("0.0088"), "sell", self.rule)
        self.assertTrue(clock["pass"])
        self.assertFalse(clock["pays"])

    def test_a_rule_written_to_earn_funding_refuses_a_position_that_pays(self):
        rule = autopilot.Rule({**RULE, "funding_position": "earns"})
        clock = autopilot.funding_clock(self.premium("0.0088"), "buy", rule)
        self.assertFalse(clock["pass"])

    def test_a_position_that_pays_inside_the_clearance_is_held(self):
        rule = autopilot.Rule({**RULE, "funding_position": "either"})
        clock = autopilot.funding_clock(self.premium("0.0088", minutes=5), "buy", rule)
        self.assertFalse(clock["pass"])
        self.assertIn("clearance", clock["why"])

    def test_a_position_that_pays_with_time_to_spare_passes(self):
        rule = autopilot.Rule({**RULE, "funding_position": "either"})
        self.assertTrue(autopilot.funding_clock(self.premium("0.0088", minutes=30), "buy", rule)["pass"])

    def test_a_rule_that_pays_by_design_is_not_stopped_by_the_clock(self):
        clock = autopilot.funding_clock(self.premium("0.0088", minutes=1), "buy", self.rule)
        self.assertTrue(clock["pass"])

    def test_an_unknown_next_charge_holds_a_paying_position(self):
        rule = autopilot.Rule({**RULE, "funding_position": "either"})
        premium = {**self.premium("0.0088"), "next_funding_at": None}
        self.assertFalse(autopilot.funding_clock(premium, "buy", rule)["pass"])

    # -- catalyst ----------------------------------------------------------
    def blackout(self, start_minutes, end_minutes, symbol="ETH-USD"):
        at = autopilot.now()
        (self.desk.root / "desk").mkdir(parents=True, exist_ok=True)
        self.desk.blackouts_file.write_text(json.dumps([{
            "symbol": symbol, "reason": "FOMC",
            "start": autopilot.iso(at + dt.timedelta(minutes=start_minutes)),
            "end": autopilot.iso(at + dt.timedelta(minutes=end_minutes))}]))

    def test_no_calendar_file_means_no_known_entry(self):
        self.assertTrue(autopilot.catalyst_clock(self.desk, "ETH-USD")["pass"])

    def test_a_window_in_force_holds(self):
        self.blackout(-10, 10)
        self.assertFalse(autopilot.catalyst_clock(self.desk, "ETH-USD")["pass"])

    def test_a_window_opening_inside_the_hour_holds(self):
        self.blackout(30, 90)
        self.assertFalse(autopilot.catalyst_clock(self.desk, "ETH-USD")["pass"])

    def test_a_window_tomorrow_does_not_hold(self):
        self.blackout(1440, 1500)
        self.assertTrue(autopilot.catalyst_clock(self.desk, "ETH-USD")["pass"])

    def test_a_window_on_another_market_does_not_hold_this_one(self):
        self.blackout(-10, 10, symbol="BTC-USD")
        self.assertTrue(autopilot.catalyst_clock(self.desk, "ETH-USD")["pass"])

    def test_a_wildcard_window_holds_every_market(self):
        self.blackout(-10, 10, symbol="*")
        self.assertFalse(autopilot.catalyst_clock(self.desk, "ETH-USD")["pass"])

    def test_an_unreadable_calendar_holds_rather_than_passing(self):
        (self.desk.root / "desk").mkdir(parents=True, exist_ok=True)
        self.desk.blackouts_file.write_text("{ not json")
        with self.assertRaises(autopilot.Held):
            autopilot.catalyst_clock(self.desk, "ETH-USD")

    # -- liquidity ---------------------------------------------------------
    def book(self, depth="5", spread=D("0.5"), reach=D("10")):
        mid = D("2450")
        return {"bids": [[mid - spread / 2, D(depth)], [mid - reach, D("5")]],
                "asks": [[mid + spread / 2, D(depth)], [mid + reach, D("5")]],
                "mid": mid, "best_bid": mid - spread / 2, "best_ask": mid + spread / 2,
                "spread_bps": spread / mid * D(10_000), "capped": False}

    def test_a_book_that_can_take_five_times_the_ticket_passes(self):
        clock = autopilot.liquidity_clock(self.desk, "ETH-USD", "sell", D("931"), self.rule,
                                          self.book())
        self.assertTrue(clock["pass"], clock["why"])
        self.assertEqual(clock["side_taken"], "bids")

    def test_a_short_is_measured_against_the_bids(self):
        book = self.book()
        book["bids"] = [[D("2449.75"), D("0.001")], [D("2440"), D("5")]]
        self.assertFalse(autopilot.liquidity_clock(
            self.desk, "ETH-USD", "sell", D("931"), self.rule, book)["pass"])
        self.assertTrue(autopilot.liquidity_clock(
            self.desk, "ETH-USD", "buy", D("931"), self.rule, book)["pass"])

    def test_a_wide_spread_holds(self):
        clock = autopilot.liquidity_clock(self.desk, "ETH-USD", "sell", D("931"), self.rule,
                                          self.book(spread=D("10")))
        self.assertFalse(clock["pass"])
        self.assertIn("spread", clock["why"])

    def test_a_book_that_does_not_reach_twenty_five_bps_holds(self):
        clock = autopilot.liquidity_clock(self.desk, "ETH-USD", "sell", D("931"), self.rule,
                                          self.book(reach=D("1")))
        self.assertFalse(clock["pass"])
        self.assertIn("reaches", clock["why"])

    def test_the_seven_day_median_binds_once_there_is_enough_history(self):
        for i in range(30):
            self.desk.append_history("depth", "ETH-USD", {
                "at": autopilot.iso(autopilot.now() - dt.timedelta(hours=i)),
                "bid_10bps_usd": "50000", "spread_bps": "1"})
        clock = autopilot.liquidity_clock(self.desk, "ETH-USD", "sell", D("931"), self.rule,
                                          self.book(spread=D("0.5")))
        self.assertFalse(clock["pass"])
        self.assertIn("7d median", clock["why"])

    def test_a_ticket_too_big_for_the_band_holds(self):
        clock = autopilot.liquidity_clock(self.desk, "ETH-USD", "sell", D("100000"), self.rule,
                                          self.book())
        self.assertFalse(clock["pass"])
        self.assertIn("depth within", clock["why"])


class TestEquity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.desk = autopilot.Desk(Path(self._tmp.name))
        (self.desk.root / "desk").mkdir(parents=True, exist_ok=True)

    def snapshot(self, equity="10000", age=30):
        self.desk.equity_file.write_text(json.dumps(
            {"equity": equity,
             "at": autopilot.iso(autopilot.now() - dt.timedelta(seconds=age))}))

    def test_a_fresh_snapshot_sizes_when_there_is_no_signer(self):
        self.snapshot()
        equity, source = autopilot.equity_now(self.desk, None)
        self.assertEqual(equity, D("10000"))
        self.assertIn("snapshot", source)

    def test_a_stale_snapshot_refuses_to_size_anything(self):
        self.snapshot(age=3600)
        with self.assertRaises(autopilot.Held) as caught:
            autopilot.equity_now(self.desk, None)
        self.assertIn("cron is not running", str(caught.exception))

    def test_a_missing_snapshot_refuses(self):
        with self.assertRaises(autopilot.Held):
            autopilot.equity_now(self.desk, None)

    def test_a_zero_snapshot_refuses(self):
        self.snapshot(equity="0")
        with self.assertRaises(autopilot.Held):
            autopilot.equity_now(self.desk, None)

    def test_the_account_read_wins_over_the_snapshot(self):
        self.snapshot(equity="10000")

        class FakeSigner:
            def equity(self):
                return D("12345")

        equity, source = autopilot.equity_now(self.desk, FakeSigner())
        self.assertEqual(equity, D("12345"))
        self.assertEqual(source, "account read")


class TestTicketsAndState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.desk = autopilot.Desk(Path(self._tmp.name))
        self.desk.proposals_dir.mkdir(parents=True, exist_ok=True)

    def test_the_first_ticket_of_the_day_is_01(self):
        self.assertTrue(autopilot.next_ticket(self.desk).endswith("-01"))

    def test_a_ticket_id_a_human_took_is_not_reused(self):
        day = autopilot.now().strftime("%Y%m%d")
        (self.desk.proposals_dir / f"SG-{day}-01.md").write_text("taken by hand")
        self.assertEqual(autopilot.next_ticket(self.desk), f"SG-{day}-02")

    def test_a_gap_in_the_sequence_is_filled_rather_than_skipped(self):
        day = autopilot.now().strftime("%Y%m%d")
        for number in (1, 3):
            (self.desk.proposals_dir / f"SG-{day}-{number:02d}.md").write_text("x")
        self.assertEqual(autopilot.next_ticket(self.desk), f"SG-{day}-02")

    def test_yesterdays_tickets_do_not_crowd_today(self):
        yesterday = (autopilot.now() - dt.timedelta(days=1)).strftime("%Y%m%d")
        (self.desk.proposals_dir / f"SG-{yesterday}-01.md").write_text("x")
        self.assertTrue(autopilot.next_ticket(self.desk).endswith("-01"))

    def test_an_id_this_script_issued_is_not_handed_back_if_the_file_is_deleted(self):
        day = autopilot.now().strftime("%Y%m%d")
        state = {"issued": [f"SG-{day}-01", f"SG-{day}-02"]}
        self.assertEqual(autopilot.next_ticket(self.desk, state), f"SG-{day}-03")

    def test_an_id_issued_last_week_does_not_crowd_today(self):
        old_day = (autopilot.now() - dt.timedelta(days=7)).strftime("%Y%m%d")
        state = {"issued": [f"SG-{old_day}-01"]}
        self.assertTrue(autopilot.next_ticket(self.desk, state).endswith("-01"))

    def test_writing_over_an_existing_proposal_is_refused(self):
        autopilot.write_proposal(self.desk, "SG-20260916-01", "first")
        with self.assertRaises(autopilot.Held):
            autopilot.write_proposal(self.desk, "SG-20260916-01", "second")

    def test_state_survives_a_round_trip_and_prunes_what_is_old(self):
        self.desk.save_state({"fired": {"a": {"at": autopilot.iso(autopilot.now())}}})
        self.assertIn("a", self.desk.state()["fired"])
        old = {"old": {"at": autopilot.iso(autopilot.now() - dt.timedelta(days=30))},
               "new": {"at": autopilot.iso(autopilot.now())}}
        self.assertEqual(set(autopilot.prune(old)), {"new"})

    def test_a_suspension_is_appended_and_never_duplicated(self):
        (self.desk.root / "desk").mkdir(parents=True, exist_ok=True)
        self.desk.suspend_sa("SA-03", "blind")
        self.desk.suspend_sa("SA-03", "blind")
        entries = json.loads(self.desk.suspend_file.read_text())
        self.assertEqual(len(entries), 1)
        self.assertEqual(autopilot.suspended_ids(self.desk), {"SA-03"})

    def test_an_unreadable_suspension_file_is_not_permission_to_trade(self):
        (self.desk.root / "desk").mkdir(parents=True, exist_ok=True)
        self.desk.suspend_file.write_text("{ not json")
        with self.assertRaises(autopilot.Held):
            autopilot.suspended_ids(self.desk)

    def test_history_round_trips_and_the_window_is_respected(self):
        for days, rate in ((40, "0.9"), (1, "0.1")):
            self.desk.append_history("funding", "ETH-USD", {
                "at": autopilot.iso(autopilot.now() - dt.timedelta(days=days)), "rate": rate})
        self.assertEqual(autopilot.recent(self.desk.history("funding", "ETH-USD"), "rate", 30,
                                          autopilot.now()), [D("0.1")])


# ---------------------------------------------------------------------------
# The whole fire, against the real policy layer.
# ---------------------------------------------------------------------------
class FireCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = Fixtures(self._tmp.name)
        self.fx.install(self)

    def run_monitor(self, *extra):
        return quiet(self.fx.run_cli, "monitor", "--rule", str(self.fx.rule_path), *extra)

    def gate(self, body=None, policy=None):
        """Hand what autopilot wrote to the object strike_request.py calls."""
        body = body if body is not None else json.loads(self.fx.bodies()[0].read_text())
        return (policy or self.fx.policy()).check("POST", "/v2/order/strategy", body)


class TestTheRehearsal(FireCase):
    def test_a_dry_run_writes_a_proposal_the_real_policy_layer_accepts(self):
        self.assertEqual(self.run_monitor("--dry"), 0)
        self.assertEqual(len(self.fx.proposals()), 1)
        record = self.gate()
        self.assertEqual(record["tier"], "tier1")
        self.assertEqual(record["sa"], "SA-03")

    def test_a_dry_run_sends_nothing(self):
        self.run_monitor("--dry")
        self.assertEqual(self.fx.sends(), [])
        self.assertIn("DRY", self.fx.journal())

    def test_the_preview_is_the_exact_request_body(self):
        self.run_monitor("--dry")
        body = json.loads(self.fx.bodies()[0].read_text())
        self.assertEqual(body["type"], "market")
        self.assertEqual(body["side"], "sell")
        self.assertEqual(body["size"], "0.38")
        self.assertEqual(body["slippage"], "0.001")
        self.assertNotIn("price", body)
        self.assertNotIn("leverage", body)
        self.assertEqual(body["sl_order"]["stop_price"], "2510")
        self.assertTrue(body["sl_order"]["reduce_only"])
        self.assertEqual(body["sl_order"]["working_type"], "mark_price")
        self.assertEqual(body["sl_order"]["size"], body["size"])
        self.assertEqual(body["tp_order"]["stop_price"], "2330")
        # market-on-trigger, not the limit forms: a stop that might not fill is
        # not protection, and the venue lists both
        self.assertEqual(body["sl_order"]["type"], "stop_market")
        self.assertEqual(body["tp_order"]["type"], "take_profit_market")
        self.assertTrue(body["tp_order"]["reduce_only"])

    def test_the_pass_block_carries_every_field_the_gate_reads(self):
        self.run_monitor("--dry")
        text = self.fx.proposals()[0].read_text()
        ticket = self.fx.proposals()[0].stem
        fields = desk_policy.pass_block_fields(text, ticket)
        for key in ("symbol", "side", "size", "order_type", "ref_price", "stop_price",
                    "risk_usd", "equity", "liquidity", "sa", "signal", "fired_at", "expires_at"):
            self.assertIn(key, fields, f"the PASS block has no {key}:")
        self.assertEqual(fields["signal"], "funding-fade-v1@3")
        self.assertEqual(fields["liquidity"], "pass")
        self.assertEqual(fields["order_type"], "market")

    def test_the_block_and_the_body_agree_digit_for_digit(self):
        self.run_monitor("--dry")
        body = json.loads(self.fx.bodies()[0].read_text())
        fields = desk_policy.pass_block_fields(self.fx.proposals()[0].read_text(),
                                               self.fx.proposals()[0].stem)
        self.assertEqual(fields["size"], body["size"])
        self.assertEqual(fields["stop_price"], body["sl_order"]["stop_price"])
        self.assertEqual(fields["symbol"], body["symbol"])
        self.assertEqual(fields["side"], body["side"])

    def test_a_size_changed_after_the_block_was_written_is_refused(self):
        self.run_monitor("--dry")
        body = json.loads(self.fx.bodies()[0].read_text())
        body["size"] = "3.8"
        body["sl_order"]["size"] = "3.8"
        with self.assertRaises(desk_policy.PolicyRefusal) as caught:
            self.gate(body)
        self.assertIn("size", str(caught.exception))

    def test_a_stop_removed_after_the_block_was_written_is_refused(self):
        self.run_monitor("--dry")
        body = json.loads(self.fx.bodies()[0].read_text())
        del body["sl_order"]
        with self.assertRaises(desk_policy.PolicyRefusal) as caught:
            self.gate(body)
        self.assertIn("protection", str(caught.exception))

    def test_the_outcome_heading_closes_the_block_rather_than_adding_fields(self):
        self.run_monitor("--dry")
        ticket = self.fx.proposals()[0].stem
        before = desk_policy.pass_block_fields(self.fx.proposals()[0].read_text(), ticket)
        autopilot.close_proposal(autopilot.Desk(self.fx.desk), ticket,
                                 "expired unfilled; size: 999; sa: SA-99")
        after = desk_policy.pass_block_fields(self.fx.proposals()[0].read_text(), ticket)
        self.assertEqual(before, after)


class TestTheLiveFire(FireCase):
    def test_one_send_goes_through_the_gate_at_tier_one(self):
        self.assertEqual(self.run_monitor(), 0)
        sends = self.fx.sends()
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["path"], "/v2/order/strategy")
        self.assertEqual(sends[0]["tier"], "tier1")
        self.assertEqual(sends[0]["body"]["size"], "0.38")

    def test_the_journal_records_the_send_and_the_read_back(self):
        self.run_monitor()
        journal = self.fx.journal()
        self.assertIn("SEND", journal)
        self.assertIn("ACCEPTED", journal)
        self.assertIn("RULE FIRED", self.fx.signals())

    def test_the_day_is_opened_by_the_signers_own_account_read(self):
        self.run_monitor()
        sod = json.loads((self.fx.state / "sod_equity.json").read_text())
        self.assertEqual(sod["date"], autopilot.now().date().isoformat())
        self.assertEqual(sod["equity"], 10000.0)

    def test_the_same_bar_is_never_fired_twice(self):
        self.run_monitor()
        self.run_monitor()
        self.assertEqual(len(self.fx.sends()), 1)
        self.assertEqual(len(self.fx.proposals()), 1)

    def test_a_hold_that_clears_inside_the_bar_is_tried_again(self):
        self.fx.write_price_fixtures(self.fx.last_close_at, spread=D("20"))   # too wide
        self.assertEqual(self.run_monitor(), 2)
        self.assertEqual(self.fx.sends(), [])
        self.fx.write_price_fixtures(self.fx.last_close_at)                   # the book recovers
        autopilot._cache.clear()
        self.assertEqual(self.run_monitor(), 0)
        self.assertEqual(len([s for s in self.fx.sends() if s["path"] == "/v2/order/strategy"]), 1)

    def test_a_bar_that_was_sent_is_never_tried_again(self):
        self.run_monitor()
        for _ in range(3):
            self.run_monitor()
        self.assertEqual(len([s for s in self.fx.sends() if s["path"] == "/v2/order/strategy"]), 1)

    def test_a_deleted_proposal_does_not_hand_its_ticket_id_back(self):
        self.run_monitor("--dry")
        first = self.fx.proposals()[0].stem
        for path in self.fx.proposals() + self.fx.bodies():
            path.unlink()
        # a fresh bar, so the rule fires again
        self.fx.write_price_fixtures(autopilot.now() - dt.timedelta(seconds=30))
        autopilot._cache.clear()
        self.run_monitor("--dry")
        self.assertNotEqual(self.fx.proposals()[0].stem, first)

    def test_a_venue_rejection_is_journalled_without_an_incident(self):
        os.environ["STUB_VENUE"] = "reject"
        self.addCleanup(lambda: os.environ.pop("STUB_VENUE", None))
        self.run_monitor()
        self.assertIn("REJECTED", self.fx.journal())
        self.assertEqual(self.fx.incidents(), [])

    def test_a_send_with_no_readable_answer_files_an_incident(self):
        os.environ["STUB_VENUE"] = "unknown"
        self.addCleanup(lambda: os.environ.pop("STUB_VENUE", None))
        self.run_monitor()
        self.assertTrue(any("unknown-send-result" in name for name in self.fx.incidents()))
        self.assertIn("UNKNOWN", self.fx.journal())

    def test_a_policy_refusal_files_an_incident_and_does_not_retry(self):
        # The policy layer suspended the SA in its own state - somewhere this
        # script cannot see, which is exactly the case the incident path is for.
        policy = self.fx.policy()
        with desk_policy.State(policy.state_dir) as st:
            policy._suspend(st, "SA-03", "suspended inside policy state", 1)
        self.assertEqual(self.run_monitor(), 3)
        self.assertTrue(any("policy-refusal" in name for name in self.fx.incidents()))
        self.assertIn("REFUSED", self.fx.signals())
        self.assertEqual([s for s in self.fx.sends() if s["path"] == "/v2/order/strategy"], [])

    def test_a_refusal_is_not_tried_again_on_the_next_run(self):
        policy = self.fx.policy()
        with desk_policy.State(policy.state_dir) as st:
            policy._suspend(st, "SA-03", "suspended inside policy state", 1)
        self.run_monitor()
        second = self.run_monitor()
        self.assertEqual([s for s in self.fx.sends() if s["path"] == "/v2/order/strategy"], [])
        self.assertEqual(second, 0)   # the bar is already recorded; nothing is attempted again


class TestTheHolds(FireCase):
    """Every bound the gate would refuse for, caught on this side of it."""

    def assert_held(self, fragment):
        code = self.run_monitor()
        self.assertEqual(code, 2)
        self.assertEqual(self.fx.sends(), [])
        self.assertEqual(self.fx.proposals(), [])
        self.assertIn(fragment, self.fx.journal())

    def test_a_stale_fire_is_not_sent(self):
        self.fx.write_price_fixtures(autopilot.now() - dt.timedelta(hours=2))
        self.assert_held("stale")

    def test_an_open_incident_stops_everything(self):
        (self.fx.desk / "journal/incidents/open/2026-ticket.md").write_text("open")
        self.assert_held("open incident")

    def test_a_suspended_standing_approval_stops_the_rule(self):
        autopilot.Desk(self.fx.desk).suspend_sa("SA-03", "under review")
        self.assert_held("suspended")

    def test_a_changed_rules_file_holds_instead_of_burning_the_approval(self):
        (self.fx.desk / "strategies/funding-fade-v1/RULES.md").write_text("# edited\n")
        self.assert_held("does not match the hash")

    def test_a_notional_over_the_standing_approval_cap_holds(self):
        self.fx.write_register(max_notional=100)
        self.assert_held("notional")

    def test_a_ticket_under_the_venue_minimum_holds(self):
        self.fx.write_register(risk_per_trade=0.0000001)
        self.assert_held("less than one step")

    def test_a_thin_book_holds(self):
        self.fx.write_price_fixtures(self.fx.last_close_at, spread=D("20"))
        self.assert_held("liquidity clock")

    def test_a_calendar_entry_inside_the_hour_holds(self):
        at = autopilot.now()
        (self.fx.desk / "desk/blackouts.json").write_text(json.dumps([{
            "symbol": "ETH-USD", "reason": "FOMC", "start": autopilot.iso(at),
            "end": autopilot.iso(at + dt.timedelta(hours=2))}]))
        self.assert_held("catalyst clock")

    def test_a_register_with_no_approval_for_this_rule_holds(self):
        self.fx.write_register(rule="something-else")
        self.assert_held("no standing approval")

    def test_an_expired_register_entry_holds(self):
        self.fx.write_register(expires=(dt.date.today() - dt.timedelta(days=1)).isoformat())
        self.assert_held("expired")

    def test_a_register_signed_for_a_different_bar_holds(self):
        self.fx.write_register(bar_seconds=3600)
        self.assert_held("the register and the rule disagree")

    def test_a_side_the_approval_does_not_permit_holds(self):
        self.fx.write_register(sides=["long"])
        self.assert_held("does not permit a sell")

    def test_outside_the_approvals_hours_holds(self):
        hour = autopilot.now().hour
        self.fx.write_register(hours_utc=[(hour + 2) % 24, (hour + 3) % 24])
        self.assert_held("outside")

    def test_a_third_open_position_holds(self):
        os.environ["STUB_POSITIONS"] = json.dumps(
            [{"symbol": f"X{i}-USD", "size": "1"} for i in range(3)])
        self.assert_held("positions already open")

    def test_exposure_the_approval_already_holds_on_this_market_holds(self):
        os.environ["STUB_POSITIONS"] = json.dumps([{"symbol": "ETH-USD", "size": "1"}])
        self.assert_held("already has exposure")

    def test_a_stale_equity_snapshot_with_no_signer_holds(self):
        self.fx.write_equity(age_seconds=7200)
        code = quiet(self.fx.run_cli, "monitor", "--rule", str(self.fx.rule_path),
                     "--no-signer", "--dry")
        self.assertEqual(code, 2)
        self.assertEqual(self.fx.proposals(), [])

    def test_a_day_already_down_against_this_scripts_own_mark_holds(self):
        desk = autopilot.Desk(self.fx.desk)
        state = desk.state()
        state["sod"] = {"date": autopilot.now().date().isoformat(), "equity": "11000"}
        desk.save_state(state)
        self.assert_held("down against this script's own opening mark")


class TestBlindMonitors(FireCase):
    def test_a_read_that_came_back_short_is_could_not_tell_not_not_fired(self):
        self.fx.write_funding_history(10)
        self.assertEqual(self.run_monitor(), 2)
        log = (self.fx.desk / "watch/rule-funding-fade-v1/log").read_text()
        self.assertIn("could_not_tell", log)
        self.assertNotIn("not_fired", log)
        self.assertIn("WATCH COULD NOT TELL", self.fx.signals())

    def test_two_blind_bars_suspend_the_standing_approval(self):
        self.fx.write_funding_history(10)
        self.run_monitor()
        self.assertEqual(autopilot.suspended_ids(autopilot.Desk(self.fx.desk)), set())
        self.run_monitor()
        self.assertEqual(autopilot.suspended_ids(autopilot.Desk(self.fx.desk)), {"SA-03"})

    def test_a_reading_that_works_again_clears_the_blind_count(self):
        self.fx.write_funding_history(10)
        self.run_monitor()
        self.fx.write_funding_history(300)
        self.run_monitor()
        self.assertEqual(autopilot.Desk(self.fx.desk).state()["blind"], {})

    def test_a_book_that_goes_one_sided_after_the_fire_holds(self):
        # the rule evaluates from candles and funding; the book is only read
        # once the fire is real, so this failure lands mid-fire
        book = json.loads((self.fx.prices / "depth-ETH-USD.json").read_text())
        book["asks"] = []
        (self.fx.prices / "depth-ETH-USD.json").write_text(json.dumps(book))
        self.assertEqual(self.run_monitor(), 2)
        self.assertEqual(self.fx.sends(), [])
        self.assertEqual(self.fx.proposals(), [])
        self.assertIn("no resting asks", self.fx.journal())

    def test_the_watch_log_prints_a_readable_percentile(self):
        self.run_monitor()
        log = (self.fx.desk / "watch/rule-funding-fade-v1/log").read_text()
        self.assertIn("funding_pct30d=100", log)
        self.assertNotIn("E+", log)

    def test_a_missing_price_feed_is_blind_rather_than_an_exception(self):
        (self.fx.prices / "premiumIndex-ETH-USD.json").unlink()
        self.assertEqual(self.run_monitor(), 2)
        self.assertIn("could_not_tell", (self.fx.desk / "watch/rule-funding-fade-v1/log").read_text())


class TestTwoMarkets(FireCase):
    def setUp(self):
        super().setUp()
        self.fx.write_register(markets=["ETH-USD", "SOL-USD"], max_open=2)
        self.fx.write_funding_history(300, symbol="SOL-USD")
        (self.fx.prices / "premiumIndex-SOL-USD.json").write_text(json.dumps({
            "symbol": "SOL-USD", "markPrice": "150", "indexPrice": "150",
            "fundingRate": "0.0088",
            "nextFundingTime": ms(autopilot.now() + dt.timedelta(minutes=30))}))
        self.rule_path = self.fx.root / "two-markets.json"
        self.rule_path.write_text(json.dumps({**RULE, "markets": ["ETH-USD", "SOL-USD"]}))

    def run_monitor(self, *extra):
        return quiet(self.fx.run_cli, "monitor", "--rule", str(self.rule_path), *extra)

    def test_both_markets_fire_but_only_one_order_is_sent(self):
        self.assertEqual(self.run_monitor(), 0)
        orders = [s for s in self.fx.sends() if s["path"] == "/v2/order/strategy"]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["body"]["symbol"], "ETH-USD")
        self.assertIn("DEFER", self.fx.journal())

    def test_the_deferred_market_goes_next_run_once_the_pace_floor_has_passed(self):
        self.run_monitor()
        desk = autopilot.Desk(self.fx.desk)
        state = desk.state()
        state["last_send_at"] = autopilot.iso(autopilot.now() - dt.timedelta(minutes=10))
        desk.save_state(state)
        # the policy layer keeps its own 60s floor, in a file this script cannot write
        (self.fx.state / "pace.json").write_text(json.dumps(
            {"last_open_at": autopilot.iso(autopilot.now() - dt.timedelta(minutes=10))}))
        self.run_monitor()
        orders = [s for s in self.fx.sends() if s["path"] == "/v2/order/strategy"]
        self.assertEqual(len(orders), 2)
        self.assertEqual(orders[1]["body"]["symbol"], "SOL-USD")

    def test_the_pace_floor_holds_a_second_send_in_the_same_minute(self):
        self.run_monitor()
        self.run_monitor()   # nothing rewound: the pacing preflight must hold SOL
        orders = [s for s in self.fx.sends() if s["path"] == "/v2/order/strategy"]
        self.assertEqual(len(orders), 1)
        self.assertIn("pacing floor", self.fx.journal())


class TestSweep(FireCase):
    def ticket_is_open(self, expires_minutes, resting=True, positions=None):
        desk = autopilot.Desk(self.fx.desk)
        state = desk.state()
        state["open_tickets"] = [{"ticket": "SG-20260916-01", "coid": "SG-20260916-01-entry",
                                  "symbol": "ETH-USD", "at": autopilot.iso(autopilot.now()),
                                  "expires_at": autopilot.iso(
                                      autopilot.now() + dt.timedelta(minutes=expires_minutes))}]
        desk.save_state(state)
        os.environ["STUB_OPEN_ORDERS"] = json.dumps(
            [{"client_order_id": "SG-20260916-01-entry", "symbol": "ETH-USD", "order_id": 991,
              "reduce_only": False}] if resting else [])
        os.environ["STUB_POSITIONS"] = json.dumps(positions or [])
        autopilot.write_proposal(desk, "SG-20260916-01", "# SG-20260916-01\nopened\n")

    def sweep(self):
        return quiet(self.fx.run_cli, "sweep")

    def test_an_entry_still_resting_past_its_bar_is_cancelled(self):
        self.ticket_is_open(-5)
        self.assertEqual(self.sweep(), 0)
        cancels = [s for s in self.fx.sends() if s["path"] == "/v2/order/cancel"]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0]["tier"], "tier0")
        self.assertEqual(cancels[0]["body"], {"order_id": 991, "symbol": "ETH-USD"})
        self.assertIn("RULE STALE", self.fx.signals())

    def test_an_entry_inside_its_bar_is_left_alone(self):
        self.ticket_is_open(30)
        self.sweep()
        self.assertEqual([s for s in self.fx.sends() if s["path"] == "/v2/order/cancel"], [])
        self.assertEqual(len(autopilot.Desk(self.fx.desk).state()["open_tickets"]), 1)

    def test_a_ticket_that_filled_leaves_the_sweep_list_without_a_cancel(self):
        self.ticket_is_open(-5, resting=False, positions=[{"symbol": "ETH-USD", "size": "0.38"}])
        self.sweep()
        self.assertEqual([s for s in self.fx.sends() if s["path"] == "/v2/order/cancel"], [])
        self.assertEqual(autopilot.Desk(self.fx.desk).state()["open_tickets"], [])
        self.assertIn("FILLED", self.fx.journal())

    def test_a_ticket_with_nothing_behind_it_is_closed_after_its_expiry(self):
        self.ticket_is_open(-5, resting=False)
        self.sweep()
        self.assertEqual(autopilot.Desk(self.fx.desk).state()["open_tickets"], [])
        self.assertIn("GONE", self.fx.journal())

    def test_sweeping_never_touches_an_order_this_script_did_not_record(self):
        desk = autopilot.Desk(self.fx.desk)
        desk.save_state({"open_tickets": []})
        os.environ["STUB_OPEN_ORDERS"] = json.dumps(
            [{"client_order_id": "SG-20260101-07-entry", "symbol": "BTC-USD", "order_id": 5,
              "reduce_only": False}])
        self.sweep()
        self.assertEqual([s for s in self.fx.sends() if s["path"] == "/v2/order/cancel"], [])


class TestScan(FireCase):
    def scan(self, *markets):
        argv = ["scan"]
        for market in markets:
            argv += ["--market", market]
        return quiet(self.fx.run_cli, *argv)

    def test_a_funding_extreme_is_flagged_and_written_to_the_signals_file(self):
        self.assertEqual(self.scan("ETH-USD"), 0)
        signals = self.fx.signals()
        self.assertIn("SCAN |", signals)
        self.assertIn("ETH-USD", signals)
        self.assertIn("funding", signals)

    def test_the_scan_never_speaks_in_directions(self):
        self.scan("ETH-USD")
        for word in (" buy", " sell", " long", " short", "bullish", "bearish"):
            self.assertNotIn(word, self.fx.signals().lower())

    def test_a_quiet_market_says_so_rather_than_saying_nothing(self):
        self.assertEqual(self.scan("SOL-USD"), 0)
        self.assertIn("nothing flagged", self.fx.signals())

    def test_a_market_that_could_not_be_read_is_listed_not_dropped(self):
        self.scan("BTC-USD")
        self.assertIn("unavailable:", self.fx.signals())
        self.assertIn("BTC-USD", self.fx.signals())

    def test_the_scan_appends_the_history_the_monitors_read(self):
        self.scan("ETH-USD")
        desk = autopilot.Desk(self.fx.desk)
        self.assertEqual(desk.history("funding", "ETH-USD")[-1]["rate"], "0.0088")
        self.assertTrue(desk.history("oi", "ETH-USD"))
        self.assertTrue(desk.history("depth", "ETH-USD"))

    def test_the_scan_signs_nothing(self):
        self.scan("ETH-USD")
        self.assertEqual(self.fx.sends(), [])


class TestNoSideChannel(FireCase):
    def test_autopilot_writes_nothing_into_the_policys_state_directory(self):
        quiet(self.fx.run_cli, "--no-signer", "monitor", "--rule", str(self.fx.rule_path), "--dry")
        self.assertFalse(self.fx.state.exists(),
                         "autopilot must never create or write the policy state directory")
        self.assertTrue((self.fx.desk / "watch/autopilot/state.json").exists())

    def test_the_flags_work_before_and_after_the_verb(self):
        first = quiet(self.fx.run_cli, "--no-signer", "monitor", "--rule",
                      str(self.fx.rule_path), "--dry")
        self.assertEqual(first, 0)
        second = quiet(self.fx.run_cli, "monitor", "--rule", str(self.fx.rule_path),
                       "--dry", "--no-signer")
        self.assertEqual(second, 0)

    def test_the_network_chosen_before_the_verb_survives_the_subcommand(self):
        parsed = []
        original = autopilot.cmd_scan
        autopilot.cmd_scan = lambda args: parsed.append(args.network) or 0
        self.addCleanup(lambda: setattr(autopilot, "cmd_scan", original))
        autopilot.main(["--network", "mainnet", "scan"])
        autopilot.main(["scan", "--network", "mainnet"])
        self.assertEqual(parsed, ["mainnet", "mainnet"])


class TestSelfCheck(unittest.TestCase):
    """The last gate before the send, which is this script checking its own work.

    Each case below is a mutation the real policy layer would refuse. Catching
    them here is what keeps a refusal meaningful: a refusal should mean the desk
    learned something, not that this script wrote a ticket it could not send.
    """

    def pair(self):
        fields = {"symbol": "ETH-USD", "side": "sell", "size": "0.38", "order_type": "market",
                  "ref_price": "2450", "stop_price": "2510", "risk_usd": "25.00",
                  "equity": "10000.00", "liquidity": "pass", "sa": "SA-03",
                  "signal": "funding-fade-v1@3"}
        body = {"client_order_id": "SG-20260916-01-entry", "symbol": "ETH-USD", "side": "sell",
                "type": "market", "size": "0.38", "slippage": "0.001",
                "sl_order": {"type": "stop", "size": "0.38", "stop_price": "2510",
                             "working_type": "mark_price", "reduce_only": True}}
        return fields, body

    def assert_caught(self, fragment, mutate):
        fields, body = self.pair()
        mutate(fields, body)
        with self.assertRaises(autopilot.Held) as caught:
            autopilot.self_check(fields, body)
        self.assertIn("autopilot defect", str(caught.exception))
        self.assertIn(fragment, str(caught.exception))

    def test_a_matching_pair_passes(self):
        fields, body = self.pair()
        autopilot.self_check(fields, body)

    def test_a_size_that_drifted_is_caught(self):
        self.assert_caught("size", lambda f, b: b.__setitem__("size", "3.8"))

    def test_a_symbol_that_drifted_is_caught(self):
        self.assert_caught("symbol", lambda f, b: b.__setitem__("symbol", "BTC-USD"))

    def test_a_side_that_drifted_is_caught(self):
        self.assert_caught("side", lambda f, b: b.__setitem__("side", "buy"))

    def test_a_missing_stop_is_caught(self):
        self.assert_caught("no sl_order", lambda f, b: b.pop("sl_order"))

    def test_a_stop_that_is_not_reduce_only_is_caught(self):
        self.assert_caught("not reduce-only",
                           lambda f, b: b["sl_order"].__setitem__("reduce_only", False))

    def test_a_stop_on_the_last_price_is_caught(self):
        self.assert_caught("mark_price",
                           lambda f, b: b["sl_order"].__setitem__("working_type", "contract_price"))

    def test_a_stop_that_covers_less_than_the_entry_is_caught(self):
        self.assert_caught("sl_order size", lambda f, b: b["sl_order"].__setitem__("size", "0.19"))

    def test_a_short_stop_below_the_entry_is_caught(self):
        self.assert_caught("triggers immediately", lambda f, b: (
            f.__setitem__("stop_price", "2400"), b["sl_order"].__setitem__("stop_price", "2400")))

    def test_a_long_stop_above_the_entry_is_caught(self):
        self.assert_caught("triggers immediately", lambda f, b: (
            b.__setitem__("side", "buy"), f.__setitem__("side", "buy")))

    def test_a_price_on_a_market_order_is_caught(self):
        self.assert_caught("no price", lambda f, b: b.__setitem__("price", "2450"))

    def test_a_market_order_with_no_slippage_bound_is_caught(self):
        self.assert_caught("slippage bound", lambda f, b: b.pop("slippage"))

    def test_a_client_order_id_with_no_ticket_is_caught(self):
        self.assert_caught("SG- ticket id",
                           lambda f, b: b.__setitem__("client_order_id", "abc-entry"))

    def test_a_stop_that_loses_more_than_the_ticket_said_is_caught(self):
        self.assert_caught("against a stated risk_usd",
                           lambda f, b: f.__setitem__("risk_usd", "1.00"))

    def test_a_leverage_the_block_did_not_state_is_caught(self):
        self.assert_caught("leverage", lambda f, b: b.__setitem__("leverage", "20"))


class TestVenueLimits(unittest.TestCase):
    """exchangeInfo says what the venue will reject. Read it before sending."""

    def limits(self, **overrides):
        base = {"tick": D("0.1"), "step": D("0.001"), "lot_filter": "MARKET_LOT_SIZE",
                "min_qty": D("0.001"), "max_qty": D("2000"), "min_price": D("39.9"),
                "max_price": D("306177"), "min_notional": D("10"),
                "order_types": ["LIMIT", "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT",
                                "TAKE_PROFIT_MARKET"]}
        base.update(overrides)
        return base

    def sizing(self, **overrides):
        base = {"size": D("0.38"), "stop": D("2510"), "take_profit": D("2330")}
        base.update(overrides)
        return base

    def test_a_ticket_inside_every_filter_passes(self):
        self.assertEqual(autopilot.venue_limits(self.limits(), self.sizing(),
                                                autopilot.Rule(dict(RULE)), D("2450")), [])

    def test_a_size_under_the_minimum_quantity_is_caught(self):
        out = autopilot.venue_limits(self.limits(min_qty=D("1")), self.sizing(),
                                     autopilot.Rule(dict(RULE)), D("2450"))
        self.assertIn("minQty", out[0])

    def test_a_size_over_the_market_maximum_is_caught(self):
        out = autopilot.venue_limits(self.limits(max_qty=D("0.1")), self.sizing(),
                                     autopilot.Rule(dict(RULE)), D("2450"))
        self.assertIn("maxQty", out[0])
        self.assertIn("MARKET_LOT_SIZE", out[0])

    def test_a_stop_outside_the_price_filter_is_caught(self):
        out = autopilot.venue_limits(self.limits(max_price=D("2500")), self.sizing(),
                                     autopilot.Rule(dict(RULE)), D("2450"))
        self.assertTrue(any("maxPrice" in reason for reason in out))

    def test_a_leg_type_the_venue_does_not_list_is_caught(self):
        out = autopilot.venue_limits(self.limits(order_types=["LIMIT", "MARKET"]), self.sizing(),
                                     autopilot.Rule(dict(RULE)), D("2450"))
        self.assertIn("does not list order type STOP_MARKET", out[0])

    def test_a_rule_may_choose_the_legs_the_venue_offers(self):
        rule = autopilot.Rule({**RULE, "sl_order_type": "stop",
                               "tp_order_type": "take_profit"})
        self.assertEqual(autopilot.venue_limits(self.limits(), self.sizing(), rule, D("2450")), [])
        self.assertEqual(rule.sl_order_type, "stop")

    def test_the_default_leg_is_market_on_trigger(self):
        rule = autopilot.Rule(dict(RULE))
        self.assertEqual(rule.sl_order_type, "stop_market")
        self.assertEqual(rule.tp_order_type, "take_profit_market")


class TestConstraintsFromExchangeInfo(FireCase):
    def test_the_market_lot_filter_wins_where_the_venue_publishes_one(self):
        limits = autopilot.constraints("base", "ETH-USD")
        self.assertEqual(limits["lot_filter"], "MARKET_LOT_SIZE")
        self.assertEqual(limits["step"], D("0.01"))
        self.assertEqual(limits["min_qty"], D("0.01"))

    def test_the_lot_filter_is_used_where_there_is_no_market_one(self):
        limits = autopilot.constraints("base", "SOL-USD")
        self.assertEqual(limits["lot_filter"], "LOT_SIZE")
        self.assertEqual(limits["step"], D("0.1"))

    def test_a_market_that_is_not_trading_is_blind(self):
        info = json.loads((self.fx.prices / "exchangeInfo.json").read_text())
        info["symbols"][0]["status"] = "halted"
        (self.fx.prices / "exchangeInfo.json").write_text(json.dumps(info))
        autopilot._cache.clear()
        with self.assertRaises(autopilot.Blind):
            autopilot.constraints("base", "ETH-USD")

    def test_an_exchange_info_with_no_order_types_is_blind(self):
        info = json.loads((self.fx.prices / "exchangeInfo.json").read_text())
        del info["symbols"][0]["orderType"]
        (self.fx.prices / "exchangeInfo.json").write_text(json.dumps(info))
        autopilot._cache.clear()
        with self.assertRaises(autopilot.Blind):
            autopilot.constraints("base", "ETH-USD")

    def test_a_size_under_the_venue_minimum_holds_the_whole_fire(self):
        info = json.loads((self.fx.prices / "exchangeInfo.json").read_text())
        for entry in info["symbols"][0]["filters"]:
            if entry["filterType"] == "MARKET_LOT_SIZE":
                entry["minQty"] = "50"
        (self.fx.prices / "exchangeInfo.json").write_text(json.dumps(info))
        autopilot._cache.clear()
        self.assertEqual(self.run_monitor(), 2)
        self.assertIn("minQty", self.fx.journal())
        self.assertEqual(self.fx.proposals(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
