---
name: desk-strategy-lab
description: How the Strategist turns the user's own trading idea into explicit rules, tests it honestly on Strike candle and funding history without fooling anybody, and runs it forward at minimum size through the desk lifecycle. Method only - the desk ships no strategies and makes no claims about returns. Use when the user wants to design, test, compare or forward-test an idea.
license: MIT
metadata:
  version: "3.0.0"
  author: Mendurim
  category: desk
---

# Strategy lab

The lab exists so the user can find out whether an idea holds up before it costs them anything. The Strategist brings method, code and scepticism. The ideas are the user's. Nothing here suggests what to trade.

## Where the work lives

```
/workspace/trading-desk/strategies/<name>/
  RULES.md                   the rules in words, agreed before any code is written
  data.md                    the exact requests used: symbol, interval, range, when fetched
  backtest.py                one readable file
  runs/YYYY-MM-DD-HHMM.md    one file per run: parameters, results, caveats
  POSTMORTEM.md              why it was abandoned, if it was
/workspace/trading-desk/data/<symbol>-<interval>-<range>.csv
```

## 1. Rules before code

Interview the user until nothing is ambiguous, then write `RULES.md`:

```markdown
# funding-fade-v1

- universe: BTC-USD, ETH-USD, SOL-USD
- data: 4h klines (close), hourly funding
- entry: when mean hourly funding over the last 8 hours exceeds +0.005%/h, sell at the next 4h open
- exit: funding back to zero or below, or 72 hours elapsed, or the stop
- stop: 2 x 24h ATR above entry, ATR from the prior 24 bars
- sizing: risk 0.5% of equity to the stop; the desk's own limits apply on top
- one position per market, no adds
- abandon if: no edge after costs across 12 months and 3 markets
```

If a rule still needs "it depends", it is not a rule. Do not start coding.

### Ideas that arrive from elsewhere

Most ideas turn up as a claim rather than a rule: a repository of indicator settings, a thread with a chart, a video, a screenshot of an equity curve. A claim is a candidate for `RULES.md`. It is never a result this desk inherits — published win rates, backtests and "this called the top" images carry no validation across.

Before an imported claim becomes testable, freeze all of this in writing:

1. Instrument and venue. A Strike perp does not inherit a spot or index claim.
2. Timeframe, when a bar closes, and on whose clock.
3. The price field and the exact formula, including how early values are seeded.
4. The transition rule and every case where it does nothing — not only the entries.
5. The next observable entry, when a signal expires, the mandatory stop, the target, the time exit.
6. Costs, funding, spread, slippage, and what happens on a rejection or a partial fill.
7. **The whole family the claim came from** — every parameter set, market and timeframe tried before this one was shown to you. That count feeds the multiplicity note below.
8. Chronological in-sample and out-of-sample boundaries, plus an untouched holdout.
9. How long it will be run forward, agreed before the first run.

Whatever the source does not supply is *missing*, not implied. If it cannot supply enough for a complete rule, say so and stop: the claim stays an idea, and the desk does not paper over the gaps with plausible defaults. Record the source — exact URL or commit, and the date read — in `RULES.md`, so a later reader can tell what was claimed from what the desk had to invent.

## 2. Data

Candles come from `/v2/klines` and funding from `/v2/premiumIndex` for the current rate, or `GET /v2/history/funding` for what an account was actually charged. Mark-price and index-price kline series are also available; say which one a dataset holds, because a backtest on last-trade prices and one on mark prices are different backtests.

Page with the endpoint's `limit` and time range, save to CSV under `data/`, and record every request in `data.md`. Before using a file, check it for gaps and duplicated timestamps. Newer markets have shorter histories, and a universe is only as long as its shortest member.

## 3. Backtesting without fooling yourself

One readable Python file the user can follow beats a framework they cannot.

**No look-ahead.** A signal at bar `t` uses data up to and including `t`. Execution happens at the `t+1` open, or at the `t` close if you say so explicitly.

**Real costs.** Fees on both legs at the rate recorded in `risk-limits.md`, funding for every hour the position is held, and slippage per side — a few bps by default, or the Market Analyst's depth read for the intended size. An edge that disappears when costs double was a cost assumption, not an edge.

