---
name: strike-advanced
description: Strike Finance execution features beyond a single order - TWAP strategies, batch and batch-replace orders, cancel-all, isolated-margin adjustment, post-only and price-protect flags, and trailing stops - with the approval each needs and the ones this desk refuses. Use when a ticket calls for a TWAP or a multi-order send, when adjusting isolated margin, or when deciding whether a feature is in scope. Write paths - Execution Trader only.
license: MIT
metadata:
  version: "1.0.0"
  author: Mendurim
  category: strike
  network-default: mainnet
---

# Strike advanced

Everything here writes, and everything here is a ticket decision, not an execution flourish. Read `strike-orders` first.

The rule that governs this whole skill: **a feature the Risk Manager did not size for is a feature the desk does not use.** Each of these changes the shape of the risk, so each belongs on the ticket, in the PASS, and in the user's approval.

## 1. TWAP

`POST /v2/algo/twap` slices an order over time. For a size the book cannot absorb in one go, this is the honest way to execute it - and the Market Analyst's depth read is what says whether it is needed.

```json
{
  "symbol": "ADA-USD",
  "side": "BUY",
  "total_size": "250000",
  "duration_sec": 1800,
  "limit_price": "0.2150",
  "reduce_only": false,
  "randomize": true
}
```

- `side` is uppercase here (`BUY` / `SELL`), unlike the lowercase `buy` / `sell` on `/v2/order`. An easy and silent mistake.
- `limit_price` bounds the slices. Set it. A TWAP without one is a standing instruction to pay whatever the book asks for half an hour.
- `randomize` jitters slice timing.

```bash
python3 scripts/strike_request.py POST /v2/algo/twap --body-file twap.json
python3 scripts/strike_request.py GET /v2/algo/twap                    # list running strategies
python3 scripts/strike_request.py GET /v2/algo/twap/<id>               # one strategy
python3 scripts/strike_request.py DELETE /v2/algo/twap/<id>            # cancel it
```

**A running TWAP is an open commitment.** It keeps sending while nobody is watching, which is exactly the thing the one-writer rule exists to bound. So:

- A TWAP ticket states total size, duration, limit price and the protection that will be attached when it completes.
- The desk holds no other position-opening order on that symbol while it runs.
- It is monitored: `GET /v2/algo/twap/<id>` on a schedule, and the journal records each check.
- Protection cannot attach until there is a position, so the Execution Trader places the stop as soon as the first slice fills, sized to the position so far, and resizes it (place new, confirm, cancel old) as the TWAP builds. A half-built TWAP position with no stop is an unprotected position.
- Cancelling a TWAP is its own approved action, except when protection has failed - then it is an incident and the desk cancels first and reports immediately.

## 2. Batch orders

`POST /v2/orders/batch` takes an `orders` array of the same objects `/v2/order` accepts. `POST /v2/order/replace-batch` replaces several at once.

Legitimate uses on this desk: laddering an entry the Risk Manager sized as one position across several price levels, and resizing several protective stops together after a partial fill.

Not legitimate: bundling unrelated tickets. **One approval, one send** still holds - a batch is one send of one ticket's orders, never a convenient way to execute two decisions at once. Every order in the batch carries its own `client_order_id` derived from the same ticket id.

Read the response per order. A batch can partially succeed, and a batch where three of five rested is a state the desk must reconcile order by order, not summarise.

## 3. Cancel-all

`DELETE /v2/order/cancel-all`, optionally scoped by `symbol`.

This is a blunt instrument: unscoped, it removes protective stops on positions that are still open, turning a tidy-up into a naked book. The desk uses it only:

- scoped to a symbol, when flattening that symbol and the position is already confirmed closed; or
- under an incident playbook the user has pre-authorised in `desk.md`.

Never as a reflex, never to "clean up", and never before checking `GET /v2/positions` to see what the cancelled orders were protecting.

## 4. Isolated margin

`POST /v2/marginMode` switches a symbol between `cross` and `isolated`; Strike rejects the change while a position is open on that symbol.

`POST /v2/isoMargin` adds or removes margin on an isolated position (`symbol`, `amount`, `modify_type`).

Adding margin moves the liquidation price away and is risk-reducing; removing it moves the liquidation price closer and is **increasing risk**, so it needs the same approval as opening a position. Re-read `GET /v2/positions` afterwards and report the new liquidation distance in price and percent. On an isolated position it is the position's own margin, not account equity, that stands between it and liquidation.

## 5. Order flags worth knowing

| Flag | Effect | Desk position |
| --- | --- | --- |
| `post_only` | rejected rather than crossing; guarantees maker | fine on a resting entry the ticket wants as maker |
| `price_protect` | exchange-side protection against triggering on a bad print | on for triggers unless the ticket says otherwise |
| `close_position` | trigger closes the whole position, whatever the size field says | preferred for a flattening stop; survives partial fills and adds |
| `working_type` | `mark_price` (default) or `contract_price` for trigger evaluation | `mark_price`, because that is what liquidation settles against |
| `slippage` | bound on a market order, decimal fraction | always set on a market order |
| `callback_rate` | trailing distance, `"0.1"` to `"5"` percent | only when the ticket specifies a trailing stop |

## 6. What this desk does not do

- **No `vault_id`.** Every endpoint accepts one, to trade on behalf of a vault as its leader. The desk trades the user's own account only, and never passes the field.
- **No deposits, withdrawals, bridging or transfers.** The API wallet cannot do them and the desk would not if it could; those happen in the Strike app, with the user.
- **No unattended sends.** Routines and watches alert and draft. A TWAP is the one thing on the desk that keeps executing after the send, which is why it is monitored and bounded rather than treated as fire-and-forget.
- **No dead-man's switch.** Strike has no such endpoint. Do not improvise one, and do not tell the user the desk has that protection.
