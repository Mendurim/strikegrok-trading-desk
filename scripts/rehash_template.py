#!/usr/bin/env python3
"""Re-pin template/grok-bot.json to the current skill bytes.

The template records a sha256 per skill so a Bot can tell a reviewed skill from
a drifted one. Run this after editing any SKILL.md, then re-run scripts/check.sh.
"""
import hashlib
import json
import os

TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "template", "grok-bot.json")
ROOT = os.path.dirname(os.path.dirname(TEMPLATE))


def main() -> None:
    with open(TEMPLATE, encoding="utf-8") as handle:
        template = json.load(handle)
    skills = sorted(
        name for name in os.listdir(os.path.join(ROOT, "skills"))
        if os.path.isdir(os.path.join(ROOT, "skills", name))
    )
    template["skills"] = [
        {
            "name": name,
            "path": f"skills/{name}/SKILL.md",
            "sha256": hashlib.sha256(
                open(os.path.join(ROOT, "skills", name, "SKILL.md"), "rb").read()
            ).hexdigest(),
        }
        for name in skills
    ]
    with open(TEMPLATE, "w", encoding="utf-8") as handle:
        json.dump(template, handle, indent=2)
        handle.write("\n")
    print(f"re-pinned {len(skills)} skills in template/grok-bot.json")


if __name__ == "__main__":
    main()