**Out-of-sample, and spending it.** Hold back the most recent 15–25% until the rules and parameters are frozen, then report in-sample and out-of-sample separately. Note carefully: **looking at the holdout spends it.** Once you have seen that result, the data is no longer out of sample whatever it said. A rule that fails, gets adjusted, and is re-run on the same window has no honest out-of-sample evidence left — the failed run was discovery. Record in every run file how many times the holdout has been read. More than once and the run file says the result is in-sample only.

**Multiplicity.** Record every parameter, how it was chosen, and every variant run — including the failures. The number that matters is how many distinct rules, parameter sets, markets and timeframes were tried in total before this one was reported, and that count includes whatever the idea's original source tried. Search a fixed history hard enough and something will look profitable by luck. State the trial count beside the result. Above a handful, treat a marginal edge as unproven rather than small, and expect the best-looking variant to be the luckiest rather than the best.

**Position accounting.** Sizes from the sizing rule and the stop; results in R and USD; an equity curve; drawdown taken from that curve.

Each run reports:

```
run 2026-09-10-1540 | funding-fade-v1 | IS 2025-01-01..2026-07-31 | OOS 2026-08-01..2026-08-31
trades 58 IS / 6 OOS | win 41% / 50% | avg win 1.9R / 1.6R | avg loss 1.0R / 1.0R
expectancy 0.19R / 0.30R | max DD 9.4% / 2.1% | costs 31% of gross PnL
expectancy 5th pct 0.02R (block bootstrap, 2000 resamples, 10-trade blocks) | profit factor 1.31
trials 9 (3 stop multipliers x 3 markets) | holdout reads 1
verdict: WEAK - forward-test before believing it
caveats: stop multiplier chosen from {1.5, 2, 3}; 5 bps slippage assumed; SOL history shortest
```

No annualised headline. The distribution and the costs are the result.

**Report an interval, not a point.** A mean expectancy over one path is a single draw, and 58 autocorrelated trades is a small sample. Resample the trade sequence in contiguous blocks — a few thousand resamples, blocks long enough to preserve streaks — and report the 5th percentile of mean expectancy next to the mean. If that lower bound sits at or below zero, the honest verdict is "no demonstrated edge", not "a small edge", no matter how the mean looks.

Every run gets a verdict of PASS, WEAK or REJECTED, and rejected runs are kept. A strategy the lab killed is the lab working.

## 4. Checks before believing anything

- Flip the entry condition. If the mirror also "works", you are measuring drift, not edge.
- Shuffle entry dates within the sample. How often does random timing beat it?
- Drop the best two trades. Does anything survive?
- Fewer than 30 trades in sample: report "not enough evidence", not a result.
- Does it all come from one market, or one month?
- Double the assumed slippage and fees.

Write down what you checked in the run file, including the checks that passed.

## 5. Running it forward

Strike's testnet cannot paper-trade anything: four markets, empty books, no fills. So forward testing happens on the live account at **minimum size** — `MIN_NOTIONAL` is $10 — and through the ordinary lifecycle, not around it.

The Strategist runs the rules on live data and posts each signal as a **proposal** (`SG-...`), citing the rules file as the idea. The Risk Manager sizes it, the user approves it, the Execution Trader sends it, the Trade Reviewer reviews it. The Strategist never sends, and never asks another Bot to skip a stage because the rules said so.

Agree the number of forward trades with the user before starting — twenty is a common choice — and be explicit that minimum size buys a test of the *rules and the plumbing*, not of the returns at real size, because slippage does not scale down linearly. Afterwards, put the Trade Reviewer's numbers and the backtest side by side.

## 6. Abandoning cleanly

When it does not hold up: `POSTMORTEM.md` with the data, the runs and one paragraph on why. Tell the user plainly. That is a good outcome, and it is cheaper than the alternative.

## Never

- Never propose a strategy the user did not bring. Offering test structures is method; offering "what works" is not.
- Never quote a backtest result without trade count, drawdown, costs, the out-of-sample split and the trial count.
- Never carry an imported claim's published results in as evidence. The desk's evidence is what the desk ran.
- Never send an order.
- Never let a live signal loop become unattended execution.
