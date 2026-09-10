#!/usr/bin/env python3
"""Fixtures for desk_policy.py - the desk's only enforceable control.

Every test builds a whole desk in a temporary directory: a real Ed25519 key
pair, a genuinely signed register, a state directory, and proposal files in the
format the Risk Manager writes. Nothing is mocked, because the failure this
suite exists to catch is a gate that says allow when it should say refuse, and a
mocked signature would hide exactly that.

The last class is the important one: it parses the PASS template out of
`agents/risk-manager.md` itself and runs it through the policy. If the prompt
and the code ever drift apart, that is the test that fails - which is the whole
problem this layer exists to solve, one level up.

No network. The exchange reads are the callables strike_request.py supplies, so
a test passes its own.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import desk_policy  # noqa: E402

REPO = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RULES = "# funding-fade-v1 v3\nshort when funding > 95th pct and close < sma20\n"


def quiet(fn, *a, **kw):
    """keygen and sign report to stdout for the user's benefit; the fixtures
    call them dozens of times and the suite's own output is the point."""
    with redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


def utc(offset_seconds: float = 0) -> str:
    stamp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_seconds)
    return stamp.isoformat().replace("+00:00", "Z")


class Desk:
    """A desk on disk, with the knobs each test needs to turn."""

    def __init__(self, root: str, *, signed: bool = True):
        self.root = Path(root)
        for sub in ("desk", "proposals", "approvals", "strategies/funding-fade-v1",
                    "journal/incidents/open"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        (self.root / "strategies/funding-fade-v1/RULES.md").write_text(RULES)
        self.key = self.root / "signing-key"
        quiet(desk_policy.keygen, self.key)
        if signed:
            (self.root / "desk/user-signing.pub").write_bytes(
                Path(str(self.key) + ".pub").read_bytes())
            self.write_register()

    # -- fixtures ----------------------------------------------------------
    def write_register(self, version=1, **overrides):
        approval = {
            "id": "SA-03", "rule": "funding-fade-v1", "rule_version": 3,
            "rules_sha256": hashlib.sha256(RULES.encode()).hexdigest(),
            "markets": ["ETH-USD"], "sides": ["long", "short"],
            "risk_per_trade": 0.0025, "max_notional": 1500, "max_open": 1,
            "max_leverage": 5, "bar_seconds": 14400, "hours_utc": None,
            "granted": dt.date.today().isoformat(),
            "expires": (dt.date.today() + dt.timedelta(days=30)).isoformat(),
            "kill": {"consecutive_losses": 3},
        }
        approval.update(overrides)
        path = self.root / "desk/standing-approvals.json"
        path.write_text(json.dumps({"version": version, "signed_at": utc(), "approvals": [approval]}))
        quiet(desk_policy.sign_file, self.key, path)

    @staticmethod
    def fields(**overrides):
        f = {"symbol": "ETH-USD", "side": "sell", "size": "0.43", "price": "2431",
             "order_type": "limit", "stop_price": "2489", "leverage": "3",
             "risk_usd": "26.03", "equity": "10412.60", "liquidity": "pass",
             "sa": "SA-03", "signal": "funding-fade-v1@3", "fired_at": utc(-60),
             "expires_at": utc(3600)}
        f.update(overrides)
        return {k: v for k, v in f.items() if v is not None}

    def proposal(self, blocks=None, ticket="SG-20260912-03"):
        """Write a proposal. `blocks` is a list of (verdict-line, fields) pairs so a
        test can put an approved and a rejected ticket in one file."""
        if blocks is None:
            blocks = [(f"RISK | {ticket} | PASS | SA-03 | {utc()}", self.fields())]
        text = f"# {ticket}\nstatus: open\n\n"
        for verdict, fields in blocks:
            text += verdict + "\nfields\n"
            text += "".join(f"  {k}: {v}\n" for k, v in fields.items()) + "\n"
        base = "-".join(ticket.split("-")[:3])
        (self.root / "proposals" / f"{base}.md").write_text(text)

    @staticmethod
    def body(**overrides):
        b = {"symbol": "ETH-USD", "side": "sell", "size": "0.43", "price": "2431",
             "client_order_id": "SG-20260912-03-entry",
             "sl_order": {"type": "stop", "size": "0.43", "stop_price": "2489",
                          "working_type": "mark_price", "reduce_only": True}}
        b.update(overrides)
        return {k: v for k, v in b.items() if v is not None}

    def token(self, ticket="SG-20260912-03", **overrides):
        payload = {"proposal": ticket, "symbol": "ETH-USD", "side": "sell",
                   "size": "0.43", "price": "2431", "expires": utc(3600)}
        payload.update(overrides)
        path = self.root / "approvals" / f"{ticket}.json"
        path.write_text(json.dumps(payload))
        quiet(desk_policy.sign_file, self.key, path)

    def policy(self, equity=10412.60, positions=None, fills=None, resting=None, known=None):
        return desk_policy.Policy(
            account_reader=(lambda: equity) if equity is not None else None,
            positions_reader=(lambda: list(positions or [])),
            fills_reader=(lambda symbol, n: list(fills)) if fills is not None else None,
            open_orders_reader=(lambda: list(resting)) if resting is not None else None,
            order_lookup_reader=(lambda coid: coid in (known or ())) if known is not None else None,
            desk=self.root)

    def open_the_day(self, equity=10412.60):
        """Start-of-day equity is captured on the first signed read of the day, and
        the daily stop refuses rather than re-basing if it is missing."""
        self.policy(equity=equity).check("GET", "/v2/account", {})


class PolicyCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("STRIKEGROK_POLICY_STATE", None))
        os.environ.pop("STRIKEGROK_POLICY_STATE", None)
        # Tier 1 is off unless the operator asserts that policy-state really is a
        # boundary. The suite asserts it; TestSingleUserBox checks the default.
        os.environ["STRIKEGROK_STATE_TRUSTED"] = "1"
        self.addCleanup(lambda: os.environ.pop("STRIKEGROK_STATE_TRUSTED", None))
        self.desk = Desk(self._tmp.name)
        self.desk.open_the_day()

    def allow(self, body=None, path="/v2/order/strategy", method="POST", **kw):
        return self.desk.policy(**kw).check(method, path, Desk.body() if body is None else body)

    def refuse(self, body=None, path="/v2/order/strategy", method="POST", **kw):
        with self.assertRaises(desk_policy.PolicyRefusal) as caught:
            self.desk.policy(**kw).check(method, path, Desk.body() if body is None else body)
        return str(caught.exception)

    def age_slot(self, sa_id, coid, seconds):
        """Backdate a reserved slot so the reconciliation grace period has passed."""
        path = self.desk.root / "desk/policy-state/sa_open.json"
        data = json.loads(path.read_text())
        data[sa_id][coid]["at"] = utc(-seconds)
        path.write_text(json.dumps(data))

    def set_sod(self, equity):
        """Set today's opening equity. The engine refuses to re-base it, so a test
        that needs a different account size writes the day's baseline directly."""
        path = self.desk.root / "desk/policy-state/sod_equity.json"
        path.write_text(json.dumps(
            {"date": dt.datetime.now(dt.timezone.utc).date().isoformat(), "equity": equity}))

    def rewind_pace(self, seconds=600):
        """Put the pace clock in the past. Several tests need two opening orders
        and the floor is deliberately not lowerable by an environment variable."""
        state = self.desk.root / "desk/policy-state/pace.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"last_open_at": utc(-seconds)}))

    def send(self, body=None, path="/v2/order/strategy", **kw):
        """check() then commit(), which is what strike_request.py does."""
        pol = self.desk.policy(**kw)
        record = pol.check("POST", path, Desk.body() if body is None else body)
        pol.commit(Desk.body() if body is None else body)
        return record


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
class TestTicketIdsFromLegSuffixes(unittest.TestCase):
    """A leg suffix is not an amendment suffix.

    The Execution Trader appends `-entry`, `-sl` and `-tp` to a ticket id, and an
    amendment is a single uppercase letter. A regex that does not pin the suffix
    reads `-Entry` as amendment E and `-TP` as amendment T, then looks for a
    ticket block nobody wrote.
    """

    def check(self, coid, expected):
        self.assertEqual(desk_policy.ticket_from_coid(coid)[1], expected)

    def test_lowercase_leg_suffixes_are_the_base_ticket(self):
        for leg in ("entry", "sl", "tp", "close"):
            self.check(f"SG-20260912-03-{leg}", "SG-20260912-03")

    def test_capitalised_leg_suffixes_are_the_base_ticket(self):
        for leg in ("Entry", "TP", "SL", "Close"):
            self.check(f"SG-20260912-03-{leg}", "SG-20260912-03")

    def test_a_bare_ticket_id_is_itself(self):
        self.check("SG-20260912-03", "SG-20260912-03")

    def test_an_amendment_suffix_is_kept(self):
        self.check("SG-20260912-03-B-entry", "SG-20260912-03-B")
        self.check("SG-20260912-03-B", "SG-20260912-03-B")

    def test_the_base_id_selects_the_file(self):
        self.assertEqual(desk_policy.ticket_from_coid("SG-20260912-03-B-sl")[0], "SG-20260912-03")

    def test_no_ticket_id_refuses(self):
        with self.assertRaises(desk_policy.PolicyRefusal):
            desk_policy.ticket_from_coid("some-other-id")


