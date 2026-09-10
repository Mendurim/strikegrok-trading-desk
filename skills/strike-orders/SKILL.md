---
name: strike-orders
description: Placing, protecting and cancelling Strike Finance orders through the signed REST API - limit, market with a slippage bound, bracket strategy orders with attached take-profit and stop-loss, standalone triggers, trailing stops, replace, cancel, client order ids, rounding, and reading the order back from the exchange. Use when executing an approved ticket, attaching protection to a position, or amending a resting order. Write paths - Execution Trader only, after a Risk PASS and the user's approval by ticket id.
license: MIT
metadata:
  version: "3.0.0"
  author: Mendurim
  category: strike
  network-default: mainnet
---

# Strike orders

Every endpoint here writes. Read `strike-auth` first: signing, the API wallet and the `strike_request.py` helper live there, and none of it is repeated below.

**Who may run this:** the Execution Trader, on a ticket carrying a Risk Manager PASS and the user's approval line by id. Nobody else, for any reason.

Symbols here are the same `-USD` symbols the Price Service uses. There is no translation on this desk.

## 1. Before any send

All of these must be true. Same pre-send checklist as `desk-execution-protocol`, in this API's terms.

1. The proposal file carries a Risk PASS and the user's `approve <ticket id>` line.
2. Live equity and positions read this minute: `GET /v2/account`, `GET /v2/positions`.
3. `size` respects the market's `LOT_SIZE.stepSize`; notional clears `MIN_NOTIONAL.notional`.
4. `price` respects `PRICE_FILTER.tickSize`.
5. Leverage on the symbol already equals the ticket's (`strike-positions`), set before the entry.
6. A fresh `client_order_id` is generated, written to the proposal file **before** the send.
7. The **preview block** has been posted to the floor and matches the ticket field for field (section 3).

## 2. Rounding

Constraints come from the market, not from memory: `GET /v2/exchangeInfo` on the Price Service gives `tickSize`, `stepSize` and `MIN_NOTIONAL` per symbol.

Round **size down**, never up: rounding up spends risk budget the ticket did not authorise. Round price to the tick in the direction that does not flatter your own fill assumption.

```bash
python3 -c "
from decimal import Decimal, ROUND_DOWN
qty, step = Decimal('1234.87'), Decimal('1')      # ADA-USD trades in whole tokens
print((qty / step).to_integral_value(rounding=ROUND_DOWN) * step)
"
```

A size that rounds to zero, or to a notional under the minimum, is a reject the Execution Trader reports. Never a size nudged up to clear the floor.

Sizes and prices are **strings** in this API. Send `"4800"`, not `4800`.

## 3. The preview block replaces a dry run

The API has no dry-run mode. Nothing stops a POST but the desk's own discipline, so that discipline is written down and mandatory.

Before every send, build the exact request body, save it to a file, and post its contents to the floor as a **PREVIEW** block beside the ticket. Send only after the user's approval line refers to that ticket id. The bytes sent must be the bytes previewed.

```bash
cat > /workspace/trading-desk/proposals/SG-20260910-01-entry.json <<'JSON'
{
  "strategy_id": "SG-20260910-01",
  "client_order_id": "SG-20260910-01-entry",
  "symbol": "ADA-USD",
  "side": "buy",
  "type": "limit",
  "size": "4800",
  "price": "0.2100",
  "time_in_force": "GTC",
  "sl_order": {
    "client_order_id": "SG-20260910-01-sl",
    "type": "stop",
    "size": "4800",
    "stop_price": "0.1995",
    "working_type": "mark_price"
  },
  "tp_order": {
    "client_order_id": "SG-20260910-01-tp",
    "type": "take_profit",
    "size": "4800",
    "stop_price": "0.2310",
    "working_type": "mark_price"
  }
}
JSON
cat /workspace/trading-desk/proposals/SG-20260910-01-entry.json   # this is the PREVIEW block
```

Because the file is the request, a preview that differs from the send is a bug the desk can see, not a story it tells.

## 4. Client order ids

Every order carries a `client_order_id` the desk chooses. Derive it from the ticket: `SG-20260910-01-entry`, `-sl`, `-tp`, `-B-entry` for a follow-up ticket. Unique, never reused.

This is the desk's most important reconciliation handle. `GET /v2/order?client_order_id=...` answers authoritatively whether an order exists, so a send whose response was lost is a **lookup**, not a guess. Write the id into the proposal file before the send, not after.

Brackets also take a `strategy_id`, which the desk sets to the ticket id, and a `client_order_id` per leg.

## 5. The order shapes

### Bracket strategy - the desk's default for a new position

