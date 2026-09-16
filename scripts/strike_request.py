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

Every non-GET goes through `desk_policy.py` before it is signed. The policy
layer is the desk's only enforceable control: prompts live in files that every
Bot can rewrite, and this script is the one thing that holds the API wallet. A
refusal is final here - it is not an error to be retried or worked around.

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
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from desk_policy import Policy, PolicyRefusal  # noqa: E402

# The release this signer belongs to. Kept in step with plugin.json by a test,
# because a User-Agent naming a version the desk stopped being is a support
# question nobody can answer from the logs.
VERSION = "3.1.0"
USER_AGENT = f"strikegrok-desk/{VERSION}"

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
    """Sign with the openssl CLI, for a computer with no `cryptography` package.

    `openssl pkeyutl -rawin` cannot sign a zero-length message ("Could not
    allocate 0 bytes"), where the library backend can. The desk never signs one -
    every message is at least `GET:/path:...` - so the two backends agree on
    every input this script actually produces, which is what the tests pin down.
    """
    if not message:
        raise SystemExit("openssl cannot sign an empty message; this should be unreachable")
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


# Field names the exchange might use for account equity and position size. The
# reads are the policy layer's only unforgeable view of the account, so an
# unrecognised response shape is reported rather than guessed at - guessing low
# on equity loosens the daily-loss stop, and guessing zero on a position would
# let a leverage change through on a live one.
EQUITY_KEYS = ("equity", "accountEquity", "totalEquity", "marginBalance",
               "totalMarginBalance", "accountValue")
SIZE_KEYS = ("size", "positionAmt", "quantity", "amount", "positionSize")
PNL_KEYS = ("realized_pnl", "realizedPnl", "realisedPnl", "pnl", "closed_pnl", "closedPnl")


def _json_or_refuse(status: int, text: str, what: str):
    if status >= 400:
        raise PolicyRefusal(f"{what}: exchange returned HTTP {status}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise PolicyRefusal(f"{what}: response was not JSON")


def _dig(payload, keys):
    """Find the first of `keys` in a response that may be flat or wrapped in
    `data`/`result`."""
    for layer in (payload, (payload or {}).get("data"), (payload or {}).get("result")):
        if isinstance(layer, dict):
            for key in keys:
                if layer.get(key) not in (None, ""):
                    return layer[key]
    return None


def policy_readers(base_url: str, timeout: float):
    def account_reader() -> float:
        status, text = _send(base_url, "GET", "/v2/account", "", [], timeout)
        value = _dig(_json_or_refuse(status, text, "account"), EQUITY_KEYS)
        if value is None:
            raise PolicyRefusal(
                "account: no equity field in GET /v2/account (looked for "
                + ", ".join(EQUITY_KEYS) + "); the daily-loss stop cannot be evaluated")
        return float(value)

    def positions_reader():
        status, text = _send(base_url, "GET", "/v2/positions", "", [], timeout)
        payload = _json_or_refuse(status, text, "positions")
        rows = payload if isinstance(payload, list) else _dig(payload, ("positions", "data", "result"))
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise PolicyRefusal("positions: GET /v2/positions was not a list")
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            size = next((row[k] for k in SIZE_KEYS if row.get(k) not in (None, "")), None)
            if size is not None:
                out.append({"symbol": row.get("symbol"), "size": size})
        return out

    def fills_reader(symbol: str, limit: int):
        """The last `limit` closed PnLs for a symbol, newest first.

        The policy layer recomputes the consecutive-loss kill from this rather
        than believing a Bot's journal. An unrecognised response shape returns
        nothing, which leaves the kill un-evaluated rather than falsely clear -
        the layer treats a short list as "not enough history", not "no losses".
        """
        status, text = _send(base_url, "GET", "/v2/closedPositions", "",
                             [f"symbol={symbol}", f"limit={max(int(limit), 1)}"], timeout)
        payload = _json_or_refuse(status, text, "fills")
        rows = payload if isinstance(payload, list) else _dig(payload, ("positions", "data", "result"))
        out = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            value = next((row[k] for k in PNL_KEYS if row.get(k) not in (None, "")), None)
            if value is not None:
                out.append(float(value))
        return out

    def open_orders_reader():
        """Resting orders, so a standing approval's slot count survives a state
        wipe and an entry that is still working still counts."""
        status, text = _send(base_url, "GET", "/v2/openOrders", "", [], timeout)
        payload = _json_or_refuse(status, text, "open orders")
        rows = payload if isinstance(payload, list) else _dig(payload, ("orders", "data", "result"))
        out = []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict):
                out.append({"symbol": row.get("symbol"),
                            "client_order_id": row.get("client_order_id") or row.get("clientOrderId"),
                            "reduce_only": row.get("reduce_only", row.get("reduceOnly"))})
        return out

    def order_lookup_reader(coid: str) -> bool:
        """Does the venue already know this client order id? Asking it is what
        stops a wiped `consumed.json` from handing back a spent id."""
        status, text = _send(base_url, "GET", "/v2/order", "", [f"client_order_id={coid}"], timeout)
        if status == 404:
            return False
        if status >= 400:
            raise PolicyRefusal(f"order lookup: the venue returned HTTP {status} for {coid}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise PolicyRefusal(f"order lookup: the venue's answer for {coid} was not JSON")
        if isinstance(payload, list):
            return bool(payload)
        return bool(payload) and bool(_dig(payload, ("client_order_id", "clientOrderId", "order_id", "orderId")))

    return account_reader, positions_reader, fills_reader, open_orders_reader, order_lookup_reader


def _never_left(exc: BaseException) -> bool:
    """True only when the request provably never reached the venue.

    A timeout is deliberately excluded: the desk cannot tell a slow answer from a
    lost one, and treating it as "never sent" is how an order gets placed twice.
    """
    if isinstance(exc, socket.timeout) or isinstance(getattr(exc, "reason", None), socket.timeout):
        return False
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (urllib.error.URLError, ConnectionError, socket.gaierror))


