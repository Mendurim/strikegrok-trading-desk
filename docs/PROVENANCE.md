# Provenance

## What this repository is

StrikeGrok is a port of [HyperGrok Trading Desk](https://github.com/galleonlabs/hypergrok-trading-desk) v1.4.3 by Andrew Wilkinson and Galleon Labs, MIT licensed.

**Taken from HyperGrok, largely unchanged:** the seven agent roles and their system prompts, the eight `desk-*` process skills, the trade lifecycle and ticket format, the proposal and journal formats, the evidence standard and the `unavailable` verdict, the repository layout, and the `scripts/check*.py` release checks. The venue-specific mechanics inside those files were rewritten; the process they describe is Galleon Labs' design.

**Written for this port:** the ten `strike-*` skills, `skills/strikegrok-bootstrap`, `scripts/strike_request.py`, the rewritten `scripts/opening_bell.py`, `scripts/rehash_template.py`, the Strike half of `scripts/desk_doctor.py`, and the venue-specific sections of `desk-execution-protocol` and `desk-risk-limits`.

## Sources

Verified against the live APIs on 2026-09-10.

| Source | Used for | Licence |
| --- | --- | --- |
| Strike [market](https://github.com/strike-finance/strike-finance-skills/blob/main/openapi/market-api.yaml), [trade](https://github.com/strike-finance/strike-finance-skills/blob/main/openapi/trade-api.yaml) and [user](https://github.com/strike-finance/strike-finance-skills/blob/main/openapi/user-api.yaml) OpenAPI specs | every endpoint, parameter, default, field and order flag in the `strike-*` skills and `strike-api-reference` | MIT |
| [strike-auth SKILL.md](https://github.com/strike-finance/strike-finance-skills/blob/main/skills/strike-auth/SKILL.md) | the exact API-wallet signing scheme, including that `BODY_HASH` is the SHA-256 of the body and of `""` when there is none | MIT |
| RFC 8032 test vector 1 | verifying `scripts/strike_request.py` signs correctly on both its backends before it ever touched a live key | public standard |
| `https://api.strikefinance.org/price/v2` (live) | market list, per-symbol filters, depth behaviour and the `limit` default of 20, funding cadence, response shapes | public API |
| `https://api-v2-testnet.strikefinance.org/price/v2` (live) | the four testnet markets and their empty books - the basis for the rehearsal rule in `strike-setup` | public API |
| `https://mcp.crowdtime.io/mcp` `tools/list` (live) | the research tools in `strike-research-tools` - the optional add-on only; its order tools are documented as deliberately unused | the server's own declaration |
| `strike_scan_markets` (live, read-only) | the add-on's fifteen-market coverage and its liquidity floors | the server's own response |
| `strike_get_mark_price` on `GOLD-PERP` vs REST `XAU-USD` (live) | confirming the add-on's symbol map, including that gold is the one pair that is not a suffix swap | cross-check |
| [Strike Finance docs](https://docs.strikefinance.org) | product context | public docs |
| [strike-finance/strike-finance-skills](https://github.com/strike-finance/strike-finance-skills) | structure survey and the OpenAPI specs above; no skill text reused | MIT |
| [Grok connectors](https://docs.x.ai/grok/connectors), [connector management](https://docs.x.ai/grok/connector-management) | the two routes to the optional add-on, and the caveat that connectors are team-level and undocumented for Bots | public docs |
| HyperGrok's Grok Bot findings | Bots, group chats of six, shared computer, shared skills, routines, approvals, secret store | inherited from the upstream repository |

## Deliberately not used

- **The crowdtime MCP's order tools.** They work, and the desk does not use them: two write paths is exactly what the one-writer rule exists to prevent. Execution is the signed REST API only, and the add-on is read-only research.
- **The MCP's OAuth flow** (dynamic client registration, PKCE). Available at the endpoint, but a static bearer token is simpler for the optional add-on.
- **`@nktkas/hyperliquid` and `hyperliquid-python-sdk`**, which HyperGrok installed. Nothing here needs an SDK: reads are `curl`, signed calls go through `scripts/strike_request.py`, which uses the `cryptography` package when present and `openssl` when not. The desk computer installs no packages.
- **`vault_id`.** Every trading endpoint accepts it, to trade on behalf of a vault as its leader. The desk trades the user's own account only.

## Claims this repository does not make

No tool here was benchmarked, and no strategy is shipped. The `strike_scan_markets` "hotness" score and the indicators in `strike_get_market_snapshot` are the server's computations, reported as such; the skills say explicitly that neither is a signal. Figures in worked examples are illustrative, not results.
