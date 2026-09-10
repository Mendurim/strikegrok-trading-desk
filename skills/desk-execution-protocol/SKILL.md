---
name: desk-execution-protocol
description: The Execution Trader's procedure for turning an approved ticket into one Strike action through the signed API and reconciling it - the pre-send checklist, the preview block, single-send discipline, unknown-result recovery by client order id, and the execution report. Use before and after every send, cancel, replacement, leverage change or close.
license: MIT
metadata:
  version: "2.0.1"
  author: Mendurim
  category: desk
---

# Execution protocol

This is the only skill on the desk that ends with a signed write to Strike, and only the Execution Trader uses it. The endpoint mechanics live in `strike-orders`, `strike-positions` and `strike-advanced`; signing lives in `strike-auth`. This skill is the discipline around them.

## Inputs

- The proposal file `/workspace/trading-desk/proposals/<id>.md` with a Risk Manager PASS and exact ticket fields.
- The user's approval line, by id, in chat, after the ticket was shown, inside the ticket's expiry.
- The desk computer configured per `strike-setup`: `STRIKE_API_PUBLIC_KEY` and `STRIKE_API_PRIVATE_KEY` available to `scripts/strike_request.py` from the secure secret store, never printed.

## Pre-send checklist

Run every item and write the result under `## execution` before sending. Any failure: do not send, name the item, hand back to the Desk Lead.

1. **Ticket integrity.** Id, PASS and approval refer to the same ticket text. If the Desk Lead edited the ticket after the PASS, it goes back to Risk.
2. **Symbol.** The ticket's market, with `status: trading` in `/v2/exchangeInfo`. One symbol vocabulary across both surfaces, so nothing to translate - but a halted market is still a reject.
3. **Account.** `GET /v2/account` and `GET /v2/positions` read this minute. Equity matches what the Risk Manager sized against, within the ticket's tolerance. Nothing unexpected is already open.
4. **Price still valid.** A fresh `/v2/markPrice` read is within the ticket's slippage tolerance of the ticket price. For a trigger, `stop_price` is on the correct side of the current mark.
5. **Formatting.** `size` rounded **down** to `LOT_SIZE.stepSize`; `price` rounded to `PRICE_FILTER.tickSize`; notional at least `MIN_NOTIONAL`; sizes and prices as **strings**; leverage already set on the symbol; a market order carries a `slippage` bound.
6. **Protection in the same call.** A new position uses `POST /v2/order/strategy` with an `sl_order`, so entry and stop arrive as one strategy order and the exit legs arm when the entry fills. Protection for an existing position is a standalone reduce-only trigger. A naked entry is not sent.
7. **Client order id.** A fresh `client_order_id` derived from the ticket id, written into the proposal file **before** the send, never reused. A bracket also carries a `strategy_id` and one id per leg.
8. **Preview block posted.** The exact request body, saved as a file and posted verbatim to the floor, matching the ticket field for field.
9. **Nothing else pending.** No other unreconciled send from this desk in the last few minutes. If there is, reconcile it first.

## The preview block

Strike's API has no dry-run mode. A POST is a POST, so the rehearsal is the desk's own and it is written down rather than assumed.

Build the exact request body as a JSON file under the proposal, and post its contents to the floor verbatim as a **PREVIEW** block beside the ticket. Send that same file. Because the file *is* the request, a preview that differs from the send is a mismatch the desk can see rather than a story it tells.

If anything in the body changes after the preview, the ticket is void and the Risk Manager re-sizes; a preview of one order is not authorisation for another.

Strike's testnet cannot rehearse anything - it lists four markets and its books are empty, so an order there proves only that the request is well-formed and correctly signed. That is worth exactly that much: use `--testnet` to prove the signing scheme before the key touches real money, and claim nothing more.

The desk therefore rehearses two ways: the preview block above, and a **minimum-size live run** the first time it performs any new kind of action (first bracket, first trigger, first replace, first close, first TWAP). `MIN_NOTIONAL` is $10, so that costs a few dollars in fees. Record each rehearsed action kind in `desk.md`. Never claim testnet coverage the desk does not have.

## Send

- Send exactly once. Do not wrap the send in a retry loop. Set a finite timeout.
- Capture the raw result and the send timestamp to the millisecond.
- Then **read the order back from the exchange** before reporting anything: `GET /v2/order --query client_order_id=<id>`. The HTTP response is a claim; the exchange's record is the fact.

**Report the exchange's status, never the submission.** A submitted order is not a live one, and "I sent it" is not an outcome. Status values: `2` open, `3` filled, `4` canceled, **`5` untriggered**, `6` rejected, `7` expired.

## Unknown results

A timeout, a connection reset, or an exception after the request left the machine is an **unknown result**. Treat it as possibly executed.

