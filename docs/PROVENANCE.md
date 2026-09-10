# Provenance

Everything here is written against public documentation and the live APIs, and verified on 2026-09-10.

## Sources

| Source | Used for | Licence |
| --- | --- | --- |
| Strike [market](https://github.com/strike-finance/strike-finance-skills/blob/main/openapi/market-api.yaml), [trade](https://github.com/strike-finance/strike-finance-skills/blob/main/openapi/trade-api.yaml) and [user](https://github.com/strike-finance/strike-finance-skills/blob/main/openapi/user-api.yaml) OpenAPI specs | every endpoint, parameter, default, field and order flag in the `strike-*` skills and `strike-api-reference` | MIT |
| [strike-auth SKILL.md](https://github.com/strike-finance/strike-finance-skills/blob/main/skills/strike-auth/SKILL.md) | the API-wallet signing scheme, including that `BODY_HASH` is the SHA-256 of the body and of `""` when there is none | MIT |
| `https://api.strikefinance.org/price/v2` (live) | market list, per-symbol filters, depth behaviour and the `limit` default of 20, funding cadence, response shapes | public API |
| `https://api-v2-testnet.strikefinance.org/price/v2` (live) | the four testnet markets and their empty books - the basis for the rehearsal rule in `strike-setup` | public API |
| RFC 8032 test vector 1 | verifying `scripts/strike_request.py` signs correctly on both its backends before the key ever touched a live account | public standard |
| [Strike Finance docs](https://docs.strikefinance.org) | product context | public docs |
| [Grok connectors](https://docs.x.ai/grok/connectors), [connector management](https://docs.x.ai/grok/connector-management) | the two routes to the optional research add-on, and the caveat that connectors are team-level and undocumented for Bots | public docs |
| `https://mcp.crowdtime.io/mcp` `tools/list` (live) | the research tools in `strike-research-tools` - the optional add-on only | the server's own declaration |
| Grok Bot product behaviour | Bots, group chats of six, the shared computer, shared skills, routines, approvals, the secret store | public docs |

## Deliberately not used

- **The research add-on's order tools.** They work, and the desk does not use them: two write paths is exactly what the one-writer rule exists to prevent. Execution is the signed REST API only.
- **OAuth against the research add-on** (dynamic client registration, PKCE). Available, but a static token is simpler for an optional read-only path.
- **Any SDK.** Reads are `curl`; signed calls go through `scripts/strike_request.py`, which uses the `cryptography` package when present and `openssl` when not. The desk computer installs no packages.
- **`vault_id`.** Every trading endpoint accepts it, to trade on behalf of a vault as its leader. The desk trades the user's own account only.

## Claims this repository does not make

No tool here was benchmarked, and no strategy is shipped. Indicators and liquidity scores from the optional add-on are that service's computations, reported as such; the skills say explicitly that neither is a signal. Figures in worked examples are illustrative, not results.
