---
name: strike-mcp
description: The desk's execution transport - the crowdtime MCP server at mcp.crowdtime.io, its twenty-two tools, the two ways to reach it from Grok Bot (connector or the desk computer), the bearer token, and the dry-run/confirm gate that stands between a ticket and a live order. Use when connecting the desk to Strike, when any MCP tool errors, when deciding which tool a job needs, or before the first send of the day. Read this before strike-orders.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs (HyperGrok), ported for Strike Finance
  category: strike
  network-default: mainnet
---

# Strike MCP

Execution on this desk goes through one place: the crowdtime MCP server. Market data does not - that comes from Strike's public REST Price Service (`strike-market-data`). Keep the two apart in your head, because **they do not even use the same symbols**.

## 1. The endpoint

| | |
| --- | --- |
| URL | `https://mcp.crowdtime.io/mcp` |
| Transport | Streamable HTTP, JSON-RPC 2.0 |
| Protocol | `2025-06-18` |
| Server | `strikealgobot-mcp` |
| Auth | `Authorization: Bearer <token>` |
| Session | Stateless - no `mcp-session-id` to carry |

The server also advertises OAuth (`/.well-known/oauth-protected-resource`, dynamic client registration, PKCE). The desk does not use it. A static bearer token issued from crowdtime API Settings is simpler, works identically from both routes below, and is the only credential the desk holds.

**The token is the whole account.** It can open, close and cancel. Treat it with the care a private key deserves: it lives in Grok Bot's secure secret store as `STRIKE_MCP_TOKEN`, never in chat, never in a file in the repository, never in a message to another Bot, never echoed into a journal or a receipt. If it appears in a conversation, it is burned - tell the user to rotate it in crowdtime API Settings before the desk trades again.

## 2. Two routes to the server

**Route A - Grok connector (preferred when available).** `grok.com/connectors` -> New Connector -> Custom -> the URL above -> complete authentication. Grok discovers the tools and offers them in conversation. Connectors are provisioned at team level by an admin, so a member cannot add one alone, and xAI does not document whether a connector reaches a named Bot. Test it before relying on it: ask the Execution Trader to call `strike_get_account_balance`. If it has no such tool, use Route B.

**Route B - the desk computer (always works).** All the user's Bots share one computer with network access, so the Execution Trader can speak JSON-RPC directly:

```bash
# The token comes from the secret store; it is never written into a file.
call_mcp() {                     # usage: call_mcp <tool> <json-arguments>
  curl -sS --max-time 60 https://mcp.crowdtime.io/mcp \
    -H "Authorization: Bearer $STRIKE_MCP_TOKEN" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$(printf '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"%s","arguments":%s}}' "$1" "$2")" \
  | python3 -c 'import sys,json
d=json.load(sys.stdin)
if "error" in d: print("MCP ERROR:", json.dumps(d["error"])); raise SystemExit(1)
for c in d["result"]["content"]:
    if c.get("type")=="text": print(c["text"])
print("isError:", d["result"].get("isError", False))'
}

call_mcp strike_get_account_balance '{}'
```

Route B is the one the desk documents everywhere else, because it is the one that always exists. If Route A works for the user, the same tool names and arguments apply.

## 3. Symbols: the translation you must not skip

Execution symbols and market-data symbols are different strings for the same market.

| Market | MCP (execution) | REST (market data) |
| --- | --- | --- |
| Bitcoin | `BTC-PERP` | `BTC-USD` |
| Ether | `ETH-PERP` | `ETH-USD` |
| Cardano | `ADA-PERP` | `ADA-USD` |
| Solana | `SOL-PERP` | `SOL-USD` |
| Zcash | `ZEC-PERP` | `ZEC-USD` |
| Hyperliquid | `HYPE-PERP` | `HYPE-USD` |
| NEAR | `NEAR-PERP` | `NEAR-USD` |
| Midnight | `NIGHT-PERP` | `NIGHT-USD` |
| **Gold** | **`GOLD-PERP`** | **`XAU-USD`** |
| Nvidia | `NVDA-PERP` | `NVDA-USD` |
| Tesla | `TSLA-PERP` | `TSLA-USD` |
| Micron | `MU-PERP` | `MU-USD` |
| SanDisk | `SNDK-PERP` | `SNDK-USD` |
| SK Hynix | `SKHYNIX-PERP` | `SKHYNIX-USD` |
| SpaceX | `SPCX-PERP` | `SPCX-USD` |

