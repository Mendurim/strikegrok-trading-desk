#!/usr/bin/env python3
"""Check that this repository is internally consistent and safe to publish.

Offline, standard library only, no credentials. Run it before every commit and
before every tag:

    python3 scripts/validate.py            # check
    python3 scripts/validate.py --list     # show what each check does

Each check returns a list of problems. A problem is something that would give a
reader, a Bot, or an installer the wrong answer - a skill an agent references
but that does not exist, a version two manifests disagree about, an install
command naming an id this repository does not declare. Style is not a problem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLUGIN_NAME = "strikegrok"
REPO_SLUG = "Mendurim/strikegrok-trading-desk"
REPO_URL = f"https://github.com/{REPO_SLUG}"

# Manifests that must all agree about who this is and which version it is.
MANIFESTS = (
    "plugin.json",
    ".claude-plugin/plugin.json",
    ".grok-plugin/plugin.json",
    ".cursor-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    ".grok-plugin/marketplace.json",
)

# Files that describe the desk to a reader and so must name only this release.
# The changelog and provenance record cite other versions on purpose.
VERSION_EXEMPT = {"CHANGELOG.md", "CONTRIBUTING.md", "docs/PROVENANCE.md"}

SKILL_BODY_LIMIT = 320
DESCRIPTION_LIMIT = 1024

VERSION_TAG = re.compile(r"\bv(\d+\.\d+\.\d+)\b")
CLONE_BRANCH = re.compile(r"git clone[^\n`]*--branch\s+(\S+)")
# Stop at the id itself: these appear inside backticks and sentences, so a
# greedy \S+ would swallow the closing backtick and the full stop with it.
MARKETPLACE_ADD = re.compile(r"/plugin marketplace add\s+([A-Za-z0-9._/-]+)")
PLUGIN_INSTALL = re.compile(r"/plugin install\s+([A-Za-z0-9._@-]+)")
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


def markdown_files() -> list[str]:
    found = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules"}]
        for name in names:
            if name.endswith(".md"):
                found.append(os.path.relpath(os.path.join(base, name), ROOT))
    return sorted(found)


def split_frontmatter(text: str) -> tuple[dict, str] | tuple[None, str]:
    """Parse the small YAML subset these files use: scalars and one list.

    Deliberately not a YAML parser. The frontmatter here is a handful of
    `key: value` lines plus a `skills:` list, and a real parser would be a
    dependency for no gain.
    """
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return None, text
    fields: dict = {}
    key = None
    for line in text[4:end].splitlines():
        item = re.match(r"^\s+-\s+(.*)$", line)
        if item and key:
            fields.setdefault(key, [])
            if isinstance(fields[key], list):
                fields[key].append(item.group(1).strip())
            continue
        pair = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if pair:
            key = pair.group(1)
            value = pair.group(2).strip()
            fields[key] = value if value else []
        # Indented `key: value` lines belong to a nested map (metadata); skip.
    return fields, text[end + 5:]


def skill_names() -> list[str]:
    path = os.path.join(ROOT, "skills")
    return sorted(d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d)))


def agent_names() -> list[str]:
    path = os.path.join(ROOT, "agents")
    return sorted(f[:-3] for f in os.listdir(path) if f.endswith(".md"))


def declared_version() -> str | None:
    try:
        return json.loads(read("plugin.json")).get("version")
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def check_skills() -> list[str]:
    """Every skill parses, is named after its directory, and stays in budget."""
    problems = []
    for name in skill_names():
        rel = f"skills/{name}/SKILL.md"
        if not os.path.isfile(os.path.join(ROOT, rel)):
            problems.append(f"skills/{name}: no SKILL.md")
            continue
        fields, body = split_frontmatter(read(rel))
        if fields is None:
            problems.append(f"{rel}: frontmatter missing or unterminated")
            continue
        if fields.get("name") != name:
            problems.append(f"{rel}: name is {fields.get('name')!r}, directory is {name!r}")
        description = fields.get("description")
        if not isinstance(description, str) or not description:
            problems.append(f"{rel}: needs a description")
        elif len(description) > DESCRIPTION_LIMIT:
            problems.append(f"{rel}: description is {len(description)} chars, limit {DESCRIPTION_LIMIT}")
        if fields.get("license") != "MIT":
            problems.append(f"{rel}: license must be MIT")
        lines = body.count("\n")
        if lines > SKILL_BODY_LIMIT:
            problems.append(f"{rel}: body is {lines} lines, budget {SKILL_BODY_LIMIT}")
        for companion in ("LICENSE", "ATTRIBUTION.md"):
            if not os.path.isfile(os.path.join(ROOT, "skills", name, companion)):
                problems.append(f"skills/{name}: missing {companion}")
    return problems


def check_agents() -> list[str]:
    """Agents are well formed, reference real skills, and exactly one writes.

    The single-writer rule is the desk's central control. If this check ever
    fails, the repository is describing a desk with a different safety model.
    """
    problems = []
    known = set(skill_names())
    writers = []
    for name in agent_names():
        rel = f"agents/{name}.md"
        fields, body = split_frontmatter(read(rel))
        if fields is None:
            problems.append(f"{rel}: frontmatter missing or unterminated")
            continue
        if fields.get("name") != name:
            problems.append(f"{rel}: name is {fields.get('name')!r}, filename is {name!r}")
        for required in ("title", "description", "seat", "writes_to_exchange"):
            if required not in fields:
                problems.append(f"{rel}: missing {required!r}")
        if fields.get("seat") not in ("floor", "off-floor"):
            problems.append(f"{rel}: seat must be floor or off-floor")
        for skill in fields.get("skills") or []:
            if skill not in known:
                problems.append(f"{rel}: references unknown skill {skill!r}")
        for heading in ("## Bot profile", "## System prompt"):
            if heading not in body:
                problems.append(f"{rel}: needs a {heading!r} section")
        if fields.get("writes_to_exchange") == "true":
            writers.append(name)
    if writers != ["execution-trader"]:
        problems.append(
            "exactly one agent may write to the exchange and it must be the "
            f"execution-trader; found {writers or 'none'}"
        )
    return problems


def check_inventory() -> list[str]:
    """The runbook and the skills index list every skill that ships."""
    problems = []
    setup = read("SETUP.md")
    index = read("skills/README.md")
    for name in skill_names():
        if f"`{name}`" not in setup:
            problems.append(f"SETUP.md does not list {name}")
        if f"({name}/SKILL.md)" not in index:
            problems.append(f"skills/README.md does not link {name}")
    for name in agent_names():
        if f"agents/{name}.md" not in setup:
            problems.append(f"SETUP.md does not list agents/{name}.md")
    return problems


def check_links() -> list[str]:
    """Relative markdown links resolve to something that exists."""
    problems = []
    for rel in markdown_files():
        for target in MD_LINK.findall(read(rel)):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#")[0]
            if not target:
                continue
            resolved = os.path.normpath(os.path.join(ROOT, os.path.dirname(rel), target))
            if not os.path.exists(resolved):
                problems.append(f"{rel}: broken link -> {target}")
    return problems


def check_manifests() -> list[str]:
    """All six manifests agree on the plugin's identity and version."""
    problems = []
    version = declared_version()
    if not version:
        return ["plugin.json: no version"]
    for rel in MANIFESTS:
        try:
            data = json.loads(read(rel))
        except OSError:
            problems.append(f"{rel}: missing")
            continue
        except json.JSONDecodeError as exc:
            problems.append(f"{rel}: invalid JSON ({exc})")
            continue
        entries = [data] + list(data.get("plugins") or [])
        for entry in entries:
            where = rel if entry is data else f"{rel} plugins[]"
            if entry.get("name") not in (PLUGIN_NAME, None):
                problems.append(f"{where}: name is {entry.get('name')!r}, expected {PLUGIN_NAME!r}")
            if entry.get("version") not in (version, None):
                problems.append(f"{where}: version is {entry.get('version')!r}, expected {version!r}")
            for key in ("repository", "homepage"):
                if key in entry and entry[key] != REPO_URL:
                    problems.append(f"{where}: {key} is {entry[key]!r}, expected {REPO_URL!r}")
    return problems