class TestPassBlockAnchoring(PolicyCase):
    """Fields come only from the last RISK block for this exact ticket."""

    def test_a_rejected_amendment_cannot_be_sent(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields()),
            ("RISK | SG-20260912-03-B | REJECT | gate failed: correlation",
             Desk.fields(size="0.20", price="2450")),
        ])
        self.assertIn("is REJECT", self.refuse(
            Desk.body(size="0.20", price="2450", client_order_id="SG-20260912-03-B-entry",
                      sl_order={"type": "stop", "size": "0.20", "stop_price": "2489",
                                "working_type": "mark_price", "reduce_only": True})))

    def test_a_later_block_does_not_overwrite_the_approved_one(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields()),
            ("RISK | SG-20260912-03-B | REJECT | gate failed", Desk.fields(size="0.20")),
        ])
        self.assertEqual(self.allow()["tier"], "tier1")

    def test_a_later_reject_for_the_same_ticket_wins(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields()),
            ("RISK | SG-20260912-03 | REJECT | the Risk Manager changed their mind", Desk.fields()),
        ])
        self.assertIn("is REJECT", self.refuse())

    def test_trailing_prose_cannot_supply_fields(self):
        self.desk.proposal()
        path = self.desk.root / "proposals/SG-20260912-03.md"
        path.write_text(path.read_text() + "\nexecution report\n  size: 9.99\n  price: 1.00\n")
        self.assertIn("size", self.refuse())

    def test_a_proposal_with_no_block_for_the_ticket_refuses(self):
        (self.desk.root / "proposals/SG-20260912-03.md").write_text("status: open\nsize: 0.43\n")
        self.assertIn("no RISK block", self.refuse())

    def test_a_missing_proposal_refuses(self):
        self.assertIn("is missing", self.refuse())

    def test_a_body_that_differs_from_the_ticket_refuses(self):
        self.desk.proposal()
        self.assertIn("size", self.refuse(Desk.body(size="4.3")))


# ---------------------------------------------------------------------------
# The six defects
# ---------------------------------------------------------------------------
class TestOpenSlots(PolicyCase):
    """How many markets a standing approval already has working.

    Counted from three sources unioned - the state reservation, live positions,
    and resting non-reduce-only orders - so wiping the state file grants nothing
    and an entry that has not filled yet still holds its place.
    """

    def setUp(self):
        super().setUp()
        self.desk.write_register(markets=["ETH-USD", "BTC-USD"], max_open=1)
        self.desk.proposal()
        self.desk.proposal(ticket="SG-20260912-05", blocks=[
            (f"RISK | SG-20260912-05 | PASS | SA-03 | {utc()}",
             Desk.fields(symbol="BTC-USD", price="77120", stop_price="78000",
                         size="0.013", risk_usd="11.44"))])

    def btc(self):
        return Desk.body(symbol="BTC-USD", price="77120", size="0.013",
                         client_order_id="SG-20260912-05-entry",
                         sl_order={"type": "stop", "size": "0.013", "stop_price": "78000",
                                   "working_type": "mark_price", "reduce_only": True})

    def test_the_first_trade_under_max_open_one_is_allowed(self):
        self.assertEqual(self.allow()["tier"], "tier1")

    def test_a_reserved_market_fills_the_only_slot(self):
        self.send()
        self.rewind_pace()
        self.assertIn("1/1 open tickets", self.refuse(self.btc()))

    def test_the_slot_is_taken_by_the_gate_not_by_the_commit(self):
        """The window between check() and commit() is one network round trip. If
        the slot were taken at commit, every Bot inside that window would see it
        free - six processes went through a max_open of one that way."""
        self.allow()                       # checked, never committed
        self.rewind_pace()
        self.assertIn("1/1 open tickets", self.refuse(self.btc()))

    def test_a_second_ticket_on_a_market_already_held_refuses(self):
        self.send()
        self.rewind_pace()
        self.desk.proposal(ticket="SG-20260912-07", blocks=[
            (f"RISK | SG-20260912-07 | PASS | SA-03 | {utc()}", Desk.fields())])
        body = Desk.body(client_order_id="SG-20260912-07-entry")
        self.assertIn("already has exposure or a working order on ETH-USD", self.refuse(body))

    def test_a_live_position_counts_even_with_the_state_wiped(self):
        (self.desk.root / "desk/policy-state/sa_open.json").write_text("{}\n")
        self.assertIn("1/1 open tickets",
                      self.refuse(self.btc(), positions=[{"symbol": "ETH-USD", "size": "0.43"}]))

    def test_a_resting_entry_counts_even_with_the_state_wiped(self):
        (self.desk.root / "desk/policy-state/sa_open.json").write_text("{}\n")
        resting = [{"symbol": "ETH-USD", "client_order_id": "SG-20260912-03-entry",
                    "reduce_only": False}]
        self.assertIn("1/1 open tickets", self.refuse(self.btc(), resting=resting))

    def test_a_resting_reduce_only_stop_does_not_count(self):
        (self.desk.root / "desk/policy-state/sa_open.json").write_text("{}\n")
        resting = [{"symbol": "ETH-USD", "client_order_id": "SG-20260912-03-sl",
                    "reduce_only": True}]
        self.assertEqual(self.allow(self.btc(), resting=resting)["tier"], "tier1")

    def test_a_slot_frees_once_the_market_is_flat_and_the_bar_has_passed(self):
        self.send()
        self.age_slot("SA-03", "SG-20260912-03-entry", seconds=20000)   # past one 4h bar
        self.rewind_pace()
        self.assertEqual(self.allow(self.btc(), positions=[], resting=[])["tier"], "tier1")

    def test_a_reservation_inside_the_bar_still_holds_its_market(self):
        """An entry that has not filled has no position and may have no resting
        order yet either. Freeing it at once would let a second ticket in."""
        self.send()
        self.rewind_pace()
        self.assertIn("1/1 open tickets", self.refuse(self.btc(), positions=[], resting=[]))

    def test_release_frees_a_slot(self):
        self.send()
        self.desk.policy().release("SA-03", "SG-20260912-03-entry")
        self.rewind_pace()
        self.assertEqual(self.allow(self.btc(), positions=[], resting=[])["tier"], "tier1")

    def test_a_rejected_send_frees_the_slot_but_keeps_the_id(self):
        pol = self.desk.policy()
        pol.check("POST", "/v2/order/strategy", Desk.body())
        pol.commit(Desk.body(), accepted=False)          # the venue said 400
        self.rewind_pace()
        self.assertEqual(self.allow(self.btc(), positions=[], resting=[])["tier"], "tier1")
        self.assertIn("has already been sent", self.refuse())

    def test_a_dead_transport_gives_the_reservation_back(self):
        pol = self.desk.policy()
        pol.check("POST", "/v2/order/strategy", Desk.body())
        pol.release_pending()                            # the request never left
        self.rewind_pace()
        self.assertEqual(self.allow()["tier"], "tier1")


