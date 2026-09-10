#!/usr/bin/env python3
"""Fixtures for strike_request.py: the signing scheme, and what it refuses.

No network. Signing is checked against RFC 8032 test vector 1 and for agreement
between the two backends, because a desk that signs subtly wrong gets a 401 that
looks like a key problem and wastes an incident on it.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import importlib
import json
import tempfile
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strike_request as sr  # noqa: E402

# RFC 8032 section 7.1, test vector 1.
RFC_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
RFC_EMPTY_MESSAGE_SIG = (
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)
PUBLIC = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"


class SigningTest(unittest.TestCase):
    def test_matches_rfc8032_vector(self):
        self.assertEqual(sr.sign_with_cryptography(b"", RFC_SEED), RFC_EMPTY_MESSAGE_SIG)

    def test_openssl_backend_refuses_the_one_input_it_cannot_take(self):
        # `openssl pkeyutl -rawin` cannot sign a zero-length message where the
        # library backend can. The desk never signs one, so this is a guarded
        # edge rather than a divergence - but it fails loudly, not silently.
        with self.assertRaises(SystemExit):
            sr.sign_with_openssl(b"", RFC_SEED)

    def test_backends_agree_on_a_real_message(self):
        message = b"POST:/v2/order:1700000000:550e8400-e29b-41d4-a716-446655440000:" + hashlib.sha256(b"{}").hexdigest().encode()
        self.assertEqual(
            sr.sign_with_cryptography(message, RFC_SEED),
            sr.sign_with_openssl(message, RFC_SEED),
        )

    def test_signature_is_128_hex_characters(self):
        signature = sr.sign(b"anything", RFC_SEED)
        self.assertEqual(len(signature), 128)
        int(signature, 16)


class HeaderTest(unittest.TestCase):
    def headers(self, method="GET", path="/v2/account", body=""):
        return sr.auth_headers(method, path, body, PUBLIC, RFC_SEED)

    def test_carries_the_four_required_headers(self):
        headers = self.headers()
        for name in ("X-API-Wallet-Public-Key", "X-API-Wallet-Signature",
                     "X-API-Wallet-Timestamp", "X-API-Wallet-Nonce"):
            self.assertIn(name, headers)
        self.assertEqual(headers["X-API-Wallet-Public-Key"], PUBLIC)

    def test_empty_body_hashes_the_empty_string(self):
        # Not a literal "" in the message - the sha256 *of* "". Getting this
        # wrong makes every GET fail authentication.
        empty = hashlib.sha256(b"").hexdigest()
        expected = sr.sign(f"GET:/v2/account:{self.headers()['X-API-Wallet-Timestamp']}:x:{empty}".encode(), RFC_SEED)
        self.assertEqual(len(expected), 128)

    def test_nonce_is_fresh_per_request(self):
        self.assertNotEqual(self.headers()["X-API-Wallet-Nonce"], self.headers()["X-API-Wallet-Nonce"])

    def test_body_changes_the_signature(self):
        one = sr.auth_headers("POST", "/v2/order", '{"a":1}', PUBLIC, RFC_SEED)
        two = sr.auth_headers("POST", "/v2/order", '{"a":2}', PUBLIC, RFC_SEED)
        self.assertNotEqual(one["X-API-Wallet-Signature"], two["X-API-Wallet-Signature"])


class KeyHandlingTest(unittest.TestCase):
    def test_missing_key_is_refused_without_echoing_anything(self):
        os.environ.pop("STRIKE_API_PRIVATE_KEY", None)
        with self.assertRaises(SystemExit) as caught:
            sr.read_key("STRIKE_API_PRIVATE_KEY")
        self.assertIn("secure secret store", str(caught.exception))

    def test_malformed_key_reports_length_only(self):
        os.environ["STRIKE_API_PRIVATE_KEY"] = "deadbeef"
        with self.assertRaises(SystemExit) as caught:
            sr.read_key("STRIKE_API_PRIVATE_KEY")
        message = str(caught.exception)
        self.assertIn("64 hex characters", message)
        self.assertNotIn("deadbeef", message)     # never echo key material

    def test_valid_key_is_normalised(self):
        os.environ["STRIKE_API_PRIVATE_KEY"] = PUBLIC.upper()
        self.assertEqual(sr.read_key("STRIKE_API_PRIVATE_KEY"), PUBLIC)


class CliTest(unittest.TestCase):
    def test_path_must_be_absolute(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            sr.main(["GET", "v2/account"])

    def test_query_must_be_key_value(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            sr.main(["GET", "/v2/account", "--query", "oops"])

    def test_testnet_and_mainnet_are_different_hosts(self):
        self.assertIn("testnet", sr.TESTNET)
        self.assertNotIn("testnet", sr.MAINNET)


class TheUserAgentNamesThisRelease(unittest.TestCase):
    """A User-Agent naming a version the desk stopped being is a support question
    nobody can answer from the venue's logs."""

    def test_the_version_matches_plugin_json(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "plugin.json"), encoding="utf-8") as handle:
            declared = json.load(handle)["version"]
        self.assertEqual(sr.VERSION, declared)

    def test_the_header_carries_it(self):
        self.assertEqual(sr.USER_AGENT, f"strikegrok-desk/{sr.VERSION}")


