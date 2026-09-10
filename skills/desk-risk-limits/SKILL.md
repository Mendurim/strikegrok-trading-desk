---
name: desk-risk-limits
description: How the Risk Manager agrees the desk's limits with the user, sizes each proposed trade from live account state and the market's real constraints, inspects the book, and returns a PASS with exact ticket fields or a REJECT naming the gate that failed. Use when writing or changing limits, sizing any trade, and answering "how does the book look".
license: MIT
metadata:
  version: "3.0.0"
  author: Mendurim
  category: desk
---

# Risk limits and sizing

The user decides the limits once, in writing. The Risk Manager applies them to every ticket using numbers read live. On top of both sit the market's own constraints — maximum leverage, tick and step sizes, minimum notional — which no limits file can relax.

## 0. Ceilings the desk will not cross

The desk carries a handful of its own ceilings. They are not advice, and they are deliberately far looser than any sensible discretionary setting. Their only job is to stop a mistyped, corrupted or over-optimistic limits file from authorising something catastrophic while nobody is watching.

| Ceiling | Value |
| --- | --- |
| risk on one trade | 2% of equity |
| total open risk | 6% of equity |
| leverage on any market | 20x, and never above the market's own maximum |
| daily loss stop | −10% of equity at the start of the day |
| a stop resting on the exchange for every entry | required |
| a standing approval covering anything that can open or grow a position | never |

Exactly one kind of send may sit behind a standing approval: **reduce-only protection** — placing or resizing a stop on a position that has none. It cannot do anything but shrink exposure, and the alternative is a naked position waiting for somebody to read a message. Entries, adds and leverage increases always need approval against a ticket id.

The user's file may only be **tighter** than this table. A value looser than a ceiling is not applied. The Risk Manager returns `gate failed: limits file exceeds desk ceiling <name>`, keeps enforcing the ceiling, and asks the user to edit the file. The desk never edits it, and no Bot raises a ceiling.

## 1. Writing the limits file

Interview the user one question at a time, then write `/workspace/trading-desk/risk-limits.md`. Version it and date every change. Only the user changes it, in chat; the Risk Manager records who asked, when, and why.

```markdown
# Risk limits v1 - 2026-09-10 - set by user

- account: the Strike account the API wallet acts for
- equity basis: equity from GET /v2/account, read live, USD
- risk per trade: 0.5% of equity            # what a hit stop costs
- total open risk: 2% of equity             # summed across open positions
- leverage cap: 3x                          # and never above the market's own maximum
- position count: 3 maximum
- allowed markets: BTC-USD, ETH-USD, ADA-USD, SOL-USD   # each must show status=trading
- stops: required on every entry, resting on the exchange, never "mental"
- daily loss stop: -2% of start-of-day equity, then no new risk until the user resets in writing
- slippage tolerance at send: 10 bps from the ticket price
- correlation: BTC, ETH and SOL count as one cluster; at most 2 positions in a cluster
- standing approvals: none                  # reduce-only protection is the one worth granting
- unprotected position deadline: 15m        # then hand it back to the user to fix in the app
- taker fee assumption: 0.045%              # note the source; correct it against realised fills
- notes:
```

For someone new to perpetuals, 0.25–0.5% per trade and 3x or less is a reasonable place to begin, with a minimum-size first trade in each new market. Strike's testnet has empty books and cannot serve as practice, so the first live position is the first genuine test — keep it small. Record what the user chooses and enforce it. Do not talk them up or down.

## 2. Sizing a trade

You need the entry, the stop, the side, the market, the current limits file, and live state. Anything missing or stale is `gate failed: missing input`. Never fill a gap with a guess.

### 2.1 Read the state, every time

- **Account** — `GET /v2/account` for equity and margin usage, `GET /v2/positions` for size, entry, leverage, margin and liquidation price, `GET /v2/openOrders` for what is already resting including untriggered protection. See `strike-account`.
- **Market constraints** — `/v2/exchangeInfo` for `PRICE_FILTER.tickSize`, `LOT_SIZE.stepSize`, `MIN_NOTIONAL.notional` and `liquidationFee`. See `strike-market-data`.
- **Price and depth** — `/v2/markPrice` for the price liquidation and PnL settle against, and the Market Analyst's depth bands from `/v2/depth?limit=1000`.
- **Day PnL** — start-of-day equity from the journal against equity now.

None of this comes from memory or from another Bot's earlier message.

### 2.2 The arithmetic, shown in full on every PASS

```
risk_usd          = equity x risk_per_trade
stop_distance     = |entry - stop|                          (must be greater than zero)
slip_stop         = assumed slippage when the stop fires, in price units
                    at minimum the current spread; widen it when depth for this size is thin
stop_fill         = stop - slip_stop   (long)  |  stop + slip_stop   (short)
taker_fee         = the rate recorded in risk-limits.md
                    a triggered stop exits at market and pays taker on the way out
                    this is a written assumption until fills exist to check it against; say so in
                    the PASS and correct it later from GET /v2/history/fill and
                    GET /v2/history/transaction (type 3 = fee)
fees_per_unit     = (entry + stop_fill) x taker_fee         (both legs)
stressed_distance = |entry - stop_fill| + fees_per_unit
raw_size          = risk_usd / stressed_distance            (never risk_usd / stop_distance)
size              = round_down(raw_size, stepSize)          (never round up)
notional          = size x entry

check  notional >= MIN_NOTIONAL                             ($10 across Strike's markets)
check  size >= one stepSize                                 (ADA-USD trades in whole tokens,
                                                             which is the commonest cause of this)
lev_cap = min(20x desk ceiling, limits.leverage_cap, the market's own maximum)
margin_needed = notional / requested_leverage               (requested_leverage <= lev_cap)
check  margin_needed <= free_margin x 0.8                   (20% headroom, or tighter if asked)
open_risk_after = sum(stressed risk of open positions) + risk_usd
check  open_risk_after <= equity x total_open_risk
check  open_risk_after <= equity x 6%                       (desk ceiling)
check  risk_usd <= equity x 2%                              (desk ceiling)
check  positions_after <= position count ; cluster within its limit
check  market allowed and status=trading ; stop present ; daily loss stop not hit
```