class TestPositionConfig(PolicyCase):
    """Leverage, margin mode and margin transfers are not free actions."""

    def setUp(self):
        super().setUp()
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}",
             Desk.fields(margin_action="remove", amount="-300", margin_type="isolated"))])

    def test_adding_margin_is_tier_zero(self):
        body = {"symbol": "ETH-USD", "amount": "300", "type": "add"}
        self.assertEqual(self.allow(body, "/v2/positionMargin")["tier"], "tier0")

    def test_removing_margin_is_not_tier_zero(self):
        body = {"symbol": "ETH-USD", "amount": "-300", "type": "remove",
                "client_order_id": "SG-20260912-03-margin"}
        self.assertIn("never covered by a standing approval",
                      self.refuse(body, "/v2/positionMargin"))

    def test_an_ambiguous_margin_type_is_not_an_add(self):
        body = {"symbol": "ETH-USD", "amount": "300", "type": "2",
                "client_order_id": "SG-20260912-03-margin"}
        self.assertNotIn("tier0", self.refuse(body, "/v2/positionMargin"))

    def test_a_leverage_change_needs_a_ticket(self):
        body = {"symbol": "ETH-USD", "leverage": "3"}
        self.assertIn("does not carry an SG-", self.refuse(body, "/v2/leverage"))

    def test_a_leverage_change_on_a_ticket_is_tier_one(self):
        body = {"symbol": "ETH-USD", "leverage": "3", "client_order_id": "SG-20260912-03-lev"}
        self.assertEqual(self.allow(body, "/v2/leverage")["tier"], "tier1")

    def test_leverage_above_the_sa_maximum_refuses(self):
        self.desk.write_register(max_leverage=2)
        body = {"symbol": "ETH-USD", "leverage": "3", "client_order_id": "SG-20260912-03-lev"}
        self.assertIn("> SA-03 max", self.refuse(body, "/v2/leverage"))

    def test_leverage_above_the_hard_ceiling_refuses(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(leverage="25"))])
        body = {"symbol": "ETH-USD", "leverage": "25", "client_order_id": "SG-20260912-03-lev"}
        self.assertIn("outside 1..5", self.refuse(body, "/v2/leverage"))

    def test_a_leverage_that_differs_from_the_ticket_refuses(self):
        body = {"symbol": "ETH-USD", "leverage": "5", "client_order_id": "SG-20260912-03-lev"}
        self.assertIn("leverage '3' != request '5'", self.refuse(body, "/v2/leverage"))

    def test_a_margin_type_change_matches_the_ticket(self):
        body = {"symbol": "ETH-USD", "margin_type": "cross", "client_order_id": "SG-20260912-03-mt"}
        self.assertIn("margin_type", self.refuse(body, "/v2/marginType"))


class TestSuspensions(PolicyCase):
    """A Bot may suspend. Only a re-signed register lifts one."""

    def setUp(self):
        super().setUp()
        self.desk.proposal()

    def bot_suspends(self, reason="3 consecutive losses"):
        (self.desk.root / "desk/standing-approvals.suspended.json").write_text(
            json.dumps([{"id": "SA-03", "reason": reason, "by": "strategist", "at": utc()}]))

    def test_a_bot_suspension_refuses(self):
        self.bot_suspends()
        self.assertIn("is suspended", self.refuse())

    def test_deleting_the_bot_file_does_not_lift_it(self):
        self.bot_suspends()
        self.refuse()                                     # ingests into state
        (self.desk.root / "desk/standing-approvals.suspended.json").unlink()
        self.assertIn("is suspended", self.refuse())

    def test_an_unreadable_bot_file_refuses(self):
        (self.desk.root / "desk/standing-approvals.suspended.json").write_text("{not json")
        self.assertIn("unreadable", self.refuse())

    def test_a_resigned_register_with_a_higher_version_lifts_it(self):
        self.bot_suspends()
        self.refuse()
        (self.desk.root / "desk/standing-approvals.suspended.json").unlink()
        self.desk.write_register(version=2)
        self.assertEqual(self.allow()["tier"], "tier1")

    def test_resigning_at_the_same_version_does_not_lift_it(self):
        self.bot_suspends()
        self.refuse()
        (self.desk.root / "desk/standing-approvals.suspended.json").unlink()
        self.desk.write_register(version=1)
        self.assertIn("is suspended", self.refuse())

    def test_a_changed_rules_file_suspends_the_sa_itself(self):
        (self.desk.root / "strategies/funding-fade-v1/RULES.md").write_text(RULES + "# tweak\n")
        self.assertIn("hash mismatch", self.refuse())
        (self.desk.root / "strategies/funding-fade-v1/RULES.md").write_text(RULES)
        self.assertIn("is suspended", self.refuse())

    def test_consecutive_losses_suspend_the_sa(self):
        self.assertIn("3 consecutive losses", self.refuse(fills=[-1.0, -2.0, -3.0]))
        self.assertIn("is suspended", self.refuse(fills=[5.0, 5.0, 5.0]))

    def test_a_win_in_the_window_does_not_suspend(self):
        self.assertEqual(self.allow(fills=[-1.0, 2.0, -3.0])["tier"], "tier1")

    def test_too_little_history_does_not_suspend(self):
        self.assertEqual(self.allow(fills=[-1.0, -2.0])["tier"], "tier1")


class TestEquity(PolicyCase):
    """The daily-loss stop reads the exchange, never a file on the shared disk."""

    def setUp(self):
        super().setUp()
        self.desk.proposal()

    def test_no_account_reader_disables_opening_exposure(self):
        self.assertIn("no exchange account reader", self.refuse(equity=None))

    def test_a_bot_written_equity_file_is_ignored(self):
        (self.desk.root / "desk/equity-latest.json").write_text(json.dumps({"equity": 99999.0}))
        self.assertIn("daily loss", self.refuse(equity=9000.0))

    def test_the_daily_loss_stop_blocks_opening_exposure(self):
        self.assertIn(">= ceiling 3.00%", self.refuse(equity=10000.0))

    def test_start_of_day_is_captured_on_a_read_not_the_first_trade(self):
        state = json.loads((self.desk.root / "desk/policy-state/sod_equity.json").read_text())
        self.assertAlmostEqual(state["equity"], 10412.60)

    def test_wiping_the_baseline_refuses_rather_than_re_basing(self):
        """Re-capturing the baseline after a loss would reset the stop to whatever
        the account is worth once the damage is done."""
        (self.desk.root / "desk/policy-state/sod_equity.json").write_text("{}\n")
        self.assertIn("no start-of-day equity", self.refuse(equity=9500.0))

    def test_reduce_only_still_works_past_the_daily_loss_stop(self):
        self.assertEqual(self.allow(Desk.body(reduce_only=True), "/v2/order", equity=1.0)["tier"], "tier0")

    def test_a_pass_block_sized_against_stale_equity_refuses(self):
        self.assertIn("re-size before sending", self.refuse(equity=13000.0))

    def test_zero_equity_refuses(self):
        self.assertIn("zero or negative equity", self.refuse(equity=0.0))


