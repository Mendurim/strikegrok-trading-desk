---
name: strikegrok-bootstrap
description: Build and verify a StrikeGrok trading desk from the pinned public release. Use for first-run setup, repair, or a readiness check. Starts with a live zero-key Opening Bell on Strike's public Price Service, installs the seven role profiles and nineteen shared skills, prepares the Trading Floor, provisions the Strike API wallet only when the user asks to trade, and returns an evidence receipt. Read-only by default; never requests a key or places an order.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs (HyperGrok), ported for Strike Finance
  category: desk
---

# StrikeGrok bootstrap

Turn a fresh shared **Desk Lead** into a working StrikeGrok desk. Finish with evidence, not a claim that setup probably worked.

## Safety boundary

Bootstrap is research-only.

- Do not request, read or store the Strike API wallet keys, or any other credential.
- Do not call any signed endpoint. Do not create an order, change leverage, or move funds.
- Public Price Service reads are allowed. State the service, symbol and UTC time.
- Local writes are limited to `/workspace/strikegrok` and `/workspace/trading-desk`.
- Creating the seven named Bots and the private Trading Floor group is in scope. Sharing anything publicly is not.
- If a capability is unavailable, return the exact manual card or step and continue independent supported work. Do not pretend it happened or mark dependent checks complete.

## 1. Install the reviewed release

If `/workspace/strikegrok` is already a Git checkout, read its `plugin.json` and run its check; do not overwrite it. Otherwise:

```bash
mkdir -p /workspace && cd /workspace
git clone --depth 1 --branch v1.0.0 https://github.com/mendurim/strikegrok-trading-desk.git strikegrok
cd /workspace/strikegrok
git rev-parse HEAD
bash scripts/check.sh
```

Stop if the structural check fails. Record the printed commit for `desk.md`.

`git clone` prints `warning: refs/tags/... is not a commit!` when it shallow-clones an annotated tag. That is expected and harmless. Judge the step by `scripts/check.sh`, not by that line.

If the clone fails, do not fall back to an unverified archive over plain HTTP. Ask the user to attach the archive, unpack it to `/workspace/strikegrok`, and run `bash scripts/check.sh` before going further.

## 2. Ring the Opening Bell

Show useful output before asking the user configuration questions:

```bash
cd /workspace/strikegrok
python3 scripts/opening_bell.py --symbol BTC-USD
```

Return the output verbatim. It is a public market snapshot from Strike's Price Service, not a signal. It uses no token and reads no account. If it is unavailable, say so and continue offline; do not substitute stale or invented figures.

## 3. Prepare the desk computer (read-only)

```bash
mkdir -p /workspace/trading-desk/{proposals,briefs,research,strategies,data,journal/incidents,watch}
cd /workspace/strikegrok
python3 scripts/opening_bell.py --symbol ADA-USD
python3 scripts/desk_doctor.py --desk-root /workspace/trading-desk
```

Follow `strike-setup` sections 1-3 only. A `desk.md` warning from the doctor is expected until step 7; a repository or public API failure is not. The API wallet comes later, and only when the user asks to trade.

## 4. Create the Bots

For each file in `agents/`, create one Bot, using the profile card exactly: **Name** and **Job** from the card, **Description** verbatim, the mascot as avatar. Then send the new Bot its full **System prompt** section as its first message, prefixed with: "These are your standing instructions. Confirm you have read them and state your job in one sentence." Ask it to re-read `/workspace/strikegrok/agents/<name>.md` whenever it is unsure.

| File | Name |
| --- | --- |
| `agents/desk-lead.md` | Desk Lead |
| `agents/market-analyst.md` | Market Analyst |
| `agents/research-analyst.md` | Research Analyst |
| `agents/strategist.md` | Strategist |
| `agents/risk-manager.md` | Risk Manager |
| `agents/execution-trader.md` | Execution Trader |
| `agents/trade-reviewer.md` | Trade Reviewer |

If you can create Bots, do so now. If you cannot, give the user the seven profile cards as labelled copy-and-paste blocks and wait until they confirm. Seven Bots, not one: the separation between the six that read and the one that writes is the design.

## 5. Install the skills

Skills are shared across all the user's Bots, and a Desk Lead added from the public StrikeGrok template already carries this release's reviewed set. Inspect the shared skills before saving anything: **do not create duplicate skills**.

For each directory under `skills/`, compare `name` and instructions with the shared skill when one exists. Matching: enabled, recorded `template`. Missing: saved unchanged, recorded `installed`. Too long to save: save a pointer skill - "When this skill is used, read `/workspace/strikegrok/skills/<name>/SKILL.md` and follow it" - recorded `pointer`. A same-name skill with different instructions that cannot be replaced is a `mismatch` and fails readiness.

The receipt lists **exactly nineteen unique skill names** and one status each. A name alone is not proof its content is current.

Nineteen skills:

