---
name: strike-api-reference
description: Compact reference for both Strike Finance surfaces the desk uses - every public REST Price Service endpoint with its parameters, every crowdtime MCP tool with its arguments, the symbol map between them, order types and time-in-force, order status codes, per-market constraints, rate limits and error strings. Use to look up an endpoint, a tool argument, a status code or a symbol without re-reading a whole skill.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs (HyperGrok), ported for Strike Finance
  category: strike
  network-default: mainnet
---

# Strike API reference

Two surfaces. Market data is public REST; execution is the crowdtime MCP. They use different symbols.

## Endpoints and transports

| | Market data | Execution |
| --- | --- | --- |
| Mainnet | `https://api.strikefinance.org/price` | `https://mcp.crowdtime.io/mcp` |
| Testnet | `https://api-v2-testnet.strikefinance.org/price` | - |
| WebSocket | `wss://api.strikefinance.org/ws/price` (public), `wss://api.strikefinance.org/ws/user-api` (auth) | - |
| Method | `GET` | `POST`, JSON-RPC 2.0, protocol `2025-06-18` |
| Auth | none | `Authorization: Bearer $STRIKE_MCP_TOKEN` |

## REST: Price Service `/v2`

| Path | Parameters | Returns |
| --- | --- | --- |
| `/exchangeInfo` | - | every symbol, filters, order types, rate limits |
| `/ticker/price` | `symbol` optional | last price |
| `/ticker/bookTicker` | `symbol` optional | best bid and ask |
| `/ticker/24hr` | `symbol` optional | change, high, low, volume, quoteVolume |
| `/premiumIndex` | `symbol` | `markPrice`, `indexPrice`, `fundingRate`, `nextFundingTime` |
| `/markPrice` | `symbol` | mark only |
| `/indexPrice` | `symbol` | index only |
| `/openInterest` | `symbol` | open interest in base units |
| `/depth` | `symbol`, `limit` 1-1000 **default 20** | `bids`, `asks`, `lastUpdateId`, `E`, `T` |
| `/trades` | `symbol`, `limit` | recent trades |
| `/klines` | `symbol`, `interval`, `limit`, time range | candles; mark and index series also available |

Always pass `limit=1000` to `/depth`. The default of 20 stops short of 25 bps on a liquid market.

Rate limits from `/exchangeInfo`: 2400 request weight per minute, 1200 orders per minute.

## MCP tools

**Account, read** - `strike_get_account_balance {}` · `strike_get_open_positions {}` · `strike_get_open_orders {}` · `strike_get_closed_positions {symbol?, start_time?, end_time?, limit=25}` · `strike_get_order_history {symbol?, status?, order_id?, start_time?, end_time?, limit=25, from_order_id?}` · `strike_get_fill_history {symbol?, order_id?, start_time?, end_time?, limit=50, from_id? | since_trade_id?}`

**Market, read** - `strike_get_mark_price {symbol}` · `strike_get_market_snapshot {symbol, interval}` · `strike_scan_markets {days=7, symbols?, top?, min_oi_notional_usd?, min_avg_daily_volume_usd?}`

**Execution, write - all default to a dry run and need `confirm=true`**

| Tool | Required | Optional |
| --- | --- | --- |
| `strike_place_market_trade` | `symbol`, `side`, `quantity` | `reduce_only`, `leverage`, `confirm` |
| `strike_place_limit_trade` | `symbol`, `side`, `quantity`, `price` | `reduce_only`, `leverage`, `confirm` |
| `strike_place_bracket_limit` | `symbol`, `side`, `quantity`, `entry_price`, `tp_stop_price` | `tp_limit_price`, `sl_stop_price`, `sl_limit_price`, `time_in_force`, `leverage`, `confirm` |
| `strike_place_take_profit` | `symbol`, `side`, `quantity`, `stop_price` | `limit_price`, `confirm` |
| `strike_place_stop_loss` | `symbol`, `side`, `quantity`, `stop_price` | `limit_price`, `confirm` |
| `strike_set_leverage` | `symbol`, `leverage` | `margin_mode` (`cross`/`isolated`) - **no confirm parameter** |
| `strike_cancel_order` | `orderId`, `symbol` | `confirm` |

