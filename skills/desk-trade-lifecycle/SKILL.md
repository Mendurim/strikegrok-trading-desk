---
name: desk-trade-lifecycle
description: How one trade travels the StrikeGrok desk from first idea to written review - the seven stages, who owns each, the proposal file, the ticket format, what counts as approval, and when a proposal is live, closed or void. Use whenever the user wants to open, adjust or close a position, and whenever any Bot is about to write to the exchange.
license: MIT
metadata:
  version: "3.0.0"
  author: Mendurim
  category: desk
---

# Trade lifecycle

Every change to a position travels the same seven stages in the same order. Missing one is a defect, not a shortcut — the stages exist because each catches something the others do not. The Desk Lead keeps things moving; whoever owns a stage does its work.

```
idea -> evidence -> risk sign-off -> user approval -> execution -> reconciliation -> review
 DL       MA/RA          RM             user            ET            ET             TR
```

A quick trade runs all seven quickly. It does not run fewer.

## 0. Open the proposal

**Desk Lead.** The moment an idea appears — from the user, the Strategist or a routine — give it an id of the form `SG-YYYYMMDD-NN`, counting up within the day, and start `/workspace/trading-desk/proposals/<id>.md`:

```markdown
# SG-20260910-01

- opened: 2026-09-10 14:02 UTC by user
- market: ADA-USD
- idea: long ADA on a retest of 0.2100, invalidated below 0.1995 (user's idea)
- status: evidence

## evidence
## risk
## approval
## execution
## reconciliation
## review
```

Each stage appends beneath its heading and nothing is rewritten. This file is the trade's record; the chat is only context around it.

## 1. Evidence

**Market Analyst, plus Research Analyst whenever the idea rests on anything outside the exchange.**

The Desk Lead asks for exactly what sizing and execution will consume, no more: mid, mark and index; the current funding rate and when it next charges; open interest and 24-hour volume; executable depth within 5, 10 and 25 bps for the size in question; the market's `tickSize`, `stepSize` and minimum notional; the recent range. Research adds anything scheduled or breaking — for Strike's equity and commodity markets that includes earnings dates, guidance and macro prints. Both write under `## evidence` with sources and UTC times.

Done when the Risk Manager has every input it needs and none of it is more than a few minutes old.

## 2. Risk sign-off

**Risk Manager.** Reads `risk-limits.md`, reads the live account, sizes the position from the user's stop and risk budget, tests every gate, and writes PASS or REJECT with exact ticket fields under `## risk`. The arithmetic is in `desk-risk-limits`.

A PASS produces the **ticket**:

```
TICKET SG-20260910-01
market: ADA-USD                 side: buy       size: 4800 ADA (~$1,008)
entry: limit 0.2100 GTC          reduce-only: no
stop: sell 4800 trigger 0.1995, market on trigger, off mark price,
      attached to the entry as one strategy order
take-profit: 0.2310              leverage: 5x (set before the entry if it differs)
slippage tolerance: 10 bps from the ticket price at send time
risk: $50.40 = 0.5% of equity $10,080.00 (GET /v2/account 14:11 UTC)
      R = 0.0105 USD/ADA
sizing: stressed distance 0.0110 USD/ADA (stop 0.0105 + slippage 0.0003 + fees 0.0002)
risk sign-off: PASS 14:12 UTC against risk-limits.md v3
expires: 14:42 UTC
approve with: "approve SG-20260910-01"
```

A REJECT names the single gate that failed and the number that failed it, plus what would have to change. That ends the proposal unless the user changes the idea — a different stop, a different size, fresh evidence — in which case it returns to stage 1 under the same id with a note saying why.

## 3. The user approves

**The user.** The Desk Lead shows the ticket in full and asks for approval by id. Approval is the literal phrase carrying the id, typed by the user, after the ticket was visible. "Yes", "go on", "looks good" and a thumbs-up are **not** approval; the Desk Lead asks again for the exact phrase. The line and its timestamp go under `## approval`.

It has to come from the user's own turn. No Bot writes it, quotes an older one forward as though it were new, infers it from earlier enthusiasm, or records one it did not watch the user type. Absent that line, the honest status is "not approved" — never "approved", never "assumed approved". A Bot uncertain whether a line came from the user treats it as absent and asks again.

A ticket that expires before approval is void. Prices and the book have moved, so it needs a fresh sign-off rather than a fresh timestamp.

## 4. Execution

**Execution Trader.** Works the pre-send checklist in `desk-execution-protocol`, posts the preview block, sends once, and records under `## execution`: the preview, the client order id, the request as sent, the numeric order id that came back, the status read back from the exchange, and the timestamps. Anything other than a clean result goes to `desk-incident-response`.

## 5. Reconciliation

**Execution Trader.** Establishes what actually happened from the exchange itself — the order looked up by its client order id, then `GET /v2/openOrders`, `GET /v2/history/fill` and `GET /v2/positions` — and writes it under `## reconciliation`. Posts the execution report to the floor and DMs the Trade Reviewer.

Reconciliation is not a single moment. While the order rests, fills arrive and go into the same section as they come.

## 6. Review

**Trade Reviewer.** Journals the trade on the day it happens and writes the review when it closes, or sooner on request, per `desk-post-trade-review`. Sets `status: closed` in the proposal.

## Adjusting and exiting are trades

Moving a stop, adding, trimming, closing, changing leverage — each is a new ticket under the same proposal id with a letter suffix (`SG-20260910-01-B`), each needs its own PASS, and each needs the user's approval against that suffixed id. A close states the reduce-only size read live from the account seconds before, and its slippage bound.

The single exception is an action the user has pre-authorised in writing in `desk.md` — typically placing a reduce-only stop on a position that has none. Even then it is journaled the same way.

## When is it done

- **Live** — stage 5 shows an order resting or filled.
- **Closed** — the position is flat, orphaned protective orders have been cancelled, and stage 6 is written.
- **Void** — it expired, or it was rejected and not retried.

## Where this goes wrong, and the fix

| What happens | What to do |
| --- | --- |
| The user asks the Execution Trader to "just buy some" | It routes to the Desk Lead and a proposal opens. The lifecycle can be fast; it cannot be skipped. |
| The PASS was computed from evidence older than the ticket | Risk re-reads live state and re-issues with a new expiry. |
| Approval arrives as "yes" | The Desk Lead asks again for the phrase with the id. |
| Two Bots both think they hold the next step | The `status` line in the proposal names the stage; its owner acts. |
| The ticket names a market that is not `trading` in `exchangeInfo` | The Execution Trader stops at the checklist and returns it. |
| A stop was meant to be attached but went as a separate order that got rejected | Unprotected position. Incident, immediately — not a note at the bottom of a report. |
