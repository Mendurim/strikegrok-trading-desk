---
name: strike-orders
description: Placing, protecting and cancelling Strike Finance orders through the crowdtime MCP - market and limit entries, bracket orders with attached take-profit and stop-loss, standalone TP/SL triggers, cancels, size and price rounding, the dry-run preview, and reading the LIVE STATUS line. Use when executing an approved ticket, attaching protection to a position, or cancelling a resting order. Write paths - Execution Trader only, after a Risk PASS and the user's approval by ticket id.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs (HyperGrok), ported for Strike Finance
  category: strike
  network-default: mainnet
---

# Strike orders

Every tool here writes. Read `strike-mcp` first: the transport, the bearer token, the symbol table and the confirm gate all live there, and none of it is repeated at length below.

**Who may run this:** the Execution Trader, on a ticket carrying a Risk Manager PASS and the user's approval line by id. Nobody else, on any network, for any reason.

## 1. Before any send

All of these must be true. This is the same pre-send checklist as `desk-execution-protocol`, in the terms this API uses.

1. The proposal file carries a Risk PASS and the user's `approve <ticket id>` line.
2. The symbol is one of the fifteen the MCP trades, spelled the MCP way (`GOLD-PERP`, not `XAU-USD`).
3. `quantity` respects the symbol's size precision, and the notional clears `min_notional_usd`.
4. `price` respects `tick_size`.
5. `leverage` is set and is at or below the server's per-symbol cap.
6. Live equity and positions were read this minute, not remembered: `strike_get_account_balance`, `strike_get_open_positions`.
7. A dry run has been shown to the floor and matches the ticket exactly.

## 2. Rounding

Get the constraints from the market, not from memory: `strike_get_market_snapshot` returns `tick_size`, `size_precision`, `min_notional_usd` and `max_leverage` for the symbol.

Known size precisions: `BTC-PERP` 5dp, `ETH-PERP` 3dp, `SOL-PERP` 2dp, `ZEC-PERP` 2dp, `NVDA-PERP` 2dp, `HYPE-PERP` 2dp, **`ADA-PERP` 0dp - whole tokens only**.

Round **size down** and never up: rounding up spends risk budget the ticket did not authorise. Round price to the tick in the direction that does not improve your own fill assumption - a long's limit down, a short's limit up.

```bash
# Sizing arithmetic is shown in the open, every time. See desk-risk-limits.
python3 -c "
from decimal import Decimal, ROUND_DOWN
qty = Decimal('1234.87')          # from the Risk Manager's ticket
step = Decimal('1')               # ADA-PERP: whole tokens
print((qty / step).to_integral_value(rounding=ROUND_DOWN) * step)
"
```

A size that rounds to zero, or to a notional under `min_notional_usd`, is a reject the Execution Trader reports back - never a size quietly nudged up to clear the floor.

## 3. The four entry shapes

### Bracket limit - the desk's default for a new position

`strike_place_bracket_limit` sends entry, take-profit and stop-loss as one strategy order. The exit legs arm only after the entry fills, so they cannot be rejected for "no position to reduce", and Strike OCO-cancels the survivor when one fills. This is the closest thing Strike has to Hyperliquid's grouped TP/SL entry, and it is what the desk uses whenever a ticket has both an entry and a stop.

```bash
call_mcp strike_place_bracket_limit '{
  "symbol": "ADA-PERP",
  "side": "long",
  "quantity": 4800,
  "entry_price": 0.2100,
  "tp_stop_price": 0.2310,
  "sl_stop_price": 0.1995,
  "time_in_force": "GTC",
  "leverage": 5,
  "confirm": false
}'
```

- `side: long` means buy entry, sell TP and SL. `short` is the mirror.
- `tp_limit_price` defaults to `tp_stop_price` when omitted.
- Omit `sl_stop_price` for a TP-only bracket. **The desk does not.** A ticket without a stop does not reach this skill.
- Leaving `sl_limit_price` unset makes the stop a **market** stop: it flattens, with slippage. Setting it makes a stop-limit, which can fail to fill on a fast move straight through the limit. The desk's default is the market stop - a stop that might not fill is not protection. This is exactly why the Risk Manager sizes on a stressed stop.
- Multi-TP (50/30/20) means calling this once per slice with the matching size, not one call with three targets.

