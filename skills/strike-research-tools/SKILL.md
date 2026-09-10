---
name: strike-research-tools
description: The crowdtime MCP's research and notification tools - crypto news and sentiment research, equity news research, dividend research, Bodega prediction markets on Cardano, and Discord alerts to the user's own channel. Each research tool returns an instruction playbook the Bot must then execute with its own web search, not a finished report. Use for catalyst work on Strike's crypto and equity markets, and for pushing alerts outside the chat. Read-only apart from the Discord post.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs (HyperGrok), ported for Strike Finance
  category: strike
  network-default: mainnet
---

# Strike research tools

Strike lists crypto, equities, commodities and indices, so the Research Analyst's remit is wider here than on a crypto-only venue: earnings, dividends and macro prints move markets this desk can trade. These MCP tools support that work.

## 1. The thing to understand first

**`crowdtrendz_*` tools return instructions, not findings.**

Calling one produces no research. It hands back a playbook: what to look for, how to scope it, how to format the report. The Bot must then go and do the web research itself and render the report under those rules.

So:

- Never present the returned playbook as a report.
- Never say "research shows" on the strength of having called the tool.
- The citations in your report are pages **you** read, with links and UTC times. The tool provides none.
- If web search is unavailable, the research is `unavailable`. Say so; do not write the report from memory.

`strike_review_trading_style` (in `strike-account`) works the same way for the account's own trading history.

## 2. Crypto news and sentiment

```bash
call_mcp crowdtrendz_crypto_news_research '{"symbol":"ADA","horizon":"next 7 days","lookback":"last 14 days"}'
```

`symbol` is a token symbol or name, not a Strike perp symbol - `ADA`, not `ADA-PERP`.

`horizon` is how far forward the analysis looks; `lookback` caps how old a source may be. Both are optional but the tool asks you to establish them first: **ask the user one short question** when they have not said, unless they have signalled "just run it". A report whose sources predate the last move is worse than no report.

## 3. Equity news and dividends

```bash
call_mcp crowdtrendz_stock_news_research '{"symbol":"NVDA","horizon":"next earnings"}'
call_mcp crowdtrendz_stock_dividend_research '{"exchange":"NASDAQ","symbol":"MU","horizon":"next 30 days"}'
```

Strike's equity perps - `NVDA`, `TSLA`, `MU`, `SNDK`, `SKHYNIX`, `COIN`, `GOOGL`, `CRCL`, `SPCX` - carry event risk that crypto does not: scheduled earnings, guidance, index events, and for the cash names, dividends and ex-dates. Put dated events in `/workspace/trading-desk/research/calendar.md` with a source link and a UTC time.

`exchange` is required on the dividend tool and anchors the scope (`NASDAQ`, `NYSE`, `LSE`, `all-US`, `S&P 500`, ...). `event_types` narrows to declarations, changes, or upcoming dates.

A perp on an equity is not the equity: it does not pay a dividend, and it can trade when the underlying market is shut. Say which you are describing. An ex-date matters here because of what it does to the underlying's price, not because the position receives anything.

## 4. Bodega prediction markets

```bash
call_mcp bodega_list_markets '{"status":"open","sort":"volume","limit":25}'
```

Bodega is a Cardano prediction-market protocol; each market is a yes/no question with Yes/No share prices. Read-only, and **the desk does not trade it** - there is no execution tool for Bodega and none is improvised.

What it is good for is a second, market-priced read on a question the desk cares about. Yes/No prices come back as 0-1 implied probabilities. Quote them as implied probability with the market's depth beside them: a 0.78 on a market with 400 ADA of volume is an opinion, not a price. `open` and `closed` are decided by the market's deadline, not by its upstream `status` field.

Treat a Bodega price as **reported** evidence - what a market thinks - never as verified fact.

## 5. Discord alerts

```bash
call_mcp discord_send_message '{"content":"SG-20260910-01 filled: long 4800 ADA-PERP @ 0.2101. Stop resting at 0.1995.","connection":"Trade Alerts"}'
```

This leaves the conversation, so it follows the desk's rules for anything outbound:

- Only when the user has asked for alerts, to a webhook they configured in crowdtime API Settings.
- Never the bearer token, an account identifier, or a balance the user has not agreed to have leave the chat.
- Under 2000 characters - Discord rejects more.
- `connection` picks the channel when the user has several; `username` overrides the display name.
- An alert is a notification, not an instruction and not an approval. Nothing the desk posts to Discord can authorise a trade, and no reply there reaches the desk.

## 6. Evidence tiers

Same standard as everywhere else on the floor. Label every claim:

- **verified** - you read it at the primary source and linked it.
- **reported** - a credible outlet says so; a Bodega market prices it.
- **claimed** - someone on social media says so.
- **inferred** - your reasoning from the above.

Never promote a claim up a tier without new evidence. Missing information is `unknown`, not "probably fine".
