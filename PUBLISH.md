# Publishing this desk

The Grok Bot install prompt points at a public Git URL, so this repository has to
live under your own GitHub account before the desk can be installed from it.

## 1. Set your GitHub owner

Every manifest, doc and checker currently says `OWNER`. Replace it once:

```bash
GH_OWNER=your-github-username
grep -rl OWNER --exclude-dir=.git . | xargs sed -i "s/OWNER/$GH_OWNER/g"
bash scripts/check.sh          # must pass before you publish
```

Set a real contact address in `.claude-plugin/marketplace.json` and
`.grok-plugin/marketplace.json` (both currently `owner@example.com`) while you
are there.

## 2. Re-hash the template

`template/grok-bot.json` pins the reviewed bytes of every skill, so it has to be
regenerated whenever a skill file changes:

```bash
python3 - <<'PY'
import json, hashlib
p = "template/grok-bot.json"
t = json.load(open(p))
for e in t["skills"]:
    e["sha256"] = hashlib.sha256(open(e["path"], "rb").read()).hexdigest()
json.dump(t, open(p, "w"), indent=2); open(p, "a").write("\n")
PY
bash scripts/check.sh
```

## 3. Create the repository and tag the release

```bash
git add -A && git commit -m "StrikeGrok v1.0.0"
gh repo create "$GH_OWNER/strikegrok-trading-desk" --public --source=. --push
git tag -a v1.0.0 -m "StrikeGrok v1.0.0" && git push --tags
```

The tag matters. The bootstrap skill clones `--branch v1.0.0`, so the desk your
Bots build is the desk you reviewed, not whatever `main` happens to hold. When
you change anything, bump the version in all six manifests, re-hash the
template, and cut a new tag - `scripts/check_manifests.py` fails the build if a
document names a release the manifests do not declare.

## 4. Install it in Grok Bot

Paste this to any Bot:

> Set up the StrikeGrok trading desk from
> `https://github.com/OWNER/strikegrok-trading-desk/blob/v1.0.0/skills/strikegrok-bootstrap/SKILL.md`.
> Follow the bootstrap skill, use
> `https://github.com/OWNER/strikegrok-trading-desk/blob/v1.0.0/SETUP.md` for the
> complete runbook, and finish with its evidence receipt.

The desk builds itself research-only: no token, no MCP write tool, no order.

## 5. Connect execution, when you are ready to trade

The desk reads markets with no credential at all. Execution needs the crowdtime
bearer token, and that is per-user, so it never lives in this repository.

1. Create the token in **crowdtime API Settings**.
2. Add it to **Grok Bot's secure secret store** as `STRIKE_MCP_TOKEN`.
3. Tell the Desk Lead "connect the Strike MCP" and it will run
   `strike-setup` step 4, then the read-only readiness check.

Optionally add the server as a Grok connector instead
(`grok.com/connectors` -> New Connector -> Custom ->
`https://mcp.crowdtime.io/mcp`). That needs a team admin, and xAI does not
document whether a connector reaches a named Bot, so test it before relying on
it; the secret-store route always works.

**Never commit the token.** If it has ever appeared in a chat, a log or a
screenshot, rotate it in crowdtime API Settings before the desk trades.

## 6. Optional: publish a one-click Desk Lead

`template/grok-bot.json` is `ready-to-publish`. If you publish a public Desk Lead
Bot from it, set `status` to `published`, add its `https://x.ai/bot/<id>` share
URL as `publicShareUrl`, and put the same URL in `README.md` and `docs/FAQ.md` -
`scripts/check_grok_template.py` enforces all three together.
