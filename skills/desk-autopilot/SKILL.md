---
name: desk-autopilot
description: Running the desk's runbooks on a clock instead of a prompt - scripts/autopilot.py, which evaluates a frozen rule at bar close, checks the four clocks, sizes on a stressed stop, writes the proposal and its RISK PASS block, and asks the signer for exactly one bracketed order. Covers the two-OS-user split that makes unattended trading a boundary rather than a claim, the rule file format, the cron and sudoers layout, the preflight that predicts every refusal, and the staged rollout from rehearsal to testnet to a small live standing approval. Use when the user asks to let the desk run unattended, when setting up the autopilot, or when it holds or refuses.
license: MIT
metadata:
  version: 1.0.0
  author: Mendurim
  category: desk
---

# Autopilot

`desk-signal-scan` and `desk-standing-approvals` describe a desk that scans hourly, fires on a frozen rule at 03:00 and sends inside signed bounds. They are written for agents. `scripts/autopilot.py` is the same two runbooks with the language model taken out of the path.

That is the whole point. A Bot deciding at 03:00 whether a rule fired is a Bot writing its own evidence. A script evaluating a frozen rule on closed bars produces the same answer twice, and the answer can be checked afterwards against the bars.

```bash
python3 scripts/autopilot.py scan                                  # hourly
python3 scripts/autopilot.py monitor --rule strategies/r/rule.json # at bar close
python3 scripts/autopilot.py sweep                                 # Tier 0 housekeeping
python3 scripts/autopilot.py monitor --rule … --dry                # rehearsal, sends nothing
```

## The prerequisite that is not negotiable

Tier 1 refuses unless `STRIKEGROK_STATE_TRUSTED=1`, and that flag is the operator asserting the signer runs as its own OS user. **On a shared Grok workspace the Bots and the signer are the same user, so the assertion is false and autopilot must not be run there unattended.** Setting the flag to make standing approvals work hands the slot, replay and daily-loss bookkeeping back to the thing it is meant to bound.

Unattended trading therefore needs your own machine or VPS, and two accounts:

| User | Owns | May |
| --- | --- | --- |
| `strike-signer` | the API wallet, `desk/policy-state/`, `policy.log` | run `strike_request.py`; nothing else runs as this user |
| `botuser` | the agents, `autopilot.py`, the desk directory | read everything, write proposals, signals, journals and **suspensions**; sign nothing |

`botuser` reaches the exchange only through one sudoers line (`template/autopilot.sudoers`), and `desk_policy.py` runs on the signer's side of it. Everything else follows from that: a Bot can suspend a standing approval and cannot lift one; it can write a proposal and cannot approve it; it can delete a state file and gain nothing, because the layer asks the venue.

Confirm before going further:

```bash
sudo -u strike-signer python3 scripts/desk_policy.py verify    # mode: unattended
```

## Setting it up

1. `SETUP.md` steps 1-6 on the VPS, plus `desk-signal-scan` and `desk-standing-approvals`.
2. Create both users. `chown -R strike-signer` the state directory; `chmod 700` it. The desk directory is `botuser`'s.
3. Put the API wallet in `~strike-signer/.strikegrok/api-wallet.env`, `chmod 600`. It is never in a Bot's environment, a command line or a file `botuser` can read.
4. Install `template/autopilot.sudoers` with `visudo -f /etc/sudoers.d/strikegrok`.
5. Install both crontabs from `template/autopilot.crontab`, one per user.
6. Register a **testnet** API wallet first. `--network testnet` is autopilot's default; mainnet is something a rule earns.

## The frozen rule

A rule is a JSON file next to its `RULES.md`, and the register is signed against the sha256 of that `RULES.md`. Every key is known and an unknown key is an error: a rule the script half-understands is a rule nobody backtested. `template/rules/funding-fade-v1.json` is a worked example and **not a recommendation** - it has no backtest behind it here.

