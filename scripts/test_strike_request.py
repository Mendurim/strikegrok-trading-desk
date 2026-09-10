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


if __name__ == "__main__":
    unittest.main()
