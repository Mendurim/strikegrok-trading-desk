<p align="center">
  <img src="assets/mascot-320.jpg" width="160" alt="StrikeGrok mascot">
</p>

<h1 align="center">StrikeGrok</h1>

<p align="center"><strong>Turn your Grok Bot into a 7-agent Strike Finance trading desk.</strong></p>

Add the Desk Lead to [Grok Bot](https://grok.com). It opens with a live, zero-key Strike market snapshot, builds research, risk, execution and review as separate agents, then proves the floor is ready. They brief you on markets, size your trades, send what you approve and tell you honestly how it went. You bring the ideas. Let them cook.

Market data comes from Strike's **public REST Price Service**; execution goes through the **signed Strike API** with an Ed25519 API wallet you register yourself. Same symbols on both, all thirty-one markets tradeable.

## Start

Open Grok Bot and paste this to any Bot:

> Set up the StrikeGrok trading desk from https://github.com/Mendurim/strikegrok-trading-desk/blob/v3.1.2/skills/strikegrok-bootstrap/SKILL.md. Follow the bootstrap skill, use https://github.com/Mendurim/strikegrok-trading-desk/blob/v3.1.2/SETUP.md for the complete runbook, and finish with its evidence receipt.

The desk starts in research mode. The first demo uses only Strike's public Price Service: no key, no account read, no order. Register a Strike API wallet when you are ready to trade.

### Opening Bell

The first thing the Desk Lead shows is useful, live output—not a configuration form:

```bash
python3 scripts/opening_bell.py --symbol BTC-USD
```

It reports source and UTC time, mid/mark/index, 24-hour change and range, hourly funding and the next funding time, open interest, spread, per-market tick/step/minimum notional, and depth at 5/10/25 bps. `/v2/depth` defaults to twenty levels a side, which stops short of the wider bands, so the script always asks for the documented maximum and names any band the resting book does not reach as a floor rather than a total. `scripts/desk_doctor.py` then checks the release, team files, the reviewed skill bytes, desk folders and public connectivity. Both are read-only and standard-library Python.

## Meet the desk

| Bot | What they do for you |
| --- | --- |
| **Desk Lead** | Your main contact. Runs the floor, keeps every trade moving through the same clean process. |
| **Market Analyst** | Live Strike data on demand: price, depth, funding, open interest, candles. Timestamped and sourced. |
| **Research Analyst** | What is happening and what is scheduled. Strike lists equities and commodities as well as crypto, so earnings, dividends and macro count here. |
| **Strategist** | Turns your idea into explicit rules, backtests it honestly on Strike history, and stress-tests it before a cent is risked. |
| **Risk Manager** | Keeps your written limits, sizes every trade from your live account, watches the book, and can say no. |
| **Execution Trader** | The one Bot with the key. Previews every order, sends the ticket you approved once, and reconciles it from the exchange record. |
| **Trade Reviewer** | Keeps the desk journal and grades every trade on process and outcome, separately. |

Six sit together on the **Trading Floor** group chat; the Trade Reviewer works by DM. Every trade follows the same path:

```
idea -> evidence -> risk sign-off -> your approval -> dry run -> one send -> reconciliation -> review
```

## A day on the desk

**"Brief me on ADA."** The Market Analyst pulls mid, mark, index, funding, open interest, 24h volume and depth at 5/10/25 bps from the Price Service, and posts a brief with sources and UTC times.

**"I want to long ADA at 0.21 with a stop at 0.1995."** The Desk Lead opens `SG-20260910-01`, the Risk Manager reads your account live through Strike's signed API and comes back with a ticket:

```
TICKET SG-20260910-01 | ADA-USD
market ADA-USD  side long  size 4800 ADA (~$1,008)
entry limit 0.2100 GTC    stop 0.1995 market (attached to the entry as one bracket)
risk $51.00 = 0.5% of equity $10,200 (strike_get_account_balance 14:11 UTC)
sized on a stressed stop: 0.0105 + slippage + fees per ADA
approve with: "approve SG-20260910-01"
```

You type `approve SG-20260910-01`. The Execution Trader posts the exact request body as a preview, sends that same file once, then reads the order back from the exchange by its client order id and reports what the exchange says — not "sent". When it closes, the Trade Reviewer tells you what it cost, whether the process was clean, and one thing worth keeping.

**"Is anything scheduled for NVDA?"** The Research Analyst pulls the research playbook, does the web work itself, and comes back with a sourced catalyst list — because on Strike you can trade the equity perp.

## What the desk knows

Eighteen skills, in the portable `SKILL.md` format, shared by all your Bots.

**Bootstrap** — pinned release install, Opening Bell, team construction, desk doctor and a receipt that distinguishes what happened from what still needs a manual step.

**Strike** — the two surfaces and the symbol map, the API wallet signing scheme, market data, account state, orders (market, limit, brackets with attached TP/SL, triggers, cancels), positions and leverage, WebSocket feeds, the research tools, and a compact API reference. Copy-pasteable `curl` for reads; one signed request per write, through `scripts/strike_request.py`.

**Desk** — how the team works: operating model, the trade lifecycle and ticket, risk limits and sizing arithmetic, the execution protocol, monitoring and routines, post-trade review, incident playbooks, and the strategy lab.

## Built for real money

- **Nothing opens exposure without an approval the code can check.** `scripts/desk_policy.py` runs inside the one script that holds your API wallet, on every write, before it is signed. A Bot that can read an approval could also write one, so the layer does not read approvals from the chat: it verifies an Ed25519 signature made with a key that never touches the desk computer. See [the policy layer](#the-policy-layer) below.
- **Every opening order carries its own stop.** The layer refuses a naked entry, a stop that is not reduce-only, a stop on the wrong side of the entry, and a stop that would lose more than the ticket says the trade risks.
- **Every order is previewed before it is sent.** The desk builds the exact request as a file, posts it to you verbatim, and sends that same file. The bytes you approved are the bytes that go.
- **Sized on a stressed stop.** A triggered stop is a market order: it slips and pays taker on both legs. The desk sizes on what the stop will actually cost, not its trigger price, so your risk budget means what it says.
- **Ceilings you cannot trade through.** Your limits file may only tighten the desk's own caps, never loosen them, and the caps compiled into the policy layer refuse a notional over $5,000 or over one times your equity, leverage over 5, a position opened past a 3% daily loss, a fourth open position, and a second order inside a minute. Environment variables may tighten every one of those and loosen none.
- **One writer.** Six Bots read; one Bot sends, once per approval, and reconciles from the exchange record.
- **A reviewer who keeps you honest.** Process and outcome graded separately, in a journal you can read.

## The policy layer

Every Bot on the desk shares one computer and one filesystem. So a Bot can write to the desk record, to a proposal file, and to the floor — which means any approval a Bot can read is one a Bot could have composed. The only enforcement that survives that is inside `scripts/strike_request.py`, the one thing holding the API wallet.

`scripts/desk_policy.py` is that enforcement, and it runs on every non-GET before signing. Three tiers:

| Tier | What | Approval |
| --- | --- | --- |
| **0** | reduce-only: place or tighten a stop, exit on a rule's invalidation, cancel an orphaned or expired order, add margin | none needed — it can only shrink exposure |
| **1** | open exposure when a frozen, tested rule fires | a standing approval in a register you signed |
| **2** | open exposure on anything else | a per-trade token you signed |

**The desk ships with no key installed, and that mode is honest about what it is.** With no `desk/user-signing.pub`, a `RISK | … | PASS` block is markdown any Bot can write, and that is all the layer requires before it will sign an opening order. The only approval control is Grok Bot's Require Approval rule, which lives in chat — and a Bot that runs `scripts/strike_request.py` directly with the wallet in its environment never passes through chat. The layer logs every such send as `WARN`, and `desk_policy.py verify` says so in as many words.

**Installing one file changes that.** Generate a key pair on your own machine, copy only the public half to the desk, and from that moment no order can open exposure without a signature the desk cannot produce. `SETUP.md` step 7 is the runbook; it takes about a minute.

Standing approvals (Tier 1) go further and let a tested rule fire at 03:00 with nobody awake — but they rely on a state directory that is only a real boundary when the signer runs as its own OS user. On a shared Grok workspace it does not, so Tier 1 stays off unless you set `STRIKEGROK_STATE_TRUSTED=1` to assert otherwise. Leave it unset and trade at Tier 2 until that separation is real.

## Unattended, on a computer where the split is real

`scripts/autopilot.py` is the desk's two autonomy runbooks — the hourly universe scan and the live rule monitors — executed by a clock instead of a prompt. It evaluates a frozen rule on closed bars, checks the four clocks, sizes on a stressed stop, writes the proposal and its PASS block, and asks the signer for exactly one bracketed order. There is no model in the send path, so the same bars produce the same ticket twice and the ticket can be checked afterwards.

```bash
python3 scripts/autopilot.py scan                                    # hourly
python3 scripts/autopilot.py monitor --rule strategies/r/rule.json   # at bar close
python3 scripts/autopilot.py monitor --rule … --dry                  # rehearsal; sends nothing
```

It never holds the API wallet. Every authenticated call, reads included, goes through `sudo -u strike-signer … strike_request.py`, so the policy layer gates it exactly as it gates a person. That only means anything with two OS users — one that owns the wallet and the state directory, one that runs the Bots and can write proposals but sign nothing — which is why `desk-autopilot` and `SETUP.md` section 11 spend as much space on the split as on the script.

Before it sends, autopilot predicts every refusal the gate can produce: open incidents, suspensions, the rule hash, notional and risk caps, pacing, the book, the approval's occupied markets, and the venue's own filters. A refusal that gets through anyway files an incident and is never retried, because it means the script is wrong about the gate rather than the market being wrong about the trade.

### Two things this desk is honest about

**Strike has no dry-run mode.** An earlier version of this desk had one in its transport; the signed API does not. The desk replaces it with a preview block: every request is built as a file, posted to you verbatim, and that same file is sent. It is a discipline rather than a gate, so the Require Approval rule in Grok Bot matters more, not less.

**Strike has no order expiry.** An order cannot age out into safety, so elapsed time never proves a lost send is dead. What the desk relies on instead is a `client_order_id` it chooses before every send, and an endpoint that answers whether that exact order exists - so a lost response is a lookup, not a guess. A replacement always gets a fresh id.

Perpetual futures can liquidate an account. StrikeGrok is documentation and instructions, not financial advice.

## Also runs in Grok Build, Cursor and Claude Code

The same `agents/`, `skills/` and `rules/` load as a plugin: twenty-two skills, and the seven roles as subagents.

In Claude Code, install it from this repository:

```
/plugin marketplace add Mendurim/strikegrok-trading-desk
/plugin install strikegrok@strikegrok
```

In Grok Build and Cursor, open the repository and enable the plugin.

The same pack installs as a skill, straight from this repository:

```
npm exec --package=skills@1.5.23 -- skills add Mendurim/strikegrok-trading-desk
```

Either way, run `/desk-operating-model` to begin.

## Inside the repository

```
SETUP.md     what your Grok Bot follows to build the desk
agents/      seven roles: Bot profile card + full system prompt
skills/      twenty-two skills (bootstrap, strike-*, desk-*)
template/    Grok Bot profile, skill hashes, example rule, cron and sudoers
scripts/     the signer, the policy layer, the autopilot, Opening Bell, checks
docs/        how it works, FAQ, provenance
assets/      the mascot - use it as your Bots' avatar
```

| Doc | |
| --- | --- |
| [How the desk works](docs/ARCHITECTURE.md) | roles, files, trust boundaries |
| [FAQ](docs/FAQ.md) | the token, approvals, rehearsal, customising the team |
| [Skills index](skills/README.md) | every skill and who uses it |
| [Grok Bot template](docs/GROK_BOT_TEMPLATE.md) | public profile, publish contract and clean-install evaluation |
| [Publishing](PUBLISH.md) | put this on your GitHub and install it in Grok Bot |
| [Provenance](docs/PROVENANCE.md) | sources and licences |
| [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md) | |

## License

[MIT licensed](LICENSE). Keep the copyright and permission notice when reusing copies or substantial portions. See [reuse and attribution](ATTRIBUTION.md) for a ready-to-copy credit line, and [provenance](docs/PROVENANCE.md) for sources.

Perpetual futures can liquidate an account. StrikeGrok is documentation and instructions, not financial advice.
