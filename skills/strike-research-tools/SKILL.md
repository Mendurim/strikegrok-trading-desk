---
name: strike-research-tools
description: The optional crowdtime MCP research add-on - how to reach it, and its market liquidity screen, computed technical indicators, crypto and equity news research, dividend research, Bodega prediction markets and Discord alerts. None of it is required: the desk trades entirely without it. Use for catalyst work on Strike's crypto and equity markets, for a liquidity screen before analysing a market, and for pushing alerts outside the chat.
license: MIT
metadata:
  version: "2.0.0"
  author: Mendurim
  category: strike
  network-default: mainnet
---

# Strike research tools

**This skill is optional.** The desk executes through the signed Strike REST API (`strike-orders`) and reads markets from the public Price Service (`strike-market-data`). It needs nothing here to trade.

What the crowdtime MCP adds is a handful of things Strike's own API does not compute: a liquidity screen, ready-made technical indicators, research playbooks, and a way to reach the user outside the chat. If it is not connected, say so when a request would have used it and fall back to primary sources; never let its absence become a silent gap.

**Nothing here executes.** The desk deliberately does not use the MCP's order tools: two write paths is exactly what the one-writer rule exists to prevent. Execution is the signed REST API, always.

## 1. Connecting (optional)

| | |
| --- | --- |
| URL | `https://mcp.crowdtime.io/mcp` |
| Transport | Streamable HTTP, JSON-RPC 2.0, stateless |
| Auth | `Authorization: Bearer $STRIKE_MCP_TOKEN` from crowdtime API Settings |

Two routes. **Grok connector:** `grok.com/connectors` -> New Connector -> Custom -> the URL above. Provisioned at team level by an admin, and xAI does not document whether a connector reaches a named Bot, so test it. **Desk computer:** JSON-RPC over HTTPS, which always works.

```bash
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
```

The token goes in Grok Bot's secure secret store as `STRIKE_MCP_TOKEN`, never in chat or a file. It is a second credential with trading power, so if the desk is not using the research tools, **do not provision it at all** - the smallest credential set is the safest one.

**Symbols differ here.** The MCP speaks `-PERP` (`ADA-PERP`, and `GOLD-PERP` for what the Price Service calls `XAU-USD`); everything else on this desk speaks `-USD`. It also covers only fifteen of Strike's thirty-one markets, so a market can be tradeable and still have no MCP coverage.

## 2. Liquidity screen

```bash
call_mcp strike_scan_markets '{"days":7}'
```

Ranks the markets it covers by a composite "hotness" score and returns an `excluded` list naming those below its open-interest and volume floors.

**Hotness is a screen, not a signal.** It has no direction, and a market tops it as readily for being violently liquidated as for being accumulated. The `excluded` list is the genuinely useful half: it says which markets are too thin to trade before anyone reads a chart. Leave `min_oi_notional_usd` and `min_avg_daily_volume_usd` unset unless the user names a figure.

## 3. Computed indicators

```bash
call_mcp strike_get_market_snapshot '{"symbol":"ADA-PERP","interval":"1h"}'
```

Mark, last close, RSI(14), EMA(20/50/200), MACD, Bollinger(20,2), ADX(14), ATR(14), and a trend label. Returns `isError` when its upstream feed is stale - then the indicators are `unavailable` and the desk falls back to `/v2/klines` and says it did.

An indicator is a fact about recent closes. RSI(14) at 78 is a fact; "so it will fall" is not this desk's job, and no Bot on the floor says it.

## 4. Research playbooks

```bash
call_mcp crowdtrendz_crypto_news_research '{"symbol":"ADA","horizon":"next 7 days","lookback":"last 14 days"}'
call_mcp crowdtrendz_stock_news_research '{"symbol":"NVDA","horizon":"next earnings"}'
call_mcp crowdtrendz_stock_dividend_research '{"exchange":"NASDAQ","symbol":"MU","horizon":"next 30 days"}'
```

**These return instructions, not findings.** Calling one produces no research: it hands back a playbook, and the Bot must then do the web research itself and write the report under the rules it was given. Never present a returned playbook as findings, and never say "research shows" on the strength of having called the tool. The citations in the report are pages **you** read, with links and UTC times.

`symbol` is a plain ticker (`ADA`, `NVDA`), not a Strike perp symbol. Ask the user for `horizon` and `lookback` in one short question when they have not said - a report whose sources predate the last move is worse than none. `strike_review_trading_style` behaves the same way for the account's own history, and is advisory: its suggestions never become trades except through the lifecycle.

Strike lists equities and commodities, so scheduled earnings, guidance, index events and ex-dates matter here. Put dated events in `/workspace/trading-desk/research/calendar.md` with a source link and a UTC time. A perp on an equity is not the equity: it pays no dividend and can trade when the underlying market is shut. Say which you are describing.

## 5. Bodega prediction markets

```bash
call_mcp bodega_list_markets '{"status":"open","sort":"volume","limit":25}'
```

A Cardano prediction-market protocol; each market is a yes/no question with Yes/No prices as 0-1 implied probabilities. Read-only, and **the desk does not trade it** - there is no execution path and none is improvised.

Its use is a second, market-priced read on a question the desk cares about. Quote the implied probability with the market's depth beside it: 0.78 on a market with 400 ADA of volume is an opinion, not a price. `open` and `closed` are decided by the deadline, not the upstream `status` field. Treat a Bodega price as **reported** evidence, never verified fact.

## 6. Discord alerts

```bash
call_mcp discord_send_message '{"content":"SG-20260910-01 filled: long 4800 ADA-USD @ 0.2101. Stop resting at 0.1995.","connection":"Trade Alerts"}'
```

This leaves the conversation, so: only when the user has asked for alerts, to a webhook they configured; never a credential, an account identifier or a balance they have not agreed to have leave the chat; under 2000 characters.

An alert is a notification, not an instruction and not an approval. Nothing posted to Discord can authorise a trade, and no reply there reaches the desk.

## 7. Evidence tiers

Label every claim: **verified** (you read it at the primary source and linked it), **reported** (a credible outlet says so; a Bodega market prices it), **claimed** (someone on social media says so), **inferred** (your reasoning). Never promote a claim up a tier without new evidence. Missing information is `unknown`, not "probably fine".