class TestReplay(PolicyCase):
    def setUp(self):
        super().setUp()
        self.desk.proposal()

    def test_a_committed_client_order_id_cannot_be_sent_again(self):
        self.send()
        self.assertIn("has already been sent", self.refuse())

    def test_an_uncommitted_check_still_burns_the_id(self):
        """A send that timed out is not a send that did not happen. The desk
        cannot tell, so the id is spent either way and the ticket goes back."""
        self.allow()
        self.assertIn("has already been sent", self.refuse())

    def test_a_consumed_token_cannot_be_reused(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | {utc()}", Desk.fields(sa=None))])
        self.desk.token()
        self.send()
        self.rewind_pace()
        body = Desk.body(client_order_id="SG-20260912-03-entry-2")
        self.assertIn("already been consumed", self.refuse(body))


class TestStateDirectory(PolicyCase):
    def test_a_world_writable_state_dir_refuses(self):
        self.desk.proposal()
        state = self.desk.root / "desk/policy-state"
        state.mkdir(parents=True, exist_ok=True)
        os.chmod(state, 0o777)
        self.addCleanup(lambda: os.chmod(state, 0o700))
        self.assertIn("writable by group or other", self.refuse())

    def test_a_world_writable_state_file_refuses(self):
        self.desk.proposal()
        self.allow()
        target = self.desk.root / "desk/policy-state/sa_open.json"
        os.chmod(target, 0o666)
        self.addCleanup(lambda: os.chmod(target, 0o600))
        self.assertIn("writable by group or other", self.refuse())

    def test_state_files_are_created_private(self):
        self.desk.proposal()
        self.allow()
        for name in desk_policy.State.FILES:
            mode = (self.desk.root / "desk/policy-state" / name).stat().st_mode
            self.assertFalse(mode & (stat.S_IWGRP | stat.S_IWOTH), name)


# ---------------------------------------------------------------------------
# Order shape
# ---------------------------------------------------------------------------
class TestProtectionIsMandatory(PolicyCase):
    """An opening order carries its own stop. Prose was never a control."""

    def setUp(self):
        super().setUp()
        self.desk.proposal()

    def test_a_naked_limit_entry_is_refused(self):
        self.assertIn("must be a bracket", self.refuse(Desk.body(sl_order=None), "/v2/order"))

    def test_a_bracket_with_no_stop_leg_is_refused(self):
        self.assertIn("carries no sl_order", self.refuse(Desk.body(sl_order=None)))

    def test_a_stop_that_differs_from_the_ticket_is_refused(self):
        body = Desk.body(sl_order={"type": "stop", "size": "0.43", "stop_price": "2495",
                                   "working_type": "mark_price", "reduce_only": True})
        self.assertIn("stop_price '2489' != request '2495'", self.refuse(body))

    def test_a_stop_on_the_wrong_side_is_refused(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(stop_price="2400"))])
        body = Desk.body(sl_order={"type": "stop", "size": "0.43", "stop_price": "2400",
                                   "working_type": "mark_price", "reduce_only": True})
        self.assertIn("would trigger immediately", self.refuse(body))

    def test_a_stop_that_does_not_cover_the_entry_is_refused(self):
        body = Desk.body(sl_order={"type": "stop", "size": "0.20", "stop_price": "2489",
                                   "working_type": "mark_price", "reduce_only": True})
        self.assertIn("does not cover the entry", self.refuse(body))

    def test_a_stop_on_the_contract_price_is_refused(self):
        body = Desk.body(sl_order={"type": "stop", "size": "0.43", "stop_price": "2489",
                                   "working_type": "contract_price", "reduce_only": True})
        self.assertIn("mark_price", self.refuse(body))

    def test_a_stop_that_is_not_reduce_only_is_refused(self):
        """A stop that can open a position is not a stop. `strike-positions` warns
        about exactly this for orphaned stops; a bracket leg is no different."""
        body = Desk.body(sl_order={"type": "stop", "size": "0.43", "stop_price": "2489",
                                   "working_type": "mark_price"})
        self.assertIn("must be reduce_only", self.refuse(body))

    def test_a_stop_flagged_close_position_is_accepted(self):
        body = Desk.body(sl_order={"type": "stop", "size": "0.43", "stop_price": "2489",
                                   "working_type": "mark_price", "close_position": True})
        self.assertEqual(self.allow(body)["tier"], "tier1")

    def test_a_stop_wider_than_the_stated_risk_is_refused(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(stop_price="2600"))])
        body = Desk.body(sl_order={"type": "stop", "size": "0.43", "stop_price": "2600",
                                   "working_type": "mark_price", "reduce_only": True})
        self.assertIn("the stop and the sizing disagree", self.refuse(body))


class TestOrderKinds(PolicyCase):
    def market_ticket(self, **over):
        fields = {"order_type": "market", "ref_price": "2431", "price": None}
        fields.update(over)
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(**fields))])

    def test_a_market_ticket_with_a_slippage_bound_is_allowed(self):
        self.market_ticket()
        self.assertEqual(self.allow(Desk.body(price=None, slippage="0.003"))["tier"], "tier1")

    def test_a_market_ticket_without_a_slippage_bound_refuses(self):
        self.market_ticket()
        self.assertIn("slippage bound", self.refuse(Desk.body(price=None)))

    def test_a_market_ticket_carrying_a_price_refuses(self):
        self.market_ticket()
        self.assertIn("carries a price", self.refuse(Desk.body(slippage="0.003")))

    def test_a_market_ticket_still_needs_its_stop(self):
        self.market_ticket()
        self.assertIn("carries no sl_order",
                      self.refuse(Desk.body(price=None, slippage="0.003", sl_order=None)))

    def test_a_market_ticket_is_bounded_by_its_reference_price(self):
        self.market_ticket(size="900")
        body = Desk.body(size="900", price=None, slippage="0.003",
                         sl_order={"type": "stop", "size": "900", "stop_price": "2489",
                                   "working_type": "mark_price", "reduce_only": True})
        self.assertIn("ceiling: notional", self.refuse(body))

    def test_a_market_ticket_without_a_reference_price_refuses(self):
        self.market_ticket(ref_price=None)
        self.assertIn("needs 'ref_price:'", self.refuse(Desk.body(price=None, slippage="0.003")))

    def test_an_order_type_that_does_not_match_the_path_refuses(self):
        self.desk.proposal()
        self.assertIn("but the path is", self.refuse(path="/v2/algo/twap"))

    def test_a_twap_must_declare_how_it_is_protected(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(order_type="twap"))])
        body = Desk.body(price=None, limit_price="2431", sl_order=None)
        self.assertIn("protection: as_it_builds", self.refuse(body, "/v2/algo/twap"))

    def test_a_twap_that_declares_it_is_allowed(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}",
             Desk.fields(order_type="twap", protection="as_it_builds"))])
        body = Desk.body(price=None, limit_price="2431", sl_order=None)
        self.assertEqual(self.allow(body, "/v2/algo/twap")["tier"], "tier1")

    def test_an_unknown_order_type_refuses(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(order_type="iceberg"))])
        self.assertIn("must be limit, market or twap", self.refuse())

    def test_a_leverage_in_the_body_must_match_the_ticket(self):
        self.desk.proposal()
        self.assertIn("leverage '3' != request '50'", self.refuse(Desk.body(leverage="50")))


class TestExactDecimalMatching(PolicyCase):
    """Sizes and prices are decimal on the wire because ticks and steps are.

    A float comparison with a 1e-12 tolerance calls two different orders the same
    order, which is precisely the thing ticket equality exists to prevent.
    """

    def setUp(self):
        super().setUp()
        self.desk.proposal()

    def test_a_difference_under_the_old_float_tolerance_is_still_a_difference(self):
        drifted = "0.430000000000001"          # 1e-15 away; the old tolerance accepted it
        self.assertLess(abs(float(drifted) - 0.43), 1e-12)
        self.assertIn("size", self.refuse(Desk.body(size=drifted)))

    def test_trailing_zeros_are_the_same_number(self):
        self.assertEqual(self.allow(Desk.body(size="0.430"))["tier"], "tier1")

    def test_a_non_numeric_size_refuses_rather_than_raising(self):
        self.assertIn("not a decimal number", self.refuse(Desk.body(size="lots")))

    def test_the_stop_leg_size_is_compared_exactly_too(self):
        body = Desk.body(sl_order={"type": "stop", "size": "0.430000000000001",
                                   "stop_price": "2489", "working_type": "mark_price",
                                   "reduce_only": True})
        self.assertIn("does not cover the entry", self.refuse(body))


