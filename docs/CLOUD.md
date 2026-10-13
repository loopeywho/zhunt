# zhunt Cloud: design and cost plan (not built)

Written 2026-10-13. Status: **design + waitlist only**. Nothing here runs yet; build it when
the gate in section 7 is met. Prices and provider terms below are planning assumptions:
re-check each one before committing money or publishing a price.

## 1. What Cloud is for

The two complaints from first use of zhunt Free/Pro are that every provider needs its own
key, and that nothing (briefs, loops, triage) runs while the laptop sleeps. Cloud fixes both:

- **Always on.** Scheduled briefs, `/loop` tasks and email triage run on our servers.
- **No keys.** The user signs in; models come from our paid provider accounts.
- **Same product.** The same engine and workspace as the local app, served from the web.

Proposed price: **$12/month**, premium-model credits included up to a monthly cap.

## 2. Hard rules (from docs/LAUNCH.md section 4)

- **Never pool free-tier keys for customers.** Free tiers are for the key holder. Cloud
  calls only **paid** accounts whose terms allow serving end users. Read each provider's
  commercial terms before adding it.
- Routing stays cheap-first: the cheapest paid model that can do the job, premium only
  when needed, so the margin survives.
- Email content is untrusted data (prompt-injection rules from `zhunt/agent/runner.py`
  carry over unchanged). Sending anything still needs the user's approval.

## 3. Architecture (recommended MVP)

```
browser ──HTTPS──> edge (Cloudflare) ──> app server(s) ──> paid model APIs
                                           │   ├─ zhunt engine (routing, quotas, failover)
                                           │   ├─ agent + briefs scheduler
                                           │   └─ per-user workspace API (same contract as docs/WORKSPACE_API.md)
                                           ├─> Postgres (users, plans, usage, briefs, encrypted secrets)
                                           └─> job queue (scheduled briefs/loops)
```

- **One multi-tenant service, not a container per user.** zhunt's engine is already a
  library behind FastAPI. Add a user id to every state key (quota, spend, layouts, bots,
  briefs) and an auth layer in front of the workspace API. A container per user is simpler
  to isolate but costs far more at $12/month.
- **Auth:** email magic link or a hosted auth provider; sessions in HttpOnly cookies. The
  local app's master-key-in-fragment scheme does not carry over.
- **Secrets:** each user's email credentials are encrypted at rest with a key held outside
  the database (envelope encryption with a managed KMS, or at minimum a server-side key
  file that is never in backups). Never logged, never returned by the API, same as local.
- **Scheduler:** a queue worker runs each user's briefs at their chosen times in their
  time zone; a run is one batched extraction call (see the briefs design), not an agent loop.
- **Metering:** every model call records tokens and cost against the user's monthly credit
  cap (`zhunt/spend.py` already does this per machine); at the cap, premium stops and
  cheap models continue, with a clear notice.
- **Billing:** the existing Stripe subscription flow, plus a webhook that marks the account
  active/past-due. No card handling of our own.

## 4. Email access: the expensive decision

| Option | User experience | Cost and obligations |
| --- | --- | --- |
| IMAP + app password (what the local agent uses) | user creates an app password once | cheap; we store a powerful credential, so encryption and breach response matter |
| Gmail / Microsoft 365 OAuth (what Hark uses) | "Sign in with Google" | Gmail's read/modify scopes are *restricted*: Google requires app verification and an annual third-party security assessment for apps that store or process that data on servers. Budget time and money for it before promising Gmail OAuth |

Recommendation: launch Cloud with IMAP app passwords (as local), add OAuth only once revenue
covers the assessment.

## 5. Cost model per user per month (planning assumptions)

| Item | Assumption | Per user |
| --- | --- | --- |
| Model tokens | cheap-first routing, premium capped | ~$5.00 (cap; most users below) |
| Compute + database | small multi-tenant servers, a few hundred users per instance | ~$0.30–1.00 |
| Email/queue/storage | briefs 3x/day, logs kept 30 days | ~$0.10 |
| Stripe fees | 2.9% + $0.30 on $12 (card, US) | ~$0.65 |
| **Total** | | **~$6–7** → gross margin ~45–50% at $12 |

The token line decides everything. Measure it from Pro first (how often premium fallback
fires, tokens per brief) before fixing the price.

## 6. Security, privacy and legal checklist (before the first paying Cloud user)

- Privacy policy and terms updated for hosted processing of prompts and email (the local
  drafts in `docs/legal/` say the opposite today), reviewed by a professional.
- Data processing terms for business customers; list of sub-processors (host, database,
  model providers, Stripe, Cloudflare).
- Retention: prompts and email snippets kept only as long as needed (e.g. briefs 30 days),
  deletion on account close, export on request.
- Encryption in transit and at rest; secrets in KMS; least-privilege access; audit log of
  admin access.
- Incident response plan, and someone on call for it.
- Provider terms reviewed for each paid account (serving end users, data use, training
  opt-outs).

## 7. Gate and milestones

**Gate to start building:** about 100 Pro subscribers, or 300+ waitlist sign-ups with
replies saying they would pay $12. Track the waitlist from the site's "Join the waitlist"
email (subject "Zhunt Cloud waitlist"); move to a proper sign-up form only when the
privacy policy covers it.

1. **Pilot (owner's own VPS, 5–10 invited users, free).** Multi-tenant engine + auth +
   scheduled briefs on IMAP. Bound to localhost behind a reverse proxy with HTTPS. Measure
   tokens per user per day.
2. **Paid beta ($12, capped at ~50 users).** Stripe webhook, credit cap, privacy policy
   and terms live, backups and monitoring.
3. **General availability.** Managed database and KMS, OAuth for Gmail/Microsoft if the
   assessment is affordable, teams plan later.

## 8. Open questions for the owner

- Which paid model accounts to start with, and their per-token prices on the day.
- Hosting for the pilot: the existing Hostinger VPS is fine for 5–10 users; move to a
  managed platform before paid beta.
- Support load: Cloud users will expect faster help than Pro; who answers?
