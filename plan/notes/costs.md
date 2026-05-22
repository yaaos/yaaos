# Costs

6-month hosting cost projection for yaaos control plane. Customers BYOK the review LLM, so yaaos's own LLM spend is negligible (small HITL classifiers on cheap models). AWS dropped — baseline NAT/ALB/WAF tax makes it the wrong shape at POC scale.

Scenarios:

- **Pessimistic** — solo (Jack only)
- **Realistic** — 24 devs at Dscout
- **Optimistic** — 40 devs at Dscout + 30 devs at Gaggle

## Hosting providers considered

| Provider | Sweet spot | What it bundles |
|---|---|---|
| **Fly.io** | Containers, global edge | Compute + private networking + load balancing; no NAT tax |
| **Railway** | All-in-one PaaS | Compute + Postgres + Redis on one bill |
| **Render** | Heroku-style PaaS | Compute + managed PG + Redis |
| **Hetzner** | Bare VPS | Just a Linux box; ops on you |
| **Neon** | Serverless Postgres | Auto-scale, branching, generous free tier |
| **Upstash** | Serverless Redis | Pay-per-command, free tier |
| **Cloudflare** | Edge / CDN | Free WAF, free SPA on Pages, free DNS |

## Chosen stack: Fly.io + Neon + Upstash + Cloudflare

| Component | Pessimistic | Realistic | Optimistic |
|---|---|---|---|
| Fly.io compute | 1× shared-1x-1GB ($5) | 2× shared-2x-2GB ($30) | 2× perf-2x-4GB ($85) |
| Neon Postgres | Free | Launch ($19) | Scale ($69) |
| Upstash Redis | Free | PAYG (~$10) | Pro ($30) |
| Cloudflare | Free | Pro ($25) | Pro ($25) |
| Observability + backups | $0 | ~$10 | ~$25 |
| **Total / mo** | **~$5** | **~$94** | **~$234** |
| **6-month total** | **~$30** | **~$564** | **~$1,404** |

## Email — mailboxes (`jack@yaaos.com`)

| Option | Cost | Use when |
|---|---|---|
| Cloudflare Email Routing | Free | Solo founder; forward to existing inbox |
| iCloud+ Custom Domain | $1/mo | Already on iCloud+ |
| Fastmail | $5/user/mo | Independent, no ads, no AI training |
| Google Workspace | $7/user/mo | Team formed; enterprise customer-facing |
| Zoho Mail Free | Free (≤5 users) | Cheapest real mailbox |

Plan: Cloudflare Email Routing today → Google Workspace once there's a co-founder or first paying customer.

## Email — transactional (app-sent mail)

| Service | Cost | Notes |
|---|---|---|
| Resend | Free (3K/mo), $20/mo Pro (50K) | Default choice |
| Postmark | $15/mo (10K) | Best transactional deliverability |
| AWS SES | ~$0.10 per 1K | Cheapest at scale, more setup |

Use a subdomain (`mail.yaaos.com`) for marketing later; keep `yaaos.com` clean for transactional.

## Email cost impact

| Tier | Mailboxes | Transactional | Email / mo |
|---|---|---|---|
| Pessimistic | $0 (CF routing) | $0 (Resend free) | **$0** |
| Realistic | $7 (1× Google Workspace) | $0 (Resend free) | **$7** |
| Optimistic | $21 (3× Google Workspace) | $20 (Resend Pro) | **$41** |

## All-in 6-month totals

| Scenario | Hosting | Email | **Total** |
|---|---|---|---|
| Pessimistic | $30 | $0 | **~$30** |
| Realistic | $564 | $42 | **~$606** |
| Optimistic | $1,404 | $246 | **~$1,650** |
