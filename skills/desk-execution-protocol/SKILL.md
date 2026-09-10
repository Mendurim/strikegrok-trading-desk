---
name: desk-execution-protocol
description: The Execution Trader's procedure for turning an approved ticket into one Strike action through the crowdtime MCP and reconciling it - the pre-send checklist, the dry-run rehearsal, single-send discipline, unverified-result handling and the execution report. Use before and after every send, cancel, replacement, leverage change or close.
license: MIT
metadata:
  version: "2.0.0"
  author: Galleon Labs (HyperGrok), ported for Strike Finance
  category: desk
---

# Execution protocol

This is the only skill on the desk that ends with a write to the crowdtime MCP, and only the Execution Trader uses it. The tool mechanics live in `strike-orders` and `strike-positions`; this skill is the discipline around them.

## Inputs

- The proposal file `/workspace/trading-desk/proposals/<id>.md` with a Risk Manager PASS and exact ticket fields.
- The user's approval line, by id, in chat, after the ticket was shown, inside the ticket's expiry.
- The desk computer configured per `strike-setup`: `STRIKE_MCP_TOKEN` available to scripts from the secure secret store, never printed.

## Pre-send checklist

Run every item and write the result under `## execution` before sending. Any failure: do not send, name the item, hand back to the Desk Lead.

1. **Ticket integrity.** Id, PASS and approval refer to the same ticket text. If the Desk Lead edited the ticket after the PASS, it goes back to Risk.
2. **Symbol.** The ticket's market, spelled the **MCP** way: `ADA-PERP`, not `ADA-USD`; `GOLD-PERP`, not `XAU-USD`. It is one of the fifteen the MCP trades. A symbol the Price Service quotes is not necessarily one the desk can send.
3. **Account.** `strike_get_account_balance` and `strike_get_open_positions` read this minute. Equity matches what the Risk Manager sized against, within the ticket's tolerance. Nothing unexpected is already open.
4. **Price still valid.** Fresh `strike_get_mark_price` is within the ticket's slippage tolerance of the ticket price. For a trigger, `stop_price` is on the correct side of the current mark.
5. **Formatting.** `quantity` rounded **down** to the symbol's `size_precision`; `price` rounded to `tick_size`; notional at least `min_notional_usd`; `leverage` set, at or below the server's per-symbol cap.
6. **Protection in the same call.** A new position uses `strike_place_bracket_limit` with `sl_stop_price` set, so entry and stop arrive as one strategy order and the exit legs arm when the entry fills. Protection for an existing position is a standalone reduce-only trigger. A naked entry is not sent.
7. **Dry run shown.** The same call with `confirm` absent or `false`, its preview read back to the floor, and it matches the ticket field for field.
8. **Nothing else pending.** No other unreconciled send from this desk in the last few minutes. If there is, reconcile it first.

## The dry run is the rehearsal

Every MCP write tool defaults to a dry run and changes nothing without `confirm=true`. That gate is in the transport, not in a Bot's good behaviour, and it is the strongest control this desk has.

Preview first, always. Then send with the **identical** arguments and `confirm=true`. If any argument other than `confirm` differs between the two calls, the ticket is void and the Risk Manager re-sizes; a preview of one order is not authorisation for another.

Strike's testnet cannot rehearse anything - it lists four markets and its books are empty, so an order there proves only that the call is well-formed. The desk therefore rehearses two ways: the dry run above, and a **minimum-size live run** the first time it performs any new kind of action (first bracket, first trigger, first close, first cancel). `min_notional_usd` is $10, so that costs a few dollars in fees. Record each rehearsed action kind in `desk.md`. Never claim testnet coverage the desk does not have.

## Send