def _policy_for(base_url: str, timeout: float) -> Policy:
    account, positions, fills, open_orders, lookup = policy_readers(base_url, timeout)
    return Policy(account_reader=account, positions_reader=positions, fills_reader=fills,
                  open_orders_reader=open_orders, order_lookup_reader=lookup)


def request(base_url: str, method: str, path: str, body: str, query: list[str], timeout: float):
    """Gate, send, then commit. Every caller goes through here, so there is no
    import path that reaches the signer without the policy layer having said yes.

    `commit()` runs on any answer from the exchange, including a rejection,
    because a rejection is still a used client order id. It does not run when the
    send itself never completed - a timeout leaves the id uncommitted, and the
    Execution Trader looks the order up rather than resending.
    """
    policy = _policy_for(base_url, timeout)
    if method.upper() == "GET":
        # Reads are never gated, but they are how the layer learns the day's
        # opening equity. Pinning it on the first read rather than the first
        # trade means a loss taken before the desk next tries to open still
        # counts against the daily stop.
        policy.check("GET", path, {})
        return _send(base_url, method, path, body, query, timeout)

    try:
        parsed = json.loads(body) if body else {}
    except json.JSONDecodeError:
        raise PolicyRefusal("body is not valid JSON")
    decision = policy.check(method, path, parsed)
    sys.stderr.write(f"policy: {decision.get('tier')} - {decision.get('why')}\n")

    try:
        status, text = _send(base_url, method, path, body, query, timeout)
    except (urllib.error.URLError, OSError) as exc:
        # The venue never saw this. Give the reservation back so the same ticket
        # can be retried. A TIMEOUT is not this case - it is an unknown result,
        # and urllib raises it as a socket.timeout inside URLError, so check.
        if _never_left(exc):
            policy.release_pending()
            sys.stderr.write("policy: reservation released; the request never left\n")
        raise
    # The venue answered, so the id is spent either way. A rejection frees the
    # standing-approval slot, because nothing opened.
    policy.commit(parsed, accepted=200 <= int(status) < 300)
    return status, text


def _send(base_url: str, method: str, path: str, body: str, query: list[str], timeout: float):
    public_key = read_key("STRIKE_API_PUBLIC_KEY")
    seed = bytes.fromhex(read_key("STRIKE_API_PRIVATE_KEY"))
    headers = auth_headers(method, path, body, public_key, seed)
    headers["Accept"] = "application/json"
    # Cloudflare in front of the API bans urllib's default User-Agent outright
    # (error 1010), so every request must name itself. Without this the desk
    # gets a 403 that looks nothing like an auth problem.
    headers["User-Agent"] = USER_AGENT
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
    try:
        status, text = request(base_url, args.method, args.path, body, args.query, args.timeout)
    except PolicyRefusal as exc:
        # Not an error to retry. The desk treats an unexpected refusal as an
        # incident and takes the ticket back to the Risk Manager.
        print(f"POLICY REFUSED: {exc}", file=sys.stderr)
        return 3
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
