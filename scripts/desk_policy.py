#!/usr/bin/env python3
"""desk_policy.py - the gate that lives in code, not in a prompt.

Called by scripts/strike_request.py around every non-GET request:

    from desk_policy import Policy, PolicyRefusal
    pol = Policy(account_reader=..., positions_reader=..., fills_reader=...)
    pol.check(method, path, body)   # raises PolicyRefusal, or returns an allow record
    pol.commit(body)                # after the exchange answered: consumes the id, takes the slot

TRUST MODEL - the layer believes only three things:

  1. files signed with the user's Ed25519 key, whose private half never touches
     the desk computer: the standing-approval register and per-trade tokens
  2. live exchange reads it performs itself through the supplied readers
  3. its own state directory, which must be owned by the user running this
     script and writable by nobody else

Every Bot shares one filesystem, so nothing a Bot can write may turn a refusal
into an allow. Bot-written files may only ever TIGHTEN: a Bot may suspend a
standing approval, never lift one, and deleting the file it suspended through
changes nothing, because suspensions are ingested one-way into state and lifted
only by a register the user re-signed with a higher version.

Two modes, set by whether desk/user-signing.pub exists:

  attended    no key installed. The platform's Require Approval rule and the
              user's eyes are the approval. Every shape check below still runs.
  unattended  key installed. Tier 1 and Tier 2 signatures are required.

Tiers
  0  reduce-only, cancels, and adding margin: free
  1  opening exposure under a signed standing approval
  2  opening exposure, or a leverage/margin-mode/margin-removal change, under a
     signed per-trade token

CLI:
  desk_policy.py verify | keygen --out P | sign --key K --file F
  desk_policy.py suspend SA-NN --reason R --by WHO
  desk_policy.py explain BODY.json [--path P] [--equity N]

Standard library only, except Ed25519 verification, which uses `cryptography`.
If it is missing, Tier 1 and Tier 2 refuse rather than downgrade.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey)
    from cryptography.hazmat.primitives import serialization
    from cryptography.exceptions import InvalidSignature
    HAVE_ED25519 = True
except Exception:  # pragma: no cover - only on a desk without the package
    HAVE_ED25519 = False

# ---------------------------------------------------------------------------
# Hard ceilings. The environment may only TIGHTEN these. They sit above
# risk-limits.md, which sits above any SA. The tightest wins.
# ---------------------------------------------------------------------------
CEILING_MAX_NOTIONAL_PER_ORDER_USD = 5_000.0
CEILING_MAX_NOTIONAL_EQUITY_MULT = 1.0         # one order may not exceed the account
CEILING_MAX_BAR_SECONDS = 86_400               # an SA's bar may not be longer than a day
CEILING_MAX_LEVERAGE = 5
CEILING_MAX_DAILY_LOSS_FRACTION = 0.03
CEILING_MAX_OPEN_PER_SA = 3
CEILING_MAX_OPEN_POSITIONS = 3                 # across every SA and the user's own
CEILING_MAX_SA_RISK_PER_TRADE = 0.005
CEILING_SA_MAX_LIFETIME_DAYS = 31
CEILING_MAX_CONSECUTIVE_LOSSES = 5             # an SA may set fewer, never more
CEILING_PASS_EQUITY_DRIFT = 0.02               # PASS block equity vs the live read
FLOOR_SECONDS_BETWEEN_OPENS = 60               # a runaway monitor cannot machine-gun the book

def effective_ceilings() -> dict:
    """What the ceilings actually are after any environment tightening. The desk
    doctor and `verify` print this, so nobody has to read the source to find out
    what the box will refuse."""
    return {
        "MAX_NOTIONAL_PER_ORDER_USD": _ceiling("MAX_NOTIONAL_PER_ORDER_USD", CEILING_MAX_NOTIONAL_PER_ORDER_USD),
        "MAX_NOTIONAL_EQUITY_MULT": _ceiling("MAX_NOTIONAL_EQUITY_MULT", CEILING_MAX_NOTIONAL_EQUITY_MULT),
        "MAX_LEVERAGE": _ceiling("MAX_LEVERAGE", CEILING_MAX_LEVERAGE),
        "MAX_DAILY_LOSS_FRACTION": _ceiling("MAX_DAILY_LOSS_FRACTION", CEILING_MAX_DAILY_LOSS_FRACTION),
        "MAX_OPEN_PER_SA": _ceiling("MAX_OPEN_PER_SA", CEILING_MAX_OPEN_PER_SA),
        "MAX_OPEN_POSITIONS": _ceiling("MAX_OPEN_POSITIONS", CEILING_MAX_OPEN_POSITIONS),
        "MAX_SA_RISK_PER_TRADE": _ceiling("MAX_SA_RISK_PER_TRADE", CEILING_MAX_SA_RISK_PER_TRADE),
        "SA_MAX_LIFETIME_DAYS": _ceiling("SA_MAX_LIFETIME_DAYS", CEILING_SA_MAX_LIFETIME_DAYS),
        "MAX_CONSECUTIVE_LOSSES": _ceiling("MAX_CONSECUTIVE_LOSSES", CEILING_MAX_CONSECUTIVE_LOSSES),
        "MAX_BAR_SECONDS": _ceiling("MAX_BAR_SECONDS", CEILING_MAX_BAR_SECONDS),
        "SECONDS_BETWEEN_OPENS": _floor("SECONDS_BETWEEN_OPENS", FLOOR_SECONDS_BETWEEN_OPENS),
    }


DESK = Path(os.environ.get("STRIKEGROK_DESK", "/workspace/trading-desk"))

ORDER_PATHS = {"/v2/order", "/v2/order/strategy", "/v2/order/batch", "/v2/algo/twap"}
CANCEL_PATHS = {"/v2/order/cancel", "/v2/order/cancelAll", "/v2/order/batch/cancel", "/v2/algo/twap/cancel"}
CONFIG_PATHS = {"/v2/leverage", "/v2/marginType", "/v2/positionMargin"}
FORBIDDEN_FRAGMENTS = ("withdraw", "deposit", "transfer", "vault", "bridge")

# An amendment suffix is ONE uppercase letter that ends the id or is followed by
# another dash. Without the lookahead the leg suffixes the Execution Trader
# actually writes are misread: "-Entry" becomes amendment E and "-TP" becomes
# amendment T, both pointing at ticket blocks nobody wrote.
TICKET_RE = re.compile(r"(SG-\d{8}-\d{2})(-[A-Z])?(?=-|$)")
RISK_HDR_RE = re.compile(r"^RISK \| (SG-\d{8}-\d{2}(?:-[A-Z])?) \| (PASS|REJECT)\b.*$", re.M)
FIELD_RE = re.compile(r"^\s*(\w+):\s*(.+?)\s*$", re.M)
SA_ID_RE = re.compile(r"SA-\d{2,}")


class PolicyRefusal(Exception):
    """The request may not be signed. The message names the step that failed."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(ts: Any, what: str) -> dt.datetime:
    """Parse a timestamp that MUST carry a timezone. A naive one is a refusal,
    not a silent local-time reading that could slip through a freshness bound."""
    try:
        parsed = dt.datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise PolicyRefusal(f"{what}: '{ts}' is not an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        raise PolicyRefusal(f"{what}: '{ts}' has no timezone; use a trailing Z")
    return parsed.astimezone(dt.timezone.utc)


def _date(value: Any, what: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value).strip())
    except (ValueError, AttributeError):
        raise PolicyRefusal(f"{what}: '{value}' is not a YYYY-MM-DD date")