- Send exactly once. Do not wrap the send in a retry loop. Set a finite timeout.
- Capture the raw result and the send timestamp to the millisecond.
- Read the **LIVE STATUS** line: `filled`, `resting`, `rejected` (with the exchange's reason), or `unverified`. The server produces it by reading the order back from the exchange.

**Report the LIVE STATUS line, never the submission.** A submitted order is not a live one, and "I sent it" is not an outcome.

## Unverified results

`unverified`, a timeout, a connection reset, or an exception after the request left the machine is an **unverified result**. Treat it as possibly executed:

1. Do not resend. Do not "try once more to be safe".
2. Read the record: `strike_get_open_orders`, then `strike_get_order_history` for the symbol and window (status 2 open, 3 filled, 5 untriggered, 6 rejected), then `strike_get_fill_history`, then `strike_get_open_positions`.
3. If found: proceed to reconciliation as if the result had arrived, and record that the original result was lost.
4. If not found: **a negative check is not proof.** The order may still be in flight and can land after any number of clean reads.
5. **Here the desk is weaker than it was on Hyperliquid, and says so.** That desk put `expiresAfter` on every send, so a lost order became provably incapable of arriving once the deadline passed. Strike's MCP offers no expiry and no client order id on placement, so **the desk cannot prove an order is dead.** There is no elapsed time that makes it safe to assume.
6. Therefore: freeze new sends on that symbol, report `unverified, cannot prove dead` to the Desk Lead with the full read-back, and **hand the decision to the user**. A replacement needs the user's explicit fresh approval by id, given in the knowledge that the original may still appear. The desk never makes that call for them.
7. Keep reading the record on a schedule until the position and order state settle, and journal each read.

## Reconciliation

Immediately after the result, and again when fills arrive:

- `strike_get_open_orders` - what is resting, including trigger legs.
- `strike_get_order_history` - the exchange's view, by status. Remember **5 = untriggered**: a resting stop lives there, not in 2.
- `strike_get_fill_history` - fill price, size, fee, timestamp; filter by `order_id`.
- `strike_get_open_positions` - size, entry, leverage, margin used, unrealised PnL.
- `strike_get_mark_price` - the mark distance to the stop, now that it exists.

Record the returned **order id** in the proposal file the moment it comes back. Strike gives no client order id on placement, so the order id plus symbol, side, size and timestamp is the only handle the desk gets for the life of the trade.

Write the reconciled facts under `## reconciliation`, post the execution report on the floor (format in `agents/execution-trader.md`), and DM the Trade Reviewer with the id and the report.

**A bracket whose entry filled but whose exit legs are not in `strike_get_open_orders` is an unprotected position.** That is an incident, reported the moment it is seen.

## Cancels, replacements, leverage, closes

Each is its own ticket (suffix `-B`, `-C`...) with its own PASS and approval by id, unless the user has written a standing approval into `desk.md` for that exact class of action.

- **Cancel:** `strike_cancel_order` with the order id from `strike_get_open_orders` and the symbol. Dry run, then `confirm=true`. Confirm removal from `strike_get_open_orders`.
- **Replacement:** the MCP has **no modify or replace tool**. Changing a resting order means cancel then place, which is two actions with a gap between them. For an unfilled entry, cancel first. For protection, do it the other way round: place the new trigger, confirm it is resting, *then* cancel the old one, so the position is never unprotected for even a moment.
- **Leverage or margin mode:** `strike_set_leverage` before the entry. It has no `confirm` parameter and takes effect immediately, so raising leverage needs approval before the call, not after the preview. It affects new positions only, and Strike rejects a margin-mode change while a position is open on the symbol - read the response rather than assuming both halves applied.
- **Close:** `strike_place_market_trade` with `reduce_only=true` for the position size read live seconds before the send. Then read `strike_get_open_positions` again: if the position is gone, cancel the orphaned protective orders, because a stop left resting can open a *new* position if it triggers; if it only shrank, the remainder is still open and still needs protection, so resize protection before cancelling anything. Report which case it was.
- **Dead-man's switch:** Strike's MCP has none. Do not improvise one, and do not tell the user the desk has that protection.

## Report

Post the block from `agents/execution-trader.md` on the floor: sent, LIVE STATUS, reconciled, fees, next. Keep the raw result in the proposal file, not in chat.

## Never

- Never call a write tool with `confirm=true` without a PASS and the user's approval by id for this exact ticket.
- Never let the bearer token appear in chat, a file, a log or a receipt.
- Never send on a symbol the desk did not translate and check against the fifteen.
- Never send a new position without a stop in the same bracket.
- Never resend on an unverified result. The desk cannot prove an order is dead, so a replacement is the user's decision, not the desk's.
- Never round a size up to clear a minimum, and never raise leverage to make a ticket fit.
- Never let a routine, a watch or a schedule send. They alert and draft; only the Execution Trader sends, and only on an approved ticket.
- Never treat a clean read as proof that an unverified result did not execute.
