# Changelog

All notable changes to StrikeGrok are recorded here. Versions follow the release tags the bootstrap skill pins.

## v1.1.0

The desk's process documentation and all seven role prompts rewritten from scratch. Same seven roles, same lifecycle, same controls - new text throughout, and several things corrected on the way:

- The workspace layout still showed `HG-` proposal ids. Now `SG-`, matching everything else.
- The engagement levels still offered a testnet practice tier. Strike's testnet lists four markets with empty books and cannot fill an order, so the levels are now research and trading, with the preview block and a minimum-size live run in place of practice.
- `strike-market-data` still told Bots that execution went through an MCP and warned of a `-PERP`/`-USD` symbol split. Both untrue since execution moved to the signed API, and both would have confused the Market Analyst on every brief.
- Several skills still carried mechanics from a venue this desk does not use - grouped TP/SL parents, a funding-history request that does not exist here, a rate-limit endpoint, an `/info` path - and the Research Analyst's worked example cited sources from that venue.
- The strategy lab still described paper trading on testnet. It now describes forward testing at minimum size through the ordinary lifecycle, and says plainly that this tests the rules and the plumbing rather than returns at size.
- The `$schema` URL in `plugin.json` had been caught by an earlier version bump and pointed at a schema version that does not exist.

## v1.0.1

Presentation pass. Skill authorship, the changelog, the attribution page and the README credit block rewritten so the repository describes itself rather than its lineage, and `LICENSE` carries a Mendurim copyright line.

## v1.0.0


First release. A seven-agent Strike Finance trading desk for Grok Bot: nineteen skills, seven role prompts, and a zero-key Opening Bell.

**The desk**

- Seven roles with full system prompts - Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager, Execution Trader, Trade Reviewer. Six on a Trading Floor group chat, the Trade Reviewer off it.
- One lifecycle for every trade: idea, evidence, risk sign-off, approval by ticket id, one send, reconciliation, review.
- One writer. Six Bots read; the Execution Trader alone sends, and only on a ticket the user approved by id.
- An evidence standard with an `unavailable` verdict of its own, so a dead feed never passes for a calm market.

**Market data** - Strike's public Price Service (`api.strikefinance.org/price/v2`), unauthenticated, thirty-one markets. `scripts/opening_bell.py` renders a timestamped snapshot with mark, index, funding, open interest, spread and depth at 5/10/25 bps. Depth is read at `limit=1000` because the endpoint defaults to twenty levels a side, and a band the resting book does not reach is reported as a floor rather than a total.

**Execution** - the signed Strike API (`api.strikefinance.org/v2`), authenticated with an Ed25519 API wallet the user registers at `app.strikefinance.org/api-keys`. `scripts/strike_request.py` is the single signing primitive; it uses the `cryptography` package when present and `openssl` when not, so the desk installs nothing. Its signing is verified against RFC 8032 test vector 1 on both backends.

**Order handling**

- Bracket strategy orders carry entry, take-profit and stop-loss in one request, with a client order id per leg.
- Every order carries a `client_order_id` the desk chooses before it sends, so a lost response is a lookup against `GET /v2/order` rather than a guess. A replacement always gets a fresh id.
- Limit, bounded market, standalone triggers, trailing stops, replace, replace-batch, batch, cancel, cancel-all, leverage, margin mode, isolated margin and TWAP are all supported and each carries the approval it needs.
- Every request is built as a JSON file and posted to the floor as a preview block before it is sent. Strike has no dry-run mode, so the bytes previewed being the bytes sent is the desk's own discipline, backed by Grok Bot's Require Approval rule.

**Known limits, stated plainly**

- **No order expiry.** An order cannot age out into safety, so elapsed time never proves a lost send is dead. Only the client-order-id lookup does.
- **No usable testnet.** Strike's testnet lists four markets with empty books. It proves a request is well-formed and correctly signed, and nothing else. Rehearsal is the preview block plus a minimum-size live run ($10 notional) for each new kind of action.
- **No dead-man's switch.** Strike has no such endpoint, and the desk does not improvise one or claim to have it.

**Optional research add-on** - the crowdtime MCP supplies a market liquidity screen, computed technical indicators, crypto/equity/dividend research playbooks, Bodega prediction markets and Discord alerts. It is not on the trading path, its order tools are deliberately unused, and the desk trades fully without it.
