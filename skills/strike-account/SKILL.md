---
name: strike-account
description: Reading Strike Finance account state through the crowdtime MCP - equity, balances and margin usage, open positions, resting orders, closed positions with realised PnL, order history by status, and individual fills with paging. Use before sizing any trade, when checking whether protection actually exists, when reconciling a send, and when building a post-trade review. Read-only; no tool here changes anything.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs (HyperGrok), ported for Strike Finance
  category: strike
  network-default: mainnet
---

# Strike account

Six read-only MCP tools. They are the desk's only view of its own money, and the only acceptable one: a position from a brief an hour ago is not a position.

Transport, token and the `call_mcp` helper are in `strike-mcp`. Symbols here are MCP symbols (`GOLD-PERP`, not `XAU-USD`).

## 1. Live state

```bash
call_mcp strike_get_account_balance '{}'     # equity, balances, margin usage - all USD
call_mcp strike_get_open_positions  '{}'     # what the desk is carrying
call_mcp strike_get_open_orders     '{}'     # what is resting, with the ids cancels need
```

Every monetary field is USD-denominated whatever the collateral is called. Read all three before sizing anything: equity sets the risk budget, positions set what is already at risk, and resting orders are exposure that has not happened yet.

**`strike_get_open_orders` is the protection check.** A position in `strike_get_open_positions` with no matching reduce-only trigger in `strike_get_open_orders` is unprotected. That is an incident under `desk-incident-response`, reported the moment it is seen - not a line at the bottom of a brief.

## 2. History

```bash
call_mcp strike_get_closed_positions '{"symbol":"ADA-PERP","limit":25}'
call_mcp strike_get_order_history    '{"symbol":"ADA-PERP","status":3,"limit":50}'
call_mcp strike_get_fill_history     '{"symbol":"ADA-PERP","limit":50}'
```

Order status codes: **2** open, **3** filled, **4** canceled, **5** untriggered, **6** rejected, **7** expired.

`5 untriggered` is the one to know: a resting stop-loss or take-profit that has not fired sits here, not in `2 open`. A reviewer counting only status 2 will report a protected position as unprotected.

Time filters are Unix milliseconds (`start_time`, `end_time`). Both history tools clamp `limit` to 1000.

## 3. Paging

- `strike_get_order_history`: `from_order_id` is a forward cursor - orders with id greater than the value.
- `strike_get_fill_history`: `from_id` pages **backward** (newest first); `since_trade_id` polls **forward** for new fills. They cannot be combined - passing both is an error, not a narrower filter.

Page until the window you were asked about is covered, and say how many pages you read. A review that silently stopped at the first 50 fills is a wrong review.

## 4. Reconstructing a trade

One order can produce many fills; one position can span many orders. To reconstruct a round trip:

1. `strike_get_closed_positions` for the position and its realised PnL.
2. `strike_get_order_history` over the same window for every order that touched the symbol.
3. `strike_get_fill_history` filtered by `order_id` for the actual execution prices and sizes.
4. Compare against the ticket in `/workspace/trading-desk/proposals/SG-*.md`.

Report the exchange's numbers. Where a figure had to be derived, show the arithmetic. Where the record is incomplete, say `unavailable` and name what is missing - never interpolate a fill price.

## 5. The trading-style review

`strike_review_trading_style` returns an **instruction playbook**, not a report. After calling it the Bot pulls the history itself with the tools above, reconstructs round trips, and writes the analysis. Never present the returned instructions as findings, and never let its suggestions become trades: it is advisory, and every trade still goes through the lifecycle.

```bash
call_mcp strike_review_trading_style '{"window":"last 30 days"}'
```

## 6. Rules

- Live or nothing. Call the tools; never answer account questions from memory or from another Bot's message.
- Every figure carries a UTC time.
- Equity, margin usage and distance to liquidation are the Risk Manager's inputs and are re-read for each ticket.
- Nothing in this skill writes. If a job needs a change, it belongs to the Execution Trader and `strike-orders`.
