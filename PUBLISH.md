# Publishing this desk

The Grok Bot install prompt points at a public Git URL, so this repository has to
live under your own GitHub account before the desk can be installed from it.

## 1. The repository owner

This release is already set to **`Mendurim/strikegrok-trading-desk`** throughout:
manifests, docs, the pinned template and the release checkers.

If you fork it to another account, substitute once and re-check:

```bash
GH_OWNER=your-github-username
grep -rl Mendurim --exclude-dir=.git . | xargs sed -i "s/Mendurim/$GH_OWNER/g"
python3 scripts/rehash_template.py
bash scripts/check.sh
```

The contact address in `.claude-plugin/marketplace.json` and
`.grok-plugin/marketplace.json` is GitHub's `users.noreply.github.com` form, so
no personal address is published. Change it if you want a real one.

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
git add -A && git commit -m "StrikeGrok v1.0.1"
gh repo create "$GH_OWNER/strikegrok-trading-desk" --public --source=. --push
git tag -a v1.0.1 -m "StrikeGrok v1.0.1" && git push --tags
```

The tag matters. The bootstrap skill clones `--branch v1.0.1`, so the desk your
Bots build is the desk you reviewed, not whatever `main` happens to hold. When
you change anything, bump the version in all six manifests, re-hash the
template, and cut a new tag - `scripts/check_manifests.py` fails the build if a
document names a release the manifests do not declare.

## 4. Install it in Grok Bot

Paste this to any Bot:

> Set up the StrikeGrok trading desk from
> `https://github.com/Mendurim/strikegrok-trading-desk/blob/v1.0.1/skills/strikegrok-bootstrap/SKILL.md`.
> Follow the bootstrap skill, use
> `https://github.com/Mendurim/strikegrok-trading-desk/blob/v1.0.1/SETUP.md` for the
> complete runbook, and finish with its evidence receipt.

The desk builds itself research-only: no token, no MCP write tool, no order.

## 5. Connect execution, when you are ready to trade

The desk reads markets with no credential at all. Execution needs a **Strike API
wallet** - an Ed25519 keypair you generate and register yourself.

1. Generate the keypair on the desk computer (`strike-auth` section 2):

   ```bash
   umask 077
   openssl genpkey -algorithm ed25519 -out /tmp/api-wallet.pem
   openssl pkey -in /tmp/api-wallet.pem -outform DER | tail -c 32 | xxd -p -c 64   # private seed
   openssl pkey -in /tmp/api-wallet.pem -pubout -outform DER | tail -c 32 | xxd -p -c 64   # public
   shred -u /tmp/api-wallet.pem
   ```

2. Register the **public** key at `app.strikefinance.org/api-keys`.
3. Put both into **Grok Bot's secure secret store** as `STRIKE_API_PUBLIC_KEY`
   and `STRIKE_API_PRIVATE_KEY`. Never chat, never a file in the repository.
4. Tell the Desk Lead "set up the Strike API wallet" and it will run the
   readiness check.

The key can trade. Neither the trade API nor the user API exposes a withdraw,
deposit or transfer endpoint, so it cannot take money out - moving funds happens
in the Strike app, with you.

**Never commit a private key.** If one has appeared in a chat, a log or a
screenshot, register a new key and delete the old one before the desk trades.

### Optional: the research add-on

`strike-research-tools` uses the crowdtime MCP for a liquidity screen, computed
indicators, news and dividend research playbooks, Bodega prediction markets and
Discord alerts. It is **not** on the trading path and the desk works fully
without it. If you want it, put its bearer token in the secret store as
`STRIKE_MCP_TOKEN`; if you do not, provision nothing - the smallest credential
set is the safest one.

## 6. Optional: publish a one-click Desk Lead

`template/grok-bot.json` is `ready-to-publish`. If you publish a public Desk Lead
Bot from it, set `status` to `published`, add its `https://x.ai/bot/<id>` share
URL as `publicShareUrl`, and put the same URL in `README.md` and `docs/FAQ.md` -
`scripts/check_grok_template.py` enforces all three together.