Gold is the trap: `GOLD-PERP` and `XAU-USD` are one market. Everything else swaps the suffix. Verified by mark price on both services.

**The MCP trades fifteen markets. The Price Service quotes thirty-one.** A market with public data is not necessarily a market the desk can trade. Before proposing anything, confirm the symbol is in the table above; if it is not, the answer is "the desk cannot trade that", not a ticket. `strike_scan_markets` returns the live universe and is the authority when this table and the server disagree.

## 4. The twenty-two tools

**Account, read-only** - Risk Manager, Trade Reviewer, Execution Trader.

| Tool | Returns |
| --- | --- |
| `strike_get_account_balance` | balances, equity, margin usage (all USD) |
| `strike_get_open_positions` | open positions |
| `strike_get_open_orders` | resting orders, with the ids `strike_cancel_order` needs |
| `strike_get_closed_positions` | closed positions with realised PnL; `symbol`, time range, `limit` |
| `strike_get_order_history` | orders by `status` (2 open, 3 filled, 4 canceled, 5 untriggered, 6 rejected, 7 expired), `order_id`, time range; page with `from_order_id` |
| `strike_get_fill_history` | individual fills; page back with `from_id`, poll forward with `since_trade_id` (never both) |

**Market, read-only** - Market Analyst, Strategist.

| Tool | Returns |
| --- | --- |
| `strike_get_mark_price` | the live mark price alone - the value liquidation and PnL settle against |
| `strike_get_market_snapshot` | mark, last close, RSI(14), EMA(20/50/200), MACD, Bollinger(20,2), ADX(14), ATR(14), a trend label, and the market's `tick_size`, `size_precision`, `min_notional_usd`, `max_leverage`. `interval` one of 1m/5m/15m/1h/4h/1d. Returns `isError` when the upstream feed is stale |
| `strike_scan_markets` | every supported market ranked by a composite "hotness" score, plus an `excluded` list naming markets under the liquidity floors |

`strike_scan_markets` is a **screen, not a signal**: it has no direction, and a market can top it because it is being violently liquidated. Its real value to this desk is the `excluded` list - it says which markets are too thin to trade before anyone analyses their technicals. Leave `min_oi_notional_usd` and `min_avg_daily_volume_usd` unset unless the user names a figure.

**Execution, writes** - the Execution Trader alone. Section 5.

| Tool | Does |
| --- | --- |
| `strike_place_market_trade` | open or close at market; `reduce_only` routes to closeLong/closeShort and will not flip the side |
| `strike_place_limit_trade` | a resting limit order; returns the order id |
| `strike_place_bracket_limit` | limit entry with attached TP, optionally SL, as one strategy order |
| `strike_place_take_profit` | reduce-only TP trigger on an existing position |
| `strike_place_stop_loss` | reduce-only SL trigger on an existing position |
| `strike_set_leverage` | per-symbol leverage, optionally margin mode; new positions only |
| `strike_cancel_order` | cancel by numeric order id or client order id UUID |

**Research and notification** - Research Analyst, Desk Lead.

| Tool | Does |
| --- | --- |
| `crowdtrendz_crypto_news_research` | returns *instructions* for a sourced crypto news and sentiment report; the Bot then does the web research itself |
| `crowdtrendz_stock_news_research` | the same for an equity ticker |
| `crowdtrendz_stock_dividend_research` | the same for dividends, scoped to an exchange or region |
| `bodega_list_markets` | read-only listing of Bodega prediction markets on Cardano; Yes/No prices as 0-1 implied probabilities |
| `discord_send_message` | posts to a Discord webhook the user configured in crowdtime API Settings; max 2000 characters |

