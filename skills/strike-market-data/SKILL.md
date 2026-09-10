---
name: strike-market-data
description: Reading Strike Finance market data from the public REST Price Service - mark and index price, order book depth, spread, funding, open interest, 24h statistics, candles and per-market trading constraints. No key, no account. Use for market briefs, liquidity reads before execution, and candle history for backtests. Read-only.
license: MIT
metadata:
  version: "1.0.1"
  author: Mendurim
  category: strike
  network-default: mainnet
---

# Strike market data

The desk reads markets from Strike's **public REST Price Service**. It needs no token and touches no account, which is why the desk can brief a market before it can trade one.

Execution uses the signed API (`strike-orders`) with the same `-USD` symbols, so nothing needs translating. Every figure the desk quotes carries its source and a UTC time.

## 1. Base URLs

| | |
| --- | --- |
| Mainnet | `https://api.strikefinance.org/price` |
| Testnet | `https://api-v2-testnet.strikefinance.org/price` |
| Public WebSocket | `wss://api.strikefinance.org/ws/price` |

Base path `/v2`. All endpoints below are `GET`, unauthenticated, and Binance-shaped.

Testnet quotes four markets and its books are routinely empty. That is a real state, not a failed read - see section 7.

## 2. Every read the desk uses

```bash
BASE=https://api.strikefinance.org/price

curl -sS "$BASE/v2/exchangeInfo"                          # every market + tick/lot/minNotional
curl -sS "$BASE/v2/ticker/price?symbol=BTC-USD"           # last price
curl -sS "$BASE/v2/ticker/bookTicker?symbol=BTC-USD"      # best bid / best ask
curl -sS "$BASE/v2/ticker/24hr?symbol=BTC-USD"            # change, high, low, volume
curl -sS "$BASE/v2/premiumIndex?symbol=BTC-USD"           # mark, index, funding, nextFundingTime
curl -sS "$BASE/v2/markPrice?symbol=BTC-USD"              # mark alone
curl -sS "$BASE/v2/indexPrice?symbol=BTC-USD"             # index alone
curl -sS "$BASE/v2/openInterest?symbol=BTC-USD"           # open interest in base units
curl -sS "$BASE/v2/depth?symbol=BTC-USD&limit=1000"       # order book
curl -sS "$BASE/v2/trades?symbol=BTC-USD&limit=100"       # recent trades
curl -sS "$BASE/v2/klines?symbol=BTC-USD&interval=1h&limit=500"   # candles
```

Omitting `symbol` on `ticker/24hr` or `ticker/price` returns every market at once - the cheap way to survey thirty-one markets in one call.

## 3. Depth: ask for the whole book

`/v2/depth` **defaults to 20 levels a side**, which stops short of 25 bps on a liquid Strike perp and silently understates every band. Always pass `limit=1000`, the documented maximum; at that limit the whole resting book comes back (about 40-50 levels a side on BTC).

Report executable depth as size within 5, 10 and 25 bps of the book mid, each side, in USD. When the furthest resting order sits closer than the band, the number is a **floor** (`>= $X`), not a measurement - say which, and say how far the book actually reached. A thin book and a truncated read are different facts; never let either pass as a total.

`scripts/opening_bell.py` does all of this correctly and is the reference implementation:

```bash
python3 scripts/opening_bell.py --symbol BTC-USD
python3 scripts/opening_bell.py --symbol ADA-USD --json
python3 scripts/opening_bell.py --symbol BTC-USD --testnet
```

## 4. Funding

`/v2/premiumIndex` returns `fundingRate`, `markPrice`, `indexPrice` and `nextFundingTime`.

Funding on Strike accrues **hourly** - `nextFundingTime` lands on the hour. State the hourly rate. If you annualise, say you did and show the arithmetic (`rate x 24 x 365`), and call it simple, not compounded. A funding rate is a cost of carry, not a direction.

## 5. Trading constraints

`/v2/exchangeInfo` carries what the Risk Manager needs to size and the Execution Trader needs to round, per symbol:

- `PRICE_FILTER.tickSize`, `LOT_SIZE.stepSize`, `MIN_NOTIONAL.notional`
- `liquidationFee`, `triggerProtect`, `marketTakeBound`
- `orderType`: `LIMIT, MARKET, STOP, STOP_MARKET, TAKE_PROFIT, TAKE_PROFIT_MARKET`
- `timeInForce`: `GTC, IOC, FOK`
- `status` - only `trading` is tradeable

`ADA-USD` has `stepSize: 1`: whole tokens. `MIN_NOTIONAL` is $10 across the board, which makes a minimum-size rehearsal trade cheap.

## 6. Derived measures

Anything beyond what the endpoints return is the desk's own arithmetic, shown with its formula: notional from size and mark, annualised funding from the hourly rate, depth bands from the book, realised range from candles.

The optional research add-on (`strike-research-tools`) can supply computed indicators and a liquidity screen. Use them alongside these reads, never instead of them, and treat the desk's own arithmetic as the primary figure.

**An indicator is not a signal.** RSI(14) at 78 is a fact about recent closes. "So it will fall" is not this desk's job, and no Bot on the floor says it.

## 7. Data hygiene

- Fetch, then speak. Never answer a market-data question from memory.
- Every figure carries service, symbol, and the UTC time observed. Batch these at the top of a brief.
- Open interest is in base units; give notional too, using the mark you fetched.
- Separate **facts** (what the API returned), **derived** (your arithmetic, with the formula) and **read** (your interpretation, labelled as such).
- A one-sided or empty book means mid, spread and depth are `unavailable`. Say so. On testnet this is the normal case. An empty book is never a zero.
- A stale feed, a gap in candles, or a call that failed is `unavailable` - a verdict of its own. It never collapses into "the condition did not fire".

## 8. Saving datasets for the Strategist

```bash
mkdir -p /workspace/trading-desk/data
curl -sS "$BASE/v2/klines?symbol=ADA-USD&interval=1h&limit=1000" \
  > /workspace/trading-desk/data/ADA-USD-1h-$(date -u +%Y%m%d).json
```

Hand over the file path **and** the exact request, so the Strategist can reproduce it. Klines are also available as mark-price and index-price series; say which one a dataset holds, because a backtest on last-trade prices and one on mark prices are different backtests.
