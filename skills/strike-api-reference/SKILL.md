---
name: strike-api-reference
description: Compact reference for the Strike Finance surfaces the desk uses - every public Price Service endpoint, every signed trading and account endpoint with its parameters, the API wallet signing scheme, order types, flags and time-in-force, order status codes, per-market constraints, rate limits and error strings, plus the optional crowdtime MCP research tools. Use to look up an endpoint, a field, a status code or a flag without re-reading a whole skill.
license: MIT
metadata:
  version: "3.0.0"
  author: Mendurim
  category: strike
  network-default: mainnet
---

# Strike API reference

One symbol vocabulary throughout: `BTC-USD`, `ADA-USD`, `XAU-USD`. Thirty-one markets, all tradeable.

| | Base URL |
| --- | --- |
| Market data (public) | `https://api.strikefinance.org/price` |
| Trading and account (signed) | `https://api.strikefinance.org` |
| Testnet (both) | `https://api-v2-testnet.strikefinance.org` |
| WebSocket | `wss://api.strikefinance.org/ws/price` (public), `.../ws/user-api` (signed) |

## Auth

Four headers on every signed request: `X-API-Wallet-Public-Key` (64 hex), `X-API-Wallet-Signature` (128 hex), `X-API-Wallet-Timestamp` (Unix seconds), `X-API-Wallet-Nonce` (fresh UUID v4).

```
message   = {METHOD}:{PATH}:{TIMESTAMP}:{NONCE}:{BODY_HASH}
BODY_HASH = sha256 hex of the exact body bytes, or of "" when there is none
```

`PATH` excludes the query string. Use `scripts/strike_request.py`; see `strike-auth`.

## Market data, public, `GET /price/v2`

| Path | Parameters | Returns |
| --- | --- | --- |
| `/exchangeInfo` | - | symbols, filters, order types, rate limits |
| `/ticker/price` | `symbol` optional | last price |
| `/ticker/bookTicker` | `symbol` optional | best bid and ask |
| `/ticker/24hr` | `symbol` optional | change, high, low, volume, quoteVolume |
| `/premiumIndex` | `symbol` | `markPrice`, `indexPrice`, `fundingRate`, `nextFundingTime` |
| `/markPrice`, `/indexPrice` | `symbol` | one price each |
| `/openInterest` | `symbol` | open interest in base units |
| `/depth` | `symbol`, `limit` 1-1000 **default 20** | `bids`, `asks`, `lastUpdateId`, `E`, `T` |
| `/trades` | `symbol`, `limit` | recent trades |
| `/klines` | `symbol`, `interval`, `limit`, time range | candles; mark and index series too |

Always pass `limit=1000` to `/depth`. Rate limits: 2400 request weight/min, 1200 orders/min.

## Account, signed, `GET /v2`

`/account` · `/balances` · `/portfolio` · `/positions` · `/openOrders` · `/closedPositions` · `/history/order` · `/history/fill` · `/history/funding` · `/history/transaction`

`GET /v2/order` takes `order_id` **or** `client_order_id` and returns that one order - the authoritative lookup after an unknown send result.

Transaction types: `1` deposit, `2` withdraw, `3` fee. Time filters are Unix milliseconds.

## Trading, signed

| Endpoint | Method | Notes |
| --- | --- | --- |
| `/v2/order` | POST | one order |
| `/v2/order/strategy` | POST | bracket: entry + `tp_order` + `sl_order` |
| `/v2/orders/batch` | POST | `orders` array |
| `/v2/order/replace` | POST | `{cancel:{order_id,symbol}, new_order:{...}}` |
| `/v2/order/replace-batch` | POST | several at once |
| `/v2/order/cancel` | DELETE | `order_id` (integer) + `symbol` - **not** a client order id |
| `/v2/order/cancel-all` | DELETE | optional `symbol` |
| `/v2/leverage` | POST | `symbol`, `leverage`; new positions only |
| `/v2/marginMode` | POST | `symbol`, `marginMode` `cross`/`isolated`; rejected with a position open |
| `/v2/isoMargin` | POST | `symbol`, `amount`, `modify_type` |
| `/v2/algo/twap` | POST/GET | create / list; `/v2/algo/twap/{id}` GET and DELETE |

### `POST /v2/order` fields

Required: `symbol`, `side` (`buy`/`sell`), `type`, `size` (string).

Optional: `client_order_id`, `price`, `stop_price`, `time_in_force` (`GTC`/`IOC`/`FOK`, default GTC), `working_type` (`mark_price` default / `contract_price`), `post_only`, `reduce_only`, `close_position`, `price_protect`, `callback_rate`, `activation_price`, `slippage`, `vault_id`.

`type`: `limit` · `market` · `stop` · `stop_limit` · `take_profit` · `take_profit_limit` · `trailing_stop_market`.

Sizes and prices are **strings**. `slippage` is a decimal fraction (`"0.005"` = 50 bps) and applies to market orders. `callback_rate` is a percentage `"0.1"`-`"5"` for trailing stops.

### `POST /v2/order/strategy`

Required: `strategy_id`, `symbol`, `side`, `type` (`limit`/`market`), `size`. Plus `tp_order` and `sl_order`, each taking `type` (`take_profit`/`take_profit_limit`/`stop`/`stop_limit`), `size`, `stop_price`, and optionally `client_order_id`, `price`, `time_in_force`, `working_type`, `post_only`, `price_protect`.

### `POST /v2/algo/twap`

`symbol`, `side` (**uppercase** `BUY`/`SELL`), `total_size`, `duration_sec`; optional `limit_price`, `reduce_only`, `randomize`.

## Codes and constraints

**Order status:** `2` open · `3` filled · `4` canceled · **`5` untriggered** · `6` rejected · `7` expired. A resting stop that has not fired is 5, not 2.

**Per-market, from `/exchangeInfo`:** `PRICE_FILTER.tickSize`, `LOT_SIZE.stepSize`, `MIN_NOTIONAL.notional` ($10 across markets), `liquidationFee`, `triggerProtect`, `marketTakeBound`, `status` (only `trading` is tradeable). `ADA-USD` has `stepSize: 1` - whole tokens.

**Funding** accrues hourly; `nextFundingTime` lands on the hour.

## Errors

| Symptom | Meaning |
| --- | --- |
| 401 | key unregistered, wrong key, clock skew, reused nonce, or a body hash that does not match the bytes sent |
| Invalid or expired signature | timestamp too old, or the path signed included a query string |
| reduce-only rejected, no position | use a trigger order, or a bracket, rather than a reduce-only limit |
| leverage rejected | above the market maximum at that notional |
| timeout, no body | unknown result - look the order up by `client_order_id`, do not resend |

## Optional research add-on (crowdtime MCP)

`https://mcp.crowdtime.io/mcp`, bearer token, JSON-RPC. Not part of the trading path. Symbols are `-PERP` there (`GOLD-PERP` = `XAU-USD`) and it covers fifteen markets.

Read-only tools the desk uses: `strike_scan_markets`, `strike_get_market_snapshot`, `crowdtrendz_crypto_news_research`, `crowdtrendz_stock_news_research`, `crowdtrendz_stock_dividend_research`, `bodega_list_markets`, `discord_send_message`, `strike_review_trading_style`. Its order tools exist and **this desk does not use them**: execution is the signed REST API only. See `strike-research-tools`.

## Documentation

- Strike docs: `https://docs.strikefinance.org`
- OpenAPI specs: `https://github.com/strike-finance/strike-finance-skills` (`openapi/market-api.yaml`, `user-api.yaml`, `trade-api.yaml`) - authoritative over this page.
