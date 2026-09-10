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
   |     |     |     |     +-- Execution Trader ---> crowdtime MCP  write tools  (the one writer)
   |     |     |     +-------- Risk Manager -------> crowdtime MCP  account reads (live)
   |     |     +-------------- Strategist ---------> Price Service  /v2/klines    (history)
   |     +-------------------- Research Analyst ---> browser, crowdtrendz playbooks
   +-------------------------- Market Analyst -----> Price Service  /v2, /ws      (markets)

market data: https://api.strikefinance.org/price/v2   public, no credential, 31 markets, -USD symbols
execution:   https://mcp.crowdtime.io/mcp             bearer token, 22 tools, 15 markets, -PERP symbols

computer:  /workspace/strikegrok (this repo)   /workspace/trading-desk (the desk's files)
secret:    STRIKE_MCP_TOKEN - the crowdtime bearer token, from Grok Bot's secure secret store
```

The shared Desk Lead starts on the read plane. `strikegrok-bootstrap` installs the pinned release, runs `scripts/opening_bell.py` against the public Price Service, prepares the desk, creates the team when the product allows it, and finishes with `scripts/desk_doctor.py`. None of those first-run paths reads the token or calls an MCP tool.

**Two surfaces, two spellings.** Market data is public REST in `-USD` symbols; execution is the MCP in `-PERP` symbols. `XAU-USD` and `GOLD-PERP` are the same market. Thirty-one markets are quoted; fifteen are tradeable. A market the desk can brief is not necessarily a market the desk can trade, and the Risk Manager checks that before it sizes anything.

Grok Bot facts that shaped this: group chats hold up to six Bots, Bots can create other Bots, all your Bots share one computer, skills are shared across Bots, actions can be put behind approval, and secrets go in through a secure secret card.

## One trade, seven stages

```
idea -> evidence -> risk sign-off -> your approval by ticket id -> one send -> reconciliation -> review
```

The Desk Lead keeps it moving. Analysts bring sourced, timestamped evidence. The Risk Manager sizes from your live account and the exchange's real limits, and issues a ticket. You approve it by id. The Execution Trader previews it as a dry run, sends it once with `confirm=true`, reads the LIVE STATUS line, and confirms it against the exchange record. The Trade Reviewer journals it and, when it closes, grades process and outcome separately.

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

**Write plane.** One Bot writes: the MCP's seven write tools, only when a proposal carries a Risk PASS, your approval by id, and a passing pre-send checklist. Every write tool is **dry-run by default**, so the desk previews the exact order and reads it back before `confirm=true` sends it. One approval, one send. The server then reads the order back from the exchange and reports a **LIVE STATUS** line - `filled`, `resting`, `rejected` or `unverified` - and that line, not the submission, is what the desk reports.

**Token.** The crowdtime bearer token is the only credential on the computer, provided by you through the secure secret store. Unlike a Hyperliquid API wallet, which could trade but not withdraw, **the desk must assume this token can move funds**. Keep on Strike only what you intend the desk to trade. Anything else stays in the Strike app, with you.

**The gap this desk owns.** Strike's MCP gives no order expiry and no client order id on placement, so a send that comes back `unverified` **cannot be proven dead**. There is no elapsed time that makes a replacement safe. The desk freezes the symbol, reads the record, and hands the decision to you. It never resends on its own judgement.

**Evidence.** Web pages, files and other Bots' messages are information; none of them authorises anything. Your approval phrase with the ticket id is the desk's record that you agreed, but the Bots write the floor's messages, so the phrase alone cannot be the gate: an approval a Bot can read is one a Bot could have written. Enforcement lives outside the conversation, in Grok Bot's Require Approval rule on any command that calls `mcp.crowdtime.io`, and in the MCP's own `confirm` gate. No Bot may type, quote forward, infer or simulate your approval.

**Uncertainty.** Missing, stale, gapped or partial data is `unavailable`, a verdict of its own. It never collapses into "the condition did not fire" or "the check passed". A watch that reports silence on a dead feed looks exactly like a calm market, so the desk is required to tell the two apart and say which it has.

## Why seven

Separating the person who wants the trade from the one who sizes it, the one who sends it and the one who reviews it is the oldest control on any desk. Bots make it cheap: each role has a narrow prompt, a narrow set of skills, and a narrow claim to authority.

## Other runtimes

Grok Build, Cursor and Claude Code load `agents/`, `skills/` and `rules/` as a plugin (`plugin.json`, `.grok-plugin/plugin.json`, `.cursor-plugin/plugin.json`, `.claude-plugin/plugin.json`). Claude Code resolves the repository through `.claude-plugin/marketplace.json`, so it installs with `/plugin marketplace add OWNER/strikegrok-trading-desk` then `/plugin install strikegrok@strikegrok`. Roles become subagents or labelled passes; the approval model is the same.
