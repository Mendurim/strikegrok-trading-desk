#!/usr/bin/env bash
# Write the account's equity where the Bot user can read it, as the signer.
#
# Runs as strike-signer, every five minutes. Two jobs, both of them small:
#
#   * it performs a signed GET /v2/account, which is what captures the day's
#     start-of-day equity inside policy-state. The daily-loss stop refuses to
#     open exposure when that baseline is missing, and it is never re-based
#     from a balance taken after a loss - so this read is what opens the
#     trading day.
#   * it leaves the number in desk/equity.json for autopilot.py to size with
#     when a live read is not available. Autopilot refuses to size on a
#     snapshot older than ten minutes, so if this job stops, the desk stops
#     opening positions rather than sizing on yesterday's account.
#
# The API wallet is read from a file only this user can open. It is never
# pasted into this script, never printed, and never exported to the Bot user.
set -euo pipefail

DESK="${STRIKEGROK_DESK:-/workspace/trading-desk}"
REPO="${STRIKEGROK_REPO:-/workspace/strikegrok}"
WALLET="${STRIKEGROK_WALLET_ENV:-$HOME/.strikegrok/api-wallet.env}"   # chmod 600
NETWORK="${STRIKEGROK_NETWORK:-testnet}"

# shellcheck source=/dev/null
. "$WALLET"      # STRIKE_API_PUBLIC_KEY, STRIKE_API_PRIVATE_KEY

ARGS=(GET /v2/account)
[ "$NETWORK" = "testnet" ] && ARGS+=(--testnet)

OUT="$(python3 "$REPO/scripts/strike_request.py" "${ARGS[@]}")"

TMP="$(mktemp "$DESK/desk/.equity.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

printf '%s' "$OUT" | python3 -c '
import datetime, json, sys
KEYS = ("equity", "accountEquity", "totalEquity", "marginBalance",
        "totalMarginBalance", "accountValue")
payload = json.load(sys.stdin)
value = None
for layer in (payload, payload.get("data"), payload.get("result")):
    if isinstance(layer, dict):
        value = next((layer[k] for k in KEYS if layer.get(k) not in (None, "")), None)
        if value is not None:
            break
if value is None or float(value) <= 0:
    raise SystemExit("GET /v2/account returned no positive equity; snapshot not written")
json.dump({"equity": str(value),
           "at": datetime.datetime.now(datetime.timezone.utc)
                 .isoformat(timespec="seconds").replace("+00:00", "Z")},
          open(sys.argv[1], "w"))
' "$TMP"

chmod 644 "$TMP"
mv -f "$TMP" "$DESK/desk/equity.json"     # atomic: a half-written snapshot is never read
trap - EXIT