def check_component_paths() -> list[str]:
    """Component paths a manifest advertises exist and stay inside the repo."""
    problems = []
    for rel in MANIFESTS:
        try:
            data = json.loads(read(rel))
        except (OSError, json.JSONDecodeError):
            continue  # reported by check_manifests
        for key in ("skills", "agents", "rules", "logo"):
            value = data.get(key)
            if not isinstance(value, str):
                continue
            resolved = os.path.normpath(os.path.join(ROOT, value))
            if not resolved.startswith(ROOT):
                problems.append(f"{rel}: {key} points outside the repository")
            elif not os.path.exists(resolved):
                problems.append(f"{rel}: {key} -> {value} does not exist")
    return problems


def check_documented_release() -> list[str]:
    """Instruction files name this release, and clones of it are pinned.

    A runbook naming an older tag installs an older desk, silently. That is the
    defect a reader cannot see and cannot work around.
    """
    problems = []
    version = declared_version()
    if not version:
        return []
    expected = f"v{version}"
    for rel in markdown_files():
        if rel in VERSION_EXEMPT:
            continue
        text = read(rel)
        for tag in sorted(set(VERSION_TAG.findall(text))):
            if f"v{tag}" != expected:
                problems.append(f"{rel}: names v{tag}, but this release is {expected}")
        for branch in CLONE_BRANCH.findall(text):
            if branch != expected:
                problems.append(f"{rel}: git clone --branch {branch}, expected {expected}")
        if f"{REPO_URL}.git" in text and "--branch" not in text:
            problems.append(f"{rel}: clones this repository without pinning a tag")
    return problems


