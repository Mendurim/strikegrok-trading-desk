#!/usr/bin/env python3
"""Fixtures for desk_doctor.py.

No network: the connectivity probe is exercised through its own function with
crafted payloads, and everything else runs against temporary directories.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import desk_doctor as doctor  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def status_of(checks, name):
    for check in checks:
        if check.name == name:
            return check.status
    raise AssertionError(f"no check named {name!r} in {[c.name for c in checks]}")


def build_repo(tmp, *, version="1.1.0", pin=None, skills=("alpha",), drift=False):
    """A minimal repository shaped the way the doctor expects to find one."""
    pin = version if pin is None else pin
    os.makedirs(os.path.join(tmp, "scripts"))
    os.makedirs(os.path.join(tmp, "agents"))
    os.makedirs(os.path.join(tmp, "template"))

    with open(os.path.join(tmp, "plugin.json"), "w", encoding="utf-8") as handle:
        json.dump({"name": "strikegrok", "version": version}, handle)
    with open(os.path.join(tmp, "SETUP.md"), "w", encoding="utf-8") as handle:
        handle.write(f"git clone --depth 1 --branch v{pin} ...\n")
    for rel in doctor.REPO_FILES:
        path = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            open(path, "w", encoding="utf-8").close()
    with open(os.path.join(tmp, "agents", "execution-trader.md"), "w", encoding="utf-8") as handle:
        handle.write("---\nname: execution-trader\n---\n")

    entries = []
    for name in skills:
        skill_dir = os.path.join(tmp, "skills", name)
        os.makedirs(skill_dir, exist_ok=True)
        body = f"---\nname: {name}\n---\nbody\n"
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write(body)
        digest = hashlib.sha256(body.encode()).hexdigest()
        if drift:
            digest = "0" * 64
        entries.append({"name": name, "path": f"skills/{name}/SKILL.md", "sha256": digest})

    with open(os.path.join(tmp, "template", "grok-bot.json"), "w", encoding="utf-8") as handle:
        json.dump({"source": {"release": f"v{version}"}, "skills": entries}, handle)
    return tmp


class RepositoryChecks(unittest.TestCase):
    def test_a_well_formed_repository_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            checks = doctor.inspect_repo(build_repo(tmp))
            self.assertNotIn("FAIL", [c.status for c in checks])

    def test_the_real_repository_passes(self):
        checks = doctor.inspect_repo(REPO)
        failures = [f"{c.name}: {c.detail}" for c in checks if c.status == "FAIL"]
        self.assertEqual(failures, [])

    def test_an_edited_skill_is_reported_as_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            checks = doctor.inspect_repo(build_repo(tmp, drift=True))
            self.assertEqual(status_of(checks, "skill bytes"), "FAIL")

    def test_a_runbook_pinned_to_the_wrong_release_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            checks = doctor.inspect_repo(build_repo(tmp, version="1.1.0", pin="1.0.0"))
            self.assertEqual(status_of(checks, "setup pin"), "FAIL")

    def test_a_missing_release_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_repo(tmp)
            os.remove(os.path.join(root, "scripts", "strike_request.py"))
            checks = doctor.inspect_repo(root)
            self.assertEqual(status_of(checks, "release files"), "FAIL")

    def test_an_unreadable_manifest_fails_rather_than_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_repo(tmp)
            with open(os.path.join(root, "plugin.json"), "w", encoding="utf-8") as handle:
                handle.write("{not json")
            checks = doctor.inspect_repo(root)
            self.assertEqual(status_of(checks, "release"), "FAIL")


class DeskChecks(unittest.TestCase):
    def make_desk(self, tmp, record=None, limits=False):
        for name in doctor.DESK_DIRS:
            os.makedirs(os.path.join(tmp, name), exist_ok=True)
        if record is not None:
            with open(os.path.join(tmp, "desk.md"), "w", encoding="utf-8") as handle:
                handle.write(record)
        if limits:
            open(os.path.join(tmp, "risk-limits.md"), "w", encoding="utf-8").close()
        return tmp

    def test_a_prepared_desk_with_limits_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            checks = doctor.inspect_desk(self.make_desk(tmp, "- engagement level: trading\n", limits=True))
            self.assertNotIn("FAIL", [c.status for c in checks])
            self.assertEqual(status_of(checks, "risk limits"), "PASS")

    def test_a_missing_desk_record_warns_rather_than_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            checks = doctor.inspect_desk(self.make_desk(tmp))
            self.assertEqual(status_of(checks, "desk record"), "WARN")
            self.assertNotIn("FAIL", [c.status for c in checks])

    def test_unwritten_limits_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            checks = doctor.inspect_desk(self.make_desk(tmp, "- risk limits: not yet written\n"))
            self.assertEqual(status_of(checks, "risk limits"), "WARN")

    def test_missing_working_folders_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_desk(tmp, "- engagement level: research\n")
            os.rmdir(os.path.join(tmp, "watch"))
            checks = doctor.inspect_desk(tmp)
            self.assertEqual(status_of(checks, "desk folders"), "FAIL")

    def test_a_credential_left_in_the_desk_record_fails(self):
        for leaked in (
            "- private key: abc\n",
            "- secret: hunter2\n",
            "- STRIKE_API_PRIVATE_KEY=" + "a" * 64 + "\n",
            "- note: " + "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60" + "\n",
        ):
            with self.subTest(leaked=leaked.strip()[:24]):
                with tempfile.TemporaryDirectory() as tmp:
                    checks = doctor.inspect_desk(self.make_desk(tmp, leaked))
                    self.assertEqual(status_of(checks, "desk record"), "FAIL")


class ProbeChecks(unittest.TestCase):
    def test_a_good_payload_counts_trading_markets(self):
        original = doctor.urllib.request.urlopen
        payload = {"symbols": [{"status": "trading"}, {"status": "trading"}, {"status": "halted"}]}
        doctor.urllib.request.urlopen = lambda *a, **k: _Response(payload)
        try:
            check = doctor.probe_price_service("https://example.invalid", 1)
        finally:
            doctor.urllib.request.urlopen = original
        self.assertEqual(check.status, "PASS")
        self.assertIn("2 markets trading", check.detail)

    def test_an_empty_payload_fails(self):
        original = doctor.urllib.request.urlopen
        doctor.urllib.request.urlopen = lambda *a, **k: _Response({"symbols": []})
        try:
            check = doctor.probe_price_service("https://example.invalid", 1)
        finally:
            doctor.urllib.request.urlopen = original
        self.assertEqual(check.status, "FAIL")

    def test_a_network_error_fails_rather_than_crashing(self):
        original = doctor.urllib.request.urlopen

        def boom(*a, **k):
            raise OSError("no route to host")

        doctor.urllib.request.urlopen = boom
        try:
            check = doctor.probe_price_service("https://example.invalid", 1)
        finally:
            doctor.urllib.request.urlopen = original
        self.assertEqual(check.status, "FAIL")
        self.assertIn("unavailable", check.detail)


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CliChecks(unittest.TestCase):
    def test_offline_mode_makes_no_request_and_reports_skip(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = doctor.main(["--offline", "--repo-root", REPO])
        self.assertEqual(code, 0)
        self.assertIn("SKIP", out.getvalue())

    def test_json_mode_is_machine_readable(self):
        out = io.StringIO()
        with redirect_stdout(out):
            doctor.main(["--offline", "--json", "--repo-root", REPO])
        parsed = json.loads(out.getvalue())
        self.assertTrue(parsed["checks"])
        self.assertIn("status", parsed["checks"][0])

    def test_a_failure_sets_a_nonzero_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_repo(tmp, drift=True)
            out = io.StringIO()
            with redirect_stdout(out):
                code = doctor.main(["--offline", "--repo-root", tmp])
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