class TestReservationGrace(PolicyCase):
    """A reservation the venue never confirms should not hold a market all day."""

    def setUp(self):
        super().setUp()
        self.desk.write_register(markets=["ETH-USD", "BTC-USD"], max_open=1)
        self.desk.proposal()
        self.desk.proposal(ticket="SG-20260912-05", blocks=[
            (f"RISK | SG-20260912-05 | PASS | SA-03 | {utc()}",
             Desk.fields(symbol="BTC-USD", price="77120", stop_price="78000",
                         size="0.013", risk_usd="11.44"))])
        self.other = Desk.body(symbol="BTC-USD", price="77120", size="0.013",
                               client_order_id="SG-20260912-05-entry",
                               sl_order={"type": "stop", "size": "0.013", "stop_price": "78000",
                                         "working_type": "mark_price", "reduce_only": True})

    def test_a_fresh_reservation_holds_its_market(self):
        self.send()
        self.rewind_pace()
        self.assertIn("1/1 open tickets", self.refuse(self.other, positions=[], resting=[]))

    def test_an_unconfirmed_reservation_frees_after_the_grace_period(self):
        """Five minutes is long enough for an order to appear at the venue. Holding
        the market for a whole bar on no evidence is not proportionate."""
        self.send()
        self.age_slot("SA-03", "SG-20260912-03-entry", seconds=600)
        self.rewind_pace()
        self.assertEqual(self.allow(self.other, positions=[], resting=[])["tier"], "tier1")

    def test_without_a_positions_reader_nothing_opens_at_all(self):
        """The grace period only shortens a reservation because the venue can be
        asked instead. With no reader there is nothing to ask, and the book
        ceiling refuses before the slot count is even reached."""
        self.send()
        self.age_slot("SA-03", "SG-20260912-03-entry", seconds=99999)
        self.rewind_pace()
        policy = desk_policy.Policy(account_reader=lambda: 10412.60, desk=self.desk.root)
        with self.assertRaises(desk_policy.PolicyRefusal) as caught:
            policy.check("POST", "/v2/order/strategy", self.other)
        self.assertIn("the book cannot be counted", str(caught.exception))

    def test_an_aged_reservation_is_kept_when_only_resting_orders_can_be_read(self):
        """One reader is enough evidence to reconcile against, so the short grace
        applies - but the reservation is still held while the order may exist."""
        self.send()
        self.rewind_pace()
        self.assertIn("1/1 open tickets",
                      self.refuse(self.other, positions=[], resting=[]))


class TestTicketExpiry(PolicyCase):
    def test_an_expired_ticket_refuses(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(expires_at=utc(-60)))])
        self.assertIn("expired at", self.refuse())

    def test_a_naive_expiry_refuses_rather_than_being_read_as_local_time(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}",
             Desk.fields(expires_at="2026-09-12 16:00:00"))])
        self.assertIn("no timezone", self.refuse())

    def test_a_ticket_with_no_expiry_is_not_blocked_by_this_check(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(expires_at=None))])
        self.assertEqual(self.allow()["tier"], "tier1")


class TestRunawayGuards(PolicyCase):
    """What an unattended desk can do to itself that no prompt can prevent."""

    def setUp(self):
        super().setUp()
        self.desk.proposal()

    def test_opening_orders_are_paced(self):
        self.desk.write_register(markets=["ETH-USD", "BTC-USD"], max_open=3)
        self.send()
        self.desk.proposal(ticket="SG-20260912-05", blocks=[
            (f"RISK | SG-20260912-05 | PASS | SA-03 | {utc()}",
             Desk.fields(symbol="BTC-USD", price="77120", stop_price="78000",
                         size="0.013", risk_usd="11.44"))])
        body = Desk.body(symbol="BTC-USD", price="77120", size="0.013",
                         client_order_id="SG-20260912-05-entry",
                         sl_order={"type": "stop", "size": "0.013", "stop_price": "78000",
                                   "working_type": "mark_price", "reduce_only": True})
        self.assertIn("pace:", self.refuse(body))

    def test_the_open_position_ceiling_is_not_raised_by_any_sa(self):
        positions = [{"symbol": s, "size": "1"} for s in ("BTC-USD", "SOL-USD", "ADA-USD")]
        self.assertIn("already open across the book", self.refuse(positions=positions))

    def test_a_flat_book_passes_the_position_ceiling(self):
        self.assertEqual(self.allow(positions=[{"symbol": "BTC-USD", "size": "0"}])["tier"], "tier1")

    def test_the_pace_floor_may_be_raised_but_not_lowered(self):
        self.assertEqual(desk_policy._floor("SECONDS_BETWEEN_OPENS", 60), 60)
        os.environ["STRIKEGROK_SECONDS_BETWEEN_OPENS"] = "5"
        self.addCleanup(lambda: os.environ.pop("STRIKEGROK_SECONDS_BETWEEN_OPENS", None))
        self.assertEqual(desk_policy._floor("SECONDS_BETWEEN_OPENS", 60), 60)
        os.environ["STRIKEGROK_SECONDS_BETWEEN_OPENS"] = "600"
        self.assertEqual(desk_policy._floor("SECONDS_BETWEEN_OPENS", 60), 600)

    def test_a_ticket_without_a_liquidity_pass_is_refused(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(liquidity="thin"))])
        self.assertIn("liquidity: pass", self.refuse())

    def test_a_ticket_with_no_liquidity_line_is_refused(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(liquidity=None))])
        self.assertIn("liquidity: pass", self.refuse())


# ---------------------------------------------------------------------------
# Bounds, surfaces, tiers
# ---------------------------------------------------------------------------
class TestStandingApprovalBounds(PolicyCase):
    def setUp(self):
        super().setUp()
        self.desk.proposal()

    def test_an_unsigned_register_refuses(self):
        (self.desk.root / "desk/standing-approvals.json.sig").write_text("Zm9v\n")
        self.assertIn("does not verify", self.refuse())

    def test_a_register_signed_by_the_wrong_key_refuses(self):
        other = self.desk.root / "attacker-key"
        quiet(desk_policy.keygen, other)
        quiet(desk_policy.sign_file, other, self.desk.root / "desk/standing-approvals.json")
        self.assertIn("does not verify", self.refuse())

    def test_a_market_outside_the_sa_refuses(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(symbol="BTC-USD"))])
        self.assertIn("not in SA-03's markets", self.refuse(Desk.body(symbol="BTC-USD")))

    def test_a_side_outside_the_sa_refuses(self):
        self.desk.write_register(sides=["long"])
        self.assertIn("side short is not permitted", self.refuse())

    def test_notional_above_the_sa_maximum_refuses(self):
        self.desk.write_register(max_notional=100)
        self.assertIn("> SA-03 max", self.refuse())

    def test_risk_above_the_sa_cap_refuses(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(risk_usd="99.00"))])
        self.assertIn("> SA-03 cap", self.refuse())

    def test_a_stale_signal_refuses(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(fired_at=utc(-20000)))])
        self.assertIn("stale", self.refuse())

    def test_a_signal_from_the_future_refuses(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(fired_at=utc(600)))])
        self.assertIn("in the future", self.refuse())

    def test_a_naive_fired_at_refuses(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}",
             Desk.fields(fired_at="2026-09-12 12:00:00"))])
        self.assertIn("no timezone", self.refuse())

    def test_an_expired_sa_refuses(self):
        self.desk.write_register(granted=(dt.date.today() - dt.timedelta(days=40)).isoformat(),
                                 expires=(dt.date.today() - dt.timedelta(days=10)).isoformat())
        self.assertIn("expired", self.refuse())

    def test_an_sa_lifetime_beyond_the_ceiling_refuses(self):
        self.desk.write_register(expires=(dt.date.today() + dt.timedelta(days=365)).isoformat())
        self.assertIn("lifetime", self.refuse())

    def test_the_signal_must_name_the_rule_version_the_user_signed(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}",
             Desk.fields(signal="funding-fade-v1@2"))])
        self.assertIn("does not name", self.refuse())

    def test_an_sa_id_that_is_not_one_refuses(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | {utc()}", Desk.fields(sa="none"))])
        self.assertIn("is not a standing-approval id", self.refuse())

    def test_hours_outside_the_window_refuse(self):
        hour = dt.datetime.now(dt.timezone.utc).hour
        self.desk.write_register(hours_utc=[(hour + 2) % 24, (hour + 3) % 24])
        self.assertIn("outside SA-03's hours", self.refuse())

    def test_a_wrap_around_hours_window_still_includes_now(self):
        hour = dt.datetime.now(dt.timezone.utc).hour
        self.desk.write_register(hours_utc=[(hour - 1) % 24, (hour + 1) % 24])
        self.assertEqual(self.allow()["tier"], "tier1")

    def test_notional_above_the_hard_ceiling_refuses(self):
        self.desk.write_register(max_notional=99999)
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}",
             Desk.fields(size="9.0", stop_price="2489", risk_usd="600"))])
        body = Desk.body(size="9.0", sl_order={"type": "stop", "size": "9.0",
                                               "stop_price": "2489", "working_type": "mark_price"})
        self.assertIn("ceiling: notional", self.refuse(body))