1. Do not resend. Do not "try once more to be safe".
2. **Look it up by client order id.** This is the whole reason the desk chooses the id before it sends:

```bash
python3 scripts/strike_request.py GET /v2/order --query client_order_id=SG-20260910-01-entry
```

3. The exchange answers authoritatively. If the order exists, proceed to reconciliation as if the response had arrived, and record that the original response was lost. If the exchange says it does not exist, the send did not land.
4. Confirm with a second look before acting on a negative: `GET /v2/openOrders`, `GET /v2/history/order` for the symbol and window, `GET /v2/positions`. A single read against a service that just timed out deserves corroboration.
5. Only then may a replacement be sent, with a **fresh** `client_order_id` and the user's approval by id. Never reuse the original id: reusing it destroys the one handle that would tell the two sends apart if the first ever surfaced.

Note that elapsed time proves nothing here: Strike has no order expiry, so an order cannot age out into safety. What the desk relies on instead is the client order id it picked and an endpoint that answers whether that exact order exists. The recovery is a lookup, not an inference.

If the lookup itself is unavailable - the API is down, not just slow - the desk is blind rather than informed. Freeze sends on that symbol, say `unknown, lookup unavailable`, and wait for the API rather than guessing.

## Reconciliation

Immediately after the send, and again when fills arrive:

- `GET /v2/order --query client_order_id=<id>` - the authoritative state of the order you sent.
- `GET /v2/openOrders` - what is resting, including trigger legs. Remember **5 = untriggered**: a resting stop lives there, not in 2.
- `GET /v2/history/fill` - fill price, size, fee, timestamp; filter by `order_id`.
- `GET /v2/positions` - size, entry, leverage, margin used, liquidation price.
- `GET /v2/history/funding` - what the position has cost to hold, once it has been open across an hourly funding stamp.
- `/v2/markPrice` on the Price Service - the mark distance to the stop, now that it exists.

Record the returned numeric **order id** beside the `client_order_id` in the proposal file. Cancels need the numeric one; lookups accept either.

Write the reconciled facts under `## reconciliation`, post the execution report on the floor (format in `agents/execution-trader.md`), and DM the Trade Reviewer with the id and the report.

**A bracket whose entry filled but whose exit legs are not resting is an unprotected position.** That is an incident, reported the moment it is seen.

## Cancels, replacements, leverage, closes

Each is its own ticket (suffix `-B`, `-C`...) with its own PASS and approval by id, unless the user has written a standing approval into `desk.md` for that exact class of action.

- **Cancel:** `DELETE /v2/order/cancel` needs the **numeric** `order_id` and `symbol`; it does not accept a client order id. Look it up first. Confirm removal from `GET /v2/openOrders`.
- **Replace:** `POST /v2/order/replace` cancels and re-places in one request, with a new `client_order_id` on the replacement. Use it for an unfilled entry the user has re-priced. For **protection**, prefer place-new, confirm-resting, then cancel-old: an atomic replace that fails leaves the position with nothing.
- **Leverage or margin mode:** `POST /v2/leverage` before the entry, on a flat symbol. Raising leverage needs approval; lowering to comply with a limit does not. `POST /v2/marginMode` is rejected while a position is open - read the response rather than assuming it applied.
- **Close:** `POST /v2/order` with `reduce_only: true`, `type: "market"`, a `slippage` bound, and the size read live seconds before. Then read `GET /v2/positions` again: if the position is gone, cancel the orphaned protective orders, because a stop left resting can open a *new* position if it triggers; if it only shrank, the remainder still needs protection, so resize before cancelling anything. Report which case it was.
- **TWAP:** a running TWAP keeps executing after the send, so it is monitored on a schedule and protection is attached and resized as it builds. See `strike-advanced`.
- **Dead-man's switch:** Strike has none. Do not improvise one, and do not tell the user the desk has that protection.

## Report

Post the block from `agents/execution-trader.md` on the floor: preview, sent, read back, reconciled, fees, next. Keep the raw response in the proposal file, not in chat.

## Never

- Never send without a PASS and the user's approval by id for this exact ticket, and never a body that differs from the previewed block.
- Never let the API wallet private key appear in chat, a file, a log or a receipt.
- Never send a market order without a `slippage` bound, and never reuse a `client_order_id`.
- Never send a new position without a stop in the same bracket.
- Never resend on an unknown result before looking the order up by client order id, and never with the same id.
- Never round a size up to clear a minimum, and never raise leverage to make a ticket fit.
- Never let a routine, a watch or a schedule send. They alert and draft; only the Execution Trader sends, and only on an approved ticket.
- Never treat a list read as a substitute for the by-client-order-id lookup, and never pass `vault_id`.