`R` on the ticket is the nominal `stop_distance` per unit, because that is the unit the user quotes targets in. The **size** comes from `stressed_distance`. The ticket carries both and says which produced what.

**Why size on a stressed stop.** A stop is a trigger. When it fires it becomes a market order and fills wherever the book is — worse than the trigger price, and much worse into thin depth, a gap or a cascade. Both legs pay fees too. Sizing off the nominal distance therefore prices a loss that cannot actually occur, and quietly overspends the risk budget on every single trade, always in the same direction. Sizing off the stressed distance makes the budget mean what it says.

`slip_stop` is an assumption, not a measurement. State the figure and where it came from, and when the depth read is stale or the size is large against the book, widen it rather than narrow it.

Worked through: equity $10,200, a 0.5% budget, ETH-USD long at 3,000 with the stop at 2,900, 3.00 of assumed slippage on the trigger (10 bps of the ticket price) and 0.045% taker on both legs. The stressed distance is 105.65 rather than 100, so the size is 0.4827 ETH rather than 0.51, and the worst case lands on exactly the $51.00 budgeted. Sized naively at 0.51 ETH the same stop costs $53.88 — 0.528% of equity against a 0.5% budget. Small, consistent, and always an overspend.

None of this is a promise. A gap straight through the stop can still cost more. That residual is the user's, and the daily loss stop is what bounds it.

### 2.3 Leverage and notional headroom

Three limits apply and the lowest wins: the desk ceiling, the user's cap, and the market's own maximum. Name the binding one in the PASS.

Strike also caps notional as a function of leverage. `POST /v2/leverage` returns the maximum notional available at the leverage it has just set, and that figure is the ceiling the ticket must fit inside. When a ticket is large relative to the account, set leverage first, read that number back, and size against it rather than assuming the headline leverage holds all the way up.

Liquidation price appears on the position once it exists. `liquidationFee` from `/v2/exchangeInfo` is part of what being liquidated actually costs. Check both afterwards and report the distance from mark to liquidation in price and in percent.

### 2.4 What to return

A PASS uses the block in `agents/risk-manager.md`: inputs with their timestamps, the sizing arithmetic, the binding leverage limit, the book after the trade, the gates, the exact ticket fields, and the next owner. A REJECT uses the same header and one line — `gate failed: <gate>, <the numbers>`. Either goes under `## risk` in the proposal and onto the floor.

## 3. The book check

From `GET /v2/account`, `GET /v2/positions`, `GET /v2/openOrders` and `/v2/markPrice`:

- equity, free margin, margin usage and ratio, and per position the distance from mark to liquidation in price and percent
- each position: symbol, side, size, entry, mark, unrealised PnL, leverage and margin mode, margin used
- risk to stop per position and in total, against the limits
- **protection** — for each position, is a reduce-only trigger resting on the correct side (`sell` protects a long), sized at or above the position? Untriggered triggers report as status **5**, not 2. A check that only counts status 2 will report a protected position as naked. Where there genuinely is none, that is an incident, not a line item
- resting orders that no longer belong to any position
- day PnL against the daily loss stop
- funding paid today, read from `GET /v2/history/funding` — the amount actually charged, not a rate multiplied by an estimate of hours

Timestamp all of it. When the user wants it written down, save it under `/workspace/trading-desk/briefs/YYYY-MM-DD-book.md`.

## 4. Hitting a limit

**Daily loss stop.** Post it once, set `status: no-new-risk` in `desk.md`, and reject new proposals on that gate until the user resets it in writing. Exits and protection still go through.

**An unprotected position.** Tell the Desk Lead and Execution Trader at once. A protective stop ticket jumps the queue. If the user pre-authorised reduce-only protection it goes straight out under that standing approval. If not, the alert carries the exposure and the distance to liquidation, and once the deadline in `desk.md` has passed the desk tells the user to protect or close it themselves in the Strike app. Playbook D in `desk-incident-response`.

**No limits file, or an unversioned one.** The desk is a research desk until one exists.

## Pitfalls

- Sizing from a profit target, or from what the margin happens to allow. The stop sets the size.
- Treating a triggered stop as if it fills at its trigger price. Use the stressed distance.
- Reading a failed, empty or stale account call as a clean book. A read that did not arrive is **unavailable** — not "no positions" and not "no open risk". Reject on missing input rather than sizing against remembered numbers.
- Assuming the headline leverage holds at any size. Check the notional headroom.
- Counting correlated positions as if they were independent bets.
- Taking a plan, a chat message or a screenshot for an open order. Only the exchange record counts.
- Rounding a size up to clear the minimum notional. If the minimum implies more risk than the budget allows, that is a reject.
