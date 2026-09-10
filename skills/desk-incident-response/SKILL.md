---
name: desk-incident-response
description: What the desk does when something goes wrong - a send whose result is unknown, a rejected order, a position that does not match the ticket, a position with no stop, orphaned or stuck orders, an outage or rate limiting, and a suspected compromised API wallet. Contain with the smallest reversible action, rebuild the truth from the exchange record, act only through approved tickets, then review. Use the moment anything disagrees with the ticket.
license: MIT
metadata:
  version: "3.0.0"
  author: Mendurim
  category: desk
---

# Incident response

An incident is any moment when the exchange record and the desk's expectation disagree, or when the desk cannot see the exchange at all. The order of operations never changes: stop adding, look, contain with approval, review. Improvising is how a small problem becomes a large one.

## Declaring one

Any Bot can declare. Post on the floor:

```
INCIDENT INC-20260911-01 | 11:40 UTC | SG-20260911-02 | unknown send result | owner: Execution Trader
```

The Desk Lead confirms the owner — the Execution Trader for anything about orders or positions, the Risk Manager for exposure, the Desk Lead for access and outages. New proposals pause until it is contained.

## A. A send whose result you do not know

A timeout, a reset connection, a 5xx, or an exception after the request left the machine.

1. **Do not resend.**
2. Ask the exchange about the exact order you sent: `GET /v2/order --query client_order_id=<id>`. This is why the id is chosen and written down *before* the send — the recovery is a lookup, not a deduction.
3. **Found.** Carry on with reconciliation as normal and journal that the response was lost.
4. **Not found.** Corroborate before believing it: `GET /v2/openOrders`, `GET /v2/history/order` for the symbol and window, `GET /v2/history/fill`, `GET /v2/positions`. A single read from a service that just timed out deserves a second opinion. Once corroborated, the send did not land, and a replacement may go out — with a **fresh** client order id and the user's approval by id. Never reuse the original: that handle is the only thing that would distinguish the two if the first ever surfaced.
5. **The lookup itself is unavailable.** Then the desk is blind, not informed. Freeze sends on that symbol, report `unknown, lookup unavailable`, and wait for the API. Elapsed time settles nothing — Strike has no order expiry, so an order cannot age into safety.
6. **The original appears after a replacement went out.** The desk now has double exposure. Go to playbook C.

## B. A rejection, or a partial fill

**Rejection.** Quote the exchange's error verbatim and map it through `strike-api-reference` — price precision, minimum notional, insufficient margin, a reduce-only order that would increase the position, an unsupported time-in-force. The fix is a new ticket, never a tweak and an immediate resend.

**Partial fill on an IOC or FOK.** The unfilled remainder is gone and the position is smaller than the ticket. Report the actual size, and make sure protection matches it rather than the intended size.

**Partial fill on a resting order.** Ordinary. Reconciliation continues, but if the entry carried attached exit legs, confirm they are sized to what actually filled.

**A strategy order whose entry filled but whose exits are not resting.** That is an unprotected position. Playbook D, immediately.

## C. The position is not what the ticket said

Too large, wrong side, or in a market nobody approved.

1. Establish the exact state: `GET /v2/positions` and `GET /v2/history/fill` for the last hour.
2. The Risk Manager computes current exposure and the distance to liquidation.
3. If it breaches a limit, the Risk Manager writes an **emergency reduce ticket** — reduce-only, market, with a slippage bound, sized to the excess. The user approves it by id and the Execution Trader sends it. Priority handling, identical protocol.
4. Journal what happened, and why, once that is known.

## D. A position with no stop

The most urgent state on the desk.

1. The Risk Manager flags it and the Desk Lead treats it as priority.
2. A protective stop ticket — trigger, reduce-only, correct side, sized to the position as read live — goes through PASS and approval.
3. If the user pre-authorised reduce-only protection in `desk.md`, the Execution Trader places it under that standing approval and journals it. This is the one standing approval worth recommending at setup, precisely because it cannot do anything but reduce risk.
4. Without that authorisation the desk cannot act, and the position does not become safer while it waits. So do not simply keep pinging. The first alert carries the exposure and the distance to liquidation; escalation follows the deadline in `desk.md`, fifteen minutes being a sensible default; and when that passes, tell the user plainly that the quickest fix is theirs — close or protect it in the Strike app — rather than alerting into a channel nobody is reading.

## E. Orphaned or stuck orders

**Orphaned** — the position is flat but a stop is still resting. This matters more than it sounds: a trigger left behind can *open* a fresh position if it fires. Cancel it under a ticket, or under a standing approval for orphan clean-up if the user wrote one. Confirm from `GET /v2/openOrders`.

**Stuck** — the exchange shows an order the desk cannot cancel. Read `GET /v2/history/order` and look it up by client order id. Check whether it is an exit leg of a strategy order, which Strike manages together with its entry. If `DELETE /v2/order/cancel` still fails, report the exact response and stop. Do not reach for cancel-all to sweep it away: unscoped, that strips protection from every open position.

## F. The exchange is unreachable, slow, or rate limiting

- **Price Service down.** The desk is **blind**: no new tickets, watches log the gap, poll `/v2/exchangeInfo` every minute, announce when it returns.
- **The signed API down.** More serious — the desk can neither read its own account nor look up an order. Nothing sends until it clears.
- **HTTP 429.** Back off as the response directs, cut polling frequency, and move continuous data to the WebSocket feeds. The published budget is 2400 request weight and 1200 orders per minute.
- **Blind while holding positions.** Tell the user plainly: stops resting **on the exchange** keep working while the desk cannot see. That is the whole reason stops are mandatory rather than monitored.

## G. Suspected compromise or misuse of the API wallet

Signs: orders or fills the desk did not send, a leverage change nobody approved, order ids in `GET /v2/history/order` with no matching client order id in any proposal.

1. The user deletes the key at `app.strikefinance.org/api-keys` **immediately**. The desk cannot do this for them and must not slow them down.
2. Once deleted, the desk's credential is dead. The Execution Trader confirms that sends now fail.
3. Read the whole record since the last known-good time — `GET /v2/history/order`, `GET /v2/history/fill`, and `GET /v2/history/transaction` for anything that moved. The Risk Manager assesses exposure. Emergency reduce or protection tickets follow once the user has registered a fresh key.
4. Rotate properly: new keypair, public half registered, both halves into the secure secret store, nothing pasted into chat. Journal the rotation time, never the key.
5. Incident review with a full timeline.

Remember that every Bot shares one computer, so any credential on it is readable by all of them. That is why the only credential provisioned is an API wallet that can trade and cannot withdraw.

## H. The daily loss stop is hit

Not strictly an incident, but handled like one. The Risk Manager posts it, `desk.md` gets `status: no-new-risk`, exits and protective tickets continue, and new risk waits for the user's written reset.

## Containment rules

- Use the smallest reversible action. Reduce rather than flip. Cancel one order, not all of them.
- Every containment action is still a ticket with approval by id, or a standing approval already written in `desk.md`.
- Never withdraw, transfer or bridge as a response to anything.
- Never resend to "make sure".
- Never let an incident become a reason to skip a stage. The lifecycle is what is keeping this from getting worse.

## Closing it

The Trade Reviewer writes the incident review. The Desk Lead lifts the pause on proposals and records the corrective action, its owner and its date in `desk.md`.
