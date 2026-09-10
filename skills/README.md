# Skills

Eighteen skills in the portable `SKILL.md` format (`name` and `description` frontmatter, body under 320 lines). Grok Bot reads them from `/workspace/strikegrok/skills/<name>/SKILL.md` and saves each as a shared skill; Grok Build, Cursor and Claude Code load the directory as a plugin.

## Bootstrap

| Skill | What it teaches | Primary users |
| --- | --- | --- |
| [strikegrok-bootstrap](strikegrok-bootstrap/SKILL.md) | Pinned install, zero-key Opening Bell, team creation, desk doctor and evidence receipt | Desk Lead |

## Strike skills (how to work with the exchange)

The desk uses two surfaces: the **public REST Price Service** for market data, and the **crowdtime MCP** for execution. They use different symbols - `XAU-USD` on one is `GOLD-PERP` on the other - and `strike-mcp` holds the translation table.

| Skill | What it teaches | Writes? | Primary users |
| --- | --- | --- | --- |
| [strike-setup](strike-setup/SKILL.md) | The two surfaces, connectivity checks, the bearer token through the secure secret store, readiness check, rotation, and why testnet cannot rehearse | no | Desk Lead, Execution Trader |
| [strike-mcp](strike-mcp/SKILL.md) | The execution transport: endpoint, auth, both routes from Grok Bot, the symbol map, all twenty-two tools, the confirm gate, LIVE STATUS, errors | no (describes writes) | everyone |
| [strike-market-data](strike-market-data/SKILL.md) | REST prices, depth bands, funding, open interest, 24h stats, candles, per-market constraints; the MCP's indicator snapshot and market scan | no | Market Analyst, Strategist, Risk Manager |
| [strike-account](strike-account/SKILL.md) | Equity and margin, open positions, resting orders, closed positions, order history by status, fills with paging, reconstructing a round trip | no | Risk Manager, Execution Trader, Trade Reviewer |
| [strike-orders](strike-orders/SKILL.md) | Market and limit entries, brackets with attached TP/SL, standalone triggers, cancels, rounding, the dry run, reading LIVE STATUS | **yes** | Execution Trader |
| [strike-positions](strike-positions/SKILL.md) | Leverage and margin mode, the server's leverage cap, margin and liquidation distance, protecting a position, closing reduce-only | **yes** (leverage, protection, closes) | Execution Trader, Risk Manager (reads) |
| [strike-websocket](strike-websocket/SKILL.md) | Public price stream and authenticated user stream, order-book sync, watch processes, heartbeat and supervision | no | Market Analyst, Execution Trader, Strategist |
| [strike-research-tools](strike-research-tools/SKILL.md) | Crypto, equity and dividend research playbooks, Bodega prediction markets, Discord alerts | no (Discord posts) | Research Analyst, Desk Lead |
| [strike-api-reference](strike-api-reference/SKILL.md) | Every REST endpoint and MCP tool with arguments, the symbol map, order status codes, constraints, rate limits, error strings | no | everyone |

## Desk skills (how the team works)

| Skill | What it covers | Primary users |
| --- | --- | --- |
| [desk-operating-model](desk-operating-model/SKILL.md) | Roles, seats, workspace, engagement levels, evidence standard, approval model, handoff format | everyone |
| [desk-trade-lifecycle](desk-trade-lifecycle/SKILL.md) | The seven stages of a trade, the proposal file, the ticket, approval by id, definitions of done | Desk Lead, everyone |
| [desk-risk-limits](desk-risk-limits/SKILL.md) | The limits file, sizing arithmetic on a stressed stop, leverage caps and notional headroom, book check, veto rules | Risk Manager |
| [desk-execution-protocol](desk-execution-protocol/SKILL.md) | Pre-send checklist, the dry run as rehearsal, single send, unverified results, reconciliation | Execution Trader |
| [desk-monitoring](desk-monitoring/SKILL.md) | Routines, the desk brief, watches and alert conditions | Desk Lead, Risk Manager, Market Analyst |
| [desk-post-trade-review](desk-post-trade-review/SKILL.md) | Journal format, trade review, weekly review, incident review | Trade Reviewer |
| [desk-incident-response](desk-incident-response/SKILL.md) | Playbooks for unverified sends, mismatches, unprotected positions, outages, token compromise | Execution Trader, Risk Manager, Desk Lead |
| [desk-strategy-lab](desk-strategy-lab/SKILL.md) | Rules first, honest backtests, sanity checks, paper trading | Strategist |

## Conventions

- Frontmatter: `name` (matches the directory), `description` (what and when, under 1024 characters), `license`, `metadata` (`version`, `author`, `category`, and `network-default` for Strike skills).
- Bodies: purpose, concepts, copy-pasteable commands (`curl` for REST reads, `call_mcp` for MCP tools), procedure, pitfalls. No strategy content, no return claims, no emoji.
- Every write path says who may use it and under what approval. Every read says which surface and which symbol spelling.
- Snippets are self-contained: each includes its own environment and token loading so a Bot can copy one block and run it.