class TestTierZeroAndSurfaces(PolicyCase):
    def test_reduce_only_needs_no_ticket(self):
        self.assertEqual(self.allow(Desk.body(reduce_only=True), "/v2/order")["tier"], "tier0")

    def test_close_position_needs_no_ticket(self):
        self.assertEqual(self.allow(Desk.body(close_position=True), "/v2/order")["tier"], "tier0")

    def test_a_scoped_cancel_is_tier_zero(self):
        self.assertEqual(self.allow({"symbol": "ETH-USD"}, "/v2/order/cancel")["tier"], "tier0")

    def test_an_unscoped_cancel_all_refuses(self):
        self.assertIn("pass a symbol", self.refuse({}, "/v2/order/cancelAll"))

    def test_a_batch_that_opens_exposure_refuses(self):
        self.assertIn("one ticket at a time",
                      self.refuse({"orders": [Desk.body(), Desk.body()]}, "/v2/order/batch"))

    def test_a_batch_of_reduce_only_orders_is_tier_zero(self):
        body = {"orders": [Desk.body(reduce_only=True), Desk.body(reduce_only=True)]}
        self.assertEqual(self.allow(body, "/v2/order/batch")["tier"], "tier0")

    def test_a_mixed_batch_is_not_reduce_only(self):
        body = {"orders": [Desk.body(reduce_only=True), Desk.body()]}
        self.assertIn("one ticket at a time", self.refuse(body, "/v2/order/batch"))

    def test_fund_movement_paths_refuse(self):
        for path in ("/v2/withdraw", "/v2/transfer", "/v2/vault/deposit"):
            self.assertIn("outside the desk's scope", self.refuse({}, path))

    def test_a_vault_id_refuses(self):
        self.assertIn("vault_id", self.refuse(Desk.body(vault_id="v1"), "/v2/order"))

    def test_an_unknown_write_path_refuses(self):
        self.assertIn("not in the desk's allow-list", self.refuse({}, "/v2/somethingNew"))

    def test_a_get_is_never_gated(self):
        self.assertEqual(self.allow({}, "/v2/account", method="GET", equity=None)["tier"], "read")


class TestPerTradeTokens(PolicyCase):
    def setUp(self):
        super().setUp()
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | {utc()}", Desk.fields(sa=None))])

    def test_a_signed_token_authorises_the_trade(self):
        self.desk.token()
        self.assertEqual(self.allow()["tier"], "tier2")

    def test_no_token_refuses(self):
        self.assertIn("is missing", self.refuse())

    def test_a_token_for_another_size_refuses(self):
        self.desk.token(size="4.3")
        self.assertIn("size", self.refuse())

    def test_an_expired_token_refuses(self):
        self.desk.token(expires=utc(-60))
        self.assertIn("expired", self.refuse())

    def test_a_token_signed_by_the_wrong_key_refuses(self):
        self.desk.token()
        other = self.desk.root / "attacker-key"
        quiet(desk_policy.keygen, other)
        quiet(desk_policy.sign_file, other, self.desk.root / "approvals/SG-20260912-03.json")
        self.assertIn("does not verify", self.refuse())


class TestAttendedMode(unittest.TestCase):
    """A desk with no user-signing.pub keeps working, with the platform gate."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("STRIKEGROK_POLICY_STATE", None))
        os.environ.pop("STRIKEGROK_POLICY_STATE", None)
        os.environ["STRIKEGROK_STATE_TRUSTED"] = "1"
        self.addCleanup(lambda: os.environ.pop("STRIKEGROK_STATE_TRUSTED", None))
        self.desk = Desk(self._tmp.name, signed=False)
        self.desk.open_the_day()
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | {utc()}", Desk.fields(sa=None))])

    def test_the_mode_is_attended(self):
        self.assertEqual(self.desk.policy().mode(), "attended")

    def test_a_matching_ticket_is_allowed_without_a_signature(self):
        self.assertEqual(
            self.desk.policy().check("POST", "/v2/order/strategy", Desk.body())["tier"], "attended")

    def test_protection_still_applies(self):
        with self.assertRaises(desk_policy.PolicyRefusal) as caught:
            self.desk.policy().check("POST", "/v2/order", Desk.body(sl_order=None))
        self.assertIn("must be a bracket", str(caught.exception))

    def test_ceilings_still_apply(self):
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | {utc()}",
             Desk.fields(sa=None, size="9.0", risk_usd="600"))])
        body = Desk.body(size="9.0", sl_order={"type": "stop", "size": "9.0",
                                               "stop_price": "2489", "working_type": "mark_price"})
        with self.assertRaises(desk_policy.PolicyRefusal) as caught:
            self.desk.policy().check("POST", "/v2/order/strategy", body)
        self.assertIn("ceiling: notional", str(caught.exception))

    def test_an_unsigned_open_is_logged_as_a_warning(self):
        """Attended mode allows the open. It must not do so quietly - nothing in
        the code verified an approval, and the log is where that has to show."""
        self.desk.policy().check("POST", "/v2/order/strategy", Desk.body())
        log = (self.desk.root / "desk/policy-state/policy.log").read_text()
        self.assertIn("WARN", log)
        self.assertIn("unsigned open", log)

    def test_the_allow_record_says_nothing_verified_an_approval(self):
        decision = self.desk.policy().check("POST", "/v2/order/strategy", Desk.body())
        self.assertIn("NOTHING HERE VERIFIED AN APPROVAL", decision["why"])

    def test_a_standing_approval_claim_refuses_without_a_key(self):
        self.desk.proposal()
        with self.assertRaises(desk_policy.PolicyRefusal) as caught:
            self.desk.policy().check("POST", "/v2/order/strategy", Desk.body())
        self.assertIn("no", str(caught.exception))


class TestFailureIsAlwaysRefusal(PolicyCase):
    def setUp(self):
        super().setUp()
        self.desk.proposal()

    def test_an_internal_error_becomes_a_refusal(self):
        def explode():
            raise RuntimeError("account endpoint on fire")
        policy = desk_policy.Policy(account_reader=explode, positions_reader=lambda: [],
                                    desk=self.desk.root)
        with self.assertRaises(desk_policy.PolicyRefusal) as caught:
            policy.check("POST", "/v2/order/strategy", Desk.body())
        self.assertIn("account read failed", str(caught.exception))

    def test_a_failing_positions_read_refuses(self):
        def explode():
            raise RuntimeError("positions endpoint on fire")
        policy = desk_policy.Policy(account_reader=lambda: 10412.60, positions_reader=explode,
                                    desk=self.desk.root)
        with self.assertRaises(desk_policy.PolicyRefusal) as caught:
            policy.check("POST", "/v2/order/strategy", Desk.body())
        self.assertIn("positions: read failed", str(caught.exception))

    def test_every_refusal_is_logged(self):
        self.refuse(Desk.body(size="4.3"))
        self.assertIn("REFUSE", (self.desk.root / "desk/policy-state/policy.log").read_text())

    def test_every_allow_is_logged(self):
        self.allow()
        self.assertIn("ALLOW", (self.desk.root / "desk/policy-state/policy.log").read_text())

    def test_an_environment_ceiling_may_tighten(self):
        os.environ["STRIKEGROK_MAX_NOTIONAL_PER_ORDER_USD"] = "100"
        self.addCleanup(lambda: os.environ.pop("STRIKEGROK_MAX_NOTIONAL_PER_ORDER_USD", None))
        self.assertIn("ceiling: notional", self.refuse())

    def test_an_environment_ceiling_may_not_loosen(self):
        self.desk.write_register(max_notional=99999)
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}",
             Desk.fields(size="9.0", risk_usd="600"))])
        os.environ["STRIKEGROK_MAX_NOTIONAL_PER_ORDER_USD"] = "1000000"
        self.addCleanup(lambda: os.environ.pop("STRIKEGROK_MAX_NOTIONAL_PER_ORDER_USD", None))
        body = Desk.body(size="9.0", sl_order={"type": "stop", "size": "9.0",
                                               "stop_price": "2489", "working_type": "mark_price"})
        self.assertIn("ceiling: notional", self.refuse(body))

    def test_a_garbage_environment_ceiling_is_ignored(self):
        os.environ["STRIKEGROK_MAX_DAILY_LOSS_FRACTION"] = "not-a-number"
        self.addCleanup(lambda: os.environ.pop("STRIKEGROK_MAX_DAILY_LOSS_FRACTION", None))
        self.assertEqual(desk_policy._ceiling("MAX_DAILY_LOSS_FRACTION", 0.03), 0.03)

    def test_a_nan_environment_ceiling_is_ignored(self):
        os.environ["STRIKEGROK_MAX_DAILY_LOSS_FRACTION"] = "nan"
        self.addCleanup(lambda: os.environ.pop("STRIKEGROK_MAX_DAILY_LOSS_FRACTION", None))
        self.assertEqual(desk_policy._ceiling("MAX_DAILY_LOSS_FRACTION", 0.03), 0.03)


class TestSingleUserBox(unittest.TestCase):
    """On a box where the Bots run as the same OS user, policy-state is
    bookkeeping, not a boundary. Tier 1 says so out loud rather than pretending."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.environ.pop("STRIKEGROK_STATE_TRUSTED", None)
        os.environ.pop("STRIKEGROK_POLICY_STATE", None)
        self.addCleanup(lambda: os.environ.pop("STRIKEGROK_STATE_TRUSTED", None))
        self.desk = Desk(self._tmp.name)
        self.desk.open_the_day()
        self.desk.proposal()

    def test_tier_one_is_off_until_the_operator_asserts_the_split(self):
        with self.assertRaises(desk_policy.PolicyRefusal) as caught:
            self.desk.policy().check("POST", "/v2/order/strategy", Desk.body())
        self.assertIn("STRIKEGROK_STATE_TRUSTED=1", str(caught.exception))

    def test_tier_zero_is_unaffected(self):
        decision = self.desk.policy().check("POST", "/v2/order", Desk.body(reduce_only=True))
        self.assertEqual(decision["tier"], "tier0")

    def test_asserting_the_split_turns_tier_one_on(self):
        os.environ["STRIKEGROK_STATE_TRUSTED"] = "1"
        self.assertEqual(
            self.desk.policy().check("POST", "/v2/order/strategy", Desk.body())["tier"], "tier1")