| Key | Meaning |
| --- | --- |
| `name`, `version` | become `signal: <name>@<version>`, which must name the register's `rule` and `rule_version` |
| `markets` | the symbols this rule watches; the SA's `markets` bound them again |
| `timeframe_seconds`, `klines_interval` | the bar. `timeframe_seconds` must equal the SA's `bar_seconds` |
| `condition` | `funding_pct30d: [lo, hi]`, `price_vs_sma20: above\|below`, `range_expansion_atr20: [lo, hi]` |
| `direction` | `fade`, `follow`, `long`, `short`. A fade takes the other side of whoever is paying |
| `entry` | `market` only. A resting limit entry fills when the signal may no longer be true |
| `stop_atr_multiple`, `take_profit_atr_multiple` | the protective legs, in ATR of the rule's own timeframe |
| `slippage_bps`, `fee_bps_round_turn` | what the stressed stop assumes it pays |
| `max_spread_bps` | the liquidity clock's ceiling, tightened further by the 7-day median |
| `funding_position` | `pays`, `earns` or `either`; see the funding clock |
| `sl_order_type`, `tp_order_type` | default `stop_market` / `take_profit_market` - market on trigger, checked against the market's own `orderType` list |

**The percentile needs history.** `funding_pct30d` ranks the current rate inside `data/funding/<symbol>.csv`, which the hourly scan appends to. A rule with a 30-day percentile cannot fire until the scan has been running for ten days (240 samples, `min_funding_samples`). That is not a bug to route around: it is the rule's own condition being measurable.

**There is no backfill, and there cannot be an honest one.** Strike publishes no historical funding series: `/v2/klines` offers `last`, `mark` and `index` and no premium, there is no `fundingRate` history endpoint, and the account-scoped `/v2/history/funding` is one account's charges on markets it happened to hold. Funding cannot be reconstructed from the premium either - the published rate does not follow from the instantaneous premium, and the `averagePremiumIndex` that drives it is accumulated inside the interval and never exposed historically. A reconstructed series would be a guess in the shape of an observation, and the percentile would be wrong without anyone being able to see that it was.

So the history is collected, not recovered:

```bash
python3 scripts/funding_collect.py --desk /workspace/trading-desk --rule rules/R.json
python3 scripts/funding_collect.py --desk /workspace/trading-desk --rule rules/R.json --status
```

`--status` reports samples against the threshold and when the rule becomes measurable. The collector keeps **one sample per funding interval**, so running it more often than hourly cannot inflate the count. `autopilot.py scan` does not deduplicate that way - it appends a sample per run - so a scan on a tighter-than-hourly schedule will fill the window with less than the days it claims. Run the scan hourly, or let the collector own the funding series.

## The four clocks, in code

An item is actionable only when all four pass, and the ticket carries all four readings.

| Clock | Passes when |
| --- | --- |
| rule | the condition is true on the **last closed bar**, and the fire is younger than one bar or one hour, whichever is tighter |
| liquidity | depth within 10 bps **on the side the entry takes** is at least 5x the ticket notional, the spread is inside the rule's ceiling *and* its own 7-day median, and the book reaches 25 bps |
| funding | the position receives the next charge, or the rule earns the right to pay it, or the charge is more than ten minutes away |
| catalyst | `desk/blackouts.json` has no window in force on this market and none opening inside the hour |

A short is measured against the **bids**, because that is the side a market sell takes. Measuring the other one passes a book that cannot fill the ticket at all.

## Sizing

The Risk Manager's arithmetic, written out on every ticket:

```
risk        equity x the tighter of SA risk_per_trade and the 0.5% ceiling
stop        entry +/- stop_atr_multiple x ATR, rounded onto the tick AWAY from the entry
stressed    stop +/- slippage_bps of entry
per unit    |entry - stressed stop| + (entry + stressed stop) x half the round-turn fee
size        risk / per unit, rounded DOWN onto the step
```

The step comes from `MARKET_LOT_SIZE` where the venue publishes one, because a market entry is bounded by that filter and not by `LOT_SIZE`. Sizing on the stressed stop is why the nominal loss at the stop is always a little under the stated `risk_usd`; the policy layer refuses a ticket where it is above.

## What it writes, and what it cannot

