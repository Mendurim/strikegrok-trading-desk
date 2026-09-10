---
name: strike-positions
description: Managing Strike Finance positions through the signed REST API - leverage per symbol, cross and isolated margin mode, reading margin usage and distance to liquidation, attaching protection to an existing position, and closing with reduce-only orders. Use when setting up a market before a first trade, when a position needs protecting or closing, or when checking liquidation risk. Leverage, protection and closes write; reads are open to the Risk Manager.
license: MIT
metadata:
  version: "2.0.0"
  author: Mendurim
  category: strike
  network-default: mainnet
---

# Strike positions

Reads here are open to the Risk Manager and Trade Reviewer. The write paths - leverage, margin mode, protection, closing - belong to the Execution Trader alone, under `strike-orders` rules. Signing is in `strike-auth`.

## 1. Leverage

```bash
cd /workspace/strikegrok
python3 scripts/strike_request.py POST /v2/leverage --body-file lev.json   # {"symbol":"ADA-USD","leverage":5}
```

- Applies to **new positions only**. An open position keeps the leverage it opened at, so leverage is set *before* an entry, never as a repair afterwards.
- The response carries the resulting maximum notional at that leverage. Size against that figure, not against the headline number: a size that fits at the headline may not fit at the notional the position actually reaches.
- High leverage is not the desk's default. The Risk Manager sizes from stop distance and risk budget; leverage is whatever falls out of that arithmetic, and `risk-limits.md` may only tighten it.
- Raising leverage needs the user's approval. Lowering it to comply with a limit does not.

## 2. Margin mode

```bash
python3 scripts/strike_request.py POST /v2/marginMode --body-file mode.json  # {"symbol":"ADA-USD","marginMode":"isolated"}
```

Strike **rejects a margin-mode change while a position is open** on that symbol. Read the response rather than assuming it applied. Adjusting isolated margin on a live position is `strike-advanced`.

## 3. Reading the risk

```bash
python3 scripts/strike_request.py GET /v2/positions
python3 scripts/strike_request.py GET /v2/account
curl -sS "https://api.strikefinance.org/price/v2/markPrice?symbol=ADA-USD"
```

Report, per position: side, size, entry, current mark, unrealised PnL, effective leverage, margin used and mode, and distance to liquidation in both price and percent.

Distance to liquidation is measured against the **mark** price - that is what liquidation settles against, not the last trade and not the book mid. `liquidationFee` per market comes from `/v2/exchangeInfo` and is part of what a liquidation actually costs; include it in any honest worst case.

## 4. Protecting a position that already exists

A position with no resting stop is the desk's most urgent state.

```bash
# Stop on an open long: sell on trigger, reduce-only, off the mark price.
cat > sl.json <<'JSON'
{"client_order_id":"SG-20260910-01-sl","symbol":"ADA-USD","side":"sell","type":"stop",
 "size":"4800","stop_price":"0.1995","reduce_only":true,
 "working_type":"mark_price","close_position":true,"price_protect":true}
JSON
python3 scripts/strike_request.py POST /v2/order --body-file sl.json
python3 scripts/strike_request.py GET /v2/openOrders     # confirm it is resting (status 5, untriggered)
```

`sell` protects a long; `buy` protects a short. `close_position: true` flattens the whole position on trigger whatever the size field says, which keeps protection correct after a partial fill or an add - prefer it for a stop meant to flatten.

If `desk.md` records a standing approval for protective stops, a reduce-only stop on an unprotected position may be placed without waiting; it can only reduce exposure. Everything else waits for approval by ticket id. Record what was placed and why.

## 5. Closing

```bash
cat > close.json <<'JSON'
{"client_order_id":"SG-20260910-01-close","symbol":"ADA-USD","side":"sell","type":"market",
 "size":"4800","reduce_only":true,"slippage":"0.005"}
JSON
python3 scripts/strike_request.py POST /v2/order --body-file close.json
```

- `reduce_only: true` means the order cannot flip you into the opposite side. It is what makes this the safe way out.
- `side` is the side of the closing order: close a long by selling.
- Always set `slippage` on a market close.
- A partial close is a smaller `size`; the remainder stays open and **still needs its stop**, so resize protection before cancelling anything.
- After any close, read `GET /v2/positions` again. If the position is gone, cancel the now-orphaned protective orders - a stop left resting can open a *new* position if it triggers. Report which case it was.

Closing is a trade: new ticket under the same proposal id, Risk PASS, approval by id, one send, reconciliation.

## 6. What this desk does not do

- No leverage raised to make a ticket fit.
- No margin-mode change attempted on an open position and then reported as done.
- No position left without a resting stop.
- No close without confirming afterwards that the position is gone and its protection is cancelled.
- No `vault_id`; the desk trades the user's own account only.
