# Changelog

All notable changes to StrikeGrok are recorded here. Versions follow the release tags the bootstrap skill pins.

## v1.0.0

First StrikeGrok release: a port of [HyperGrok Trading Desk](https://github.com/galleonlabs/hypergrok-trading-desk) v1.4.3 from Hyperliquid to Strike Finance.

**Unchanged** — the part that makes this a desk rather than a bot:

- Seven roles with full system prompts, six on the Trading Floor and the Trade Reviewer off it.
- The trade lifecycle: idea, evidence, risk sign-off, approval by ticket id, one send, reconciliation, review.
- The evidence standard, the `unavailable` verdict, one-writer separation, and the rule that no Bot may write, quote forward or infer the user's approval.
- The eight `desk-*` process skills, the proposal and journal formats, and the incident playbooks.

**Replaced** — the venue integration:

- The eight `hyperliquid-*` skills are gone. In their place: `strike-setup`, `strike-mcp`, `strike-market-data`, `strike-account`, `strike-orders`, `strike-positions`, `strike-websocket`, `strike-research-tools` and `strike-api-reference`.
- Market data now comes from Strike's public REST Price Service (`api.strikefinance.org/price`, `/v2`), unauthenticated, thirty-one markets.
- Execution now goes through the crowdtime MCP (`mcp.crowdtime.io/mcp`, `strikealgobot-mcp`), twenty-two tools, fifteen tradeable markets, bearer token auth.
- `scripts/opening_bell.py` rewritten against the Price Service. Depth is read at `limit=1000` because the endpoint defaults to twenty levels a side; no coarser re-paging is needed, since the whole resting book returns in one response.
- `scripts/desk_doctor.py` checks the Price Service only. It never authenticates to the MCP: a doctor that could verify the write path would be a doctor that could place an order.
- Ticket ids are `SG-YYYYMMDD-NN`. The desk workspace reference is `/workspace/strikegrok`.

**New controls, from the venue:**

- **Dry run by default.** Every MCP write tool returns a preview unless `confirm=true`. The desk previews every order and reads it back to the floor before sending. This gate is in the transport, not in a Bot's good behaviour, and it is stronger than anything the Hyperliquid desk had.
- **LIVE STATUS.** The server reads each order back from the exchange and reports `filled`, `resting`, `rejected` or `unverified`. The desk reports that line, never the submission.
- **A leverage cap the desk cannot raise**, enforced per symbol by the MCP.

**Weaker, and documented as such:**

- **No order expiry, no client order id on placement.** The Hyperliquid desk put `expiresAfter` on every send, so a lost order became provably incapable of arriving. Here it cannot. On an `unverified` result the desk freezes the symbol, reads the record, and hands the decision to the user; it never resends on its own judgement.
- **No usable testnet.** Strike's testnet lists four markets with empty books, so it proves plumbing and nothing else. Rehearsal is now the dry run plus a minimum-size live run ($10 notional) for each new kind of action.
- **No fee endpoint.** The taker rate is a written assumption in `risk-limits.md`, stated in every PASS and corrected against realised fees from `strike_get_fill_history`.
- **No dead-man's switch.** Strike's MCP has none, and the desk does not improvise one or claim to have it.

**Two surfaces, two symbol spellings.** Market data uses `-USD` (`ADA-USD`, `XAU-USD`); execution uses `-PERP` (`ADA-PERP`, `GOLD-PERP`). Gold is the only pair that is not a suffix swap. The map lives in `strike-mcp` and `strike-api-reference`, and the bootstrap check asks the Execution Trader to name the right one before the desk is called ready.