`side` is always `long` or `short`. On trigger orders it is the side of the closing order: `short` protects a long.

**Research and notify** - `crowdtrendz_crypto_news_research {symbol, horizon?, lookback?}` · `crowdtrendz_stock_news_research {symbol, horizon?, lookback?}` · `crowdtrendz_stock_dividend_research {exchange, symbol?, event_types?, horizon?, lookback?}` · `bodega_list_markets {status=open, sort?, limit=50, include_raw?}` · `discord_send_message {content, connection?, username?}` · `strike_review_trading_style {window?, symbol?}`

## Symbol map

| MCP | REST | | MCP | REST |
| --- | --- | --- | --- | --- |
| `BTC-PERP` | `BTC-USD` | | `NVDA-PERP` | `NVDA-USD` |
| `ETH-PERP` | `ETH-USD` | | `TSLA-PERP` | `TSLA-USD` |
| `ADA-PERP` | `ADA-USD` | | `MU-PERP` | `MU-USD` |
| `SOL-PERP` | `SOL-USD` | | `SNDK-PERP` | `SNDK-USD` |
| `ZEC-PERP` | `ZEC-USD` | | `SKHYNIX-PERP` | `SKHYNIX-USD` |
| `HYPE-PERP` | `HYPE-USD` | | `SPCX-PERP` | `SPCX-USD` |
| `NEAR-PERP` | `NEAR-USD` | | `NIGHT-PERP` | `NIGHT-USD` |
| **`GOLD-PERP`** | **`XAU-USD`** | | | |

Fifteen tradeable through the MCP; thirty-one quoted by REST. `GOLD-PERP`/`XAU-USD` is the only pair that is not a suffix swap.

## Codes and constraints

**Order status** (`strike_get_order_history`): 2 open · 3 filled · 4 canceled · 5 untriggered · 6 rejected · 7 expired. A resting stop that has not fired is **5**, not 2.

**Order types** (`exchangeInfo.orderType`): `LIMIT`, `MARKET`, `STOP`, `STOP_MARKET`, `TAKE_PROFIT`, `TAKE_PROFIT_MARKET`. **Time in force**: `GTC`, `IOC`, `FOK`.

**Size precision**: `BTC-PERP` 5dp · `ETH-PERP` 3dp · `SOL-PERP` 2dp · `ZEC-PERP` 2dp · `NVDA-PERP` 2dp · `HYPE-PERP` 2dp · `ADA-PERP` 0dp. Authoritative values come from `strike_get_market_snapshot` (`size_precision`) or `exchangeInfo` (`LOT_SIZE.stepSize`).

**Min notional**: $10 across markets (`MIN_NOTIONAL.notional`).

**Leverage**: the MCP caps per symbol below or at Strike's market maximum and the cap cannot be bypassed. Defaults around 20x, 25x on ETH.

## Result lines and errors

**LIVE STATUS** ends every live placement: `filled`, `resting`, `rejected` (with reason), `unverified`. Report this, never the submission.

| Error | Meaning |
| --- | --- |
| HTTP 401 + `WWW-Authenticate: Bearer` | token missing, wrong or revoked |
| `isError: true` on `strike_get_market_snapshot` | upstream feed stale; indicators are `unavailable` |
| reduce-only limit rejected, "no position" | use `strike_place_take_profit` / `strike_place_stop_loss` or a bracket |
| leverage rejected | above the per-symbol cap; re-size under it |
| unsupported symbol | outside the fifteen |
| timeout, no body | unknown result - treat as `unverified`, do not resend |

## Documentation

- Strike docs: `https://docs.strikefinance.org`
- OpenAPI specs: `https://github.com/strike-finance/strike-finance-skills` (`openapi/market-api.yaml`, `user-api.yaml`, `trade-api.yaml`)
- MCP tool list is authoritative over this page: `tools/list` on the endpoint above.