The three `crowdtrendz_*` tools return a playbook, not a report. Calling one produces no research: the Bot must then go and do the work with its own web search and render the report under the formatting rules it was handed. Never present the returned instructions as findings. `strike_review_trading_style` behaves the same way for the account's own history, and is read-only and advisory.

`discord_send_message` leaves the conversation. It is an outbound notification to the user's own channel, so it carries the same rule as any external post: the desk sends it when the user has asked for alerts, it never contains the token, an account address, or anything the user has not agreed to have leave the chat.

## 5. The confirm gate

**Every write tool defaults to a dry run.** Without `confirm=true` the tool returns a preview and changes nothing. With `confirm=true` it places the order.

This is the strongest control the Strike desk has, and it is stronger than anything HyperGrok had on Hyperliquid: the gate is in the transport, not in a Bot's good behaviour. Use it as the desk's rehearsal step, every time:

1. The Risk Manager sizes the ticket.
2. The Execution Trader calls the tool with `confirm` absent or `false` and reads the preview back to the floor. Nothing has happened yet.
3. The user approves by ticket id.
4. The Execution Trader calls the tool **once** with `confirm=true` and the identical arguments.

A preview that does not match the ticket stops the trade. Re-sizing after a preview means a new ticket, not an edited one.

Never set `confirm=true` on a tool whose arguments the user has not approved by id. Never set it to "test" anything.

## 6. LIVE STATUS - the only line that counts

On a live placement the server reads the order back from the exchange and ends its result with a **LIVE STATUS** line: `filled`, `resting`, `rejected` (with the exchange's reason), or `unverified`.

Report that line. Not the submission, not your intent. A submitted order is not a live one.

`unverified` means the server could not confirm the order's state. It is the desk's `unavailable` verdict, and it is an incident, not a shrug:

- Do not resend. Do not "try again to be safe".
- Read the record: `strike_get_open_orders`, then `strike_get_order_history` (filter by symbol and time), then `strike_get_fill_history`.
- Only once the record shows the order absent may a replacement be considered, and only with a fresh approval.

Follow `desk-incident-response` from there.

## 7. Errors

| What you see | What it means | What to do |
| --- | --- | --- |
| HTTP 401 with `WWW-Authenticate: Bearer` | the token is missing, wrong or revoked | stop; ask the user to check crowdtime API Settings. Never retry with a guessed token |
| `isError: true` on `strike_get_market_snapshot` | the upstream price feed is stale | treat as `unavailable`; do not quote the indicators. Fall back to `strike-market-data` REST and say you did |
| A market rejected as unsupported | the symbol is outside the fifteen | check the table in section 3; do not translate the symbol yourself and retry |
| Leverage rejected | above the server's per-symbol `LEVERAGE_MAP` cap | the cap is a ceiling the desk cannot raise. Re-size under it |
| Reduce-only limit rejected, "no position" | Strike refuses reduce-only limits with no position open | use `strike_place_take_profit` / `strike_place_stop_loss`, or a bracket. See `strike-orders` |
| A timeout with no body | the result is unknown | treat exactly as `unverified` above |

Every error is reported with the tool name, the arguments minus the token, and the UTC time. A failed call is a fact the desk states, never one it smooths over.

## 8. Connectivity check (no order, no confirm)

Run this before the first send of any session. It touches only read tools.

```bash
call_mcp strike_get_account_balance '{}'                        # auth works, equity is live
call_mcp strike_scan_markets '{"top":5,"days":7}'               # the tradeable universe answers
call_mcp strike_get_open_positions '{}'                         # what the desk is already carrying
call_mcp strike_get_open_orders '{}'                            # and what is already resting
```

Four green reads and the desk is connected. Record the UTC time and the equity figure in `desk.md`. If any of them fails, the desk is not ready to trade and says so plainly rather than proceeding on the assumption that the write path is fine.
