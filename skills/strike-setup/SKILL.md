---
name: strike-setup
description: Prepare the desk computer to work with Strike Finance - confirm the public Price Service answers, install nothing that is not needed, learn the symbol map, and (only when the user asks to trade) provision the crowdtime MCP bearer token through Grok Bot's secure secret store and verify it with read-only calls. Use during desk setup, when moving between research and trading levels, when a token is rotated, or when any Strike call fails with an environment problem.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs (HyperGrok), ported for Strike Finance
  category: strike
  network-default: mainnet
---

# Strike setup

Sections 1-3 are read-only and safe at any time. Section 4 provisions the token and happens only when the user asks to trade. Section 5 is the readiness check the Execution Trader runs before its first send.

## 1. The two services

The Strike desk talks to two different things, and conflating them is the most common setup mistake.

| | Market data | Execution |
| --- | --- | --- |
| Service | Strike public Price Service | crowdtime MCP (`strikealgobot-mcp`) |
| URL | `https://api.strikefinance.org/price` | `https://mcp.crowdtime.io/mcp` |
| Auth | none | `Authorization: Bearer <token>` |
| Symbols | `BTC-USD`, `XAU-USD` | `BTC-PERP`, `GOLD-PERP` |
| Markets | 31 quoted | 15 tradeable |
| Skill | `strike-market-data` | `strike-mcp`, `strike-orders` |

A market the Price Service quotes is not necessarily one the desk can trade. The full symbol table and the fifteen tradeable markets are in `strike-mcp`.

Testnet exists at `https://api-v2-testnet.strikefinance.org/price` with four markets, but **its books are empty**. It proves plumbing, never fills. See section 6.

## 2. Install

Nothing to install. Every read is `curl` plus `python3` from the standard library, and every write is a JSON-RPC POST. Install `jq` if it is missing, for convenience only.

```bash
python3 --version          # 3.9+; the desk's scripts are standard library only
curl --version | head -1
```

The desk's own scripts need no packages:

```bash
cd /workspace/strikegrok && bash scripts/check.sh
```

## 3. Connectivity check (no token)

```bash
BASE=https://api.strikefinance.org/price
curl -sS -m 10 "$BASE/v2/exchangeInfo" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok,', len(d['symbols']), 'markets, serverTime', d['serverTime'])"
python3 scripts/opening_bell.py --symbol BTC-USD
```

Both should answer. Record the UTC time and result in the desk record. A research desk needs nothing more than this - briefs, research, dossiers and the strategy lab all run with no credential at all.

## 4. The bearer token (only when the user asks to trade)

The crowdtime token is the desk's single credential and it is **more powerful than the API wallet HyperGrok used on Hyperliquid**. That key could trade but not withdraw. This token is whatever crowdtime's account allows, and the desk must assume it can move money. Size the account accordingly: keep on Strike only what the desk is meant to be trading.

Provisioning, in order:

1. The user creates the token in **crowdtime API Settings**. Not in chat, not pasted to a Bot.
2. The user adds it to **Grok Bot's secure secret store** as `STRIKE_MCP_TOKEN`. The secret card is the only route onto the computer.
3. No Bot ever prints it, echoes it, writes it to a file, includes it in a receipt, or repeats it to another Bot. All the user's Bots share one computer, so anything written down is readable by every Bot on the desk.
4. If the token ever appears in a conversation, it is burned. Say so plainly and have the user rotate it before the desk trades again.

Optionally the user may instead add the server as a Grok connector (`grok.com/connectors` -> New Connector -> Custom -> `https://mcp.crowdtime.io/mcp`). That needs a team admin, and xAI does not document whether a connector reaches a named Bot. Test it; fall back to the secret store if the Execution Trader has no such tool. Both routes are in `strike-mcp` section 2.

## 5. Readiness check (read-only, no order)

Run before the first send of any session. Nothing here writes; no tool is called with `confirm`.

```bash
call_mcp strike_get_account_balance '{}'          # auth works, equity is live
call_mcp strike_scan_markets '{"top":5,"days":7}' # the tradeable universe answers
call_mcp strike_get_open_positions '{}'           # what is already carried
call_mcp strike_get_open_orders '{}'              # and what is already resting
```

Four green reads and the desk may trade. Record the UTC time and the equity figure in `desk.md`.

A 401 means the token is missing, wrong or revoked: stop, and ask the user to check crowdtime API Settings. Never retry with a guessed token, and never fall back to an unauthenticated path and report success.

## 6. Rehearsal, honestly

HyperGrok rehearsed every new kind of action on Hyperliquid testnet with play money before it touched mainnet. **Strike's testnet cannot do that job.** It lists four markets and its books are empty, so an order there proves the call is well-formed and nothing else - no fill, no slippage, no trigger behaviour, no protection under load.

The desk replaces that rule with two that work:

1. **The dry run is the rehearsal.** Every write tool defaults to `confirm=false` and returns a preview. Every send is previewed and read back to the floor first, every time. This is stronger than testnet was: it exercises the real account, the real symbol, the real constraints.
2. **The first live action of any new kind is minimum size.** `MIN_NOTIONAL` is $10 across Strike's markets, so a real rehearsal on mainnet costs a few dollars in fees. The first bracket, the first trigger, the first close each get one minimum-size run before the desk uses them at size.

Record in `desk.md` which action kinds have had their minimum-size rehearsal. Do not claim testnet coverage the desk does not have.

## 7. Rotation

Rotating is: create the new token in crowdtime API Settings, replace the secret store entry, re-run section 5, then revoke the old one. Do it when the token may have been exposed, when someone leaves, and on whatever schedule the user sets. Record the date in `desk.md` - never the token.