`POST /v2/order/strategy`. Entry plus take-profit plus stop-loss in one request; the exit legs arm after the entry fills, so they cannot be rejected for having no position to reduce.

```bash
python3 scripts/strike_request.py POST /v2/order/strategy \
  --body-file /workspace/trading-desk/proposals/SG-20260910-01-entry.json
```

- `side` is `buy` or `sell` for the **entry**; Strike sets the exit legs opposite and reduce-only.
- Leg `type`: `stop` / `stop_limit` for the SL, `take_profit` / `take_profit_limit` for the TP. The plain forms fill at market on trigger; the `_limit` forms rest at `price` after triggering and **can fail to fill on a fast move**. The desk's default for a stop is `stop` - a stop that might not fill is not protection, which is exactly why the Risk Manager sizes on a stressed stop.
- `working_type` decides what the trigger watches: `mark_price` (default, and what liquidation settles against) or `contract_price`. The desk uses `mark_price` and says so on the ticket.
- A ticket without a stop does not reach this skill.

### Limit entry

`POST /v2/order` with `type: "limit"`. Add `post_only: true` when the ticket calls for maker-only - the order is rejected rather than crossing. No attached protection, so prefer a bracket unless the ticket has a reason.

### Market entry, with a bound

`POST /v2/order` with `type: "market"` and **`slippage`** set - a decimal fraction, e.g. `"0.005"` for 50 bps. Always set it. An unbounded market order on a thin book is how a desk loses more than its ticket said.

### Protection on a position that already exists

`POST /v2/order` with `type: "stop"` or `"take_profit"`, `reduce_only: true`, `stop_price`, and `working_type: "mark_price"`. `side` is the side of the **closing** order: `sell` protects a long, `buy` protects a short.

`close_position: true` closes the whole position on trigger regardless of the size field, which keeps protection correct after a partial fill or an add. Prefer it for a stop that is meant to flatten.

### Trailing stop

`type: "trailing_stop_market"` with `callback_rate` (a percentage, `"0.1"` to `"5"`) and optionally `activation_price`. Available, and used only when the ticket says so - a trailing stop changes the risk the Risk Manager sized, so it is a ticket decision, never an execution flourish.

## 6. The send, and reading it back

Send once. No retry loop. A finite timeout.

```bash
python3 scripts/strike_request.py POST /v2/order/strategy --body-file <ticket>.json
```

Then confirm from the exchange, because the response is a claim and the record is the fact:

```bash
python3 scripts/strike_request.py GET /v2/order --query client_order_id=SG-20260910-01-entry
python3 scripts/strike_request.py GET /v2/openOrders
python3 scripts/strike_request.py GET /v2/positions
python3 scripts/strike_request.py GET /v2/history/fill --query symbol=ADA-USD --query limit=20
```

Report the exchange's numbers: status, filled price and size, order ids, fees, and whether protection is actually resting. Order status values are `2` open, `3` filled, `4` canceled, **`5` untriggered**, `6` rejected, `7` expired. A resting stop that has not fired is **5**, not 2 - a check that counts only 2 will call a protected position unprotected.

**A bracket whose entry filled but whose exit legs are not resting is an unprotected position.** Incident, immediately, under `desk-incident-response`.

## 7. Replace and cancel

`POST /v2/order/replace` cancels and re-places atomically: `cancel` takes `{order_id, symbol}`, `new_order` is a full order object with a **new** `client_order_id`.

```bash
python3 scripts/strike_request.py POST /v2/order/replace --body-file amend.json
```

Use it for an unfilled entry the user has re-priced. For **protection**, prefer place-new, confirm-resting, then cancel-old, so the position is never unprotected even briefly - an atomic replace that fails leaves nothing behind.

`DELETE /v2/order/cancel` takes `order_id` (integer) and `symbol`. It does **not** accept a client order id, so look the numeric id up in `GET /v2/openOrders` or `GET /v2/order?client_order_id=...` first.

```bash
python3 scripts/strike_request.py GET /v2/openOrders
python3 scripts/strike_request.py DELETE /v2/order/cancel --body-file cancel.json
```

Cancelling protection on a position that stays open increases risk and needs approval like anything else.

`DELETE /v2/order/cancel-all` is in `strike-advanced`, and is not a reflex.

## 8. What this desk does not do

- No entry without a stop in the same strategy order.
- No market order without a `slippage` bound.
- No send whose body differs from the previewed block.
- No reused `client_order_id`.
- No resend on an unknown result - look it up by client order id first (`desk-execution-protocol`).
- No size rounded up to clear a minimum, and no leverage raised to make a ticket fit.
- No `vault_id`. The desk trades the user's own account only.
