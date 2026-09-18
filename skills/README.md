# Skills

Twenty-two skills in the portable `SKILL.md` format (`name` and `description` frontmatter, body under 320 lines). Grok Bot reads them from `/workspace/strikegrok/skills/<name>/SKILL.md` and saves each as a shared skill; Grok Build, Cursor and Claude Code load the directory as a plugin.

## Bootstrap

| Skill | What it teaches | Primary users |
| --- | --- | --- |
| [strikegrok-bootstrap](strikegrok-bootstrap/SKILL.md) | Pinned install, zero-key Opening Bell, team creation, desk doctor and evidence receipt | Desk Lead |

## Strike skills (how to work with the exchange)

Market data is the **public Price Service**; everything else is the **signed trading API**, authenticated with an Ed25519 API wallet. One symbol vocabulary throughout (`ADA-USD`, `XAU-USD`), and all thirty-one markets are tradeable.

| Skill | What it teaches | Writes? | Primary users |
| --- | --- | --- | --- |
| [strike-setup](strike-setup/SKILL.md) | The two surfaces, connectivity checks, provisioning the API wallet, readiness check, rotation, and why testnet cannot rehearse | no | Desk Lead, Execution Trader |
| [strike-auth](strike-auth/SKILL.md) | Generating and registering the Ed25519 API wallet, the request signing scheme, the signed-request helper, what the key can and cannot do, rotation | no | Execution Trader, Desk Lead |
| [strike-market-data](strike-market-data/SKILL.md) | Public prices, depth bands, funding, open interest, 24h stats, candles, per-market constraints | no | Market Analyst, Strategist, Risk Manager |
| [strike-account](strike-account/SKILL.md) | Account and balances, positions, open orders, closed positions, order and fill history, funding paid, the transaction ledger, single-order lookup by client id | no | Risk Manager, Execution Trader, Trade Reviewer |
| [strike-orders](strike-orders/SKILL.md) | Limit, bounded market, bracket strategy orders, standalone triggers, trailing stops, replace, cancel, client order ids, rounding, the preview block | **yes** | Execution Trader |
| [strike-positions](strike-positions/SKILL.md) | Leverage, margin mode, margin and liquidation distance, protecting a position, closing reduce-only | **yes** | Execution Trader, Risk Manager (reads) |
| [strike-advanced](strike-advanced/SKILL.md) | TWAP, batch and batch-replace, cancel-all, isolated-margin adjustment, order flags, and what the desk refuses | **yes** | Execution Trader |
| [strike-websocket](strike-websocket/SKILL.md) | Public price stream and signed user stream, order-book sync, watch processes, heartbeat and supervision | no | Market Analyst, Execution Trader, Strategist |
| [strike-research-tools](strike-research-tools/SKILL.md) | The **optional** crowdtime MCP add-on: liquidity screen, computed indicators, news and dividend research playbooks, Bodega, Discord alerts | no | Research Analyst, Desk Lead |
| [strike-api-reference](strike-api-reference/SKILL.md) | Every endpoint and field, the signing scheme, order types and flags, status codes, constraints, rate limits, error strings | no | everyone |

## Desk skills (how the team works)

| Skill | What it covers | Primary users |
| --- | --- | --- |
| [desk-operating-model](desk-operating-model/SKILL.md) | Roles, seats, workspace, engagement levels, evidence standard, approval model, handoff format | everyone |
| [desk-trade-lifecycle](desk-trade-lifecycle/SKILL.md) | The seven stages of a trade, the proposal file, the ticket, approval by id, definitions of done | Desk Lead, everyone |
| [desk-risk-limits](desk-risk-limits/SKILL.md) | The limits file, sizing arithmetic on a stressed stop, leverage caps and notional headroom, book check, veto rules | Risk Manager |
| [desk-execution-protocol](desk-execution-protocol/SKILL.md) | Pre-send checklist, the dry run as rehearsal, single send, unverified results, reconciliation | Execution Trader |
| [desk-monitoring](desk-monitoring/SKILL.md) | Routines, the desk brief, watches and alert conditions | Desk Lead, Risk Manager, Market Analyst |
| [desk-signal-scan](desk-signal-scan/SKILL.md) | The universe scan, T-minus catalyst alerts, live rule monitors, the signals file, the four clocks | Market Analyst, Research Analyst, Strategist, Desk Lead |
| [desk-standing-approvals](desk-standing-approvals/SKILL.md) | The two modes, the three tiers, the signed register, the suspension ledger, what the policy layer checks | Desk Lead, Risk Manager, Strategist, Execution Trader |
| [desk-autopilot](desk-autopilot/SKILL.md) | The runbooks on a clock: `scripts/autopilot.py`, the two-OS-user split, the rule file, the preflight, the staged rollout | Desk Lead, Strategist, Execution Trader |
| [desk-post-trade-review](desk-post-trade-review/SKILL.md) | Journal format, trade review, weekly review, incident review | Trade Reviewer |
| [desk-incident-response](desk-incident-response/SKILL.md) | Playbooks for unverified sends, mismatches, unprotected positions, outages, token compromise | Execution Trader, Risk Manager, Desk Lead |
| [desk-strategy-lab](desk-strategy-lab/SKILL.md) | Rules first, honest backtests, sanity checks, paper trading | Strategist |

## Conventions

Each skill's `metadata.version` is that skill's own, not the release tag. A skill at `1.0.1` inside release `v3.1.1` has simply not needed changing; the release the desk was built from is the tag the bootstrap skill clones.

- Frontmatter: `name` (matches the directory), `description` (what and when, under 1024 characters), `license`, `metadata` (`version`, `author`, `category`, and `network-default` for Strike skills).
- Bodies: purpose, concepts, copy-pasteable commands (`curl` for public reads, `scripts/strike_request.py` for signed calls), procedure, pitfalls. No strategy content, no return claims, no emoji.
- Every write path says who may use it and under what approval. Every read says which surface it came from.
- Snippets are self-contained: each includes its own environment loading so a Bot can copy one block and run it.
