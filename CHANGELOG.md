# Changelog

All notable changes to StrikeGrok are recorded here. Versions follow the release tags the bootstrap skill pins.

## v1.0.0

First StrikeGrok release: a port of [HyperGrok Trading Desk](https://github.com/galleonlabs/hypergrok-trading-desk) v1.4.3 from Hyperliquid to Strike Finance.

**Unchanged** - the part that makes this a desk rather than a bot:

- Seven roles with full system prompts, six on the Trading Floor and the Trade Reviewer off it.
- The trade lifecycle: idea, evidence, risk sign-off, approval by ticket id, one send, reconciliation, review.
- The evidence standard, the `unavailable` verdict, one-writer separation, and the rule that no Bot may write, quote forward or infer the user's approval.
- The eight `desk-*` process skills, the proposal and journal formats, and the incident playbooks.

**Replaced** - the venue integration:

- The eight `hyperliquid-*` skills are gone. In their place: `strike-setup`, `strike-auth`, `strike-market-data`, `strike-account`, `strike-orders`, `strike-positions`, `strike-advanced`, `strike-websocket`, `strike-api-reference`, plus the optional `strike-research-tools`.
- **Market data** comes from Strike's public Price Service (`api.strikefinance.org/price/v2`), unauthenticated.
- **Execution** goes through the signed Strike API (`api.strikefinance.org/v2`), authenticated with an Ed25519 **API wallet** the user registers at `app.strikefinance.org/api-keys`. `scripts/strike_request.py` is the desk's single signing primitive; it uses the `cryptography` package when present and `openssl` when not, so the desk installs nothing. Its signing was verified against RFC 8032 test vector 1 on both backends.
- One `-USD` symbol vocabulary across both surfaces, and all thirty-one quoted markets are tradeable.
- `scripts/opening_bell.py` rewritten against the Price Service. Depth is read at `limit=1000` because the endpoint defaults to twenty levels a side.
- `scripts/desk_doctor.py` checks the Price Service only and never authenticates: a doctor that could verify the write path would be a doctor that could place an order.
- `scripts/rehash_template.py` re-pins the reviewed skill hashes after any skill edit.
- Ticket ids are `SG-YYYYMMDD-NN`. The desk workspace reference is `/workspace/strikegrok`.

**Capabilities the venue provides, and the desk now uses:**

- **Client order ids** on every order, including one per leg of a bracket, with `GET /v2/order` looking an order up by that id. A lost response is a lookup, not a guess - this restores the reconciliation guarantee HyperGrok got from `expiresAfter`.
- Bracket strategy orders, `replace` and `replace-batch`, batch orders, TWAP, `cancel-all`, margin-mode and isolated-margin control, trailing stops, `post_only`, `price_protect`, `close_position`, `working_type`, and a `slippage` bound on market orders.
- `GET /v2/history/funding` and `GET /v2/history/transaction`, so holding cost and fees are read rather than derived.

**Weaker than HyperGrok, and documented as such:**

- **No dry-run mode.** The desk replaces it with a preview block: every request is built as a JSON file, posted to the floor verbatim, and that same file is sent. A discipline rather than a gate, so Grok Bot's Require Approval rule matters more, not less.
- **No order expiry.** Elapsed time proves nothing about a lost send; only the client-order-id lookup does.
- **No usable testnet.** Strike's testnet lists four markets with empty books, so it proves the request is well-formed and correctly signed and nothing else. Rehearsal is the preview block plus a minimum-size live run ($10 notional) for each new kind of action.
- **No dead-man's switch.** Strike has none, and the desk does not improvise one or claim to have it.

**Optional research add-on.** The crowdtime MCP (`mcp.crowdtime.io`) supplies a market liquidity screen, computed technical indicators, crypto/equity/dividend research playbooks, Bodega prediction markets and Discord alerts - none of which Strike's own API computes. It is not on the trading path, its order tools are deliberately unused, and the desk trades fully without it.