| Path | What |
| --- | --- |
| `signals/YYYY-MM-DD.md` | `SCAN`, `RULE FIRED`, `RULE STALE`, `WATCH COULD NOT TELL`, append-only |
| `proposals/SG-…md` | the ticket and its `RISK \| … \| PASS` block |
| `proposals/SG-…-entry.json` | the exact request body. The file **is** the preview: the bytes sent are these bytes |
| `journal/YYYY-MM-DD.md` | every send, hold, defer, cancel and reconciliation |
| `journal/incidents/open/` | refusals and unknown results |
| `watch/rule-<name>/log` | `fired`, `not_fired`, `could_not_tell` with the arithmetic, every bar |
| `watch/autopilot/state.json` | fired bars, the pacing mark, open tickets. Bot-owned, and therefore never a control |
| `desk/standing-approvals.suspended.json` | suspensions, which only ever remove permission |

It cannot write `desk/policy-state/`, the register, or an approval token. The test suite asserts it never even creates the state directory.

## Exit codes

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | done, or nothing to do | nothing |
| 1 | an operating error - a bad rule file, an unreadable desk | fix it; nothing was sent |
| 2 | **held**: a clock failed, a bound refused, a read was blind | read the journal line. This is the normal quiet outcome |
| 3 | **refused** by the policy layer | an incident is filed and nothing is retried |

A refusal is a defect in autopilot's preflight, not an obstacle. The preflight checks every bound the gate checks - incidents, suspensions, the rule hash, notional, risk, pace, the book, the SA's occupied markets, the venue's own filters - so a refusal means the two disagree. The refusal names the bound; that name is authoritative. Fix the preflight, then clear the incident with a re-signed register.

## Earning the standing approval

The SA is earned in this order, and skipping a step is how a backtest becomes a loss.

1. Write the rule. Freeze `RULES.md`, record its sha256, backtest per `desk-strategy-lab` and report trials, holdout and the 5th percentile.
2. Rehearse: `monitor --rule … --dry` on testnet. It writes the proposal, the PASS block and the request body and stops. Check the body against the ticket by hand, then run `desk_policy.py explain <body>.json --path /v2/order/strategy --equity <n>` - that is the real gate answering.
3. Forward test at **Tier 2**, minimum size, every ticket signed by you, until the Trade Reviewer has 20 closed trades with live expectancy above the backtest's 5th percentile.
4. Sign the register on your own machine. First bounds are small: 0.25% risk, one or two markets, `max_open` 1 or 2, thirty days. `template/standing-approvals.example.json` is the shape.
5. Run `monitor --dry` once more against the signed register, then remove `--dry`.
6. Widen only on the Reviewer's record, in writing, dated, through the same route.

Leverage is set **out of band**, once per market, before the SA goes live. Autopilot never changes it: a leverage change is a gated write like any other, and one sent inside a minute of an entry would hit the 60-second pace floor and refuse the entry.

## How it stops

| Condition | What stops |
| --- | --- |
| daily loss against the day's opening equity | opening exposure, at the gate. Autopilot holds before it |
| three consecutive losses on the market | the SA, suspended by the policy layer from live fills |
| `RULES.md` hash changed | the SA. Autopilot holds first rather than spending the approval on a changed rule |
| an open incident | every opening order, until a re-signed register clears it |
| a monitor blind for two bars | the SA, suspended by autopilot itself |
| the equity snapshot older than ten minutes | sizing, so nothing opens |
| the register expired, or its signature invalid | Tier 1 |

Exits and protection keep working throughout: reduce-only orders and scoped cancels are Tier 0 and need no approval. A desk that cannot open is not a desk that cannot close.

## Never

- Never run autopilot unattended where the Bots and the signer share an OS user.
- Never set `STRIKEGROK_STATE_TRUSTED=1` to make a refusal go away.
- Never retry a refusal, and never edit `desk_policy.py` to get a trade through.
- Never widen a bound in the rule file to make a held signal fire. The hold is the desk working.
- Never run a rule live that is not frozen, hashed, backtested and forward tested.
- Never let a `could_not_tell` pass as `not_fired`, in the script or in a report.
- Never treat the example rule as a strategy. It is a format.