class TestVerifyReporting(unittest.TestCase):
    """`verify` is the first thing a user runs after following SETUP step 7. What
    it prints has to be true, and a Tier-2-only desk is not a broken desk."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.environ.pop("STRIKEGROK_STATE_TRUSTED", None)
        os.environ.pop("STRIKEGROK_POLICY_STATE", None)
        self.addCleanup(lambda: os.environ.pop("STRIKEGROK_STATE_TRUSTED", None))
        self.desk = Desk(self._tmp.name, signed=False)

    def run_verify(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = desk_policy._cmd_verify(self.desk.policy())
        return code, out.getvalue()

    def test_a_desk_with_no_key_reports_attended_and_warns(self):
        code, text = self.run_verify()
        self.assertEqual(code, 0)
        self.assertIn("mode: attended", text)
        self.assertIn("WARNING", text)

    def test_a_tier_two_desk_is_not_reported_as_a_failure(self):
        """A desk that signs per-trade tokens has no standing-approval register.
        Reporting that as REFUSE tells the user their setup failed when it did not."""
        (self.desk.root / "desk/user-signing.pub").write_bytes(
            Path(str(self.desk.key) + ".pub").read_bytes())
        code, text = self.run_verify()
        self.assertEqual(code, 0)
        self.assertIn("mode: unattended", text)
        self.assertNotIn("REFUSE", text)
        self.assertIn("per-trade token", text)

    def test_the_ceilings_line_names_every_enforced_bound(self):
        _, text = self.run_verify()
        for name in desk_policy.effective_ceilings():
            self.assertIn(name, text)


class TestBlackouts(PolicyCase):
    """The catalyst clock, in code. The Research Analyst writes the window; the
    layer keeps it even if the file is deleted."""

    def setUp(self):
        super().setUp()
        self.desk.proposal()

    def write(self, symbol="ETH-USD", start=-60, end=3600, reason="FOMC"):
        (self.desk.root / "desk/blackouts.json").write_text(json.dumps(
            [{"symbol": symbol, "start": utc(start), "end": utc(end), "reason": reason}]))

    def test_a_window_in_force_blocks_opening_exposure(self):
        self.write()
        self.assertIn("inside a FOMC window", self.refuse())

    def test_a_wildcard_window_covers_every_market(self):
        self.write(symbol="*")
        self.assertIn("blackout", self.refuse())

    def test_a_window_on_another_market_does_not_block(self):
        self.write(symbol="BTC-USD")
        self.assertEqual(self.allow()["tier"], "tier1")

    def test_deleting_the_file_does_not_lift_a_window_in_force(self):
        self.write()
        self.refuse()                                   # ingests it
        (self.desk.root / "desk/blackouts.json").unlink()
        self.assertIn("blackout", self.refuse())

    def test_a_window_expires_on_its_own_end(self):
        self.write(start=-7200, end=-3600)
        self.assertEqual(self.allow()["tier"], "tier1")

    def test_an_unreadable_blackout_file_refuses(self):
        (self.desk.root / "desk/blackouts.json").write_text("{not json")
        self.assertIn("unreadable", self.refuse())

    def test_a_blackout_does_not_block_reduce_only(self):
        self.write(symbol="*")
        self.assertEqual(self.allow(Desk.body(reduce_only=True), "/v2/order")["tier"], "tier0")


class TestStickyIncidents(PolicyCase):
    def setUp(self):
        super().setUp()
        self.desk.proposal()

    def test_an_open_incident_blocks_opening_exposure(self):
        (self.desk.root / "journal/incidents/open/timeout.md").write_text("open\n")
        self.assertIn("incident", self.refuse())

    def test_a_bot_deleting_the_incident_does_not_clear_it(self):
        path = self.desk.root / "journal/incidents/open/timeout.md"
        path.write_text("open\n")
        self.refuse()                                   # ingests it
        path.unlink()
        self.assertIn("incident", self.refuse())

    def test_a_resigned_register_clears_it(self):
        path = self.desk.root / "journal/incidents/open/timeout.md"
        path.write_text("open\n")
        self.refuse()
        path.unlink()
        self.desk.write_register(version=2)
        self.assertEqual(self.allow()["tier"], "tier1")


class TestVenueCheckedReplay(PolicyCase):
    def setUp(self):
        super().setUp()
        self.desk.proposal()

    def test_an_id_the_venue_knows_is_refused_even_with_state_wiped(self):
        (self.desk.root / "desk/policy-state/consumed.json").write_text("{}\n")
        self.assertIn("venue already knows",
                      self.refuse(known={"SG-20260912-03-entry"}))

    def test_an_id_the_venue_does_not_know_passes(self):
        self.assertEqual(self.allow(known=set())["tier"], "tier1")

    def test_a_failing_lookup_refuses_rather_than_assuming_fresh(self):
        def explode(coid):
            raise RuntimeError("order endpoint down")
        policy = desk_policy.Policy(account_reader=lambda: 10412.60, positions_reader=lambda: [],
                                    order_lookup_reader=explode, desk=self.desk.root)
        with self.assertRaises(desk_policy.PolicyRefusal) as caught:
            policy.check("POST", "/v2/order/strategy", Desk.body())
        self.assertIn("could not ask the venue", str(caught.exception))


class TestNotionalAndBarCeilings(PolicyCase):
    def setUp(self):
        super().setUp()
        self.desk.proposal()

    def test_one_order_may_not_exceed_the_account(self):
        self.desk.write_register(max_notional=99999)
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}",
             Desk.fields(size="1.0", stop_price="2489", risk_usd="25.00", equity="500.00"))])
        body = Desk.body(size="1.0", sl_order={"type": "stop", "size": "1.0",
                                               "stop_price": "2489", "working_type": "mark_price"})
        self.set_sod(500.0)
        self.assertIn("x equity", self.refuse(body, equity=500.0))

    def test_a_bar_longer_than_a_day_refuses(self):
        self.desk.write_register(bar_seconds=200000)
        self.assertIn("bar_seconds", self.refuse())

    def test_every_reported_ceiling_is_the_one_actually_enforced(self):
        """effective_ceilings() printing a tightened value that no check site reads
        is worse than not printing it: it reports a bound the desk does not have."""
        cases = [
            ("SA_MAX_LIFETIME_DAYS", "14", dict(granted=(dt.date.today() - dt.timedelta(days=20)).isoformat(),
                                                expires=(dt.date.today() + dt.timedelta(days=10)).isoformat()),
             "lifetime"),
            ("MAX_SA_RISK_PER_TRADE", "0.0001", {}, "cap"),
            ("MAX_OPEN_PER_SA", "0", {}, "max_open"),
        ]
        for name, value, register, expected in cases:
            with self.subTest(ceiling=name):
                self.desk.write_register(**register)
                os.environ[f"STRIKEGROK_{name}"] = value
                try:
                    self.assertEqual(desk_policy.effective_ceilings()[name], float(value))
                    self.assertIn(expected, self.refuse())
                finally:
                    os.environ.pop(f"STRIKEGROK_{name}", None)

    def test_a_tightened_consecutive_loss_ceiling_is_enforced(self):
        os.environ["STRIKEGROK_MAX_CONSECUTIVE_LOSSES"] = "2"
        self.addCleanup(lambda: os.environ.pop("STRIKEGROK_MAX_CONSECUTIVE_LOSSES", None))
        self.assertIn("2 consecutive losses", self.refuse(fills=[-1.0, -2.0, 5.0]))

    def test_a_fire_older_than_the_signal_age_ceiling_is_stale(self):
        """A daily bar would otherwise let this morning's fire be sent tonight."""
        self.desk.write_register(bar_seconds=86400)
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(fired_at=utc(-7200)))])
        self.assertIn("signal-age ceiling", self.refuse())

    def test_inside_the_signal_age_ceiling_a_daily_bar_still_works(self):
        self.desk.write_register(bar_seconds=86400)
        self.assertEqual(self.allow()["tier"], "tier1")

    def test_one_bar_still_wins_when_it_is_the_tighter_bound(self):
        self.desk.write_register(bar_seconds=600)
        self.desk.proposal(blocks=[
            (f"RISK | SG-20260912-03 | PASS | SA-03 | {utc()}", Desk.fields(fired_at=utc(-900)))])
        self.assertIn("past one bar", self.refuse())

    def test_the_effective_ceilings_are_reportable(self):
        ceilings = desk_policy.effective_ceilings()
        self.assertEqual(ceilings["MAX_LEVERAGE"], 5)
        self.assertEqual(ceilings["SECONDS_BETWEEN_OPENS"], 60)
        os.environ["STRIKEGROK_MAX_LEVERAGE"] = "2"
        self.addCleanup(lambda: os.environ.pop("STRIKEGROK_MAX_LEVERAGE", None))
        self.assertEqual(desk_policy.effective_ceilings()["MAX_LEVERAGE"], 2)


