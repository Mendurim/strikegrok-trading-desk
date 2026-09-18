#!/usr/bin/env python3
"""Report whether a StrikeGrok install is healthy, without touching anything.

Read-only by construction. It reads files, and it makes one unauthenticated
request to Strike's public Price Service. It never loads the API wallet and
never calls a signed endpoint: a doctor able to verify the write path would be
a doctor able to place an order.

    python3 scripts/desk_doctor.py
    python3 scripts/desk_doctor.py --desk-root /workspace/trading-desk
    python3 scripts/desk_doctor.py --offline --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass

PRICE_SERVICE = "https://api.strikefinance.org/price"

REPO_FILES = (
    "SETUP.md",
    "scripts/validate.py",
    "scripts/desk_doctor.py",
    "scripts/opening_bell.py",
    "scripts/strike_request.py",
    "scripts/funding_collect.py",
    "skills/strikegrok-bootstrap/SKILL.md",
)

DESK_DIRS = ("proposals", "briefs", "research", "strategies", "data", "journal", "watch")

# A desk record should describe the desk, never hold a credential.
SECRET_SHAPED = re.compile(
    r"private[ _-]?key|secret\s*[:=]\s*\S|STRIKE_API_PRIVATE_KEY\s*[:=]\s*\S|\b[0-9a-fA-F]{64}\b"
)


@dataclass
class Check:
    status: str          # PASS, WARN, FAIL or SKIP
    name: str
    detail: str


def passed(condition: bool, name: str, good: str, bad: str) -> Check:
    return Check("PASS" if condition else "FAIL", name, good if condition else bad)


def inspect_repo(root: str) -> list[Check]:
    checks: list[Check] = []

    try:
        with open(os.path.join(root, "plugin.json"), encoding="utf-8") as handle:
            version = json.load(handle)["version"]
        checks.append(Check("PASS", "release", f"plugin.json declares v{version}"))
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        checks.append(Check("FAIL", "release", f"cannot read plugin.json ({exc})"))
        version = None

    missing = [rel for rel in REPO_FILES if not os.path.isfile(os.path.join(root, rel))]
    checks.append(passed(
        not missing, "release files",
        f"all {len(REPO_FILES)} present",
        "missing: " + ", ".join(missing),
    ))

    try:
        with open(os.path.join(root, "template/grok-bot.json"), encoding="utf-8") as handle:
            entries = json.load(handle).get("skills") or []
    except (OSError, json.JSONDecodeError) as exc:
        checks.append(Check("FAIL", "skill bytes", f"cannot read the template ({exc})"))
        entries = []

    if entries:
        drifted, unreadable = [], []
        for entry in entries:
            path = os.path.join(root, entry.get("path", ""))
            try:
                with open(path, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                unreadable.append(entry.get("path", "?"))
                continue
            if digest != entry.get("sha256"):
                drifted.append(entry.get("name", "?"))
        if drifted or unreadable:
            parts = []
            if drifted:
                parts.append("changed since the template was pinned: " + ", ".join(sorted(drifted)))
            if unreadable:
                parts.append("unreadable: " + ", ".join(sorted(unreadable)))
            checks.append(Check("FAIL", "skill bytes", "; ".join(parts)))
        else:
            checks.append(Check("PASS", "skill bytes", f"all {len(entries)} match the pinned hashes"))

    if version:
        try:
            with open(os.path.join(root, "SETUP.md"), encoding="utf-8") as handle:
                setup = handle.read()
            checks.append(passed(
                f"--branch v{version}" in setup, "setup pin",
                f"SETUP.md clones v{version}",
                f"SETUP.md does not pin v{version}; this checkout is a half-updated release",
            ))
        except OSError as exc:
            checks.append(Check("FAIL", "setup pin", f"cannot read SETUP.md ({exc})"))

    skills_dir = os.path.join(root, "skills")
    skills = ([d for d in os.listdir(skills_dir) if os.path.isdir(os.path.join(skills_dir, d))]
              if os.path.isdir(skills_dir) else [])
    checks.append(passed(bool(skills), "skills", f"{len(skills)} present", "skills/ is missing or empty"))

    agents_dir = os.path.join(root, "agents")
    agents = ([f for f in os.listdir(agents_dir) if f.endswith(".md")]
              if os.path.isdir(agents_dir) else [])
    checks.append(passed(bool(agents), "agents", f"{len(agents)} present", "agents/ is missing or empty"))

    return checks


def inspect_desk(desk_root: str) -> list[Check]:
    checks: list[Check] = []

    missing = [d for d in DESK_DIRS if not os.path.isdir(os.path.join(desk_root, d))]
    checks.append(passed(
        not missing, "desk folders", "working folders present",
        "missing: " + ", ".join(missing),
    ))

    record = os.path.join(desk_root, "desk.md")
    if not os.path.isfile(record):
        checks.append(Check("WARN", "desk record", f"{record} has not been written yet"))
        return checks

    with open(record, encoding="utf-8") as handle:
        text = handle.read()
    checks.append(passed(
        not SECRET_SHAPED.search(text), "desk record",
        "present, and nothing in it looks like a credential",
        "contains something credential-shaped; secrets belong in the secret store, not on disk",
    ))

    limits = os.path.isfile(os.path.join(desk_root, "risk-limits.md"))
    if limits and "risk limits: not yet written" not in text:
        checks.append(Check("PASS", "risk limits", "risk-limits.md is present"))
    else:
        checks.append(Check("WARN", "risk limits", "not written; the desk stays research-only"))

    return checks


def probe_price_service(base_url: str, timeout: float) -> Check:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v2/exchangeInfo",
        headers={"Accept": "application/json", "User-Agent": "strikegrok-desk-doctor/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return Check("FAIL", "market data", f"unavailable ({exc})")
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, list) or not symbols:
        return Check("FAIL", "market data", "exchangeInfo returned no symbols")
    tradeable = sum(1 for s in symbols if s.get("status") == "trading")
    return Check("PASS", "market data",
                 f"Price Service answered with no credential: {tradeable} markets trading")


def render(checks: list[Check]) -> str:
    width = max(len(check.name) for check in checks)
    lines = [
        "STRIKEGROK DESK DOCTOR",
        "read only  ·  no credential loaded  ·  no signed request  ·  no writes",
        "",
    ]
    lines += [f"{check.status:<5} {check.name:<{width}}  {check.detail}" for check in checks]
    failed = sum(check.status == "FAIL" for check in checks)
    warned = sum(check.status == "WARN" for check in checks)
    lines += ["", f"{len(checks) - failed - warned} passed, {warned} warning(s), {failed} failed."]
    return "\n".join(lines)


def main(argv=None) -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", default=here)
    parser.add_argument("--desk-root", help="also inspect a prepared trading-desk directory")
    parser.add_argument("--base-url", default=PRICE_SERVICE)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--offline", action="store_true", help="skip the public connectivity check")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    checks = inspect_repo(os.path.abspath(args.repo_root))
    if args.desk_root:
        checks += inspect_desk(os.path.abspath(args.desk_root))
    checks.append(
        Check("SKIP", "market data", "offline mode")
        if args.offline else probe_price_service(args.base_url, args.timeout)
    )

    if args.json:
        print(json.dumps({"checks": [asdict(c) for c in checks]}, indent=2))
    else:
        print(render(checks))
    return 1 if any(check.status == "FAIL" for check in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
