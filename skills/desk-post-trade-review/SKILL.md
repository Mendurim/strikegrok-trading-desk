---
name: desk-post-trade-review
description: How the Trade Reviewer keeps the desk journal and reviews trades from the exchange's own record - grading process separately from outcome, measuring what execution actually cost, and drawing one repeatable finding per review - plus the weekly desk review and the incident review. Use after any send, when a trade closes, on the weekly routine, or when the user asks how something went.
license: MIT
metadata:
  version: "3.0.0"
  author: Mendurim
  category: desk
---

# Journal and review

The journal is what the desk remembers. The review is how it improves. Both are built from the exchange record first and the conversation second, and both keep process and outcome strictly apart.

## 1. The journal

One file per active day at `/workspace/trading-desk/journal/YYYY-MM-DD.md`, appended in time order.

```
14:02 UTC  SG-20260910-01  opened    ADA-USD long idea (user); status evidence
14:12 UTC  SG-20260910-01  risk      PASS 4800 ADA, stop 0.1995, 0.5% stressed (limits v3)
14:29 UTC  SG-20260910-01  approval  "approve SG-20260910-01" (user)
14:31 UTC  SG-20260910-01  sent      bracket ADA-USD long 4800 @ 0.2100 GTC, tp 0.2310, sl 0.1995
                                     cloid SG-20260910-01-entry; order id 1839201122; read back status 2
16:05 UTC  SG-20260910-01  fill      4800 @ 0.21003, maker, fee $0.29
09:12 UTC  SG-20260910-01  closed    tp at 0.23100, fee $1.44; flat; sl cancelled 09:13
10:00 UTC  limits                    v3 -> v4: position count 3 -> 4 (user; adding HYPE-USD)
11:40 UTC  incident INC-20260911-01  unknown result on SG-20260911-02; looked up by cloid at 11:41,
                                     absent; corroborated 11:46; replacement approved with a fresh id
18:00 UTC  note                      maker entries at 10 bps filled inside 2h on both attempts this week
```

Append only. A correction is a new line beginning `correction:`, never an edit to an old one. Every line carries a UTC time, and an id wherever one exists. Opinions belong in reviews, not here.

## 2. Reviewing a trade

Triggered when a proposal reaches `closed`, or whenever the user asks. List every input with its timestamp:

- the proposal file — ticket, PASS, approval, execution, reconciliation
- `GET /v2/history/fill` across the window: price, size, fee, side, maker or taker
- `GET /v2/history/order` by symbol and status — 2 open, 3 filled, 4 canceled, **5 untriggered**, 6 rejected, 7 expired — to establish what rested when and what was cancelled
- `GET /v2/closedPositions` for the realised result
- `GET /v2/history/funding` for what holding it actually cost
- where available, the Market Analyst's depth read at send time, to compare expected slippage against realised

Then measure:

| Measure | How |
| --- | --- |
| Entry slippage | (average fill − ticket price) / ticket price, in bps, signed against the trade |
| Exit slippage | the same for the exit, against its ticket or trigger price |
| Fees | fill fees in USD and as bps of notional; note maker against taker |
| Funding | the sum actually charged over the holding period |
| Net result | realised PnL after fees and funding, in USD and in R, with R taken from the ticket |
| Protection | was a reduce-only stop resting for the position's entire life? Any gap, in minutes |
| Lifecycle | every stage present, in order, with timestamps; approval by id; one send; reconciled |
| Holding time | first fill to flat |

Grade two things, separately:

**Process** — clean, minor break, or major break, naming the stage. A major break is any send without a PASS or without approval by id, any period without protection, any resend on an unknown result before looking it up, or any limit breached.

**Outcome** — the result in R and USD, stated without adjectives.

These are independent, and saying so out loud is the point. "Process: clean. Outcome: −0.9R at the stop" is a good trade that lost money. "Process: entry sent before the PASS. Outcome: +2R" is a bad trade that made money, and the review says so plainly.

Then **one finding**: a leak, a control that earned its keep, or a break — chosen because it will recur. One, not a list.

Write it under `## review` in the proposal, send the block from `agents/trade-reviewer.md` by DM to the Desk Lead and the user, and set `status: closed`.

## 3. The weekly review

From the journal, the proposals and the exchange record for the week:

- proposals opened, rejected, voided, executed, closed
- trades closed: count, hit rate, average win and average loss in R, expectancy in R, largest loss, largest equity drawdown
- costs: fees and funding in USD and as a share of gross PnL
- process: breaks by type; incidents and where each stands
- limits: what changed and why
- one pattern worth the user's attention, stated as a fact pattern rather than advice

No recommendations about what to trade. Strategy questions go to the Strategist, sizing questions to the Risk Manager.

## 4. Incident review

For each `INC-YYYYMMDD-NN`: the timeline from the journal and the exchange record, what the desk did, what the controls did, the exposure carried during the incident, the root cause where it is knowable, and one corrective action with an owner and a date. Blameless in tone and exact in fact. Written to `/workspace/trading-desk/journal/incidents/<id>.md` and linked from that day's journal.

## Pitfalls

- Reviewing from memory of the conversation rather than from fills. The chat records what people meant; the fills record what happened.
- Letting the outcome colour the process grade. Grade the process first, then look at the money.
- Counterfactuals. "If we had held" is not evidence of anything.
- Ten findings. Nobody acts on ten.
- Editing an old journal line. Append a correction.
- Counting only status 2 when checking whether protection existed. An untriggered stop is status 5, and missing that turns a protected position into a phantom incident.
