# Reuse and attribution

StrikeGrok Trading Desk is a port of **[HyperGrok Trading Desk](https://github.com/galleonlabs/hypergrok-trading-desk)**, created by **Andrew Wilkinson and Galleon Labs** and released under the [MIT license](LICENSE).

The seven-role desk design, the trade lifecycle, the ticket and proposal formats, the evidence standard, the `unavailable` verdict and the eight `desk-*` process skills are theirs. This repository replaces the Hyperliquid integration with Strike Finance market data and signed Strike API execution, and keeps everything else.

Both projects are MIT licensed. The original copyright and permission notice is retained in [`LICENSE`](LICENSE) and beside every skill.

## What MIT requires

When you copy or distribute this software or substantial portions of it, include the existing copyright notice and the MIT permission notice. Keep the complete `LICENSE` file with a redistributed repository or package. For extracted code or skill files, carry the applicable license notice into the destination's license or third-party notices rather than dropping it.

Preserve any other authors' copyright and license notices too. Dependencies and third-party material retain their own licenses; the MIT license does not replace them. The [license text](LICENSE) governs reuse.

## Give visible credit

A source link in your README, documentation or acknowledgements helps people find the original project. This is appreciated, although a public-facing credit line is not an extra MIT condition.

You can adapt this Markdown to describe what you actually reused:

```markdown
Based on [StrikeGrok Trading Desk](https://github.com/Mendurim/strikegrok-trading-desk),
a port of [HyperGrok Trading Desk](https://github.com/galleonlabs/hypergrok-trading-desk)
by [Andrew Wilkinson](https://andrewwilkinson.io) and
[Galleon Labs](https://github.com/galleonlabs). Used under the MIT license.
```

If you reused only part of it, say which part - a skill, the agent prompts, the Opening Bell - rather than implying you shipped the whole desk.

## Credit for the Strike port

The Strike Finance integration in this repository - the ten `strike-*` skills, the signed-request helper, the ported Opening Bell and desk doctor - is offered under the same MIT terms. No separate credit is required beyond the notice above.
