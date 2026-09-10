---
name: strike-websocket
description: Live Strike Finance data over WebSocket - the public price stream (mark price, depth updates, trades, klines) and the authenticated user stream (order fills, position and balance updates) - plus how to run a watch process on the desk computer, keep a local order book in sync, supervise it with a heartbeat, and tell a dead feed apart from a quiet market. Use for monitoring, alert conditions and watching a live position. Read-only.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs (HyperGrok), ported for Strike Finance
  category: strike
  network-default: mainnet
---

# Strike WebSocket

Polling is fine for a brief. A watch that must notice something within seconds uses a stream.

| Stream | URL | Auth |
| --- | --- | --- |
| Public price | `wss://api.strikefinance.org/ws/price` | none |
| User | `wss://api.strikefinance.org/ws/user-api` | yes |

The public stream carries mark price, depth updates, trades and klines. The user stream carries order fills, position and balance updates for the account.

## 1. The rule that matters most

**Silence is not information.** A watch reporting "no alert" on a socket that died two hours ago looks exactly like a calm market. Every watch on this desk must be able to tell the two apart, and must say which it has.

Concretely, a watch process:

- Records the UTC time of the **last message of any kind**, not just the last matching one.
- Has a staleness threshold. Past it, the watch reports `unavailable`, not "condition did not fire".
- Reconnects with backoff, and logs every disconnect and reconnect with times.
- On reconnect, re-reads state from REST or the MCP rather than assuming nothing happened while it was away.

A watch that cannot meet those four is not run.

## 2. Keeping a local order book in sync

`/v2/depth` gives the snapshot; the stream gives deltas. The documented procedure:

1. Fetch `/v2/depth?symbol=BTC-USD&limit=1000` and record `lastUpdateId`.
2. Connect and subscribe to `{symbol}@depth`.
3. Drop any `depthUpdate` whose `u <= lastUpdateId`.
4. Apply everything after that.

Per-symbol `+1` continuity between consecutive `u` values is **not** guaranteed - the engine skips ids when the book does not change. A gap is not a missed update and is not a reason to resynchronise. A genuinely out-of-order or backwards `u` is.

`lastUpdateId`, `U` and `u` can exceed 2^53. In Python that is free; in JavaScript, parse them as strings or BigInt or they will silently round.

## 3. A watch process on the desk computer

Watches live in `/workspace/trading-desk/watch/`, one directory per watch, each with the script, a log and the condition in plain language.

```bash
mkdir -p /workspace/trading-desk/watch/ada-funding
cd /workspace/trading-desk/watch/ada-funding
# Write the condition down before the code, in the words the user used.
cat > CONDITION.md <<'TXT'
Alert when ADA-USD hourly funding turns negative.
Checked every 5 minutes against /v2/premiumIndex.
Stale after 15 minutes without a successful read -> report unavailable.
TXT
```

For a five-minute condition, poll REST on a loop and skip the socket entirely - it is less to supervise and less to get wrong. Reach for the stream when the condition is genuinely sub-minute, or when the desk needs fills the moment they happen.

Run watches with `nohup` or the runtime's own background mechanism, write a log with UTC timestamps, and record the watch in `desk.md` so the desk knows what is running. A watch nobody remembers starting is a watch nobody notices stopping.

## 4. Alerts

An alert states: what fired, the value that fired it, the threshold, the UTC time, and the source. It never states what to do about it - that is the floor's job, through the lifecycle.

`discord_send_message` (see `strike-research-tools`) pushes an alert to the user's phone when they have configured a webhook. Keep it under 2000 characters, never include the token or an account identifier, and only for alerts the user asked for.

## 5. The user stream and fills

The authenticated stream is the fastest way to know a fill happened, but it is **not** the desk's record of what happened. Reconciliation always reads back from the MCP - `strike_get_fill_history`, `strike_get_order_history`, `strike_get_open_positions`. A stream message is a prompt to go and check, never the check itself.

## 6. Supervision

Each watch reports, on request: uptime, last message time, reconnect count, and whether it currently considers itself live or stale. The Desk Lead asks for that before treating any watch's silence as meaningful. A watch that cannot answer is presumed dead and restarted.
