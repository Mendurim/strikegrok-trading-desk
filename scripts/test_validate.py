#!/usr/bin/env python3
"""Fixtures for validate.py.

The repository as it stands must pass. Then, for each check, a copy of the
repository is broken in one specific way and that check must catch it - a check
that never fails is not a check.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

REAL_ROOT = validate.ROOT
# Derived, never hardcoded: otherwise every release bump breaks these fixtures.
RELEASE = f"v{validate.declared_version()}"


class Sandbox:
    """A throwaway copy of the repository that validate.py runs against."""

    def __enter__(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "repo")
        shutil.copytree(
            REAL_ROOT, self.root,
            ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__"),
        )
        validate.ROOT = self.root
        return self

    def __exit__(self, *exc):
        validate.ROOT = REAL_ROOT
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False

    def path(self, rel):
        return os.path.join(self.root, rel)

    def edit(self, rel, old, new, count=1):
        with open(self.path(rel), encoding="utf-8") as handle:
            text = handle.read()
        assert old in text, f"fixture anchor not found in {rel}: {old!r}"
        with open(self.path(rel), "w", encoding="utf-8") as handle:
            handle.write(text.replace(old, new) if count is None else text.replace(old, new, count))

    def edit_json(self, rel, mutate):
        with open(self.path(rel), encoding="utf-8") as handle:
            data = json.load(handle)
        mutate(data)
        with open(self.path(rel), "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)


class RepositoryIsClean(unittest.TestCase):
    def test_every_check_passes_as_committed(self):
        for name, check in validate.CHECKS:
            with self.subTest(check=name):
                self.assertEqual(check(), [], f"{name} failed on the committed tree")

    def test_cli_reports_success(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = validate.main([])
        self.assertEqual(code, 0)
        self.assertIn("ok:", out.getvalue())

    def test_list_describes_every_check(self):
        out = io.StringIO()
        with redirect_stdout(out):
            validate.main(["--list"])
        for name, _ in validate.CHECKS:
            self.assertIn(name, out.getvalue())


class BreakingItIsCaught(unittest.TestCase):
    """Each fixture breaks one thing and asserts the matching check notices."""

    def assert_caught(self, check, fragment, problems):
        joined = " | ".join(problems)
        self.assertTrue(problems, f"{check.__name__} did not fail")
        self.assertIn(fragment, joined)

    def test_skill_name_not_matching_its_directory(self):
        with Sandbox() as box:
            box.edit("skills/strike-auth/SKILL.md", "name: strike-auth", "name: strike-auth-typo")
            self.assert_caught(validate.check_skills, "directory is", validate.check_skills())

    def test_skill_missing_its_licence(self):
        with Sandbox() as box:
            os.remove(box.path("skills/strike-auth/LICENSE"))
            self.assert_caught(validate.check_skills, "missing LICENSE", validate.check_skills())

    def test_agent_referencing_a_skill_that_does_not_exist(self):
        with Sandbox() as box:
            box.edit("agents/risk-manager.md", "  - strike-account", "  - strike-nonexistent")
            self.assert_caught(validate.check_agents, "unknown skill", validate.check_agents())

    def test_a_second_agent_claiming_the_write_path(self):
        with Sandbox() as box:
            box.edit("agents/risk-manager.md", "writes_to_exchange: false", "writes_to_exchange: true")
            self.assert_caught(validate.check_agents, "exactly one agent", validate.check_agents())

    def test_no_agent_claiming_the_write_path(self):
        with Sandbox() as box:
            box.edit("agents/execution-trader.md", "writes_to_exchange: true", "writes_to_exchange: false")
            self.assert_caught(validate.check_agents, "exactly one agent", validate.check_agents())

    def test_a_skill_missing_from_the_runbook(self):
        with Sandbox() as box:
            box.edit("SETUP.md", "`strike-advanced`", "`strike-advanced-typo`", count=None)
            self.assert_caught(validate.check_inventory, "SETUP.md does not list", validate.check_inventory())

    def test_a_broken_relative_link(self):
        with Sandbox() as box:
            box.edit("README.md", "(LICENSE)", "(LICENCE-MISSPELLED)")
            self.assert_caught(validate.check_links, "broken link", validate.check_links())

    def test_manifests_disagreeing_about_the_version(self):
        with Sandbox() as box:
            box.edit_json(".cursor-plugin/plugin.json", lambda d: d.update(version="9.9.9"))
            self.assert_caught(validate.check_manifests, "version is", validate.check_manifests())

    def test_manifests_disagreeing_about_the_name(self):
        with Sandbox() as box:
            box.edit_json(".grok-plugin/marketplace.json",
                          lambda d: d["plugins"][0].update(name="somethingelse"))
            self.assert_caught(validate.check_manifests, "name is", validate.check_manifests())

    def test_invalid_json_in_a_manifest(self):
        with Sandbox() as box:
            with open(box.path(".claude-plugin/marketplace.json"), "w", encoding="utf-8") as handle:
                handle.write('{"name": "strikegrok",')
            self.assert_caught(validate.check_manifests, "invalid JSON", validate.check_manifests())

    def test_a_component_path_that_does_not_exist(self):
        with Sandbox() as box:
            shutil.rmtree(box.path("rules"))
            self.assert_caught(validate.check_component_paths, "does not exist",
                               validate.check_component_paths())

    def test_a_component_path_escaping_the_repository(self):
        with Sandbox() as box:
            box.edit_json(".cursor-plugin/plugin.json", lambda d: d.update(skills="../skills/"))
            self.assert_caught(validate.check_component_paths, "outside the repository",
                               validate.check_component_paths())

    def test_a_document_naming_an_older_release(self):
        with Sandbox() as box:
            box.edit("README.md", f"/blob/{RELEASE}/", "/blob/v0.0.1/", count=None)
            self.assert_caught(validate.check_documented_release, "but this release is",
                               validate.check_documented_release())

    def test_a_clone_pinned_to_the_wrong_tag(self):
        with Sandbox() as box:
            box.edit("SETUP.md", f"--branch {RELEASE}", "--branch main")
            self.assert_caught(validate.check_documented_release, "git clone --branch main",
                               validate.check_documented_release())

    def test_an_install_command_naming_the_wrong_marketplace(self):
        with Sandbox() as box:
            box.edit("README.md", "/plugin marketplace add Mendurim/strikegrok-trading-desk",
                     "/plugin marketplace add someoneelse/strikegrok-trading-desk")
            self.assert_caught(validate.check_documented_install, "should be",
                               validate.check_documented_install())

    def test_an_install_command_naming_an_undeclared_plugin(self):
        with Sandbox() as box:
            box.edit("README.md", "/plugin install strikegrok@strikegrok",
                     "/plugin install strikegrok@nowhere")
            self.assert_caught(validate.check_documented_install, "is not declared",
                               validate.check_documented_install())

    def test_a_skill_edited_without_repinning_the_template(self):
        with Sandbox() as box:
            with open(box.path("skills/strike-auth/SKILL.md"), "a", encoding="utf-8") as handle:
                handle.write("\nan edit nobody re-pinned\n")
            self.assert_caught(validate.check_template, "hash is stale", validate.check_template())

    def test_a_skill_added_without_listing_it_in_the_template(self):
        with Sandbox() as box:
            added = box.path("skills/strike-brand-new")
            os.makedirs(added)
            with open(os.path.join(added, "SKILL.md"), "w", encoding="utf-8") as handle:
                handle.write("---\nname: strike-brand-new\ndescription: x\nlicense: MIT\n---\n\nbody\n")
            self.assert_caught(validate.check_template, "they must match", validate.check_template())

    def test_a_template_that_still_carries_a_share_url_it_should_not(self):
        with Sandbox() as box:
            box.edit_json("template/grok-bot.json", lambda d: d.update(routines=["something"]))
            self.assert_caught(validate.check_template, "must be empty", validate.check_template())

    def test_an_emoji_in_an_instruction_file(self):
        with Sandbox() as box:
            with open(box.path("SETUP.md"), "a", encoding="utf-8") as handle:
                handle.write("\nrocket \U0001F680\n")
            self.assert_caught(validate.check_no_emoji, "contains emoji", validate.check_no_emoji())

    def test_a_committed_credential(self):
        with Sandbox() as box:
            with open(box.path("docs/FAQ.md"), "a", encoding="utf-8") as handle:
                handle.write("\ntoken: sk_" + "A1b2C3d4E5f6G7h8I9j0K1l2" + "\n")
            self.assert_caught(validate.check_no_secrets, "committed", validate.check_no_secrets())

    def test_a_committed_private_key_block(self):
        with Sandbox() as box:
            # Assembled rather than written literally: a literal here would
            # make this very file trip the check it is testing.
            marker = "-----BEGIN " + "PRIVATE KEY" + "-----"
            with open(box.path("docs/FAQ.md"), "a", encoding="utf-8") as handle:
                handle.write("\n" + marker + "\n")
            self.assert_caught(validate.check_no_secrets, "private key", validate.check_no_secrets())


class FrontmatterParsing(unittest.TestCase):
    def test_reads_scalars_and_a_list(self):
        fields, body = validate.split_frontmatter(
            "---\nname: x\nskills:\n  - a\n  - b\nmetadata:\n  version: \"1\"\n---\nbody\n"
        )
        self.assertEqual(fields["name"], "x")
        self.assertEqual(fields["skills"], ["a", "b"])
        self.assertEqual(body.strip(), "body")

    def test_rejects_a_file_with_no_frontmatter(self):
        fields, _ = validate.split_frontmatter("# just a heading\n")
        self.assertIsNone(fields)

    def test_rejects_unterminated_frontmatter(self):
        fields, _ = validate.split_frontmatter("---\nname: x\nstill going\n")
        self.assertIsNone(fields)


if __name__ == "__main__":
    unittest.main()
