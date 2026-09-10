---
name: desk-signal-scan
description: How the desk finds and times opportunities without being asked - the Market Analyst's universe scan across every trading market, the Research Analyst's T-minus catalyst alerts, the Strategist's live rule monitors, the shared signals file they all write to, the four clocks that decide whether a fired rule is actionable now, and how a signal becomes a proposal or expires. Use when setting up routines, when the user asks what the desk is watching, or when a signal needs to be turned into a ticket.
license: MIT
metadata:
  version: 1.0.0
  author: Mendurim
  category: desk
---

# Signal scan

The desk's earlier versions waited for the user to bring an idea. This skill is how the desk brings the idea to the user, at the bar it matters, without any Bot forecasting anything. Three producers write to one file; the Desk Lead reads it; the four clocks decide the tier.

## The signals file

`/workspace/trading-desk/signals/YYYY-MM-DD.md`, UTC, append-only, one line block per item. Chat is for exceptions; this file is the steady flow. Every block carries a kind, a UTC time, a source, the numbers and their arithmetic, and a `next:` owner.

| Kind | Producer | When |
| --- | --- | --- |
| `SCAN` | Market Analyst | on the scan routine, hourly by default |
| `CATALYST` | Research Analyst | T-72h, T-24h, T-1h before each calendar entry on a held, SA-covered or watched market |
| `RULE FIRED` | Strategist | on the closed bar where a frozen rule's condition is true |
| `RULE STALE` | Strategist | one bar after a fire with no fill |
| `WATCH COULD NOT TELL` | any watch owner | when a feed or read fails past its staleness bound |

Nothing in this file is a recommendation. A `RULE FIRED` block is a fact about a frozen rule plus that rule's measured record.

## The universe scan

Owner: Market Analyst. Inputs, one read per endpoint per market per run, against the cached `data/exchangeInfo.json` list of `status: trading` markets:

```
/v2/ticker/24hr           price, change, volume
/v2/premiumIndex          funding rate, next funding time
/v2/openInterest          OI in base units (notional at mark)
/v2/klines?interval=1h    480 bars for ATR20 (daily) via aggregation, or interval=1d 30 bars
/v2/depth?limit=1000      only for the top ten by the first three metrics, to save weight
```

Per market, four metrics with defaults the user may tighten in `desk.md`:

| Metric | Computation | Flag when |
| --- | --- | --- |
| funding extreme | percentile of current hourly rate within the market's own 30-day history (`data/funding/<symbol>.csv`, appended each run) | ≥ 95th or ≤ 5th |
| positioning | 24h OI change vs 24h price change | OI moves ≥ 8% with price inside ±1%, or OI and price diverge by sign with OI ≥ 10% |
| range expansion | today's range / ATR20 | ≥ 2.0 |
| depth shift | size within 10 bps vs its 7-day median (`data/depth/<symbol>.csv`) | ≤ 0.6× or ≥ 1.8× |

Rank by count of flags, then by the largest percentile deviation. Append the top three to five. A run with nothing flagged appends `SCAN | <time> | <n> markets | nothing flagged` so silence is never ambiguous. Markets whose reads failed are listed under `unavailable`, never dropped.

The scan is descriptive. It never uses the words buy, sell, long, short, bullish or bearish. Its value is that a frozen rule's threshold is usually one of these metrics, so the Strategist's monitors and the scan agree on what "extreme" means.

## T-minus alerts

Owner: Research Analyst. The calendar (`research/calendar.md`) is refreshed daily. For every entry on a market the desk holds, has a standing approval on, or has on the watch tier, three alerts land in `signals/`:

- **T-72h** — the event, source, exposure. Gives the Strategist time to decide whether the rule should be paused through the event.
- **T-24h** — same, plus any change to the event since T-72h.
- **T-1h** — same, plus the Market Analyst's liquidity clock. This line is what keeps a fired rule off the `now` tier; a missed T-1h is an incident against the routine, not a shrug.

## Live rule monitors

Owner: Strategist. One monitor per frozen rule, under `watch/rule-<name>/`, evaluating on the rule's own timeframe at bar close. Three states, always logged:

```
2026-09-12T12:00:41Z funding-fade-v1@3 ETH-USD  fired      funding8h=+0.0088 pct30d=97 close=2431 sma20=2458
2026-09-12T12:00:41Z funding-fade-v1@3 BTC-USD  not_fired  funding8h=+0.0031 pct30d=71
2026-09-12T12:00:44Z funding-fade-v1@3 SOL-USD  could_not_tell  klines returned 19/20 bars; last bar age 3h > bound 1h
```

On `fired`: append `RULE FIRED` to `signals/`, open `proposals/SG-YYYYMMDD-NN.md` with `signal: <rule>@<version>` and `fired_at: <UTC>`, and hand to the Risk Manager (SA-covered) or the Desk Lead (board). On `could_not_tell` for more than two bars: suspend every SA on the rule and post to the floor; a monitor that cannot see is not a monitor.

Signal decay: a fire unfilled after one bar of its timeframe is `RULE STALE`; the proposal is closed `expired unfilled`, any resting entry is cancelled under Tier 0, and the Trade Reviewer journals it. Stale entries are the leading cause of trades the backtest never modelled.

## The four clocks

The Desk Lead sorts `signals/` into tiers. An item is **now** only when all four clocks pass; the ticket carries all four readings.

| Clock | Owner | Passes when |
| --- | --- | --- |
| rule | Strategist | a frozen rule fired on the current bar (not a prior one) |
| catalyst | Research Analyst | no calendar entry on the market inside the next 60 minutes, or the T-1h line names the event as the thesis of a rule built for it |
| liquidity | Market Analyst | depth within 10 bps on the relevant side ≥ 5× the ticket notional, spread ≤ its 7-day median, book reached 25 bps |
| funding | Market Analyst | more than 10 minutes to the next hourly charge if the position would pay it, or the rule explicitly earns it |

Fail one and the item is **soon** (rule near trigger or catalyst inside 24h) or **watch** (scan anomaly with no rule). The Risk Manager rejects any `now` ticket whose liquidity clock reads `thin` or `unavailable`.

## From signal to proposal

```
signals/ line  ->  proposals/SG-… (signal:, fired_at:, clocks: in header)  ->  RISK PASS with `fields` (signal, fired_at, sa copied in; expiry = one bar)
              ->  approval: SA-NN (Tier 1) or user line by id (Tier 2)
              ->  Execution preview, one send, read-back  ->  journal (signal: carried through)
```

The `signal:` field never leaves the record. It is what lets the Trade Reviewer report expectancy per rule and per SA, which is the only way the desk learns which signals to keep running.

## Routines and weight

- Scan hourly at :05; rule monitors at bar close plus 30 s; calendar refresh daily at a quiet hour; stagger so no two routines read the same endpoint in the same minute.
- Weight budget is 2400 per minute. A full scan is under 150 reads. Depth for ten markets adds ten. There is no reason for any desk to be rate-limited, and a rate-limited desk is a blind one.
- Every routine writes a heartbeat to its `watch/<name>/log` and is listed in `desk.md`. One nobody remembers starting is one nobody notices stopping.

## Never

- Never phrase a scan line or a fire as advice. The permitted prediction on this desk is a frozen rule's measured record, stated with its trial count.
- Never let a stale fire become a ticket. One bar, then it is gone.
- Never let a missed read pass as "not fired".
- Never run a rule live that is not frozen, hashed and backtested with the result reported.
- Never let the scan or a monitor touch the write path. They read, compute, append and alert. The lifecycle does the rest.