def _ceiling(name: str, default: float) -> float:
    """An environment override may only tighten a ceiling."""
    raw = os.environ.get(f"STRIKEGROK_{name}")
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    if val != val:                  # NaN compares false against every bound
        return default
    return min(val, default)


def _floor(name: str, default: float) -> float:
    """The mirror of _ceiling where LARGER is tighter, such as the minimum gap
    between two opening orders."""
    raw = os.environ.get(f"STRIKEGROK_{name}")
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    if val != val:
        return default
    return max(val, default)


def _num(value: Any, what: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise PolicyRefusal(f"{what}: '{value}' is not a number")
    if out != out or out in (float("inf"), float("-inf")):
        raise PolicyRefusal(f"{what}: '{value}' is not finite")
    return out


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _truthy(v: Any) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# signatures
# ---------------------------------------------------------------------------
def _load_pub(path: Path) -> "Ed25519PublicKey":
    if not HAVE_ED25519:
        raise PolicyRefusal("signature: cryptography library unavailable; Tier 1/2 disabled")
    if not path.exists():
        raise PolicyRefusal(f"signature: public key missing at {path}")
    try:
        key = serialization.load_pem_public_key(path.read_bytes())
    except (ValueError, TypeError, OSError) as exc:
        raise PolicyRefusal(f"signature: public key at {path.name} will not load ({exc})")
    if not isinstance(key, Ed25519PublicKey):
        raise PolicyRefusal("signature: public key is not Ed25519")
    return key


def verify_signed_file(data_path: Path, pub_path: Path) -> dict:
    """Return parsed JSON only if data_path + '.sig' verifies under pub_path.

    Every failure mode lands as a PolicyRefusal so the caller logs it and stops.
    Nothing here may raise a bare exception past check(), which is what keeps a
    malformed input from ever resembling an allow.
    """
    sig_path = Path(str(data_path) + ".sig")
    if not data_path.exists():
        raise PolicyRefusal(f"signature: {data_path.name} is missing")
    if not sig_path.exists():
        raise PolicyRefusal(f"signature: {sig_path.name} is missing")
    pub = _load_pub(pub_path)
    try:
        raw = base64.b64decode(sig_path.read_text().strip(), validate=True)
    except (binascii.Error, ValueError, OSError) as exc:
        raise PolicyRefusal(f"signature: {sig_path.name} is not valid base64 ({exc})")
    try:
        pub.verify(raw, data_path.read_bytes())
    except InvalidSignature:
        raise PolicyRefusal(f"signature: {data_path.name} does not verify under the user's key")
    except Exception as exc:
        raise PolicyRefusal(f"signature: {data_path.name} could not be verified ({exc})")
    try:
        return json.loads(data_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise PolicyRefusal(f"signature: {data_path.name} is not readable JSON ({exc})")


def keygen(out: Path) -> None:
    if not HAVE_ED25519:
        raise SystemExit("cryptography library required for keygen")
    priv = Ed25519PrivateKey.generate()
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(priv.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
    Path(str(out) + ".pub").write_bytes(priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    print(f"private key {out} (KEEP OFF THE DESK COMPUTER)\npublic key  {out}.pub")


def sign_file(key: Path, target: Path) -> None:
    if not HAVE_ED25519:
        raise SystemExit("cryptography library required for sign")
    priv = serialization.load_pem_private_key(key.read_bytes(), password=None)
    Path(str(target) + ".sig").write_text(
        base64.b64encode(priv.sign(target.read_bytes())).decode() + "\n")
    print(f"signed {target} -> {target}.sig")


# ---------------------------------------------------------------------------
# Proposal parsing, anchored to this ticket's own PASS block.
#
# A proposal holds one or more RISK blocks. Each begins with its verdict line
# and the fields it governs follow beneath it:
#
#     RISK | SG-20260912-03 | PASS | SA-03 | 2026-09-12T12:01:00Z
#     fields
#       symbol: ETH-USD
#       ...
#
# The LAST block for the requested ticket wins and must be a PASS. Scraping the
# whole file instead would let a rejected amendment supply the fields for an
# approved ticket, and let a later block silently overwrite an earlier one.
# ---------------------------------------------------------------------------
def ticket_from_coid(coid: str) -> tuple[str, str]:
    """Return (base id, ticket id): ('SG-20260912-03', 'SG-20260912-03-B')."""
    m = TICKET_RE.search(coid or "")
    if not m:
        raise PolicyRefusal("proposal: client_order_id does not carry an SG- ticket id")
    return m.group(1), m.group(1) + (m.group(2) or "")


def pass_block_fields(text: str, ticket_id: str) -> dict:
    headers = [(m.start(), m.group(1), m.group(2)) for m in RISK_HDR_RE.finditer(text)]
    mine = [(start, verdict) for start, tid, verdict in headers if tid == ticket_id]
    if not mine:
        raise PolicyRefusal(f"proposal: no RISK block for ticket {ticket_id}")
    start, verdict = mine[-1]
    if verdict != "PASS":
        raise PolicyRefusal(f"proposal: the latest RISK block for {ticket_id} is {verdict}")
    later = [s for s, _, _ in headers if s > start]
    block = text[start:min(later) if later else len(text)]
    heading = re.search(r"^#{1,6} ", block[1:], re.M)
    if heading:
        block = block[:heading.start() + 1]
    fields = {k.lower(): v for k, v in FIELD_RE.findall(block)}
    fields["_ticket"] = ticket_id
    return fields


# ---------------------------------------------------------------------------
# The state directory: owned by the user running the signer, not by the Bots.
#
# Bots invoke this script; they do not own its files. A state directory anyone
# else can write to is not state, so Tier 1 and Tier 2 refuse until it is fixed.
# Every read-modify-write is under an exclusive flock, because two Bots sending
# at the same moment would otherwise both see a free slot.
# ---------------------------------------------------------------------------
class State:
    FILES = ("suspended.json", "consumed.json", "sa_open.json", "sod_equity.json",
             "pace.json", "incidents.json", "blackouts.json")

    def __init__(self, root: Path):
        self.root = root
        self.lock_path = root / ".lock"
        self._lock = None

    def ensure_secure(self) -> None:
        if not self.root.exists():
            self.root.mkdir(parents=True, mode=0o700)
        info = self.root.stat()
        if info.st_uid != os.geteuid():
            raise PolicyRefusal(
                f"state: {self.root} is not owned by the policy user (uid {os.geteuid()})")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PolicyRefusal(f"state: {self.root} is writable by group or other; refusing Tier 1/2")
        for name in self.FILES:
            path = self.root / name
            if not path.exists():
                try:
                    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    continue          # another process created it under the same lock rules
                with os.fdopen(fd, "w") as handle:
                    handle.write("{}\n")
            elif path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise PolicyRefusal(f"state: {name} is writable by group or other")

    def __enter__(self) -> "State":
        self.ensure_secure()
        self._lock = open(self.lock_path, "w")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc) -> None:
        if self._lock is not None:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    def load(self, name: str) -> dict:
        try:
            return json.loads((self.root / name).read_text() or "{}")
        except (json.JSONDecodeError, OSError) as exc:
            raise PolicyRefusal(f"state: {name} is unreadable ({exc})")

    def save(self, name: str, data: dict) -> None:
        tmp = self.root / (name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(self.root / name)


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------
class Policy:
    def __init__(self,
                 account_reader: Optional[Callable[[], float]] = None,
                 positions_reader: Optional[Callable[[], list]] = None,
                 fills_reader: Optional[Callable[[str, int], list]] = None,
                 open_orders_reader: Optional[Callable[[], list]] = None,
                 order_lookup_reader: Optional[Callable[[str], bool]] = None,
                 desk: Optional[Path] = None):
        """account_reader()          -> account equity, from a signed GET /v2/account
           positions_reader()        -> [{symbol, size}, ...] from GET /v2/positions
           fills_reader(sym, n)      -> the last n closed PnLs for a symbol, newest first
           open_orders_reader()      -> [{symbol, client_order_id, reduce_only}, ...] resting
           order_lookup_reader(coid) -> True if the venue already knows this id

        All are performed by strike_request.py, which holds the API key. Without
        account_reader the daily-loss stop cannot be measured against anything the
        Bots cannot rewrite, so opening exposure refuses.

        On a single-user box the state directory is bookkeeping, not a boundary:
        the Bots run as the same OS user as this script, so they can edit it. The
        venue readers are what survive that, and Tier 1 additionally refuses
        unless the operator sets STRIKEGROK_STATE_TRUSTED=1 to assert that this
        process really does run as its own user. Without it, keep the platform's
        Require Approval rule on.
        """
        self.open_orders_reader = open_orders_reader
        self.order_lookup_reader = order_lookup_reader
        self.state_trusted = os.environ.get("STRIKEGROK_STATE_TRUSTED") == "1"
        self.desk = desk or DESK
        self.account_reader = account_reader
        self.positions_reader = positions_reader
        self.fills_reader = fills_reader
        self.pub = self.desk / "desk" / "user-signing.pub"
        self.register = self.desk / "desk" / "standing-approvals.json"
        self.bot_suspend_file = self.desk / "desk" / "standing-approvals.suspended.json"
        self.state_dir = Path(os.environ.get(
            "STRIKEGROK_POLICY_STATE", str(self.desk / "desk" / "policy-state")))
        self.log_path = self.state_dir / "policy.log"
        self._pending: Optional[dict] = None

    def mode(self) -> str:
        """`unattended` once the user's public key is on the desk, `attended`
        before that.

        Attended is the desk as it shipped: the platform's Require Approval rule
        and the user's own eyes are the approval, and this module still enforces
        the ceilings, the forbidden surfaces, the leverage and margin rules, the
        protection requirement and ticket equality. Installing a public key is
        what switches modes, so nothing here breaks a desk that has not opted in.
        """
        return "unattended" if self.pub.exists() else "attended"

    # -- logging -------------------------------------------------------------
    def _log(self, decision: str, tier: str, detail: str, body: Any) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            coid = body.get("client_order_id", "-") if isinstance(body, dict) else "-"
            with self.log_path.open("a") as handle:
                handle.write(f"{_now().isoformat()} {decision:6} {tier:6} {coid} {detail}\n")
            os.chmod(self.log_path, 0o600)
        except OSError:
            # A desk that cannot record what it did must not act. This is the one
            # place a logging failure is louder than the thing being logged.
            raise PolicyRefusal("log: policy.log is not writable; refusing to act unrecorded")

    # -- public API ----------------------------------------------------------
    def check(self, method: str, path: str, body: Optional[dict]) -> dict:
        body = body if isinstance(body, dict) else {}
        if str(method).upper() == "GET":
            self._capture_sod_if_new_day()
            return {"allow": True, "tier": "read", "why": "read-only"}
        try:
            record = self._check(str(path), body)
        except PolicyRefusal as exc:
            self._pending = None
            self._log("REFUSE", "-", str(exc), body)
            raise
        except Exception as exc:
            # Nothing unexpected may reach the signer looking like an allow.
            self._pending = None
            self._log("REFUSE", "-", f"internal error: {exc!r}", body)
            raise PolicyRefusal(f"internal: policy check raised {exc!r}; refusing") from None
        self._log("ALLOW", record["tier"], record.get("why", ""), body)
        return record

    def commit(self, body: Optional[dict] = None, accepted: bool = True) -> None:
        """Call once the venue has SEEN the request - on 2xx and on 4xx alike.

        The slot and the client order id were already taken at check time, under
        the lock that granted them; that is what stops two Bots racing through the
        same free slot. This records what became of the reservation:

          accepted=True   the order is working. The slot stays occupied.
          accepted=False  the venue rejected it. The id stays spent, because the
                          venue has now seen it, but the slot is freed - nothing
                          opened, and holding it would strand the approval.

        A send that is never committed keeps both. That is deliberate: after a
        timeout the desk cannot tell whether the order landed, and the honest
        assumption is that it did. Use release_pending() only when the transport
        can prove the venue never saw the request.
        """
        pending = self._pending
        self._pending = None
        if not pending:
            return
        with State(self.state_dir) as st:
            sa_open = st.load("sa_open.json")
            slots = sa_open.get(pending.get("sa") or "", {})
            slot = slots.get(pending["coid"])
            if slot is not None:
                if accepted:
                    slot["state"] = "sent"
                    slot["confirmed_at"] = _now().isoformat()
                else:
                    slots.pop(pending["coid"], None)
                st.save("sa_open.json", sa_open)

    def release_pending(self) -> None:
        """The transport died before the venue saw anything: give the reservation
        back so the same ticket can be retried.

        Only the sender may call this, and only when it knows nothing was
        transmitted - a connection that was refused or a DNS failure, not a
        timeout. A timeout is an unknown result and keeps its reservation.
        """
        pending = self._pending
        self._pending = None
        if not pending:
            return
        with State(self.state_dir) as st:
            consumed = st.load("consumed.json")
            consumed.get("coids", {}).pop(pending["coid"], None)
            if pending.get("token"):
                consumed.get("tokens", {}).pop(pending["token"], None)
            st.save("consumed.json", consumed)
            if pending.get("sa"):
                sa_open = st.load("sa_open.json")
                sa_open.get(pending["sa"], {}).pop(pending["coid"], None)
                st.save("sa_open.json", sa_open)

    def release(self, sa_id: str, coid: str) -> None:
        """Free a standing-approval slot once the position behind it is gone."""
        with State(self.state_dir) as st:
            sa_open = st.load("sa_open.json")
            sa_open.get(sa_id, {}).pop(coid, None)
            st.save("sa_open.json", sa_open)

    # -- core ----------------------------------------------------------------
    def _check(self, path: str, body: dict) -> dict:
        low = path.lower()
        if any(fragment in low for fragment in FORBIDDEN_FRAGMENTS):
            raise PolicyRefusal(f"ceiling: {path} moves funds and is outside the desk's scope")
        if "vault_id" in body:
            raise PolicyRefusal("ceiling: vault_id is never passed")

        # Tier 0 - can only shrink exposure.
        if path in CANCEL_PATHS:
            if path == "/v2/order/cancelAll" and not body.get("symbol"):
                raise PolicyRefusal("tier0: an unscoped cancelAll removes protection; pass a symbol")
            return {"allow": True, "tier": "tier0", "why": "cancel"}
        if path in ORDER_PATHS and self._is_reduce_only(path, body):
            return {"allow": True, "tier": "tier0", "why": "reduce-only"}
        if path == "/v2/positionMargin" and self._is_margin_add(body):
            return {"allow": True, "tier": "tier0", "why": "adding margin can only reduce liquidation risk"}
        if path == "/v2/order/batch":
            raise PolicyRefusal(
                "batch: a batch that opens exposure is refused; send opening orders one "
                "ticket at a time so each carries its own approval")
        if path not in ORDER_PATHS and path not in CONFIG_PATHS:
            raise PolicyRefusal(f"ceiling: write path {path} is not in the desk's allow-list")

        # ---- everything below can increase exposure or liquidation risk ----
        with State(self.state_dir) as st:
            self._daily_loss_ok(st)
            self._incident_ok(st)

            base_id, ticket_id = ticket_from_coid(body.get("client_order_id", ""))
            consumed = st.load("consumed.json")
            coid = body.get("client_order_id")
            if coid in consumed.get("coids", {}):
                raise PolicyRefusal(
                    f"replay: client_order_id {coid} has already been sent. Look the order up "
                    "rather than resending; a replacement needs a fresh id and a fresh approval.")
            # State is Bot-writable on a single-user box, so the venue is asked too:
            # wiping consumed.json must not hand back a spent id.
            if self.order_lookup_reader and coid:
                try:
                    known = bool(self.order_lookup_reader(coid))
                except Exception as exc:
                    raise PolicyRefusal(f"replay: could not ask the venue about {coid} ({exc})")
                if known:
                    raise PolicyRefusal(f"replay: the venue already knows client_order_id {coid}")

            proposal = self.desk / "proposals" / f"{base_id}.md"
            if not proposal.exists():
                raise PolicyRefusal(f"proposal: {proposal} is missing")
            try:
                f = pass_block_fields(proposal.read_text(), ticket_id)
            except OSError as exc:
                raise PolicyRefusal(f"proposal: {proposal.name} is unreadable ({exc})")
            self._ticket_not_expired(f)

            if path in ORDER_PATHS:
                kind, notional = self._match_ticket(f, body, path)
                cap = _ceiling("MAX_NOTIONAL_PER_ORDER_USD", CEILING_MAX_NOTIONAL_PER_ORDER_USD)
                if notional > cap:
                    raise PolicyRefusal(f"ceiling: notional ${notional:,.0f} > ${cap:,.0f}")
                # A flat cap means nothing on a small account. One order may not be
                # worth more than the whole account is.
                mult = _ceiling("MAX_NOTIONAL_EQUITY_MULT", CEILING_MAX_NOTIONAL_EQUITY_MULT)
                equity_cap = self._equity() * mult
                if notional > equity_cap:
                    raise PolicyRefusal(
                        f"ceiling: notional ${notional:,.0f} > {mult:g}x equity (${equity_cap:,.0f})")
                self._check_protection(path, body, f, kind)
                self._blackout_ok(st, str(body.get("symbol")))
                self._book_has_room()
            else:
                kind, notional = "config", 0.0
                self._config_match(f, body, path)

            if self.mode() == "attended":
                if f.get("sa"):
                    raise PolicyRefusal(
                        f"SA: {ticket_id} claims {f['sa']} but this desk has no "
                        "desk/user-signing.pub, so no standing approval can be verified; run "
                        "desk_policy.py keygen on your own machine and install the public key")
                record = {"allow": True, "tier": "attended", "ticket": ticket_id,
                          "why": "ticket matches a Risk PASS; approval is the platform gate and the user"}
            elif f.get("sa"):
                record = self._check_sa(st, f, body, ticket_id, notional, path)
            else:
                record = self._check_token(st, f, body, ticket_id, consumed)

            # Last, so a ticket that is wrong is told what is wrong with it rather
            # than being told to slow down.
            self._pace(st)

            # Take everything now, under the lock that just granted it. Deferring
            # this to commit() leaves a window the width of one network round
            # trip in which every concurrent Bot sees the same free slot - six
            # processes went through a max_open of 1 that way.
            self._reserve(st, body, record)
            self._pending = {"coid": body.get("client_order_id"), "sa": record.get("sa"),
                             "token": record.get("token"), "symbol": body.get("symbol")}
            return record

    def _reserve(self, st: State, body: dict, record: dict) -> None:
        coid = body.get("client_order_id")
        consumed = st.load("consumed.json")
        consumed.setdefault("coids", {})[coid] = _now().isoformat()
        if record.get("token"):
            consumed.setdefault("tokens", {})[record["token"]] = _now().isoformat()
        st.save("consumed.json", consumed)
        if record.get("sa"):
            sa_open = st.load("sa_open.json")
            sa_open.setdefault(record["sa"], {})[coid] = {
                "symbol": body.get("symbol"), "at": _now().isoformat(), "state": "reserved"}
            st.save("sa_open.json", sa_open)
        st.save("pace.json", {"last_open_at": _now().isoformat()})

    # -- classification ------------------------------------------------------
    @staticmethod
    def _is_reduce_only(path: str, body: dict) -> bool:
        if path == "/v2/order/batch":
            orders = body.get("orders")
            if not isinstance(orders, list) or not orders:
                return False
            return all(isinstance(o, dict) and
                       (_truthy(o.get("reduce_only")) or _truthy(o.get("close_position")))
                       for o in orders)
        return _truthy(body.get("reduce_only")) or _truthy(body.get("close_position"))

    @staticmethod
    def _is_margin_add(body: dict) -> bool:
        """Only an unambiguous ADD is Tier 0.

        Strike follows the Binance convention of type 1 = add, 2 = remove;
        confirm against strike-positions before relying on it. Anything this
        cannot read as an add falls through to needing a ticket and a token,
        because removing margin walks the liquidation price toward the mark.
        """
        kind = str(body.get("type") or body.get("action") or "").strip().lower()
        if kind not in ("1", "add"):
            return False
        try:
            return float(body.get("amount", 0)) > 0
        except (TypeError, ValueError):
            return False

    # -- account and book ----------------------------------------------------
    def _equity(self) -> float:
        if self.account_reader is None:
            raise PolicyRefusal(
                "equity: no exchange account reader wired in; opening exposure disabled. "
                "strike_request.py must pass account_reader=")
        try:
            equity = _num(self.account_reader(), "equity")
        except PolicyRefusal:
            raise
        except Exception as exc:
            raise PolicyRefusal(f"equity: account read failed ({exc})")
        if equity <= 0:
            raise PolicyRefusal("equity: account read returned zero or negative equity")
        return equity

    def _capture_sod_if_new_day(self) -> None:
        """Record start-of-day equity on the FIRST call of any kind each UTC day.

        Reads happen constantly, so pinning it here rather than at the first
        opening request means a loss taken before the desk next tries to trade
        still counts against the daily stop.
        """
        if self.account_reader is None:
            return
        try:
            with State(self.state_dir) as st:
                sod = st.load("sod_equity.json")
                today = _now().date().isoformat()
                if sod.get("date") != today:
                    st.save("sod_equity.json",
                            {"date": today, "equity": float(self.account_reader())})
        except Exception:
            # A read must never fail because the bookkeeping behind the daily stop
            # did. If the equity could not be captured here, the first opening
            # request of the day captures it instead.
            pass

    def _daily_loss_ok(self, st: State) -> None:
        equity = self._equity()
        sod = st.load("sod_equity.json")
        today = _now().date().isoformat()
        if sod.get("date") != today:
            # Start-of-day equity is captured on the first signed read of the day.
            # If it is missing here, either that read never happened or something
            # wiped the file - and re-capturing it now would re-base the stop at
            # whatever the account is worth after the loss. Refuse; never reset.
            raise PolicyRefusal(
                "daily loss: no start-of-day equity recorded for today; run the account read "
                "first. This is never re-based from the current balance.")
        start = _num(sod.get("equity"), "daily loss: start-of-day equity")
        if start <= 0:
            raise PolicyRefusal("daily loss: start-of-day equity is not positive")
        frac = (start - equity) / start
        cap = _ceiling("MAX_DAILY_LOSS_FRACTION", CEILING_MAX_DAILY_LOSS_FRACTION)
        if frac >= cap:
            raise PolicyRefusal(
                f"daily loss: {frac:.2%} of start-of-day equity >= ceiling {cap:.2%}; "
                "opening exposure blocked")

    def _incident_ok(self, st: State) -> None:
        """`journal/incidents/open` is Bot-writable, so a Bot could simply empty it.

        Every incident seen there is copied into state once, and stays blocking
        until the user clears it by re-signing the register with a higher version -
        the same key that lifts a suspension. Deleting the file closes nothing.
        """
        directory = self.desk / "journal" / "incidents" / "open"
        seen = st.load("incidents.json")
        changed = False
        try:
            names = sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []
        except OSError as exc:
            raise PolicyRefusal(f"incident: cannot read {directory} ({exc})")
        version = self._register_version()
        for name in names:
            if name not in seen:
                seen[name] = {"at": _now().isoformat(), "register_version": version}
                changed = True
        for name, entry in list(seen.items()):
            if version > int(entry.get("register_version", 0)):
                seen.pop(name)
                changed = True
        if changed:
            st.save("incidents.json", seen)
        if seen:
            raise PolicyRefusal(
                f"incident: {len(seen)} open incident(s) ({', '.join(sorted(seen))}); opening "
                "exposure is blocked until the user clears them with a re-signed register")

    def _register_version(self) -> int:
        try:
            return int(verify_signed_file(self.register, self.pub).get("version", 0))
        except PolicyRefusal:
            return 0

    def _blackout_ok(self, st: State, symbol: str) -> None:
        """`desk/blackouts.json` is written by the Research Analyst from the calendar:

            [{"symbol": "*"|"BTC-USD", "start": iso, "end": iso, "reason": "FOMC"}]

        This is the catalyst clock with teeth. Entries are ingested one-way and
        expire on their own `end`, so a Bot deleting the file cannot lift a
        blackout that is currently in force.
        """
        source = self.desk / "desk" / "blackouts.json"
        windows = st.load("blackouts.json")
        now = _now()
        changed = False
        if source.exists():
            try:
                entries = json.loads(source.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                raise PolicyRefusal(f"blackout: {source.name} is unreadable ({exc})")
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict) or "start" not in entry or "end" not in entry:
                    continue
                key = f"{entry.get('symbol', '*')}|{entry['start']}|{entry['end']}"
                if key not in windows:
                    windows[key] = {"symbol": entry.get("symbol", "*"), "start": entry["start"],
                                    "end": entry["end"], "reason": entry.get("reason", "")}
                    changed = True
        for key, window in list(windows.items()):
            try:
                if now > _iso(window["end"], "blackout end"):
                    windows.pop(key)
                    changed = True
            except PolicyRefusal:
                windows.pop(key)
                changed = True
        if changed:
            st.save("blackouts.json", windows)
        for window in windows.values():
            if window["symbol"] not in ("*", symbol):
                continue
            if _iso(window["start"], "blackout start") <= now <= _iso(window["end"], "blackout end"):
                raise PolicyRefusal(
                    f"blackout: {symbol} is inside a {window.get('reason') or 'scheduled'} window "
                    f"until {window['end']}; opening exposure is blocked")

    def _positions(self) -> list:
        if self.positions_reader is None:
            raise PolicyRefusal(
                "positions: no exchange positions reader wired in; the book cannot be counted")
        try:
            rows = self.positions_reader() or []
        except Exception as exc:
            raise PolicyRefusal(f"positions: read failed ({exc})")
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("size", "positionAmt", "quantity", "amount"):
                if row.get(key) not in (None, ""):
                    if abs(_num(row[key], "positions: size")) > 0:
                        out.append({"symbol": str(row.get("symbol")), "size": row[key]})
                    break
        return out

    def _book_has_room(self) -> None:
        held = len(self._positions())
        cap = int(_ceiling("MAX_OPEN_POSITIONS", CEILING_MAX_OPEN_POSITIONS))
        if held >= cap:
            raise PolicyRefusal(
                f"ceiling: {held} positions are already open across the book (max {cap}); "
                "no standing approval raises this")

    def _pace(self, st: State) -> None:
        gap = _floor("SECONDS_BETWEEN_OPENS", FLOOR_SECONDS_BETWEEN_OPENS)
        last = st.load("pace.json").get("last_open_at")
        if not last:
            return
        since = (_now() - _iso(last, "pace: last_open_at")).total_seconds()
        if since < gap:
            raise PolicyRefusal(
                f"pace: {since:.0f}s since the last opening order, minimum {gap:.0f}s. "
                "A rule that fires this often is a bug, not an edge.")

    # -- ticket <-> request equality -----------------------------------------
    def _ticket_not_expired(self, f: dict) -> None:
        """The Risk Manager writes an expiry on every ticket. Read it.

        Nothing else in the layer covers a discretionary ticket left lying
        around: `fired_at` bounds a rule's signal, but a Tier 2 ticket has no
        `fired_at`, and the token's own expiry may be generous.
        """
        expires = f.get("expires_at")
        if expires is None:
            return
        if _now() > _iso(expires, f"proposal: {f['_ticket']} expires_at"):
            raise PolicyRefusal(
                f"proposal: {f['_ticket']} expired at {expires}. Prices and the book have "
                "moved; it needs a fresh sign-off, not a fresh timestamp.")

    def _one_field(self, f: dict, body: dict, key: str, body_key: Optional[str] = None) -> None:
        want, got = f.get(key), body.get(body_key or key)
        if want is None:
            raise PolicyRefusal(f"proposal: {f['_ticket']} PASS block is missing '{key}:'")
        if key in ("size", "price", "leverage", "amount", "stop_price"):
            if abs(_num(got, f"request {key}") - _num(want, f"ticket {key}")) > 1e-12:
                raise PolicyRefusal(f"proposal: {f['_ticket']} {key} '{want}' != request '{got}'")
        elif str(got).strip().lower() != str(want).strip().lower():
            raise PolicyRefusal(f"proposal: {f['_ticket']} {key} '{want}' != request '{got}'")

    def _match_ticket(self, f: dict, body: dict, path: str) -> tuple[str, float]:
        """Confirm the request is the ticket the Risk Manager passed, and return
        (order kind, notional). The notional comes from the ticket's own price,
        so a market order - which carries no price - is still bounded."""
        ticket_id = f["_ticket"]
        kind = str(f.get("order_type") or f.get("type") or "limit").strip().lower()
        if kind not in ("limit", "market", "twap"):
            raise PolicyRefusal(
                f"proposal: {ticket_id} order_type '{kind}' must be limit, market or twap")
        if (kind == "twap") != (path == "/v2/algo/twap"):
            raise PolicyRefusal(f"proposal: {ticket_id} is order_type {kind} but the path is {path}")

        for key in ("symbol", "side", "size"):
            self._one_field(f, body, key)
        size = _num(f["size"], "ticket size")
        if size <= 0:
            raise PolicyRefusal(f"proposal: {ticket_id} size must be positive")

        if kind == "market":
            if body.get("price") not in (None, ""):
                raise PolicyRefusal(f"proposal: {ticket_id} is a market ticket but the request carries a price")
            if body.get("slippage") in (None, ""):
                raise PolicyRefusal(
                    f"proposal: {ticket_id} is a market ticket; the request needs a slippage bound")
            if f.get("ref_price") is None:
                raise PolicyRefusal(
                    f"proposal: {ticket_id} is a market ticket and needs 'ref_price:' so its "
                    "notional can be bounded")
            price = _num(f["ref_price"], "ticket ref_price")
        else:
            self._one_field(f, body, "price", "limit_price" if kind == "twap" else "price")
            price = _num(f["price"], "ticket price")
        if price <= 0:
            raise PolicyRefusal(f"proposal: {ticket_id} price must be positive")

        # The Risk Manager states a leverage on every ticket. If the request
        # names one it must be that one; an order may not quietly arrive at 50x
        # on a ticket sized for 3x.
        if body.get("leverage") not in (None, ""):
            self._one_field(f, body, "leverage")
        return kind, size * price

    def _check_protection(self, path: str, body: dict, f: dict, kind: str) -> None:
        """An opening order carries its own stop, in the same request.

        The desk's prose has always said a ticket without a stop does not get
        sent. Unattended, prose is not a control: an entry that fills while
        nobody is awake and leaves no resting stop is the one failure that costs
        more than the ticket said. A TWAP is the exception - it is protected as
        it builds, and must say so.
        """
        ticket_id = f["_ticket"]
        if kind == "twap":
            if str(f.get("protection", "")).strip().lower() != "as_it_builds":
                raise PolicyRefusal(
                    f"protection: {ticket_id} is a twap and must declare 'protection: as_it_builds'")
            return
        if path != "/v2/order/strategy":
            raise PolicyRefusal(
                f"protection: an opening order must be a bracket on /v2/order/strategy so its stop "
                f"arms with the fill; {ticket_id} was sent to {path}")
        leg = body.get("sl_order")
        if not isinstance(leg, dict):
            raise PolicyRefusal(f"protection: {ticket_id} carries no sl_order")
        if str(leg.get("working_type", "")).strip().lower() != "mark_price":
            raise PolicyRefusal(
                f"protection: {ticket_id} sl_order must trigger on mark_price; that is what "
                "liquidation settles against")
        if abs(_num(leg.get("size"), f"protection: {ticket_id} sl_order.size")
               - _num(f["size"], "ticket size")) > 1e-12:
            raise PolicyRefusal(f"protection: {ticket_id} sl_order.size does not cover the entry")

        stop = _num(leg.get("stop_price"), f"protection: {ticket_id} sl_order.stop_price")
        if f.get("stop_price") is not None:
            self._one_field(f, leg, "stop_price")
        entry = _num(f.get("price") or f.get("ref_price"), f"protection: {ticket_id} entry price")
        side = str(body.get("side", "")).strip().lower()
        if side in ("buy", "long") and stop >= entry:
            raise PolicyRefusal(
                f"protection: {ticket_id} is a long with a stop at {stop:g} at or above the entry "
                f"{entry:g}; it would trigger immediately")
        if side in ("sell", "short") and stop <= entry:
            raise PolicyRefusal(
                f"protection: {ticket_id} is a short with a stop at {stop:g} at or below the entry "
                f"{entry:g}; it would trigger immediately")

        if f.get("risk_usd") is not None:
            nominal = abs(entry - stop) * _num(f["size"], "ticket size")
            stated = _num(f["risk_usd"], f"protection: {ticket_id} risk_usd")
            # The Risk Manager sizes on a STRESSED stop, so the nominal loss at the
            # stop is always a little under the stated risk. Above it means the stop
            # and the sizing disagree, and one of them is wrong.
            if nominal > stated * 1.05:
                raise PolicyRefusal(
                    f"protection: {ticket_id} loses ${nominal:,.2f} at its stop but the ticket "
                    f"states risk_usd ${stated:,.2f}; the stop and the sizing disagree")

    def _config_match(self, f: dict, body: dict, path: str) -> None:
        """Leverage, margin mode and margin removal are exposure changes wearing a
        configuration costume, so each needs its own ticket and approval."""
        self._one_field(f, body, "symbol")
        if path == "/v2/leverage":
            self._one_field(f, body, "leverage")
            lev = _num(body.get("leverage"), "config: leverage")
            cap = _ceiling("MAX_LEVERAGE", CEILING_MAX_LEVERAGE)
            if lev < 1 or lev > cap:
                raise PolicyRefusal(f"ceiling: leverage {lev:g} outside 1..{cap:g}")
        elif path == "/v2/marginType":
            self._one_field(f, body, "margin_type")
        elif path == "/v2/positionMargin":
            if str(f.get("margin_action", "")).strip().lower() != "remove":
                raise PolicyRefusal(
                    "proposal: removing margin needs 'margin_action: remove' in the PASS block; "
                    "it walks the liquidation price toward the mark and is never Tier 0")
            self._one_field(f, body, "amount")

    # -- Tier 1 --------------------------------------------------------------
    def _check_sa(self, st: State, f: dict, body: dict, ticket_id: str,
                  notional: float, path: str) -> dict:
        sa_id = str(f["sa"]).strip()
        if not self.state_trusted:
            raise PolicyRefusal(
                "SA: Tier 1 is off because STRIKEGROK_STATE_TRUSTED=1 is not set. Slots, spent "
                "ids and the daily baseline live in policy-state, which is only a boundary when "
                "this script runs as its own OS user. Assert that split deliberately, or keep "
                "the platform's Require Approval rule and trade at Tier 2.")
        if not SA_ID_RE.fullmatch(sa_id):
            raise PolicyRefusal(
                f"SA: '{sa_id}' is not a standing-approval id; a ticket either names one or "
                "carries no 'sa:' line at all")
        register = verify_signed_file(self.register, self.pub)
        version = int(_num(register.get("version", 0), "SA: register version"))
        approvals = register.get("approvals")
        if not isinstance(approvals, list):
            raise PolicyRefusal("SA: the signed register has no approvals list")
        sa = next((a for a in approvals if isinstance(a, dict) and a.get("id") == sa_id), None)
        if sa is None:
            raise PolicyRefusal(f"SA: {sa_id} is not in the signed register")

        now = _now()
        expires = _date(sa.get("expires"), f"SA: {sa_id} expires")
        granted = _date(sa.get("granted"), f"SA: {sa_id} granted")
        if now.date() > expires:
            raise PolicyRefusal(f"SA: {sa_id} expired {expires}")
        if (expires - granted).days > CEILING_SA_MAX_LIFETIME_DAYS:
            raise PolicyRefusal(
                f"SA: {sa_id} lifetime {(expires - granted).days} days exceeds the "
                f"{CEILING_SA_MAX_LIFETIME_DAYS}-day ceiling")

        # Suspensions are ingested one-way and lifted only by a re-signed register.
        self._ingest_suspensions(st, version)
        suspended = st.load("suspended.json")
        if sa_id in suspended:
            if version > int(suspended[sa_id].get("register_version", 0)):
                suspended.pop(sa_id)
                st.save("suspended.json", suspended)
            else:
                raise PolicyRefusal(
                    f"SA: {sa_id} is suspended ({suspended[sa_id].get('reason', '')}); "
                    "lifting it needs a register the user re-signed with a higher version")

        signal = str(f.get("signal", "")).strip()
        m = re.fullmatch(r"([\w.\-]+)@(\d+)", signal)
        if not m or m.group(1) != sa.get("rule") or int(m.group(2)) != int(sa.get("rule_version", -1)):
            raise PolicyRefusal(
                f"SA: signal '{signal}' does not name {sa.get('rule')}@{sa.get('rule_version')}")
        rules = self.desk / "strategies" / str(sa["rule"]) / "RULES.md"
        if not rules.exists() or _sha256(rules) != sa.get("rules_sha256"):
            self._suspend(st, sa_id, "RULES.md hash changed", version)
            raise PolicyRefusal(
                f"SA: RULES.md hash mismatch for {sa['rule']}; the rule changed since the user "
                f"signed it, and {sa_id} is now suspended")

        if body.get("symbol") not in (sa.get("markets") or []):
            raise PolicyRefusal(f"SA: {body.get('symbol')} is not in {sa_id}'s markets")

        if path in CONFIG_PATHS:
            if path == "/v2/positionMargin":
                raise PolicyRefusal(
                    "SA: removing margin is never covered by a standing approval; it needs a "
                    "signed per-trade token")
            max_lev = _num(sa.get("max_leverage", CEILING_MAX_LEVERAGE), f"SA: {sa_id} max_leverage")
            if path == "/v2/leverage" and _num(body.get("leverage"), "leverage") > max_lev:
                raise PolicyRefusal(f"SA: leverage {body.get('leverage')} > {sa_id} max {max_lev:g}")
            self._hours_ok(sa, sa_id, now)
            return {"allow": True, "tier": "tier1", "sa": sa_id, "ticket": ticket_id,
                    "why": f"{sa_id} covers this configuration change"}

        side = str(body.get("side", "")).strip().lower()
        if side in ("buy", "long"):
            side_word = "long"
        elif side in ("sell", "short"):
            side_word = "short"
        else:
            raise PolicyRefusal(f"SA: side '{body.get('side')}' is neither long nor short")
        if side_word not in [str(s).lower() for s in (sa.get("sides") or ["long", "short"])]:
            raise PolicyRefusal(f"SA: side {side_word} is not permitted by {sa_id}")
        if notional > _num(sa.get("max_notional"), f"SA: {sa_id} max_notional"):
            raise PolicyRefusal(f"SA: notional ${notional:,.0f} > {sa_id} max ${sa['max_notional']}")

        if str(f.get("liquidity", "")).strip().lower() != "pass":
            raise PolicyRefusal(
                f"SA: {ticket_id} PASS block does not read 'liquidity: pass'. A rule that fires "
                "into a book that cannot take the ticket is how a tested edge becomes an "
                "untested one.")

        risk_usd = _num(f.get("risk_usd"), f"SA: {ticket_id} risk_usd")
        stated_equity = _num(f.get("equity"), f"SA: {ticket_id} equity")
        live_equity = self._equity()
        drift = abs(live_equity - stated_equity) / live_equity
        if drift > CEILING_PASS_EQUITY_DRIFT:
            raise PolicyRefusal(
                f"SA: the PASS block sized against equity {stated_equity:,.2f}, which is "
                f"{drift:.1%} from the live {live_equity:,.2f}; re-size before sending")
        cap = min(_num(sa.get("risk_per_trade"), f"SA: {sa_id} risk_per_trade"),
                  CEILING_MAX_SA_RISK_PER_TRADE)
        if risk_usd / live_equity > cap + 1e-9:
            raise PolicyRefusal(f"SA: risk {risk_usd / live_equity:.3%} > {sa_id} cap {cap:.3%}")

        occupied = self._occupied_markets(
            st, sa_id, sa.get("markets") or [],
            _num(sa.get("bar_seconds"), f"SA: {sa_id} bar_seconds"))
        max_open = min(int(_num(sa.get("max_open"), f"SA: {sa_id} max_open")), CEILING_MAX_OPEN_PER_SA)
        if max_open < 1:
            raise PolicyRefusal(f"SA: {sa_id} max_open is {max_open}")
        if len(occupied) >= max_open and body.get("symbol") not in occupied:
            raise PolicyRefusal(
                f"SA: {sa_id} already holds {len(occupied)}/{max_open} open tickets "
                f"({', '.join(sorted(occupied))})")
        if body.get("symbol") in occupied:
            raise PolicyRefusal(
                f"SA: {sa_id} already has exposure or a working order on {body.get('symbol')}")

        fired = f.get("fired_at")
        if not fired:
            raise PolicyRefusal(
                f"SA: {ticket_id} has no fired_at; a Tier 1 send must name the bar that fired")
        age = (now - _iso(fired, f"SA: {ticket_id} fired_at")).total_seconds()
        bar = _num(sa.get("bar_seconds"), f"SA: {sa_id} bar_seconds")
        bar_cap = _ceiling("MAX_BAR_SECONDS", CEILING_MAX_BAR_SECONDS)
        if bar > bar_cap:
            raise PolicyRefusal(
                f"SA: {sa_id} bar_seconds {bar:.0f} exceeds the {bar_cap:.0f}s ceiling; a signal "
                "that stays fresh for longer than a day is not a signal")
        if age < 0:
            raise PolicyRefusal(f"SA: fired_at is {abs(age):.0f}s in the future")
        if age > bar:
            raise PolicyRefusal(f"SA: the signal is {age:.0f}s old, past one bar ({bar:.0f}s); stale")

        # Consecutive losses, recomputed from the exchange rather than believed.
        limit = min(int(_num((sa.get("kill") or {}).get("consecutive_losses",
                                                        CEILING_MAX_CONSECUTIVE_LOSSES),
                             f"SA: {sa_id} kill.consecutive_losses")),
                    CEILING_MAX_CONSECUTIVE_LOSSES)
        if self.fills_reader and limit > 0:
            try:
                pnls = list(self.fills_reader(body["symbol"], limit))
            except Exception as exc:
                raise PolicyRefusal(f"SA: fills read failed ({exc}); cannot evaluate the loss kill")
            if len(pnls) >= limit and all(_num(p, "fill pnl") < 0 for p in pnls[:limit]):
                self._suspend(st, sa_id, f"{limit} consecutive losses on {body['symbol']}", version)
                raise PolicyRefusal(f"SA: {limit} consecutive losses on {body['symbol']}; {sa_id} suspended")

        self._hours_ok(sa, sa_id, now)
        return {"allow": True, "tier": "tier1", "sa": sa_id, "ticket": ticket_id,
                "why": f"{sa_id}: every bound checked"}

    @staticmethod
    def _hours_ok(sa: dict, sa_id: str, now: dt.datetime) -> None:
        hours = sa.get("hours_utc")
        if not hours:
            return
        lo, hi = int(hours[0]), int(hours[1])
        hour = now.hour
        inside = (lo <= hour < hi) if lo < hi else (hour >= lo or hour < hi)
        if lo == hi or not inside:
            raise PolicyRefusal(f"SA: {hour:02d}:00 UTC is outside {sa_id}'s hours {hours}")

    def _ingest_suspensions(self, st: State, version: int) -> None:
        """Copy Bot-written suspensions into state, one way.

        Appending a suspension only removes permission, so Bots may do it.
        Deleting the file they wrote it in restores nothing, because the state
        copy is what the layer reads and only a re-signed register clears it.
        """
        if not self.bot_suspend_file.exists():
            return
        try:
            entries = json.loads(self.bot_suspend_file.read_text())
        except (json.JSONDecodeError, OSError):
            # An unreadable suspension file is not permission to trade.
            raise PolicyRefusal(
                f"ledger: {self.bot_suspend_file.name} is unreadable; refusing Tier 1")
        suspended = st.load("suspended.json")
        changed = False
        for entry in entries if isinstance(entries, list) else []:
            sa_id = entry.get("id") if isinstance(entry, dict) else None
            if sa_id and sa_id not in suspended:
                suspended[sa_id] = {"reason": entry.get("reason", "bot suspension"),
                                    "by": entry.get("by", "?"),
                                    "at": entry.get("at", _now().isoformat()),
                                    "register_version": version}
                changed = True
        if changed:
            st.save("suspended.json", suspended)

    def _suspend(self, st: State, sa_id: str, reason: str, version: int) -> None:
        suspended = st.load("suspended.json")
        suspended[sa_id] = {"reason": reason, "by": "policy",
                            "at": _now().isoformat(), "register_version": version}
        st.save("suspended.json", suspended)

    def _occupied_markets(self, st: State, sa_id: str, markets: list, grace_seconds: float) -> set:
        """Which of this SA's markets are already spoken for.

        Three sources, unioned, because each covers the others' blind spot:

          * state slots, which know about a send whose order has not appeared yet
          * live positions, which know about a fill nobody recorded
          * resting non-reduce-only orders, which know about an entry still working

        Counting markets rather than tickets is what the venue can actually
        confirm, and it is the right unit anyway: a perp account holds one
        position per symbol. Wiping the state file no longer hands out slots.
        """
        sa_open = st.load("sa_open.json")
        slots = sa_open.get(sa_id, {})
        occupied = set()

        live = {row["symbol"] for row in self._positions()} if self.positions_reader else set()
        resting = set()
        if self.open_orders_reader:
            try:
                rows = self.open_orders_reader() or []
            except Exception as exc:
                raise PolicyRefusal(f"open orders: read failed ({exc}); cannot count this SA's slots")
            for row in rows:
                if not isinstance(row, dict) or _truthy(row.get("reduce_only")):
                    continue
                if TICKET_RE.search(str(row.get("client_order_id", ""))):
                    resting.add(str(row.get("symbol")))

        keep = {}
        for coid, slot in slots.items():
            symbol = slot.get("symbol")
            if symbol in live or symbol in resting:
                keep[coid] = slot
                occupied.add(symbol)
                continue
            try:
                age = (_now() - _iso(slot.get("at"), "slot age")).total_seconds()
            except PolicyRefusal:
                keep[coid] = slot
                occupied.add(symbol)
                continue
            if age < grace_seconds:
                # A reservation whose order has not surfaced yet. Keeping it is what
                # stops a second ticket slipping in during the round trip.
                keep[coid] = slot
                occupied.add(symbol)
        if keep != slots:
            sa_open[sa_id] = keep
            st.save("sa_open.json", sa_open)

        for symbol in live | resting:
            if symbol in markets:
                occupied.add(symbol)
        return {s for s in occupied if s}

    # -- Tier 2 --------------------------------------------------------------
    def _check_token(self, st: State, f: dict, body: dict, ticket_id: str, consumed: dict) -> dict:
        path = self.desk / "approvals" / f"{ticket_id}.json"
        token = verify_signed_file(path, self.pub)
        if token.get("proposal") != ticket_id:
            raise PolicyRefusal(f"token: names {token.get('proposal')}, not {ticket_id}")
        if _now() > _iso(token.get("expires"), f"token: {ticket_id} expires"):
            raise PolicyRefusal(f"token: {ticket_id} expired {token.get('expires')}")
        token_id = _sha256(path)
        if token_id in consumed.get("tokens", {}):
            raise PolicyRefusal(f"replay: the token for {ticket_id} has already been consumed")
        for key in ("symbol", "side", "size", "price", "leverage", "amount", "margin_type"):
            if key in token and str(token[key]).strip().lower() != str(body.get(key)).strip().lower():
                raise PolicyRefusal(f"token: {key} '{token[key]}' != request '{body.get(key)}'")
        return {"allow": True, "tier": "tier2", "token": token_id, "ticket": ticket_id,
                "why": "signed per-trade token"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_verify(policy: Policy) -> int:
    print(f"mode: {policy.mode()}")
    print(f"state trusted: {'yes' if policy.state_trusted else 'no (Tier 1 off)'}")
    print("ceilings: " + ", ".join(f"{k}={v:g}" for k, v in effective_ceilings().items()))
    if policy.mode() == "attended":
        print("no desk/user-signing.pub, so Tier 1 and Tier 2 are off. The policy layer still "
              "enforces ceilings, forbidden surfaces, leverage and margin rules, protection and "
              "ticket equality; approval remains the platform gate plus the user's eyes.")
        return 0
    try:
        register = verify_signed_file(policy.register, policy.pub)
        with State(policy.state_dir) as st:
            policy._ingest_suspensions(st, int(register.get("version", 0)))
            suspended = st.load("suspended.json")
            slots = st.load("sa_open.json")
    except PolicyRefusal as exc:
        print(f"REFUSE {exc}")
        return 2
    print(f"register v{register.get('version')} signed {register.get('signed_at')} verifies; "
          f"{len(register.get('approvals') or [])} approvals")
    problems = 0
    for sa in register.get("approvals") or []:
        sa_id = str(sa.get("id"))
        rules = policy.desk / "strategies" / str(sa.get("rule")) / "RULES.md"
        ok = rules.exists() and _sha256(rules) == sa.get("rules_sha256")
        state = "live"
        if sa_id in suspended:
            state = f"SUSPENDED ({suspended[sa_id].get('reason', '')})"
        if not ok:
            problems += 1
        print(f"  {sa_id} {sa.get('rule')}@{sa.get('rule_version')} hash "
              f"{'ok' if ok else 'MISMATCH'} expires {sa.get('expires')} "
              f"open {len(slots.get(sa_id, {}))}/{sa.get('max_open')} {state}")
    return 2 if problems else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify", help="mode, register, suspensions, slots and rule hashes")
    keys = sub.add_parser("keygen"); keys.add_argument("--out", required=True)
    signer = sub.add_parser("sign")
    signer.add_argument("--key", required=True); signer.add_argument("--file", required=True)
    susp = sub.add_parser("suspend", help="suspend a standing approval (Bots may; only a re-signed register lifts it)")
    susp.add_argument("sa_id"); susp.add_argument("--reason", required=True); susp.add_argument("--by", required=True)
    ex = sub.add_parser("explain", help="dry-run a request body against the policy")
    ex.add_argument("body"); ex.add_argument("--path", default="/v2/order/strategy")
    ex.add_argument("--equity", type=float, help="stand in for the exchange account read")
    ex.add_argument("--positions", type=int, default=0, help="stand in for the number of open positions")
    args = parser.parse_args(argv)

    if args.cmd == "keygen":
        keygen(Path(args.out)); return 0
    if args.cmd == "sign":
        sign_file(Path(args.key), Path(args.file)); return 0

    policy = Policy()
    if args.cmd == "verify":
        return _cmd_verify(policy)
    if args.cmd == "suspend":
        try:
            with State(policy.state_dir) as st:
                register = verify_signed_file(policy.register, policy.pub)
                policy._suspend(st, args.sa_id, f"{args.reason} (by {args.by})",
                                int(register.get("version", 0)))
        except PolicyRefusal as exc:
            print(f"REFUSE {exc}"); return 2
        print(f"{args.sa_id} suspended: {args.reason}. Only a re-signed register lifts it.")
        return 0
    if args.cmd == "explain":
        try:
            body = json.loads(Path(args.body).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"body is unreadable: {exc}"); return 2
        policy = Policy(
            account_reader=(lambda: args.equity) if args.equity is not None else None,
            positions_reader=lambda: [{"symbol": f"HELD-{i}", "size": "1"} for i in range(args.positions)],
        )
        try:
            print(json.dumps(policy.check("POST", args.path, body), indent=2))
        except PolicyRefusal as exc:
            print(f"REFUSE {exc}"); return 2
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
