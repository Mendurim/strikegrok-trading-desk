#!/usr/bin/env python3
"""Make one signed Strike Finance API request with the desk's API wallet.

The desk's single execution primitive. Reads the API wallet from the
environment, signs the request the way Strike's API Wallet scheme requires, and
prints the response body.

    STRIKE_API_PUBLIC_KEY   64 hex characters, the Ed25519 public key
    STRIKE_API_PRIVATE_KEY  64 hex characters, the Ed25519 private key seed
    STRIKE_API_BASE_URL     optional; defaults to mainnet

Neither key is ever printed, logged, or included in an error message. A failure
names the status and the response body, which is what the desk needs to
reconcile, and nothing that would burn the key.

Signing (from Strike's API Wallet scheme):

    message   = {METHOD}:{PATH}:{TIMESTAMP}:{NONCE}:{BODY_HASH}
    BODY_HASH = sha256 hex of the exact request body, or of "" when there is none
    signature = Ed25519 over the UTF-8 message, as 128 hex characters

`PATH` is the path alone. A query string is part of the URL but not the signed
message, so a signed GET with filters signs `/v2/history/order`, not
`/v2/history/order?symbol=ADA-USD`.

Usage:
    strike_request.py GET  /v2/account
    strike_request.py GET  /v2/positions
    strike_request.py GET  /v2/history/order --query symbol=ADA-USD --query limit=10
    strike_request.py POST /v2/order --body-file ticket.json
    echo '{"symbol":"ADA-USD"}' | strike_request.py POST /v2/order --body-file -
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

MAINNET = "https://api.strikefinance.org"
TESTNET = "https://api-v2-testnet.strikefinance.org"
HEX = "0123456789abcdefABCDEF"


def read_key(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise SystemExit(
            f"{name} is not set. The API wallet comes from the secure secret store; "
            "it is never pasted into a command or a file."
        )
    if len(value) != 64 or any(c not in HEX for c in value):
        # Say what is wrong with the shape without echoing any of the value.
        raise SystemExit(f"{name} must be 64 hex characters; got {len(value)} characters")
    return value.lower()


def sign_with_cryptography(message: bytes, seed: bytes) -> str | None:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        return None
    return Ed25519PrivateKey.from_private_bytes(seed).sign(message).hex()


# DER prefix for a PKCS#8 Ed25519 private key: version 0, algorithm 1.3.101.112,
# then a 32-byte OCTET STRING inside an OCTET STRING. Wrapping the raw seed this
# way is what lets openssl load it, so the desk can sign with no Python packages.
PKCS8_ED25519_PREFIX = bytes.fromhex("302e020100300506032b657004220420")


def sign_with_openssl(message: bytes, seed: bytes) -> str:
    der = PKCS8_ED25519_PREFIX + seed
    with tempfile.TemporaryDirectory() as tmp:
        key_path = os.path.join(tmp, "k.der")
        msg_path = os.path.join(tmp, "m.bin")
        # 0600 before the key material is written, never after.
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(der)
        with open(msg_path, "wb") as handle:
            handle.write(message)
        try:
            done = subprocess.run(
                ["openssl", "pkeyutl", "-sign", "-inkey", key_path,
                 "-keyform", "DER", "-rawin", "-in", msg_path],
                capture_output=True, check=True,
            )
        except FileNotFoundError:
            raise SystemExit(
                "cannot sign: neither the 'cryptography' package nor 'openssl' is available"
            ) from None
        except subprocess.CalledProcessError as exc:
            raise SystemExit(f"openssl could not sign the request: {exc.stderr.decode(errors='replace')}") from None
    return done.stdout.hex()


def sign(message: bytes, seed: bytes) -> str:
    return sign_with_cryptography(message, seed) or sign_with_openssl(message, seed)


def auth_headers(method: str, path: str, body: str, public_key: str, seed: bytes) -> dict:
    timestamp = str(int(time.time()))
    nonce = str(uuid.uuid4())
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    message = f"{method.upper()}:{path}:{timestamp}:{nonce}:{body_hash}".encode("utf-8")
    return {
        "X-API-Wallet-Public-Key": public_key,
        "X-API-Wallet-Signature": sign(message, seed),
        "X-API-Wallet-Timestamp": timestamp,
        "X-API-Wallet-Nonce": nonce,
    }


def request(base_url: str, method: str, path: str, body: str, query: list[str], timeout: float):
    public_key = read_key("STRIKE_API_PUBLIC_KEY")
    seed = bytes.fromhex(read_key("STRIKE_API_PRIVATE_KEY"))
    headers = auth_headers(method, path, body, public_key, seed)
    headers["Accept"] = "application/json"
    if body:
        headers["Content-Type"] = "application/json"
    url = f"{base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode([q.split('=', 1) for q in query])}"
    req = urllib.request.Request(
        url, data=body.encode("utf-8") if body else None, headers=headers, method=method.upper()
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # An HTTP error is an answer from the exchange, not a crash. Hand the
        # status and body back so the desk can read the rejection reason.
        return exc.code, exc.read().decode("utf-8", errors="replace")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("method", choices=["GET", "POST", "PUT", "DELETE", "get", "post", "put", "delete"])
    parser.add_argument("path", help="request path, e.g. /v2/account")
    parser.add_argument("--body-file", help="JSON body file, or - for stdin")
    parser.add_argument("--query", action="append", default=[], metavar="K=V")
    parser.add_argument("--base-url", default=os.environ.get("STRIKE_API_BASE_URL", MAINNET))
    parser.add_argument("--testnet", action="store_true", help=f"shorthand for --base-url {TESTNET}")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)

    if not args.path.startswith("/"):
        parser.error("path must start with /")
    if any("=" not in q for q in args.query):
        parser.error("--query takes KEY=VALUE")

    body = ""
    if args.body_file:
        raw = sys.stdin.read() if args.body_file == "-" else open(args.body_file, encoding="utf-8").read()
        try:
            # Re-serialise so the bytes hashed are exactly the bytes sent.
            body = json.dumps(json.loads(raw), separators=(",", ":"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"body is not valid JSON: {exc}") from None

    base_url = TESTNET if args.testnet else args.base_url
    status, text = request(base_url, args.method, args.path, body, args.query, args.timeout)
    try:
        print(json.dumps(json.loads(text), indent=2))
    except json.JSONDecodeError:
        print(text)
    if status >= 400:
        print(f"HTTP {status}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
