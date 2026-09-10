---
name: desk-standing-approvals
description: How the desk trades autonomously without losing the approval control - the three tiers (reduce-only always, standing approval per frozen rule, the user's line per trade), the signed standing-approval register the Bots can read but not write, the bounds and kill conditions every approval carries, how one is earned, widened, suspended and revoked, and how scripts/strike_request.py enforces all of it in code before it signs anything. Use when the user asks to "let it run", when granting or reviewing an approval, or when the policy layer refuses a send.
license: MIT
metadata:
  version: 1.0.0
  author: Mendurim
  category: desk
---

# Standing approvals

Earlier versions of this desk approved every trade by hand. That is the right control for a discretionary idea and the wrong one for a frozen rule that fires at 03:00 UTC. This skill moves the approval from the trade to the rule, and moves the gate from the prompt to the code.

## Why the prompt cannot be the gate

Every Bot shares one computer and one filesystem. A Bot can write to `desk.md`, to a proposal file, and to the floor. So a Bot could write itself an approval. The earlier design leaned on Grok Bot's Require Approval rule for exactly this reason. Once that rule is relaxed so the desk can act unattended, the only enforcement left is inside `scripts/strike_request.py`, which is the one thing that holds the API key. `scripts/desk_policy.py` is that enforcement. It refuses to sign anything that opens exposure unless a file **the user signed, with a key the computer never holds**, says it may.

## The three tiers

| Tier | What | Approval | Enforced by |
| --- | --- | --- | --- |
| **0** | reduce-only: place or tighten a protective stop, exit on a rule's stated invalidation, cancel orphaned stops, cancel an expired unfilled entry, **add** margin to a position | none needed | policy: `reduce_only`/`close_position` true, cancel with a symbol, or margin add |
| **1** | open or add exposure when a frozen rule fires, inside signed bounds | a standing approval `SA-NN` in the signed register | policy: register signature, bounds, kill conditions, rule hash, fire time |
| **2** | open or add exposure on anything else | the user's signed per-trade approval token for `SG-…` | policy: token signature, ticket match, expiry |

A stop that *loosens* is not reduce-only in effect and is Tier 2. Changing leverage or margin mode, and **removing** margin from a position, move the liquidation price toward the mark and are treated as opening exposure: leverage and margin mode need a PASS block and an approval (an SA may cover leverage up to its `max_leverage`); margin removal always needs a per-trade token. Nothing else exists. "The Desk Lead said so", "tiny size", "the rule has been winning" are not tiers.

## The register

`/workspace/trading-desk/desk/standing-approvals.json`, plus `standing-approvals.json.sig`. Bots read it; only the user's signing key produces a valid `.sig`, so the Bots cannot alter it. The public key sits in `desk/user-signing.pub`.

```json
{
  "version": 4,
  "signed_at": "2026-09-12T09:00:00Z",
  "approvals": [
    {
      "id": "SA-03",
      "rule": "funding-fade-v1",
      "rule_version": 3,
      "rules_sha256": "9f3a…",
      "markets": ["BTC-USD", "ETH-USD", "SOL-USD"],
      "sides": ["long", "short"],
      "risk_per_trade": 0.0025,
      "max_notional": 1500,
      "max_open": 2,
      "bar_seconds": 14400,
      "hours_utc": null,
      "granted": "2026-09-12",
      "expires": "2026-10-12",
      "kill": {
        "daily_loss_stop": true,
        "consecutive_losses": 3,
        "review_window_trades": 12,
        "min_live_expectancy_r": 0.02,
        "on_incident": true,
        "on_research_alert": true
      }
    }
  ]
}
```

Bounds only ever sit *inside* `risk-limits.md`, which sits inside the desk ceilings in `desk-risk-limits`. The policy layer checks all three; the tightest wins.

Suspensions are the one thing a Bot may write, because a suspension only removes permission: `desk/standing-approvals.suspended.json`, a list of `{id, reason, at, by}`. The policy layer copies every entry it sees into its own state directory **one way** and keeps refusing that SA even if the Bot file is later deleted or edited. A suspension clears only when the user signs a register whose `version` is higher than the one in force when it was suspended. The layer also suspends on its own when it detects a hash change or the SA's consecutive-loss count from the exchange's fills.

## The state directory

`desk/policy-state/` (or `STRIKEGROK_POLICY_STATE`) holds what the layer must remember between calls: suspensions, consumed tokens and client order ids, occupied SA slots, and today's start-of-day equity captured from a live account read. Bots must not be able to write it. Run `scripts/strike_request.py` as its own OS user that owns the API key, the state directory and `policy.log`; the Bots' user invokes the script and can read the log but cannot edit anything the policy owns. The layer checks ownership and mode on every call and refuses Tier 1 and Tier 2 if the directory is writable by anyone else. On a platform where a separate user is not possible, the policy layer cannot keep state a Bot cannot forge, and the desk must stay on Tier 2 with the platform's Require Approval rule on.

## Per-trade tokens (Tier 2)

The user approves a discretionary ticket by signing a small token on their own machine and dropping it under `approvals/`:

```json
{"proposal": "SG-20260912-07", "symbol": "BTC-USD", "side": "long", "size": "0.013", "price": "77120", "expires": "2026-09-12T15:00:00Z"}
```

`approvals/SG-20260912-07.json.sig` beside it. The policy layer requires the token's fields to equal the request's. Where the Grok Bot Require Approval rule is still on, the user may prefer to keep approving by chat line; the token path is what remains when it is off.

## How an approval is earned

1. Rule written, frozen, hash recorded. Backtest reported with trials, holdout reads, distribution and 5th percentile.
2. Forward test at minimum size ($10 notional), Tier 2, every ticket approved by the user, until the Trade Reviewer has **20 closed trades** or the user sets another threshold in `desk.md`.
3. Trade Reviewer's per-rule record shows live expectancy above the backtest's 5th percentile and drawdown inside the rule's stated maximum.
4. Research Analyst clears a dossier on each market the SA will cover.
5. Desk Lead drafts the SA entry; the user signs the register on their own machine; the Desk Lead confirms the signature verifies from the computer.
6. First SA bounds are small: 0.25% risk, one or two markets, `max_open` 1 or 2. Widening follows the same route with the Reviewer's per-SA record attached, in writing, dated.

## Kill conditions

Any true condition suspends the SA on the spot. The Strategist watches them on the rule side, the Risk Manager on the account side, the policy layer re-checks what it can at send time.

| Condition | Checked by | Data |
| --- | --- | --- |
| daily loss stop hit | Risk Manager, policy | `GET /v2/account` vs `desk/start-of-day-equity.json` |
| consecutive losses ≥ N | Strategist, Reviewer | journal, `history/fill` |
| live expectancy over the window < min | Strategist, Reviewer | journal |
| `RULES.md` hash changed | Strategist, policy | sha256 on disk vs register |
| open incident | Desk Lead, policy | `journal/incidents/open` non-empty |
| Research Analyst time-sensitive alert on the market | Research Analyst | `signals/` |
| monitor `could_not_tell` > 2 bars | Strategist | `watch/rule-*/log` |
| register expired or signature invalid | policy | `.sig` |

A suspended SA is announced on the floor the same hour, DM'd to the Trade Reviewer, and recorded in `desk.md`. Exits and protection under Tier 0 continue; only opening stops.

## What the state directory is, and is not

`desk/policy-state/` holds the spent client order ids, the standing-approval slots, the day's opening equity, the ingested suspensions, incidents and blackouts. It is owned by the user running the signer and readable by nobody else.

Be honest about what that buys. **On a box where the Bots run as the same OS user as the signer, this is bookkeeping, not a boundary** - the same process that can send an order can edit the file. Two things follow, and both are enforced:

- **Attended mode is not a weaker Tier 1; it is no approval control in this code at all.** With no public key installed, a `RISK | ... | PASS` block is markdown any Bot can write, and that is all the layer requires before it will sign an opening order. The control is the platform's Require Approval rule, which lives in chat - and a Bot that runs `scripts/strike_request.py` directly with the API wallet in its environment never passes through chat. On a shared Grok workspace that is the default shape. Installing v3 does not make an unsigned open impossible; installing a public key and approving with signed tokens does. Every attended-mode allow is logged `WARN` and says so in its own record.
- The layer asks the **venue** for anything the state could lie about: whether an order id already exists, what positions are open, what orders are resting. Wiping a state file grants nothing.
- **Tier 1 refuses outright unless `STRIKEGROK_STATE_TRUSTED=1` is set**, which is the operator asserting that the signer really does run as its own OS user. On Grok Bot the Bots and this script are the same user, so that assertion is **false** and the flag should stay unset - setting it to make standing approvals work asserts a split that does not exist and hands the slot, replay and daily-loss bookkeeping back to the thing it is meant to bound. `desk_policy.py verify` warns when it is set. Unset it and trade at Tier 2.

## What the policy layer checks, in order

`scripts/desk_policy.py` is called by `scripts/strike_request.py` for every non-GET request, before signing:

1. **Forbidden surfaces**: any path mentioning withdraw, deposit, transfer, vault or bridge; any `vault_id`. Refuse.
2. **Tier 0**: a scoped cancel, an order with `reduce_only` or `close_position` true, or a margin add. Allow.
3. **State directory** owned by the policy user and not writable by others; otherwise refuse everything below.
4. **Daily loss** from a live `GET /v2/account` performed by the script itself, against the day's opening equity, which the layer captures on the **first signed read of the day** - so a loss taken before the desk next tries to trade still counts. If that baseline is missing it **refuses and never re-bases**: recapturing it after a drawdown would reset the stop to whatever survived. No account reader → refuse. **Open incident** (`journal/incidents/open` non-empty) → refuse. **Replayed `client_order_id`** → refuse, checked against both the spent list and the venue itself, so a wiped state file does not hand back a spent id. **Blackout window** in force on the ticket's market → refuse: `desk/blackouts.json` is the Research Analyst's calendar with teeth, ingested one-way and expiring on its own `end`. **Open incident** → refuse, and an incident stays blocking once seen even if the file is deleted; only a re-signed register clears it.
5. **Proposal**: the ticket id, including any `-A`/`-B` amendment suffix, is taken from the `client_order_id`; the layer reads only the **last `RISK | <ticket> |` block for that exact ticket**, which must be `PASS`; the fields inside that block must equal the request body. Prose elsewhere in the file counts for nothing. An amendment suffix is one uppercase letter; leg suffixes such as `-entry`, `-sl` and `-TP` are not amendments. `expires_at:` on the block is read: an expired ticket needs a fresh sign-off, not a fresh timestamp.
6. **Order shape**: `order_type:` is `limit`, `market` or `twap` and matches the path. A market ticket carries `ref_price:` so its notional is bounded, and its request carries a slippage bound and no price. If the request names a `leverage`, it must be the leverage the ticket states.
7. **Protection**: every opening order is a bracket on `POST /v2/order/strategy` whose `sl_order` covers the whole entry size, triggers on `mark_price`, matches the ticket's `stop_price:`, sits on the correct side of the entry, and loses no more at its stop than the ticket's `risk_usd`. A naked entry is refused. A TWAP is the exception and declares `protection: as_it_builds`. Unattended, a fill that leaves no resting stop is the one failure that costs more than the ticket said, and no prompt can prevent it.
8. **Runaway guards**, which no standing approval raises: at least 60 seconds between opening orders (`STRIKEGROK_SECONDS_BETWEEN_OPENS` may raise the gap, never lower it), and at most three open positions across the whole account, counted from `GET /v2/positions`.
9. **Tier 1 also**: the PASS block reads `liquidity: pass`, so a rule cannot fire into a book that will not take the ticket.
10. **Tier 1**: register `.sig` verifies; SA present, unexpired, lifetime ≤ 31 days, not suspended in state; `signal:` names the rule and version; `RULES.md` hash equals `rules_sha256` (a mismatch suspends the SA); symbol and side allowed; notional ≤ `max_notional`, ≤ the per-order ceiling and ≤ one times equity, so a flat cap still means something on a small account; `bar_seconds` ≤ one day; the PASS block's equity within 2% of the live read and risk ≤ `risk_per_trade`; **occupied markets** < `max_open`, counted as the union of the state reservation, live positions and resting non-reduce-only orders, and never twice on the same market; `fired_at` within one `bar_seconds` **and** within the signal-age ceiling of one hour, whichever is tighter - a daily bar would otherwise let this morning's fire be sent tonight, which is not the trade the backtest measured; consecutive-loss kill recomputed from live fills. Allow, and **take the slot and the client order id there and then**, under the same lock that granted them.
11. **Tier 2**: `approvals/<ticket>.json` + `.sig` verify; fields equal the request; unexpired; not already consumed. Allow, and consume the token there and then.
12. **The reservation is taken at the gate, not at the send.** The gate and the send are two moments, and the gap between them is one network round trip. A layer that recorded the slot after the send would show every Bot inside that window the same free slot - six concurrent processes went through a `max_open` of one that way, which is what `TestConcurrentBots` now pins down. `commit()` afterwards only marks the reservation *sent*.

    Two consequences worth knowing:

    - **A send that timed out keeps its slot and its id.** The desk cannot tell a request that never left from one that landed, and the honest assumption is that it landed. Look the order up; a replacement needs a fresh id and a fresh approval. A transport that can *prove* nothing was transmitted - a refused connection, a DNS failure - releases the reservation instead; a timeout never counts as that.
    - **A rejection frees the slot but not the id.** A 4xx means the venue saw the request, so the id is spent, but nothing opened, so the approval is not left holding an empty place.
    - **A market is released when it is flat, has nothing resting, and the reservation is more than five minutes old.** An entry still working has no position either, so a reservation is held while the order may still be surfacing at the venue - but five minutes is long enough for it to appear, and holding a market for a whole bar on no evidence is not proportionate. With no venue reader at all the reservation is held indefinitely, and the book ceiling refuses first anyway.

13. Otherwise refuse with the failed step named. Anything unexpected inside the check - a malformed file, a failed read, a timestamp with no timezone - becomes a refusal, never an allow. Every decision is appended to `policy-state/policy.log`.

`scripts/test_desk_policy.py` builds a synthetic desk with a real key and asserts each of these refusals, including the six defects found in the first version of the layer. Run it after any change to the script.

The Execution Trader does not argue with a refusal and does not edit the script. A refusal it did not expect is an incident.

## Setting it up

```bash
# on the user's own machine, once
python3 scripts/desk_policy.py keygen --out ~/.strikegrok/user-signing      # private stays here
scp ~/.strikegrok/user-signing.pub  <desk>:/workspace/trading-desk/desk/user-signing.pub

# each time the register changes, on the user's machine
python3 scripts/desk_policy.py sign --key ~/.strikegrok/user-signing --file standing-approvals.json
# then copy standing-approvals.json and .sig to /workspace/trading-desk/desk/

# on the desk computer, read-only check any Bot may run
python3 scripts/desk_policy.py verify
```

The private key is never on the desk computer. If it is ever copied there, the register is no longer a control, and the desk goes back to Tier 2 until a new key is made.

## Never

- Never let a Bot write the register or a per-trade token. Suspensions only.
- Never grant an SA to a rule without a Reviewer-verified live record.
- Never let an SA bound exceed the limits file or the desk ceilings.
- Never leave an SA without an expiry. Thirty days, then re-sign on the record.
- Never treat a chat line as an approval once the platform gate is off. Only a signature the script can verify.
- Never disable the policy layer to "get a trade through". If it refuses and you believe it is wrong, that is an incident review, not an override.