### Limit entry

`strike_place_limit_trade` for a resting entry with no attached exit. Returns the order id for `strike_cancel_order`. Protection is then a separate call, and the position is naked until it lands - use a bracket instead unless the ticket has a reason.

### Market entry

`strike_place_market_trade` when the ticket says market. `reduce_only=true` routes to closeLong/closeShort and will not flip into the opposite side, which makes it the safe way to close.

### Protection on a position that already exists

`strike_place_take_profit` and `strike_place_stop_loss` place reduce-only **trigger** orders. Use these, not `strike_place_limit_trade` with `reduce_only=true`: Strike rejects reduce-only limits when no position exists yet, while trigger orders rest until `stop_price` is hit.

Direction is stated from the order's own side, which reads backwards until you say it out loud:

- Protecting a **long**: `side: "short"` - sell on trigger.
- Protecting a **short**: `side: "long"` - buy on trigger.

```bash
# Stop-loss on an open long: sell 4800 ADA if mark trades down to 0.1995.
call_mcp strike_place_stop_loss '{
  "symbol": "ADA-PERP", "side": "short",
  "quantity": 4800, "stop_price": 0.1995, "confirm": false
}'
```

`stop_price` triggers off the **mark** price, which is the price liquidation and PnL settle against - not the last trade, and not the book mid. Read it with `strike_get_mark_price` before placing a trigger, and quote which one you used.

## 4. The send

Dry run, show the floor, then send once.

```bash
call_mcp strike_place_bracket_limit '{...,"confirm":false}'   # preview; nothing happens
# preview matches the ticket, the user has approved by id
call_mcp strike_place_bracket_limit '{...,"confirm":true}'    # the send. Once.
```

The arguments in the two calls are identical apart from `confirm`. If anything else changed between them, the ticket is void and the Risk Manager re-sizes.

Then read the **LIVE STATUS** line and report it verbatim: `filled`, `resting`, `rejected` with the exchange's reason, or `unverified`. Never report "sent" as an outcome.

## 5. After the send

Reconcile from the exchange record, not from the response you hoped for:

```bash
call_mcp strike_get_open_orders '{}'
call_mcp strike_get_order_history '{"symbol":"ADA-PERP","limit":10}'
call_mcp strike_get_fill_history  '{"symbol":"ADA-PERP","limit":20}'
call_mcp strike_get_open_positions '{}'
```

Report the exchange's numbers: filled price and size, order ids, fees, and whether protection is actually resting. A bracket whose entry filled but whose exit legs are not in `strike_get_open_orders` is an **unprotected position** - an incident under `desk-incident-response`, reported immediately, not noted in a footer.

Strike gives no client-order-id on placement, so the desk reconciles by the returned order id plus symbol, side, size and timestamp. Record the order id in the proposal file the moment it comes back; it is the only handle the desk gets.

## 6. Cancelling

```bash
call_mcp strike_get_open_orders '{}'                                              # find the id
call_mcp strike_cancel_order '{"orderId":"123456","symbol":"ADA-PERP","confirm":false}'
call_mcp strike_cancel_order '{"orderId":"123456","symbol":"ADA-PERP","confirm":true}'
```

`orderId` takes the numeric id or the client order id UUID, and `symbol` is required. Cancelling protection on a position that stays open needs the user's approval like anything else - it increases risk. Cancelling an unfilled entry that the user has stood down does not, once they have said to stand it down.

## 7. What this desk does not do

- No naked entry. Every new position carries a stop, in the same bracket where the API allows it.
- No resend on an unknown result. `unverified` goes to `desk-incident-response`, never to a retry.
- No `confirm=true` without a matching approved ticket id.
- No leverage above the server's cap, and no attempt to work around it.
- No order in a market outside the fifteen the MCP trades.
- No size rounded up to clear a minimum.
- No trading a market whose `strike_get_market_snapshot` came back `isError` stale, without a REST cross-check that says otherwise.
