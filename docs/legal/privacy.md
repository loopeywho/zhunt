# Zhunt privacy policy

> Source for `site/privacy.html`; keep the two in step. Written from what the code
> actually does (checked 2026-10-16). Not legal advice: have it reviewed.

**Who we are.** Zhunt is made by Kindred Wildcat, a business based in the United Kingdom
([registered postal address]), the controller of the personal data described here.
Contact: kwokwingfoods@gmail.com.

## The short version

Zhunt runs on your computer. We do not run a server that your prompts pass through, we
do not receive your prompts, responses or provider API keys, and the app does not phone
home unless you turn on catalog updates (below). The only personal data we handle is what is needed to sell you Pro: your email
address and subscription status, via Stripe.

## What stays on your computer (we never receive it)

- **Prompts and responses.** Zhunt classifies a request locally and sends it directly to
  the AI provider it selects (for example Groq, Google AI Studio, Cerebras or, on Pro with
  your opt-in, Anthropic, OpenAI or Google's paid Gemini API). That provider receives your prompt under its own terms and
  privacy policy, just as if you had used it directly.
- **Provider API keys**, stored in `~/.zhunt/env` readable only by your user account.
- **Usage records**, in `~/.zhunt/telemetry.jsonl` and `~/.zhunt/state.db`: time, model,
  tier, token counts, estimated cost, success or failure. No prompt or response text.
- **Your licence key**, in `~/.zhunt/licence`. It is checked offline.

You can delete any of these files at any time.

## What the agent sends, and where

When you give the agent a task, it works from your computer. Web searches go to
DuckDuckGo, and the pages it reads are fetched directly from their websites, which see
your IP address like any visit. If you connect your email, it reads and drafts mail in
your own mailbox. None of this passes through us.

## AI providers that may train on prompts

Some free tiers say they may use prompts to improve their services. Zhunt does not send
requests to those providers unless you turn on `ZHUNT_ALLOW_TRAINING`. Premium (Pro)
requests go directly to Anthropic, OpenAI or Google on your paid API key, whose terms say
paid API traffic is not used for training by default.

## What we receive when you buy Pro

- **Stripe** (our payment processor) collects your payment details and email. We never
  see your full card number. Stripe's privacy policy applies to the data it handles.
- **From Stripe we receive**: your email address, subscription id, status and renewal
  date, used to issue and renew your licence key and to contact you about your
  subscription.
- **Licence service** (a Cloudflare Pages Function): when you open the activation page
  or run `zhunt licence renew`, it asks Stripe about your subscription and returns a
  signed key. It does not store this data. Cloudflare processes request data such as
  your IP address to deliver and protect the service; we do not keep request logs.
- **Catalog updates** (off unless you run `zhunt catalog update` or switch them on in
  the Connect window): the app downloads a public, signed file listing free models from
  zhunt.kindredwildcat.com, at most every 12 hours. The request carries no cookie,
  licence, key or other identifier; Cloudflare, which hosts the file, sees your IP
  address like any website visit; we do not keep request logs.
- **Emails you send us** (feedback, support) are kept to answer you.

Lawful basis (UK GDPR): performance of the contract (licence, billing), legitimate
interests (support, fraud prevention), legal obligation (tax records). Stripe and
Cloudflare may process data outside the UK under their own safeguards (such as the UK
International Data Transfer Addendum).

## Retention

Subscription and billing records: as long as you are a customer and as required for tax
and accounting (6 years after the end of the tax year they relate to). Support
emails: 2 years after the last message, unless still needed to answer you.

## Your rights

You can ask for a copy of your data, correction or deletion, and object to processing,
by emailing us; we answer within one month. If you are unhappy with how we handle your
data, you can complain to the Information Commissioner's Office (ico.org.uk).

## Changes

We will post changes here with a new date. Material changes are emailed to customers.

*Last updated: 16 October 2026*