- Bootstrap: `strikegrok-bootstrap`
- Strike: `strike-setup`, `strike-auth`, `strike-market-data`, `strike-account`, `strike-orders`, `strike-positions`, `strike-advanced`, `strike-websocket`, `strike-api-reference`
- Optional: `strike-research-tools` (the crowdtime MCP add-on; the desk trades fully without it)
- Desk: `desk-operating-model`, `desk-trade-lifecycle`, `desk-risk-limits`, `desk-execution-protocol`, `desk-monitoring`, `desk-post-trade-review`, `desk-incident-response`, `desk-strategy-lab`

Tell each Bot which skills are its own, from its agent file's frontmatter. Any Bot may read any skill; the Execution Trader is the one Bot that acts on the write paths in `strike-orders` and `strike-positions`.

Retain each skill's `LICENSE` and `ATTRIBUTION.md` when copying it elsewhere.

## 6. Create the Trading Floor

One group chat named **Trading Floor** with exactly six Bots: Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager, Execution Trader. The Trade Reviewer works by DM.

Post this first:

> Welcome to the Trading Floor. Desk Lead routes; Market Analyst and Research Analyst bring evidence; Strategist helps the user test their own ideas; Risk Manager sizes and can refuse; Execution Trader is the one Bot that sends orders, on a ticket the user approved by id. Market data comes from Strike's public Price Service; execution goes through the signed Strike API with the desk's API wallet. Same `-USD` symbols on both. Rules: `/workspace/strikegrok/skills/desk-operating-model/SKILL.md`. Trade Reviewer is a DM away. Today is setup: nothing goes to the exchange.

## 7. Approvals and the desk record

Ask the user to open **Settings, General, Auto-review** and add a **Require Approval** rule for financial actions and for any command that runs `scripts/strike_request.py` with a method other than GET. If the rule syntax cannot express that, say so; the desk's protocol still holds. That rule is the gate: the approval phrase in chat is the desk's record that the user agreed, but the Bots write the floor's messages, so it cannot be the only thing in the way of a send.

Then ask two questions and write `/workspace/trading-desk/desk.md`:

1. Engagement level: **research** (no key) or **trading**. There is no useful testnet middle - Strike's testnet books are empty, so the desk rehearses with preview blocks and minimum-size live orders instead (`strike-setup` section 6).
2. May the desk place a protective stop for a position that has none, without waiting for approval? It is reduce-only. Record the answer either way, with the date.

```markdown
# Desk record

- created: 2026-09-10 15:00 UTC
- instructions commit: 0000000        # git rev-parse HEAD from step 1
- engagement level: research
- market data: Strike Price Service (public, no key)
- execution: Strike signed API https://api.strikefinance.org   # API wallet not yet provisioned
- tradeable universe: 31 markets   # /v2/exchangeInfo status=trading is the authority
- research add-on: crowdtime MCP   # optional, not connected
- bots: Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager, Execution Trader, Trade Reviewer
- group chats: Trading Floor (6)
- risk limits: not yet written  (Risk Manager runs the interview: skills/desk-risk-limits)
- standing approvals: none          # recommended: protective stops (reduce-only)
- rehearsed action kinds: none      # bracket / trigger / close, each at minimum size
- unprotected position deadline: 15m
- status: research-only until an API wallet is provisioned
```

Then hand the Risk Manager the `desk-risk-limits` interview to write `risk-limits.md` with the user.

## 8. Verify the desk (read-only)

```bash
cd /workspace/strikegrok
python3 scripts/desk_doctor.py --desk-root /workspace/trading-desk
python3 scripts/opening_bell.py --symbol ETH-USD
```

Then run these and record the results:

1. Trading Floor: "@Market Analyst brief us on BTC." Expect a timestamped brief with sources.
2. "@Risk Manager assuming equity of 10,000 USD and the current limits, size a hypothetical long ADA with a 5% stop." Expect a PASS or REJECT with the arithmetic and a ticket, and a note that nothing will be sent.
3. "@Execution Trader what would you need before sending that ticket?" Expect the pre-send checklist, the dry-run step, and a refusal to send without approval by id.
4. "@Execution Trader show me the preview block for that ticket." Expect the exact JSON body, with a `client_order_id` derived from the ticket id and a stop in the same strategy order - and a refusal to send it.
5. DM the Trade Reviewer: "Open today's journal and record that the desk was set up." Expect a journal entry.
6. Ask the Research Analyst for one sourced fact about Strike Finance itself.

## 9. Return the receipt

Give the user:

- the seven Bots and how each was created
- the eighteen skills and their statuses
- the Trading Floor group and its members
- the desk record and its engagement level
- the desk doctor and Opening Bell results
- the results of the six verification checks
- confirmation that setup stayed read-only: no key requested, no signed endpoint called, no order placed

Then say: "The desk is ready. Ask the Desk Lead for a market brief to see it work. When you want it to trade, say 'set up the Strike API wallet' and it will walk you through `strike-auth`."
