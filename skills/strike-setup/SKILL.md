---
name: strike-setup
description: Prepare the desk computer to work with Strike Finance - the two surfaces (public market data and the signed trading API), confirm public connectivity, provision the API wallet through the secure secret store when the user asks to trade, run the read-only readiness check, and understand why the desk rehearses with previews and minimum-size orders rather than testnet. Use during desk setup, when moving between research and trading levels, when a key is rotated, or when any Strike call fails with an environment problem.
license: MIT
metadata:
  version: "2.0.0"
  author: Galleon Labs (HyperGrok), ported for Strike Finance
  category: strike
  network-default: mainnet
---

# Strike setup

Sections 1-3 are read-only and safe at any time. Section 4 provisions the API wallet and happens only when the user asks to trade. Section 5 is the readiness check the Execution Trader runs before its first send.

## 1. The surfaces

| | Market data | Trading |
| --- | --- | --- |
| Base URL | `https://api.strikefinance.org/price` | `https://api.strikefinance.org` |
| Path | `/v2/...` | `/v2/...` |
| Auth | none | Ed25519 API wallet headers |
| Symbols | `ADA-USD`, `XAU-USD` | the same |
| Markets | 31 quoted | 31 tradeable |
| Skill | `strike-market-data` | `strike-auth`, `strike-orders` |

One symbol vocabulary across both, and every market that is quoted can be traded. Testnet is `https://api-v2-testnet.strikefinance.org` for both, with four markets - see section 6 before relying on it.

There is also an **optional** research add-on, the crowdtime MCP (`strike-research-tools`). It is not part of the trading path and the desk works fully without it. If the desk is not using it, do not provision its token at all.

## 2. Install

Nothing to install. Reads are `curl`; signed calls go through `scripts/strike_request.py`, which uses the `cryptography` package when it is present and falls back to `openssl` when it is not. Both are standard on the desk computer.

```bash
python3 --version              # 3.9+
openssl version                # needed only if the cryptography package is absent
cd /workspace/strikegrok && bash scripts/check.sh
```

## 3. Connectivity check (no credential)

```bash
BASE=https://api.strikefinance.org/price
curl -sS -m 10 "$BASE/v2/exchangeInfo" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok,', len(d['symbols']), 'markets, serverTime', d['serverTime'])"
python3 scripts/opening_bell.py --symbol BTC-USD
```

Record the UTC time and result in the desk record. A research desk needs nothing more: briefs, dossiers, the catalyst calendar and the strategy lab all run with no credential.

## 4. The API wallet (only when the user asks to trade)

Full procedure in `strike-auth`. In short:

1. Generate an Ed25519 keypair on the desk computer with `openssl`, then delete the PEM.
2. The user registers the **public** key at `app.strikefinance.org/api-keys`.
3. Both keys go into Grok Bot's secure secret store as `STRIKE_API_PUBLIC_KEY` and `STRIKE_API_PRIVATE_KEY`. Never chat, never a file.
4. No Bot prints, echoes, journals or forwards the private key.

The key can trade; neither the trade API nor the user API exposes a withdraw, deposit or transfer endpoint, so it cannot take money out. That is the bounded credential the desk is designed around. Keep on Strike only what the desk is meant to be trading anyway.

## 5. Readiness check (read-only, no order)

Run before the first send of any session.

```bash
cd /workspace/strikegrok
python3 scripts/strike_request.py GET /v2/account      # auth works, equity is live
python3 scripts/strike_request.py GET /v2/positions    # what is already carried
python3 scripts/strike_request.py GET /v2/openOrders   # and what is already resting
curl -sS "https://api.strikefinance.org/price/v2/exchangeInfo" >/dev/null && echo "market data ok"
```

Four green reads and the desk may trade. Record the UTC time and the equity figure in `desk.md`.

A 401 means the key is unregistered, wrong, or the clock has drifted - `strike-auth` section 8 has the checklist. Stop there. Never retry with a guessed key, and never fall back to an unauthenticated path and report success.

## 6. Rehearsal, honestly

HyperGrok rehearsed every new kind of action on Hyperliquid testnet with play money before touching mainnet. **Strike's testnet cannot do that job.** It lists four markets and its books are empty, so an order there proves the request is well-formed and signed correctly - and nothing else. No fill, no slippage, no trigger behaviour, no protection under load.

It is still worth exactly that much: `--testnet` on `strike_request.py` is a good way to prove the signing scheme works before the key touches real money. Use it for that, and claim nothing more.

The desk replaces the rest with two rules that do work:

1. **The preview block.** Strike's API has no dry-run mode, so every send is built as a JSON file, posted to the floor verbatim as a PREVIEW beside the ticket, and sent only after the user's approval by id. The bytes previewed are the bytes sent (`strike-orders` section 3).
2. **Minimum size first.** The first live run of any new kind of action - first bracket, first trigger, first replace, first close, first TWAP - is done at minimum size. `MIN_NOTIONAL` is $10 across Strike's markets, so a real rehearsal costs a few dollars in fees.

Record in `desk.md` which action kinds have had their minimum-size rehearsal. Do not claim testnet coverage the desk does not have.

## 7. Rotation

Generate a new keypair, register the new public key, replace both secret-store entries, re-run section 5, then delete the old key at `app.strikefinance.org/api-keys`. Record the date in `desk.md` - never the key.