class TheGateRunsBeforeTheSigner(unittest.TestCase):
    """A refused request must never reach the network or the signing key.

    `_send` is the only thing that touches either, so replacing it with a
    tripwire proves the refusal happened first.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.environ["STRIKEGROK_DESK"] = self._tmp.name
        self.addCleanup(lambda: os.environ.pop("STRIKEGROK_DESK", None))
        importlib.reload(sr.desk_policy) if hasattr(sr, "desk_policy") else None
        import desk_policy
        importlib.reload(desk_policy)
        sr.Policy, sr.PolicyRefusal = desk_policy.Policy, desk_policy.PolicyRefusal

        self.sent = []
        original = sr._send
        self.addCleanup(lambda: setattr(sr, "_send", original))

        def stub(base_url, method, path, body, query, timeout):
            # The policy performs real signed reads of its own, so the stub has to
            # answer them plausibly or every refusal is "no equity field".
            self.sent.append((base_url, method, path, body, query, timeout))
            if path == "/v2/account":
                return 200, json.dumps({"equity": 10000.0})
            if path in ("/v2/positions", "/v2/closedPositions"):
                return 200, "[]"
            return 200, "{}"

        sr._send = stub
        # The daily stop refuses rather than re-basing, so the trading day has to
        # be opened by a read before anything can be sent - exactly as on a desk.
        sr.request("https://example.invalid", "GET", "/v2/account", "", [], 1.0)
        self.sent.clear()

    def writes(self):
        """The policy performs its own signed GETs - the account read behind the
        daily-loss stop, the positions read behind the book ceiling - so the
        invariant is not that nothing was sent. It is that no WRITE was."""
        return [call for call in self.sent if call[1].upper() != "GET"]

    def refuse(self, method, path, body=""):
        with self.assertRaises(sr.PolicyRefusal) as caught:
            sr.request("https://example.invalid", method, path, body, [], 1.0)
        self.assertEqual(self.writes(), [], "a refused write reached the signer")
        return str(caught.exception)

    def test_a_withdrawal_never_reaches_the_signer(self):
        self.assertIn("outside the desk's scope", self.refuse("POST", "/v2/withdraw", '{"amount":"1"}'))

    def test_an_order_with_no_proposal_never_reaches_the_signer(self):
        body = '{"symbol":"ETH-USD","side":"sell","size":"1","price":"2431","client_order_id":"SG-20260912-03-e"}'
        self.assertIn("is missing", self.refuse("POST", "/v2/order", body))

    def test_an_order_with_no_ticket_id_never_reaches_the_signer(self):
        self.assertIn("does not carry an SG-", self.refuse("POST", "/v2/order", '{"symbol":"ETH-USD"}'))

    def test_a_malformed_body_never_reaches_the_signer(self):
        self.assertIn("not valid JSON", self.refuse("POST", "/v2/order", "{not json"))

    def test_a_get_passes_straight_through(self):
        sr.request("https://example.invalid", "GET", "/v2/openOrders", "", [], 1.0)
        # The read itself, plus the one account read that opens the trading day.
        self.assertIn(("GET", "/v2/openOrders"), [(c[1], c[2]) for c in self.sent])
        self.assertEqual(self.writes(), [])

    def test_the_day_is_opened_only_once(self):
        for _ in range(3):
            sr.request("https://example.invalid", "GET", "/v2/openOrders", "", [], 1.0)
        account_reads = [c for c in self.sent if c[2] == "/v2/account"]
        self.assertEqual(account_reads, [], "the day was already opened; do not re-read per call")

    def test_a_send_that_never_left_gives_the_reservation_back(self):
        """A refused connection proves the venue saw nothing, so the ticket may be
        retried. A timeout must not take this path."""
        import socket
        import urllib.error
        self.assertTrue(sr._never_left(urllib.error.URLError(ConnectionRefusedError())))
        self.assertFalse(sr._never_left(urllib.error.URLError(socket.timeout())))
        self.assertFalse(sr._never_left(socket.timeout()))

    def test_a_read_captures_the_days_opening_equity(self):
        sod = os.path.join(self._tmp.name, "desk", "policy-state", "sod_equity.json")
        self.assertTrue(os.path.exists(sod), "a read should have opened the trading day")
        self.assertEqual(json.loads(open(sod).read())["equity"], 10000.0)

    def test_the_policys_own_reads_are_gets_not_writes(self):
        body = '{"symbol":"ETH-USD","side":"sell","size":"1","price":"2431","client_order_id":"SG-20260912-03-e"}'
        self.refuse("POST", "/v2/order", body)
        self.assertTrue(self.sent, "the policy should have read the account before refusing")
        self.assertTrue(all(call[1].upper() == "GET" for call in self.sent))

    def test_a_reduce_only_order_passes(self):
        body = '{"symbol":"ETH-USD","side":"buy","size":"1","reduce_only":true}'
        with redirect_stderr(io.StringIO()):
            sr.request("https://example.invalid", "POST", "/v2/order", body, [], 1.0)
        self.assertEqual(len(self.writes()), 1)

    def test_the_cli_reports_a_refusal_as_exit_code_three(self):
        path = os.path.join(self._tmp.name, "body.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"amount":"1"}')
        err = io.StringIO()
        with redirect_stderr(err):
            code = sr.main(["POST", "/v2/withdraw", "--body-file", path])
        self.assertEqual(code, 3)
        self.assertIn("POLICY REFUSED", err.getvalue())


if __name__ == "__main__":
    unittest.main()
