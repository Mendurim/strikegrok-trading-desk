---
name: desk-monitoring
description: How the desk keeps an eye on markets and the account between trades - scheduled routines, the daily desk brief, book checks, and watches built on polling or the WebSocket feeds - including what a watch is allowed to do and why silence from one is never an all-clear. Use when the user asks for briefings, alerts, "watch X", or scheduled checks.
license: MIT
metadata:
  version: "3.0.0"
  author: Mendurim
  category: desk
---

# Monitoring, briefs and watches

Everything here reads. A watch may fetch, compute, save and alert; it may not touch the write path. When a condition implies an action — "the stop went, get back in" — the alert opens a proposal and the lifecycle takes it from there.

## Routines

Each Bot can hold scheduled or triggered routines. Ask the owning Bot for one and specify four things: when it runs and in which time zone, what it reads, what it should produce, and where its authority stops. Keep them few and specific. The platform retains only recent run records, so anything meant to last should also write into `/workspace/trading-desk`.

A reasonable starting set — the user picks, none runs by default:

| Routine | Owner | When | Produces |
| --- | --- | --- | --- |
| Desk brief | Desk Lead | daily, at a time the user picks | `briefs/YYYY-MM-DD-desk.md` plus a short post |
| Book check | Risk Manager | every 4–8 hours while anything is open | an alert only when something moved or a limit is close |
| Funding snapshot | Market Analyst | daily, before the user's usual hours | funding, open interest and volume across the allowed markets |
| Calendar refresh | Research Analyst | weekly | `research/calendar.md` |
| Weekly review | Trade Reviewer | weekly | the review, by DM |

## The desk brief

The Desk Lead assembles it from the specialists, or produces it alone if it is the only Bot awake.

```
DESK BRIEF | 2026-09-11 07:00 UTC
markets (Market Analyst, /v2/ticker/24hr + /v2/premiumIndex 06:58 UTC)
  BTC-USD  77,120  +0.8% 24h  funding +0.0079%/h  OI $682K
  ETH-USD   2,431  +0.2%      funding +0.0024%/h  OI $132K
  ADA-USD  0.2116  -1.1%      funding -0.0004%/h  OI $249K
book (Risk Manager, GET /v2/account + /v2/positions 06:59 UTC)
  1 position: ADA-USD long 4800 @ 0.2100, uPnL +$7.68, margin ratio 4.6%
  stop resting at 0.1995 (status 5, untriggered) | open risk 0.5% | day PnL 0.0%
research (Research Analyst)
  NVDA earnings 2026-09-17 after the close [link]; nothing breaking on held markets
open items
  SG-20260910-01 live; no pending tickets
```

Facts carry sources and times. Interpretation, if there is any, goes on one line that says it is interpretation. No trade suggestions.

## Watches

A watch is a condition plus an alert. Two ways to run one from the desk computer:

**Polling.** A loop in a script under `/workspace/trading-desk/watch/<name>/`, hitting the public Price Service. Mind the weight limit — 2400 per minute — but for a desk, one read every five to fifteen seconds per market is ample.

**Streaming.** Subscribe to `wss://api.strikefinance.org/ws/price` for mark price, depth, trades and klines, or the authenticated user stream for fills, orders and positions. Better when the condition is genuinely sub-minute or the desk needs fills the moment they land. Keep the process supervised and logged. See `strike-websocket`.

Conditions worth running:

| Watch | Read | Tell |
| --- | --- | --- |
| Price crosses a level | `/v2/markPrice`, or klines | user, Desk Lead |
| Funding flips sign or passes a threshold | `/v2/premiumIndex` | user, Desk Lead |
| An order fills or cancels | user stream, `GET /v2/history/fill` | Execution Trader, Trade Reviewer |
| Margin ratio above X, or liquidation closer than Y | `GET /v2/account`, `GET /v2/positions` | user, Risk Manager, Desk Lead |
| A position with no resting stop | `GET /v2/positions` + `GET /v2/openOrders` | Risk Manager, Desk Lead — incident |
| Daily loss stop at 80%, or hit | `GET /v2/account` against start-of-day equity | user, Risk Manager |
| A TWAP still running | `GET /v2/algo/twap/{id}` | Execution Trader |
| The exchange unreachable beyond N minutes | any read failing | Desk Lead |

An alert states what fired, the value against the threshold, the source and UTC time, and the proposal id where there is one. It never carries an instruction to trade.

## Silence is not an all-clear

A watch has three possible outcomes, never two: the condition fired, the condition did not fire, or **the watch could not tell**. A failed request, an empty response, a socket that reconnected across a gap, a candle that never closed, or a value older than the watch's own interval all belong in the third category, and all must alert as such.

A watch that quietly reports "not crossed" over a dead feed is the most dangerous process on the desk, because it is indistinguishable from a calm market right up until it isn't.

So every watch gets a staleness bound — a small multiple of its interval — and checks each result's age against it. When it trips, the alert says which read failed, how old the last good value was, and how long the gap has run. A gap that outlives the bound escalates rather than waiting politely for the feed to return.

## Hygiene

- Every watch logs to `/workspace/trading-desk/watch/<name>/log` with UTC timestamps, and keeps its condition written in plain language beside it.
- A watch that dies quietly is worse than no watch. Whatever owns it checks its heartbeat: uptime, last message, reconnect count, and whether it currently believes itself live.
- Run a few, not many. Each is a process on a shared computer.
- Record running watches in `desk.md`. One nobody remembers starting is one nobody notices stopping.
- When the desk goes flat for the day, stop the watches that only make sense alongside a position.

## Never

- Never let a watch place, change or cancel an order, or touch leverage — not even a protective one. It alerts; the desk acts through a ticket.
- Never alert with a number that has no source.
- Never let a stale or failed read pass as a condition that did not fire.
- Never poll faster than the desk needs. A rate-limited desk is a blind desk.
