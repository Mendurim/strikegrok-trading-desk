---
name: desk-incident-response
description: What the desk does when something goes wrong on Strike - unknown send results, unexpected fills or positions, unprotected positions, stuck or orphaned orders, API outages, rate limiting, and suspected API wallet compromise. Contain first, reconcile from the exchange record, act only through approved tickets, then review. Use the moment anything does not match the ticket.
license: MIT
metadata:
  version: "1.1.0"
  author: Galleon Labs
  category: desk
---

# Incident response

An incident is any moment when the exchange record and the desk's expectation disagree, or when the desk cannot see the exchange at all. The reflex is: stop adding, look, contain with approval, then review. Never widen the harm by improvising.

## Declare

Any Bot can declare an incident. Post on the floor:

```
INCIDENT INC-20260817-01 | 11:40 UTC | SG-20260817-02 | send timeout, result unknown | owner: Execution Trader
```

The Desk Lead confirms an owner (usually the Execution Trader for order/position incidents, the Risk Manager for exposure incidents, the Desk Lead for access incidents). New proposals pause until the incident is contained.

## Playbooks

### A. Unknown send result (timeout, 5xx, exception after send)

1. Do not resend.
2. `GET /v2/order --query client_order_id=<id>` first - the authoritative answer. Then corroborate: `GET /v2/openOrders`, `GET /v2/history/order` for the symbol and window, `GET /v2/history/fill` since the send, `GET /v2/positions`.
3. Found: continue reconciliation as normal; journal that the response was lost.
4. Not found by client order id, and corroborated: the send did not land. A replacement may go out with a **fresh** `client_order_id` and the user's approval by id. Never reuse the original id - that handle is the only thing that would tell the two sends apart if the first ever surfaced.

5. If the lookup itself is unavailable, the desk is blind rather than informed. Freeze sends on that symbol, report `unknown, lookup unavailable`, and wait for the API. Elapsed time proves nothing: Strike has no order expiry.
5. If the exchange later shows the original order after a second one was sent: the desk has double exposure. Go to playbook C.

### B. Rejected order or partial fill

- Rejection string in the response (`error`): quote it, map it via `strike-api-reference` (price precision, minimum notional, insufficient margin, reduce-only would increase position, invalid tif...). Fix is a new ticket, not a tweak-and-resend.
- Partial fill on an IOC: the unfilled remainder is gone and the position is smaller than the ticket. Any `normalTpsl` children were **never placed** (children appear only when the parent fills fully, or is partially filled and then margin-cancelled), so the filled part is unprotected: report the actual size and get a stop for that size placed at priority (playbook D).
- Partial fill on a resting order: normal; reconciliation continues.

### C. Position does not match expectation (too big, wrong side, unexpected market)

1. Read `GET /v2/account` and `GET /v2/history/fill` for the last hour; establish the exact state.
2. Risk Manager computes exposure and liquidation distance now.
3. If exposure breaches limits: an **emergency reduce ticket** (reduce-only IOC at a slippage bound for the excess size) is written by the Risk Manager, approved by the user by id, sent by the Execution Trader. Priority handling, same protocol.
4. Journal what happened and why the mismatch occurred once known.

### D. Unprotected position (no resting stop)

1. Risk Manager flags it; the Desk Lead treats it as priority.
2. Protective stop ticket (trigger, reduce-only, correct side, sized to the position read live) through Risk PASS and user approval. This is also the path after any partial fill of an entry that carried `normalTpsl` children.
3. If the user has pre-authorised protective stops in `desk.md`, the Execution Trader places it under that standing approval and journals it. This is the one standing approval the desk actively recommends the user grant at setup, on every network, because it can only ever reduce risk.
4. Without that pre-authorisation the desk cannot act, and an unprotected position does not become safe while it waits. Do not alert indefinitely: state the exposure and the distance to liquidation in the first alert, escalate on a deadline the user set in `desk.md` (fifteen minutes is a reasonable default), and when that passes, tell the user plainly that the fastest fix is theirs - close or protect the position in the Strike app - rather than continuing to ping a channel nobody is reading.

### E. Orphaned or stuck orders

- Orphaned (position flat, stop still resting): cancel ticket, or the standing approval for orphan clean-up if written in `desk.md`. Confirm from `GET /v2/openOrders`.
- Stuck (an order the exchange shows that the desk cannot cancel): read `GET /v2/history/order` and `GET /v2/order --query client_order_id=<id>`; check whether it is an exit leg of a strategy order, which Strike manages with the entry; if `DELETE /v2/order/cancel` still fails, report the exact response and stop. Do not cancel-all to sweep it away - that would remove protection on positions that are still open.

### F. Exchange unreachable, rate limited, or degraded

- Price Service failing or slow: mark the desk **blind**; no new tickets; watches log the outage; poll `https://api.strikefinance.org/price/v2/exchangeInfo` every minute; report when back. If the **signed** API is the one that is down, the desk cannot read its own account or look up an order either - that is the more serious outage, and no send happens until it clears.
- HTTP 429: back off (respect the response), reduce polling, prefer WebSocket for continuous data. Read `userRateLimit` for the account's remaining budget.
- Blind with open positions: the user is told plainly that stops resting **on the exchange** still work while the desk cannot see; that is why stops are mandatory.

### G. Suspected API wallet compromise or misuse

Signs: orders or fills the desk did not send, leverage changes nobody approved, order ids in `GET /v2/history/order` with no matching `client_order_id` in any proposal file.

1. The user deletes the API wallet immediately at `app.strikefinance.org/api-keys` - the desk cannot do this for them and must not delay them.
2. Once revoked, the desk's key is dead; the Execution Trader confirms sends fail.
3. Read the full `GET /v2/history/order` and `GET /v2/history/fill` since the last known-good time, and `GET /v2/history/transaction` for anything that moved; Risk Manager assesses exposure; emergency reduce or protection tickets as needed after the user registers a fresh API wallet and re-provisions it through the secure secret store.
4. Rotate: new key through the secure secret card only; nothing pasted in chat; journal the rotation time.
5. Incident review with a timeline.

Remember the shared computer: every Bot could read the key's environment; that is why only an API wallet key (trade-only, cannot withdraw) is ever provisioned, and why testnet comes first.

### H. Daily loss stop hit

Not an incident, but handled like one: Risk Manager posts it, `desk.md` gets `status: no-new-risk`, protective and exit tickets continue, new risk waits for the user's written reset.

## Containment rules

- Contain with the smallest reversible action. Reduce, don't flip. Cancel one, not all, unless the user has asked for a dead-man's switch and its condition has occurred.
- Every containment action is still a ticket with approval by id (or a pre-written standing approval in `desk.md`).
- Never withdraw, transfer or bridge as an incident response.
- Never resend to "make sure".

## Close the incident

The Trade Reviewer writes the incident review (`desk-post-trade-review`), the Desk Lead lifts the proposal pause and records the corrective action and its owner in `desk.md`.
