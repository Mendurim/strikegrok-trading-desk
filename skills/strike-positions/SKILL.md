---
name: strike-positions
description: Managing Strike Finance positions - leverage and margin mode per symbol, the server's leverage cap, reading margin usage and distance to liquidation, attaching protection to an existing position, and closing with reduce-only orders. Use when setting up a market before a first trade, when a position needs protecting or closing, or when checking liquidation risk. Leverage, protection and closes write; reads are open to the Risk Manager.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs (HyperGrok), ported for Strike Finance
  category: strike
  network-default: mainnet
---

# Strike positions

Reads here are open to the Risk Manager and Trade Reviewer. The three write paths - leverage, protection, closing - belong to the Execution Trader alone, under `strike-orders` rules.

## 1. Leverage

```bash
call_mcp strike_set_leverage '{"symbol":"ADA-PERP","leverage":5}'
call_mcp strike_set_leverage '{"symbol":"ADA-PERP","leverage":5,"margin_mode":"isolated"}'
```

- Applies to **new positions only**. An open position keeps the leverage it was opened at, so leverage is set *before* an entry, never as a fix afterwards.
- Returns the new leverage and Strike's `maxNotionalValue` at that leverage. Size subsequent orders against that figure.
- `margin_mode` is optional and Strike **rejects a margin-mode change while a position is open** on the symbol. The leverage half is still attempted, so read the response rather than assuming both applied.
- Unlike the order tools, `strike_set_leverage` has no `confirm` parameter. It takes effect immediately. Treat it as a write that needs the user's approval whenever it raises leverage; lowering it to comply with a limit does not.

**The cap is a ceiling the desk cannot raise.** The MCP enforces a per-symbol leverage cap of its own, at or below Strike's market maximum, and no Bot can bypass it. A ticket that needs more leverage than the cap allows is a reject, and the reject names the cap.

Defaults sit high - 20x on most markets, 25x on ETH. High leverage is not the desk's default. The Risk Manager sizes from stop distance and risk budget; leverage is whatever falls out of that arithmetic, and the user's `risk-limits.md` may only tighten it.

## 2. Reading the risk

```bash
call_mcp strike_get_open_positions  '{}'
call_mcp strike_get_account_balance '{}'
call_mcp strike_get_mark_price '{"symbol":"ADA-PERP"}'
```

Report, per position: side, size, entry, current mark, unrealised PnL, effective leverage, margin used, and distance to liquidation in both price and percent. Distance to liquidation is measured against the **mark** price, because that is what liquidation settles against - not the last trade and not the book mid.

`liquidationFee` per market comes from `/v2/exchangeInfo` (`strike-market-data`); it is part of what a liquidation actually costs and belongs in any honest reckoning of a worst case.

## 3. Protecting a position that already exists

A position with no resting stop is the desk's most urgent state. Use the trigger tools, not reduce-only limits - Strike rejects reduce-only limits when no position exists yet, while triggers rest until `stop_price` is hit.

```bash
# Stop on an open long: sell on trigger.
call_mcp strike_place_stop_loss '{"symbol":"ADA-PERP","side":"short","quantity":4800,"stop_price":0.1995,"confirm":false}'
```

Protecting a long is `side: "short"`; protecting a short is `side: "long"`. Full direction and rounding rules are in `strike-orders`.

If `desk.md` records a standing approval for protective stops, a reduce-only stop on an unprotected position may be placed without waiting - it can only reduce exposure. Everything else waits for approval by ticket id. Record what was placed and why.

## 4. Closing

```bash
call_mcp strike_place_market_trade '{"symbol":"ADA-PERP","side":"short","quantity":4800,"reduce_only":true,"confirm":false}'
```

`reduce_only=true` routes to closeLong/closeShort and **will not flip into the opposite side**, which is what makes it the safe way out. Side is the side of the closing order: close a long by going `short`.

- A partial close is a smaller `quantity`; the rest stays open and stays protected.
- After any close, cancel the protection that is now orphaned - a stop left resting on a position that no longer exists can open a new one if it triggers. Check `strike_get_open_orders` and cancel what is stale.
- `leverage` is ignored on reduce-only orders.

Closing is a trade: new ticket under the same proposal id, Risk PASS, approval by id, one send, reconciliation.

## 5. What this desk does not do

- No leverage raised to make a ticket fit.
- No margin-mode change attempted on an open position and then reported as done.
- No position left without a resting stop, on any network.
- No close without checking afterwards that the position is actually gone and its protection is cancelled.
