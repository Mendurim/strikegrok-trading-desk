# FAQ

**What do I actually get?**
Seven Bots in your Grok Bot workspace, one Trading Floor group chat, seventeen shared skills, a live zero-key Opening Bell and a written way of working. Ask for a market brief, a sized trade, a backtest or a review, and the right Bot answers with live data and sources.

**What do I need to start?**
Paste the setup prompt from the [README](../README.md) to any Grok Bot, then send it `Start the desk.` The desk starts in research mode and proves public market access without asking for a credential. Register a Strike API wallet only when you decide to trade. There is no useful practice mode in between: Strike's testnet lists four markets with empty books, so it proves plumbing and nothing else. The desk rehearses with dry runs and minimum-size live orders instead.

**What does the desk doctor inspect?**
The release version and tag pin, all agent and skill files, the desk folders and record, and a public Strike `allMids` response. It never reads environment variables, keys or account state, and never calls the exchange write endpoint.

**How does the desk trade?**
Through a Strike API (agent) wallet you create in the Strike app and hand over through Grok Bot's secure secret card. It can place, modify and cancel orders. Withdrawals, transfers and bridging stay in the app with your main wallet.

**How do I approve a trade?**
The Risk Manager posts a ticket with an id like `SG-20260816-01`. You type `approve SG-20260816-01`. That exact phrase, after seeing that exact ticket, is the desk's record that you agreed, and without it nothing is sent. It is deliberately not the only thing standing in the way: the Bots write the floor's messages, so the enforcing gate is Grok Bot's own Require Approval rule on the exchange write path, outside the conversation. Set it up during setup. No Bot may type, quote forward or infer your approval.

**Where do the ideas come from?**
From you. The Strategist turns your idea into rules and tests it on Strike history with fees, funding and an out-of-sample split; the desk then paper-trades it on testnet if you like. The Market and Research Analysts give you the picture; you decide.

**Why do the Bots use a "computer"?**
That is Grok Bot's name for the cloud VM every account gets. Reads are plain `curl` against Strike's public Price Service; the Execution Trader runs `scripts/strike_request.py` from there, which signs each call with the API wallet read from the secret store. The browser is only used for reading pages.

**Why six on the floor and one off it?**
Grok Bot group chats hold six Bots, and reviews are calmer after the noise. The Trade Reviewer works from its own conversation and by DM.

**Can I rename the Bots or add my own?**
Yes. Names and avatars are yours. To add a role, copy the shape of a file in `agents/`. Keep one writer: only one Bot has the exchange write skills.

**Can it run while I sleep?**
Routines can brief you, watch prices and funding, check the book and alert you. Sending always waits for your approval by id.

**Which network does market data come from?**
Mainnet, even when you trade on testnet, because testnet books are thin. Every number says which network it came from.

**What if a send times out?**
It never resends. It checks the exchange by client order id, and if the order is not there it still does not assume it failed, because a delayed send can land minutes later. Every send carries an expiry, so the desk waits for that deadline to pass, checks once more, and only then tells you the send is dead and asks for a fresh approval. You will be told which of those two happened.

**How do I set Grok Bot's own approvals?**
Settings, General, Auto-review: add a Require Approval rule for financial actions and for commands that call the Strike exchange endpoint. Require Approval wins over Always Allow.

**Does it work outside Grok Bot?**
Yes. Grok Build, Cursor and Claude Code load the same `agents/`, `skills/` and `rules/` as a plugin. In Claude Code, run `/plugin marketplace add Mendurim/strikegrok-trading-desk` then `/plugin install strikegrok@strikegrok`; in Grok Build and Cursor, open the repository and enable the plugin.

**Is this financial advice?**
It is documentation and instructions. Perpetual futures can liquidate an account.
