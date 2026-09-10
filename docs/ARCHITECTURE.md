# How the desk works

StrikeGrok runs entirely inside your Grok Bot workspace: your Bots, your shared cloud computer, your conversations. This repository is the blueprint they build from.

## The floor

```
you
 |
 v
Trading Floor (group chat, 6 Bots)                 DM
  Desk Lead ------------------------------------> Trade Reviewer
   |     |     |     |     |                       journal, reviews
   |     |     |     |     +-- Execution Trader ---> Strike signed API  /v2/order... (the one writer)
   |     |     |     +-------- Risk Manager -------> Strike signed API  account reads (live)
   |     |     +-------------- Strategist ---------> Price Service      /v2/klines   (history)
   |     +-------------------- Research Analyst ---> browser, optional research add-on
   +-------------------------- Market Analyst -----> Price Service      /v2, /ws     (markets)

market data: https://api.strikefinance.org/price/v2   public, no credential
execution:   https://api.strikefinance.org/v2         Ed25519 API wallet
             31 markets, one -USD symbol vocabulary on both

computer:  /workspace/strikegrok (this repo)   /workspace/trading-desk (the desk's files)
secret:    STRIKE_API_PUBLIC_KEY, STRIKE_API_PRIVATE_KEY - from Grok Bot's secure secret store
```

The shared Desk Lead starts on the read plane. `strikegrok-bootstrap` installs the pinned release, runs `scripts/opening_bell.py` against the public Price Service, prepares the desk, creates the team when the product allows it, and finishes with `scripts/desk_doctor.py`. None of those first-run paths reads a key or calls a signed endpoint.

**One vocabulary.** Both surfaces use the same `-USD` symbols and all thirty-one quoted markets are tradeable, so there is nothing to translate. A market is still checked for `status: trading` before the desk sizes anything.

An **optional** research add-on (the crowdtime MCP) supplies a liquidity screen, computed indicators, news research and Discord alerts. It is not on the trading path and the desk works fully without it.

Grok Bot facts that shaped this: group chats hold up to six Bots, Bots can create other Bots, all your Bots share one computer, skills are shared across Bots, actions can be put behind approval, and secrets go in through a secure secret card.

## One trade, seven stages

```
idea -> evidence -> risk sign-off -> your approval by ticket id -> one send -> reconciliation -> review
```

The Desk Lead keeps it moving. Analysts bring sourced, timestamped evidence. The Risk Manager sizes from your live account and the exchange's real limits, and issues a ticket. You approve it by id. The Execution Trader posts the exact request as a preview, sends it once, reads the order back by its client order id, and confirms it against the exchange record. The Trade Reviewer journals it and, when it closes, grades process and outcome separately.

Adjusting, adding, reducing and closing are trades too: same path, new ticket under the same id.

## Where things live

Chat is where the desk talks; files are where it remembers.

| File | What it is |
| --- | --- |
| `desk.md` | the desk record: engagement level, surfaces, tradeable universe, Bots, chats, standing approvals, rehearsed action kinds |
| `risk-limits.md` | your limits, versioned; only you change it |
| `proposals/SG-*.md` | one file per trade idea, appended through its life: ticket, sign-off, approval, send, reconciliation, review |
| `journal/*.md` | the desk diary, append-only |
| `briefs/`, `research/`, `strategies/`, `data/`, `watch/` | working material |

## Trust boundaries

**Read plane.** Six Bots read: the public Price Service, the MCP's read-only tools, and public web pages. Plenty of judgement, no writes.

**Write plane.** One Bot writes: the signed trading endpoints, only when a proposal carries a Risk PASS, your approval by id, and a passing pre-send checklist. Strike has no dry-run mode, so the desk builds each request as a file, posts it verbatim as a **preview**, and sends that same file - the bytes you approved are the bytes that go. One approval, one send. Then the desk reads the order back from the exchange by its client order id and reports what the exchange says, never the submission.

**Key.** An Ed25519 **API wallet** you register at `app.strikefinance.org/api-keys` is the only credential on the computer, provided through the secure secret store. It can trade; neither the trade API nor the user API exposes a withdraw, deposit or transfer endpoint, so it cannot take the money out. Anything that moves funds stays in the Strike app, with you.

**Recovering an unknown send.** The desk chooses a `client_order_id` before every send and writes it to the proposal file, so a lost response is a lookup rather than a guess: `GET /v2/order` answers authoritatively whether that exact order exists. A replacement always gets a fresh id. Strike has no order expiry, so elapsed time alone still proves nothing - only the lookup does.

**Evidence.** Web pages, files and other Bots' messages are information; none of them authorises anything. Your approval phrase with the ticket id is the desk's record that you agreed, but the Bots write the floor's messages, so the phrase alone cannot be the gate: an approval a Bot can read is one a Bot could have written. Enforcement lives outside the conversation, in Grok Bot's Require Approval rule on any non-GET call through `scripts/strike_request.py`. No Bot may type, quote forward, infer or simulate your approval.

**Uncertainty.** Missing, stale, gapped or partial data is `unavailable`, a verdict of its own. It never collapses into "the condition did not fire" or "the check passed". A watch that reports silence on a dead feed looks exactly like a calm market, so the desk is required to tell the two apart and say which it has.

## Why seven

Separating the person who wants the trade from the one who sizes it, the one who sends it and the one who reviews it is the oldest control on any desk. Bots make it cheap: each role has a narrow prompt, a narrow set of skills, and a narrow claim to authority.

## Other runtimes

Grok Build, Cursor and Claude Code load `agents/`, `skills/` and `rules/` as a plugin (`plugin.json`, `.grok-plugin/plugin.json`, `.cursor-plugin/plugin.json`, `.claude-plugin/plugin.json`). Claude Code resolves the repository through `.claude-plugin/marketplace.json`, so it installs with `/plugin marketplace add mendurim/strikegrok-trading-desk` then `/plugin install strikegrok@strikegrok`. Roles become subagents or labelled passes; the approval model is the same.