def check_documented_install() -> list[str]:
    """Install commands in the docs name ids this repository actually declares."""
    problems = []
    try:
        marketplace = json.loads(read(".claude-plugin/marketplace.json"))
    except (OSError, json.JSONDecodeError):
        return []
    valid_ids = {
        f"{p.get('name')}@{marketplace.get('name')}"
        for p in (marketplace.get("plugins") or [])
    }
    for rel in markdown_files():
        text = read(rel)
        for slug in MARKETPLACE_ADD.findall(text):
            if slug != REPO_SLUG:
                problems.append(f"{rel}: 'marketplace add {slug}' should be {REPO_SLUG}")
        for install in PLUGIN_INSTALL.findall(text):
            if install not in valid_ids:
                problems.append(
                    f"{rel}: 'plugin install {install}' is not declared; "
                    f"expected one of {sorted(valid_ids)}"
                )
    return problems


def check_template() -> list[str]:
    """The published template pins the exact bytes of every skill that ships."""
    problems = []
    try:
        template = json.loads(read("template/grok-bot.json"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"template/grok-bot.json: unreadable ({exc})"]

    version = declared_version()
    if version and template.get("source", {}).get("release") != f"v{version}":
        problems.append(
            f"template/grok-bot.json: source.release is "
            f"{template.get('source', {}).get('release')!r}, expected v{version}"
        )
    if template.get("source", {}).get("repository") != REPO_URL:
        problems.append("template/grok-bot.json: source.repository must be this repository")
    for empty in ("plugins", "memories", "routines"):
        if template.get(empty) != []:
            problems.append(f"template/grok-bot.json: {empty} must be empty on a public template")

    listed = [entry.get("name") for entry in template.get("skills") or []]
    if listed != skill_names():
        problems.append(
            f"template/grok-bot.json: lists {len(listed)} skills, repository ships "
            f"{len(skill_names())}; they must match in name order"
        )
        return problems
    for entry in template["skills"]:
        path = os.path.join(ROOT, entry["path"])
        if not os.path.isfile(path):
            problems.append(f"template/grok-bot.json: {entry['path']} does not exist")
            continue
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        if digest != entry.get("sha256"):
            problems.append(
                f"template/grok-bot.json: {entry['name']} hash is stale "
                "(run scripts/rehash_template.py)"
            )
    return problems


def check_no_emoji() -> list[str]:
    """Instruction files carry no emoji; a Bot reads them as content."""
    return [f"{rel}: contains emoji" for rel in markdown_files() if EMOJI.search(read(rel))]


def check_no_secrets() -> list[str]:
    """Nothing that looks like a credential is committed."""
    patterns = (
        (re.compile(r"\bsk_[A-Za-z0-9]{24,}"), "api-token-shaped string"),
        (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
        (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key block"),
    )
    problems = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules"}]
        for name in names:
            rel = os.path.relpath(os.path.join(base, name), ROOT)
            if rel.startswith("assets/"):
                continue
            try:
                text = read(rel)
            except (UnicodeDecodeError, OSError):
                continue
            for pattern, label in patterns:
                if pattern.search(text):
                    problems.append(f"{rel}: looks like a committed {label}")
    return problems


CHECKS = (
    ("skills", check_skills),
    ("agents", check_agents),
    ("inventory", check_inventory),
    ("links", check_links),
    ("manifests", check_manifests),
    ("component paths", check_component_paths),
    ("release pin", check_documented_release),
    ("install commands", check_documented_install),
    ("template", check_template),
    ("no emoji", check_no_emoji),
    ("no secrets", check_no_secrets),
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="describe the checks and exit")
    args = parser.parse_args(argv)

    if args.list:
        for name, function in CHECKS:
            summary = (function.__doc__ or "").strip().splitlines()[0]
            print(f"  {name:<17} {summary}")
        return 0

    failures = 0
    for name, function in CHECKS:
        problems = function()
        if problems:
            failures += len(problems)
            for problem in problems:
                print(f"FAIL  {name}: {problem}")
        else:
            print(f"ok    {name}")
    if failures:
        print(f"\n{failures} problem(s)", file=sys.stderr)
        return 1
    print(f"\nok: {len(skill_names())} skills, {len(agent_names())} agents, "
          f"{len(markdown_files())} markdown files, release v{declared_version()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
