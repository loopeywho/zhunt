# Zhunt: business model and launch plan

Written 2026-10-11. Prices of other products are from public listings found that day and
should be re-checked before quoting them publicly.

## 1. What we sell

**One workspace for every AI model, free first.** Zhunt answers with the best free model
available (Groq, Google AI Studio, Cerebras), fails over automatically when one runs dry,
and lives in its own desktop (floating or tiled windows, themes). It runs on the user's
computer with the user's own keys.

The ChatLLM idea (one place, many models, automatic routing) is the product. The
difference is the price floor: ChatLLM has no free plan; zhunt's free plan is genuinely
useful because free tiers do most everyday work.

| Product | Price | Free plan | Notes |
| --- | --- | --- | --- |
| Abacus ChatLLM | $10 Basic, $20 Pro /month | none | hosted, credits, top models included |
| T3 Chat | ~$8 /month | limited | hosted, rate-limits premium models |
| Poe | ~$20 /month | limited | hosted, points system |
| OpenRouter | pay per token | 50 req/day | API only, needs a separate chat app |
| **zhunt Free** | $0 | yes | local, free-tier models, user's free keys, all themes |
| **zhunt Pro** | $7 /month, 7-day trial | | adds premium fallback (user's paid keys) |
| **zhunt Cloud** (phase 2) | $12 /month (proposed) | | hosted, premium credits included: the full ChatLLM model |

## 2. Plans and what is built

| | Free | Pro (built) | Cloud (phase 2, not built) |
| --- | --- | --- | --- |
| Free-tier routing + failover | yes | yes | yes |
| Workspace, floating/tiled, dark/light | yes | yes | yes |
| Own themes and layouts (files) | yes | yes | yes |
| Premium fallback (GPT, Claude, Gemini Pro) with monthly cap | | yes, user's own paid keys | yes, included credits |
| Free-model catalog | release + monthly snapshot (`zhunt catalog update`) | live, every 12 h (opt-in switch) | live |
| All 12 themes (incl. 10 Omarchy-style) | yes | yes | yes |
| Nothing to install | | | yes |

How Pro is delivered (all in the repo):

- Premium fallback: five premium models in `zhunt/catalog.yaml`, called directly with the
  user's own paid keys: Anthropic (Claude Haiku/Sonnet 5.5), OpenAI (GPT-5.6 Luna/Sol)
  and Google (Gemini 3.8 Flash, on a separate billing-enabled key). Each provider is
  optional. Used only after every free model is busy, only with Pro **and** an explicit
  `zhunt premium on --cap N`, stopping at the monthly cap (spend kept in
  `~/.zhunt/state.db`). No reseller sits between zhunt and the model company.

- `zhunt/licence.py`: Ed25519-signed keys verified **offline** (zhunt never phones home).
  `zhunt licence activate|status|renew|remove`, `zhunt upgrade`.
- `site/functions/api/licence.js`: Cloudflare Pages Function. Stripe checkout success page
  (`site/activate.html`) exchanges the session for a key; `zhunt licence renew` gets a fresh
  key for a live subscription. Keys expire with the paid period plus 7 days' grace.
- Signing key: generated with `scripts/licence_tool.py keygen`, stored at
  `~/.zhunt-signing/licence-2026-10.pem` (outside the repo). **Back it up** (password
  manager or encrypted drive). Losing it means every customer needs a new key from a new
  signing key shipped in a new release.
- Live catalog: `zhunt/catalog_feed.py` + `scripts/catalog_tool.py`, see "The moat" below.
- Gating is honest open-core: the code is open, so the check can be patched out. The
  licence is how people who want Pro pay for it, not DRM. That is normal for open-core
  tools and keeps goodwill with the open-source audience.

### The moat: a live, verified free-model catalog

Free tiers change weekly; a catalog that only moves with releases goes stale. zhunt
publishes its hand-verified `zhunt/catalog.yaml` as an Ed25519-signed feed (same idea as
FreeLLMAPI's catalog sync, without a licence check on the server):

- `site/catalog/latest.json`: re-published whenever a row is re-verified. Pro checks it
  every 12 hours once the user switches updates on in the Connect window.
- `site/catalog/monthly.json` (+ `monthly/YYYY-MM.json`): the first publish of each
  month. Free users get it with `zhunt catalog update`.
- Both are public static files: no request carries a licence or any identifier, so the
  "never phones home unless you turn it on" promise holds. The Pro edge is freshness and
  automation, enforced client-side like every other Pro feature (honest open-core). If
  that ever needs a server-side gate, put `latest.json` behind a Pages Function that
  checks a licence header, and say so in the privacy policy first.
- Clients accept a feed only if the signature (catalog key, separate from the licence
  key), expiry (latest 45 days, monthly 75 days) and schema check out, the version is
  newer than what they run and every provider uses a route zhunt can call. Failures keep
  the bundled catalog. `catalog.yaml`'s `version` is the floor each release ships with.

The value is the verification work: re-check each provider's free tier, update the
`verified_on` dates and rows, publish. Aim for at least weekly.

**Owner steps (one-off):**
1. `uv run python scripts/catalog_tool.py keygen --out ~/.zhunt-signing/catalog-2026-10.pem --kid cat-2026-10`
   and back the key up next to the licence key (losing it means a new kid in a release).
2. Paste the printed line into `PUBLIC_KEYS` in `zhunt/catalog_feed.py`, commit, release.
   Until then every zhunt says "this zhunt has no catalog signing key yet" and keeps the
   bundled catalog.

**Weekly review (Mondays, a scheduled Claude task drafts it):**
`uv run python scripts/catalog_tool.py check` asks each free provider which models it lists
today (keys from `~/.zhunt/env`; Kilo and OVH need none) and reports models that disappeared, new
ones, and providers whose free-tier terms were not re-read for over 7 days. The task re-reads
the free-tier pages, drafts `catalog.yaml` changes on a `catalog/review-<date>` branch and
waits for approval; nothing is published until the owner says so. Add free keys
(`zhunt keys add groq|google|cerebras|zai|mistral|sambanova`) so every provider's list can be checked.

**Each time the catalog changes:**
1. Edit and re-verify `zhunt/catalog.yaml`.
2. `uv run python scripts/catalog_tool.py publish --key ~/.zhunt-signing/catalog-2026-10.pem --kid cat-2026-10`
   (validates, stamps `version`, signs, writes `site/catalog/`; the first run each month
   also writes the monthly snapshot).
3. Commit `zhunt/catalog.yaml` and `site/catalog/`, then deploy:
   `uv run python scripts/build_site.py && cd dist/site && npx wrangler pages deploy . --project-name zhunt --branch main`.
4. Check: `curl -s https://zhunt.kindredwildcat.com/catalog/latest.json | head -c 200`, then
   `zhunt catalog update` on a Pro install.

## 3. Unit economics

**Pro ($7/month, BYO keys).** zhunt pays no model costs. Stripe takes about 2.9% + $0.30
per charge (card, US; check current rates), so about **$6.50 net per subscriber-month**.
No servers beyond a static site and one serverless function (Cloudflare free tier covers
this comfortably). Break-even is effectively the first subscriber; the cost is time.

Illustrative only (not a forecast): 2,000 free installs a month, 3% start a trial, 50% of
trials convert, 6% monthly churn → about 30 new paying users a month, settling towards
~500 subscribers, about **$3,250/month net**. The levers are installs and trial→paid.

**Cloud ($12/month proposed, credits included).** This is the ChatLLM model and it has
real cost of goods: premium tokens bought from the model companies on paid accounts. Budget about $5 of model spend per user per month at $12, routed
free-first so most turns cost nothing, with a hard monthly credit cap per user. Margin
depends entirely on that routing; measure it in Pro first (telemetry of how often premium
fallback fires) before committing to a Cloud price.

## 4. Rules we must not break

- **Free tiers cannot be resold.** Groq, Google AI Studio and Cerebras free keys are for
  the key holder. Free and Pro are fine because users bring their own keys. Cloud must use
  **paid** API accounts that permit serving end users; never pool free keys for customers.
- **One key per provider** in Free/Pro (FreeLLMAPI's own docs say one account per
  provider; Cerebras forbids transferring keys). Do not add multi-account pooling.
- **Training disclosure:** keep the privacy gate on by default; say plainly which
  providers may train on prompts.
- **Attribution:** FreeLLMAPI code ported into the engine carries its MIT notice
  (`THIRD_PARTY_NOTICES.md`).
- **Consumer law:** the checkout disclosure (trial, renewal price, cancellation) is on the
  site; keep it accurate. Publish a privacy policy and terms of service before taking
  money publicly (see checklist). Have a professional review them.

## 5. Launch checklist

Owner = you unless marked. Order matters.

**A. Make it installable (week 0)**
1. Review and merge `pivot/free-tier-workspace` into `main`; push. The site's install
   command (`uv tool install git+https://github.com/loopeywho/zhunt`) installs `main`.
2. Remove the two `ANTHROPIC_*` lines from your own `~/.claude/settings.json` (left by v1).
3. Live test with real free keys: `zhunt keys add groq` (and Google, Cerebras), `zhunt
   serve`, `zhunt open`, one request per tier; run `scripts/live_tier_check.py`.
4. Optional but recommended: publish to PyPI so the command becomes `uv tool install
   zhunt` (`uv build`, `uv publish`, with a PyPI token in a password manager).

**B. Turn on payments (week 0–1)**
5. Cloudflare Pages: **done 2026-10-11** — `site/` is live on zhunt.kindredwildcat.com
   (project `zhunt`) and `LICENCE_KID=2026-10` is set. Still to set, as encrypted variables:
   `STRIPE_SECRET_KEY` (a *restricted* key: read Checkout Sessions, Subscriptions,
   Customers), `LICENCE_KID=2026-10`, `LICENCE_PRIVATE_KEY` (output of
   `uv run python scripts/licence_tool.py worker-secret --key ~/.zhunt-signing/licence-2026-10.pem`).
6. Stripe: on the existing payment link (`buy.stripe.com/28EdR883o8tSgfE2RncMM01`) set the
   confirmation page to redirect to
   `https://zhunt.kindredwildcat.com/activate?session_id={CHECKOUT_SESSION_ID}`.
   Turn on the customer portal (cancellation) and link it from emails.
7. End-to-end test in Stripe test mode first: test-mode payment link → activate page →
   `zhunt licence activate` → `zhunt licence status` shows Pro →
   `zhunt keys add anthropic` + `zhunt premium on --cap 1` → a request with free keys
   removed is answered by a premium model and the spend shows in Panels.
8. Publish privacy policy and terms, linked from the site footer and checkout. Drafts written
   from what the code actually does are in `docs/legal/privacy.md` and `docs/legal/terms.md`:
   fill in the bracketed items (legal name, jurisdiction, refund policy) and have them
   reviewed, then publish them as `site/privacy.html` and `site/terms.html`.

**C. Launch (week 1–2)**
9. 60–90 s demo video: a prompt hits a rate limit, zhunt fails over, the routing window
   says why; switch floating ↔ tiled; change theme.
10. Post in this order, a few days apart, answering every comment:
    - Show HN: "Zhunt: every free LLM tier in one local workspace, with automatic failover"
    - r/LocalLLaMA, r/selfhosted, r/opensource (lead with free + local + open source)
    - Omarchy / Hyprland communities (lead with the tiled desktop and themes)
    - X/Bluesky/LinkedIn thread with the video; Product Hunt once early feedback is in
11. Keep the feedback email flowing into a simple tracker; fix the top three complaints in
    the first week and say so publicly.

**D. Measure (from day 1, privacy-preserving)**
- Installs: GitHub clones/stars, PyPI downloads (no in-app tracking; zhunt does not phone
  home, and catalog-feed downloads carry no identifier, so they are not a user count).
- Trials started, trial→paid conversion, monthly churn: Stripe dashboard.
- Activation: ask in the welcome email whether the first request worked.
- Targets for month one: 500 installs, 30 trials, 15 paying.

**E. Phase 2: Cloud (after ~100 Pro subscribers or clear demand on the waitlist)**
- Hosted zhunt with accounts, paid provider accounts, per-user credit caps and metering.
- Price from Pro telemetry: how often premium fallback fires and what it costs.
- Teams plan: shared policies and billing, once individual Cloud works.

## 6. Risks

| Risk | Mitigation |
| --- | --- |
| Providers shrink free tiers | multi-provider catalog with dates; signed live catalog feed between releases; learned limits; Pro fallback is the safety net |
| Catalog signing key leaked | separate from the licence key; ship a new kid and drop the old one in a release; feeds expire within 75 days |
| "Why pay if it's open source?" | price low ($7), sell convenience and support; Cloud is the real business |
| Free keys feel complicated to newcomers | For you cards walk through it; Cloud removes keys entirely |
| Licence key leaked and shared | keys carry the buyer's email and expire monthly; renewals check the subscription |
| Losing the signing key | back it up today; rotation path exists (new kid in a release) |
| Provider terms change on resale | Cloud only on paid, resale-permitted accounts; review terms before Cloud launch |

Sources for competitor prices: [ChatLLM upgrade page](https://chatllm.abacus.ai/mbsKGVcJsS),
[eesel.ai guide](https://eesel.ai/blog/abacus-ai-pricing),
[perspectiveai.xyz comparison](https://perspectiveai.xyz/cheapest-way-to-access-chatgpt-claude-gemini-2026/),
[vpsranking.com on Poe](https://vpsranking.com/ai/poe/),
[morphllm.com on OpenRouter](https://www.morphllm.com/openrouter-alternative).