class TestConcurrentBots(PolicyCase):
    """Six Bots reach one free slot at the same moment.

    Every Bot on the desk drives the same signer. The gate and the send are two
    separate moments, and the gap between them is a network round trip - so the
    question is not whether one process is consistent with itself, it is whether
    six of them are. Taking the reservation at commit time rather than at the
    gate let all six through a max_open of one.
    """

    WORKERS = 6

    def race(self, delay: float) -> list:
        import multiprocessing as mp

        root = self.desk.root
        for i in range(self.WORKERS):
            self.desk.proposal(ticket=f"SG-2026091{i}-0{i}")

        def worker(i, q):
            coid = f"SG-2026091{i}-0{i}-entry"
            body = Desk.body(client_order_id=coid)
            policy = desk_policy.Policy(account_reader=lambda: 10412.60,
                                        positions_reader=lambda: [], desk=root)
            try:
                policy.check("POST", "/v2/order/strategy", body)
                time.sleep(delay)                 # the window between gate and send
                policy.commit(body)
                q.put("ALLOW")
            except desk_policy.PolicyRefusal as exc:
                q.put(f"REFUSE {exc}")

        queue = mp.Queue()
        procs = [mp.Process(target=worker, args=(i, queue)) for i in range(self.WORKERS)]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=30)
        return [queue.get() for _ in range(self.WORKERS)]

    def test_only_one_bot_gets_the_only_slot(self):
        results = self.race(delay=0.0)
        self.assertEqual(sum(r == "ALLOW" for r in results), 1, results)

    def test_a_round_trip_of_delay_does_not_open_the_gate_twice(self):
        results = self.race(delay=0.25)
        self.assertEqual(sum(r == "ALLOW" for r in results), 1, results)
        self.assertTrue(any("already h" in r for r in results if r != "ALLOW"), results)


# ---------------------------------------------------------------------------
# The prompts and the code must not drift apart
# ---------------------------------------------------------------------------
class TestTheAgentPromptsMatchTheCode(PolicyCase):
    """Run the templates out of the shipped agent files through the policy.

    Every defect in this layer's history was the same shape: a document said one
    thing and the code did another. The Risk Manager's PASS template is what the
    desk will actually produce, so it is the input this suite must accept - and
    if someone edits either side without the other, this is where it fails.
    """

    @staticmethod
    def template_from(agent: str) -> str:
        text = (REPO / "agents" / f"{agent}.md").read_text()
        blocks = re.findall(r"```\n(RISK \| SG-.*?)```", text, re.S)
        if not blocks:
            raise AssertionError(f"no RISK template found in agents/{agent}.md")
        return blocks[0]

    def live(self, template: str) -> str:
        """The shipped template carries 2026 dates; make them current so the
        freshness bounds are not what is under test here."""
        template = re.sub(r"fired_at: \S+", f"fired_at: {utc(-60)}", template)
        template = re.sub(r"expires_at: \S+", f"expires_at: {utc(3600)}", template)
        return re.sub(r"(RISK \| SG-\d{8}-\d{2} \| PASS \| SA-03 \| )\S+", r"\g<1>" + utc(), template)

    def test_the_risk_managers_pass_template_parses(self):
        template = self.live(self.template_from("risk-manager"))
        fields = desk_policy.pass_block_fields(template, "SG-20260912-03")
        for key in ("symbol", "side", "size", "price", "order_type", "stop_price",
                    "risk_usd", "equity", "liquidity", "sa", "signal", "fired_at", "expires_at"):
            self.assertIn(key, fields, f"the Risk Manager's template lost '{key}:'")

    def test_the_risk_managers_pass_template_passes_tier_one(self):
        template = self.live(self.template_from("risk-manager"))
        (self.desk.root / "proposals/SG-20260912-03.md").write_text(template)
        fields = desk_policy.pass_block_fields(template, "SG-20260912-03")
        body = {"symbol": fields["symbol"], "side": fields["side"], "size": fields["size"],
                "price": fields["price"], "leverage": fields["leverage"],
                "client_order_id": "SG-20260912-03-entry",
                "sl_order": {"type": "stop", "size": fields["size"],
                             "stop_price": fields["stop_price"], "working_type": "mark_price",
                             "reduce_only": True}}
        self.desk.write_register(markets=[fields["symbol"]],
                                 rules_sha256=hashlib.sha256(RULES.encode()).hexdigest())
        decision = self.desk.policy(equity=float(fields["equity"])).check(
            "POST", "/v2/order/strategy", body)
        self.assertEqual(decision["tier"], "tier1")

    def test_the_prose_liquidity_line_is_not_what_is_enforced(self):
        """The template mentions liquidity twice: once in prose, once as a field.
        Only the field counts, so the field must be there."""
        template = self.template_from("risk-manager")
        fields = desk_policy.pass_block_fields(self.live(template), "SG-20260912-03")
        self.assertEqual(fields.get("liquidity"), "pass")


if __name__ == "__main__":
    unittest.main(verbosity=1)
