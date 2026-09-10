---
name: strike-auth
description: The desk's Strike Finance API wallet - generating an Ed25519 keypair, registering the public key, the request signing scheme (method, path, timestamp, nonce, body hash), the signed-request helper every authenticated call goes through, what the key can and cannot do, and rotation. Use when connecting the desk to a Strike account, when any authenticated call returns 401, or when a key is rotated. Read this before strike-account and strike-orders.
license: MIT
metadata:
  version: "1.0.0"
  author: Mendurim
  category: strike
  network-default: mainnet
---

# Strike auth

Public market data needs no credential (`strike-market-data`). Everything else - the account, positions, history, and every order - is signed with the desk's **API wallet**.

## 1. What an API wallet is

An Ed25519 keypair you generate yourself and register with your Strike account. It signs trading requests on the account's behalf. It is not your Cardano wallet, and it is not a seed phrase.

**What it can do:** read the account, place, replace and cancel orders, change leverage and margin mode, run TWAPs.

**What it cannot do:** neither the trade API nor the user API exposes a withdraw, deposit or transfer endpoint. `withdraw` appears only as a read-only transaction-history type and a `maxWithdrawAmount` field. Moving money happens in the Strike app, with you, through a flow that needs a chain signature this key cannot produce.

That is the bounded credential the desk is built around: it can trade, and it cannot take the money out. Treat it as trade-only, and still keep on Strike only what the desk is meant to be trading.

## 2. Generate the keypair

On the desk computer, with nothing installed:

```bash
umask 077
openssl genpkey -algorithm ed25519 -out /tmp/api-wallet.pem

# Private key seed - 64 hex characters. This is the secret.
openssl pkey -in /tmp/api-wallet.pem -outform DER | tail -c 32 | xxd -p -c 64

# Public key - 64 hex characters. This is what you register.
openssl pkey -in /tmp/api-wallet.pem -pubout -outform DER | tail -c 32 | xxd -p -c 64
```

Then **delete the PEM**: `shred -u /tmp/api-wallet.pem` (or `rm -P`, or `rm` if neither exists). The desk keeps keys in the secret store, not on disk.

## 3. Register the public key

The user goes to **`app.strikefinance.org/api-keys`**, connects their wallet, and registers the **public** key from step 2. Only the public key leaves the desk computer. No Bot ever asks for, reads, prints or stores the private key.

## 4. Put both into the secret store

Through Grok Bot's secure secret card, never through chat:

| Name | Value |
| --- | --- |
| `STRIKE_API_PUBLIC_KEY` | the 64-hex public key |
| `STRIKE_API_PRIVATE_KEY` | the 64-hex private seed |

All the user's Bots share one computer, so anything written to a file is readable by every Bot on the desk. The secret store is the only route.

If the private key ever appears in a conversation, a log, a journal entry or a receipt, it is **burned**. Say so plainly, and have the user register a new key and delete the old one at `app.strikefinance.org/api-keys` before the desk trades again.

## 5. The signing scheme

Every authenticated request carries four headers:

| Header | Value |
| --- | --- |
| `X-API-Wallet-Public-Key` | the public key, 64 hex |
| `X-API-Wallet-Signature` | Ed25519 signature, 128 hex |
| `X-API-Wallet-Timestamp` | Unix seconds |
| `X-API-Wallet-Nonce` | a fresh UUID v4, one per request |

The signed message is:

```
{METHOD}:{PATH}:{TIMESTAMP}:{NONCE}:{BODY_HASH}
```

- `METHOD` uppercase.
- `PATH` is the **path alone**. A query string is part of the URL but not of the signed message: a filtered history read signs `/v2/history/order`, not `/v2/history/order?symbol=ADA-USD`.
- `BODY_HASH` is the SHA-256 hex digest of the exact body bytes sent, and the digest of the empty string when there is no body.
- The signature is Ed25519 over the UTF-8 message.

The body must be hashed **exactly as sent**. Re-serialising JSON between hashing and sending changes the bytes and invalidates the signature; that is the commonest cause of a 401 that looks like a key problem.

## 6. The helper every call goes through

`scripts/strike_request.py` implements all of the above. It signs with the `cryptography` package when it is present and falls back to `openssl` when it is not, so the desk installs nothing. It never prints either key.

```bash
cd /workspace/strikegrok

python3 scripts/strike_request.py GET /v2/account
python3 scripts/strike_request.py GET /v2/positions
python3 scripts/strike_request.py GET /v2/history/order --query symbol=ADA-USD --query limit=10

# A body comes from a file or stdin, never from an inline shell string, so the
# exact bytes signed are the exact bytes sent.
python3 scripts/strike_request.py POST /v2/order --body-file ticket.json
```

Add `--testnet` to route to `https://api-v2-testnet.strikefinance.org`. Bear in mind its books are empty (`strike-setup` section 6).

Every skill on this desk assumes this helper. Do not hand-roll signing in a one-off snippet: a subtly wrong body hash is a silent 401, and a subtly wrong path is a signature that verifies against the wrong request.

## 7. Connectivity check

```bash
python3 scripts/strike_request.py GET /v2/account
```

A JSON account object means the key is registered, the clock is close enough, and the desk can read. Record the UTC time and the equity figure in `desk.md` - never the key.

## 8. When it returns 401

Work through these in order, and do not retry with a guessed key:

| Cause | Check |
| --- | --- |
| Key not registered, or deleted | `app.strikefinance.org/api-keys` shows this public key |
| Wrong key in the secret store | the public key the helper sends matches the registered one |
| Clock skew | `date -u` on the desk computer against real time; the timestamp is checked |
| Reused nonce | the helper generates a fresh UUID per request; a retry loop that reuses one will fail |
| Body hash mismatch | the body was re-serialised after hashing, or a query string was signed into the path |

A 401 stops the desk. It never falls back to an unauthenticated path and reports success.

## 9. Rotation

Generate a new keypair (step 2), register the new public key (step 3), replace both secret-store entries (step 4), re-run the connectivity check (step 7), then delete the old key at `app.strikefinance.org/api-keys`.

Rotate when the key may have been exposed, when someone leaves, and on whatever schedule the user sets. Record the date in `desk.md`. Never the key.
