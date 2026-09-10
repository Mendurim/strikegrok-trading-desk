# Changelog

All notable changes to StrikeGrok are recorded here. Versions follow the release tags the bootstrap skill pins.

## v3.0.1

Six defects found by review of the v3.0.0 policy layer, each reproduced against the code before it was changed.

- **Four ceilings were reported but not enforced.** `effective_ceilings()` applied the environment override for the standing-approval lifetime, per-SA risk, per-SA open count and consecutive-loss kill, while the check sites read the raw constants. `STRIKEGROK_SA_MAX_LIFETIME_DAYS=14` printed 14 and still allowed a 31-day approval. Every ceiling now goes through the same function, and there is a test per ceiling asserting the printed value is the one that refuses. A reported bound nothing enforces is worse than an unreported one.
- **A bracket's stop leg was not required to be reduce-only.** `strike-positions` already warns that a resting stop can *open* a position when it triggers; a bracket leg is no different. A stop without `reduce_only` (or `close_position`) is now refused.
- **A daily bar allowed a fire from this morning to be sent tonight.** Signal age is now bounded by one bar *and* by an absolute ceiling of one hour, whichever is tighter. A rule that enters at the next open is inside it; a daily-bar rule keeps working.
- **A reservation the venue never confirmed held its market for a whole bar.** Held for five minutes now, which is long enough for an order to appear at the venue, after which live positions and resting orders decide. With no venue reader the reservation is held indefinitely and the book ceiling refuses first.
- **Sizes and prices were compared as floats with a 1e-12 tolerance.** Ticks and steps are decimal, so they are compared as exact decimals: `0.430` still equals `0.43`, and a size differing in the fifteenth place is now a different order rather than the same one.
- **The signed `User-Agent` still read `strikegrok-desk/1.0`.** It tracks the release, pinned to `plugin.json` by a test.

Two things the review was right about that are not code defects, and are now said plainly by the tooling rather than only in a document:

- **Attended mode is not a weaker Tier 1; it is no approval control in this code at all.** With no public key installed, a `RISK | ... | PASS` block is markdown any Bot can write, and that is all the layer requires before signing an opening order. The control is the platform's Require Approval rule, which lives in chat - and a Bot running `scripts/strike_request.py` directly with the wallet in its environment never passes through chat. Every attended-mode allow is now logged `WARN`, says so in its own record, and `verify` states it. Installing this release does not make an unsigned open impossible; installing a public key and approving with signed tokens does.
- **`STRIKEGROK_STATE_TRUSTED=1` asserts something that is false on a shared workspace.** On Grok Bot the Bots and the signer are the same OS user. `verify` now warns when the flag is set, and says to unset it and stay at Tier 2 rather than set it to make standing approvals work.

Also: each skill's `metadata.version` is its own and not the release tag, which `skills/README.md` now states, and the bootstrap skill - which embeds the clone pin - carries the release version so the two cannot be read as disagreeing.

## v3.0.0

The desk can now run unattended, and the control that lets it is enforced in code rather than in a prompt.

Every earlier version approved each trade by hand. That is the right control for a discretionary idea and the wrong one for a frozen rule that fires at 03:00 UTC. This release moves the approval from the trade to the rule, and moves the gate from the conversation into `scripts/strike_request.py`, which is the one thing that holds the API wallet.

- `scripts/desk_policy.py` runs on every non-GET before it is signed. Three tiers: reduce-only and cancels are free, opening exposure needs either a standing approval in a register the user signed with a key that never touches the desk computer, or a signed per-trade token. It refuses anything else and names the step that failed.
- `scripts/test_desk_policy.py` builds a whole desk per test - a real key pair, a genuinely signed register, a state directory - and asserts each refusal. One class runs the PASS template out of `agents/risk-manager.md` through the policy, so the prompt and the code cannot drift apart without the build failing. Another forks six processes at one free slot.
- Two new skills: `desk-signal-scan`, how the desk finds and times opportunities without being asked, and `desk-standing-approvals`, the approval model and what the layer checks.
- All seven role prompts rewritten around the scan, the signals file and the standing-approval register.

What the layer will not let through, each because it was demonstrated first:

- an opening order with no stop, a stop on the wrong side of the entry, or a stop that loses more than the ticket's stated risk
- a ticket whose bytes differ from the Risk PASS block that approved it, or whose fields come from a rejected amendment elsewhere in the file
- a client order id the desk or the venue has already seen, which is what makes a resend after a timeout impossible rather than merely discouraged
- a second order under a standing approval that already has exposure or a working order on that market, including when two Bots ask at the same instant
- opening exposure past the daily loss stop, inside an open incident, inside a blackout window the Research Analyst wrote from the calendar, or more often than once a minute
- removing margin, or changing leverage on a live position, both of which move the liquidation price and are not configuration
- more than three open positions across the account, which no approval raises

Being honest about the limit: on a box where the Bots run as the same OS user as the signer, the state directory is bookkeeping rather than a boundary. Anything the state could lie about is therefore asked of the venue instead, and Tier 1 refuses outright unless the operator sets `STRIKEGROK_STATE_TRUSTED=1` to assert that the split is real. Without it, keep the platform's Require Approval rule on and trade at Tier 2.

The desk still ships no strategies and makes no return claims. None of this creates an edge; it decides when the user's own tested rules are allowed to act on one.

## v2.0.0

Every file in this repository is now original work, and `LICENSE` carries a single copyright.

The release-check tooling has been replaced rather than edited. Five scripts and four test files - around 1,460 lines - are gone, and in their place:

- `scripts/validate.py`, the whole repository contract in eleven checks: skills parse and stay inside their budget, agents reference real skills and exactly one writes to the exchange, the runbook and index list everything that ships, relative links resolve, all six manifests agree, declared component paths exist and stay inside the repository, instruction files name only this release and pin their clones, install commands name declared ids, the template pins the current skill bytes, no emoji, and no committed credential. `--list` describes them.
- `scripts/test_validate.py`, which breaks a copy of the repository one way per fixture and asserts the matching check notices. A check that never fails is not a check.
- `scripts/desk_doctor.py`, rewritten. It reports on the release, the pinned skill bytes, the setup pin and the public Price Service, and it will not load a credential or call a signed endpoint - a doctor able to verify the write path would be a doctor able to place an order.

Two things fixed along the way:

- The CI workflow targeted self-hosted runners that do not exist for this repository, so it could never have run. It now uses a GitHub-hosted runner.
- A scheduled workflow invoked a checker that no longer exists.

The new tooling is roughly 40% smaller than what it replaces and keeps every guard that has actually caught a defect: skill-hash drift, an agent pointing at a skill that was renamed, manifests disagreeing after a version bump, and a document left naming an older tag.

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
