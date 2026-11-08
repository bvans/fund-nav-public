# Cloudflare external scheduler for index quotes

This Worker is the primary time-sensitive scheduler for
`.github/workflows/fetch-index-quotes.yml`.

GitHub's own `schedule` remains enabled only as a fallback because scheduled
workflow events can be delayed substantially.

## Schedule

Cloudflare Cron Triggers use UTC. These expressions correspond to China
Standard Time (Asia/Shanghai, UTC+8):

| China time | UTC cron |
| --- | --- |
| Weekdays 11:27 | `27 3 * * 1-5` |
| Weekdays 14:45 | `45 6 * * 1-5` |
| Weekdays 15:10 | `10 7 * * 1-5` |
| Weekdays 21:30 | `30 13 * * 1-5` |

## GitHub token

Create a fine-grained GitHub personal access token limited to repository
`bvans/fund-nav-public`.

Required repository permission:

- Actions: Read and write

Do not commit the token.

Store it as a Cloudflare Worker secret named `GITHUB_TOKEN`.

With Wrangler:

```bash
cd scheduler/cloudflare-worker
npx wrangler secret put GITHUB_TOKEN
npx wrangler deploy
```

Or add `GITHUB_TOKEN` as a Secret in the Cloudflare Workers dashboard and deploy.

## What the Worker does

At each cron trigger it calls GitHub's workflow-dispatch endpoint for
`fetch-index-quotes.yml` on `main` with these inputs:

- `source=cloudflare-cron`
- `scheduled_at=<Cloudflare scheduled timestamp>`

The GitHub workflow logs these values, so external dispatches are distinguishable
from GitHub-native `schedule`, `push`, and manual runs.

## Verification

A successful external trigger should produce a GitHub Actions run with:

- event: `workflow_dispatch`
- workflow: `Fetch Eastmoney index quotes`
- log line: `Dispatch source: cloudflare-cron`

The quote collector must then pass its normal Eastmoney/source-time validation
and publish `data/indices/latest.json`.
