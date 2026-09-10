---
name: strike-account
description: Reading Strike Finance account state through the signed REST API - the account object, balances, portfolio, open positions, open orders, closed positions with realised PnL, order and fill history, funding actually paid, and the transaction ledger. Use before sizing any trade, when checking whether protection exists, when reconciling a send, and when building a post-trade review. Read-only; nothing here changes anything.
license: MIT
metadata:
  version: "2.0.0"
  author: Galleon Labs (HyperGrok), ported for Strike Finance
  category: strike
  network-default: mainnet
---

# Strike account

Nine read-only endpoints. They are the desk's only view of its own money, and the only acceptable one: a position from a brief an hour ago is not a position.

Signing, the API wallet and the `strike_request.py` helper are in `strike-auth`.

## 1. Live state

```bash
cd /workspace/strikegrok
python3 scripts/strike_request.py GET /v2/account      # equity, margin usage, maxWithdrawAmount
python3 scripts/strike_request.py GET /v2/balances     # per-asset balances
python3 scripts/strike_request.py GET /v2/positions    # size, entry, leverage, liquidation, uPnL
python3 scripts/strike_request.py GET /v2/openOrders   # resting orders, with the numeric ids cancels need
python3 scripts/strike_request.py GET /v2/portfolio    # account-level portfolio view
```

Read account, positions and open orders before sizing anything: equity sets the risk budget, positions set what is already at risk, and resting orders are exposure that has not happened yet.

**`GET /v2/openOrders` is the protection check.** A position with no matching reduce-only trigger is unprotected - an incident under `desk-incident-response`, reported the moment it is seen, not a footnote.

Remember order status **5 = untriggered**. A resting stop that has not fired lives there, not in `2 open`.

## 2. History

```bash
python3 scripts/strike_request.py GET /v2/closedPositions --query symbol=ADA-USD --query limit=25
python3 scripts/strike_request.py GET /v2/history/order  --query symbol=ADA-USD --query limit=50
python3 scripts/strike_request.py GET /v2/history/fill   --query symbol=ADA-USD --query limit=50
python3 scripts/strike_request.py GET /v2/history/funding --query symbol=ADA-USD
python3 scripts/strike_request.py GET /v2/history/transaction
```

`GET /v2/history/funding` is what the desk actually paid or received in funding - a real read, not a derivation from the current rate. Use it for the holding-cost line in every review; a rate multiplied by a guess at hours is not a fee figure.

`GET /v2/history/transaction` types: `1` deposit, `2` withdraw, `3` fee, `4` and up per the spec. Fees appear here, which is how the desk corrects the taker-rate assumption recorded in `risk-limits.md` against what it was really charged.

Time filters are Unix milliseconds. Page until the window you were asked about is covered, and say how many pages you read. A review that silently stopped at the first page is a wrong review.

## 3. A single order, authoritatively

```bash
python3 scripts/strike_request.py GET /v2/order --query client_order_id=SG-20260910-01-entry
python3 scripts/strike_request.py GET /v2/order --query order_id=1839201122
```

Either id works. This is the endpoint that makes an unknown send result recoverable: because the desk chose the `client_order_id` before sending, it can ask the exchange directly whether that order exists rather than inferring from a list. Use it first in any reconciliation.

## 4. Reconstructing a trade

One order can produce many fills; one position can span many orders.

1. `GET /v2/closedPositions` for the position and its realised PnL.
2. `GET /v2/history/order` over the same window for every order that touched the symbol.
3. `GET /v2/history/fill` for the actual execution prices, sizes and fees.
4. `GET /v2/history/funding` for what the position cost to hold.
5. Compare against the ticket in `/workspace/trading-desk/proposals/SG-*.md`, matched by `client_order_id`.

Report the exchange's numbers. Show the arithmetic where a figure was derived. Where the record is incomplete, say `unavailable` and name what is missing - never interpolate a fill price.

## 5. Rules

- Live or nothing. Call the endpoints; never answer account questions from memory or from another Bot's message.
- Every figure carries a UTC time.
- Equity, margin usage and distance to liquidation are the Risk Manager's inputs and are re-read for every ticket.
- Nothing here writes. If a job needs a change, it belongs to the Execution Trader and `strike-orders`.
