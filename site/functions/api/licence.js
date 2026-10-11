// Cloudflare Pages Function: GET /api/licence
//
//   ?session_id=cs_...            after Stripe checkout (the payment link's success URL is
//                                 https://<site>/activate.html?session_id={CHECKOUT_SESSION_ID})
//   ?renew=sub_...&email=...      `zhunt licence renew`: a fresh key for a live subscription
//
// Returns {licence, email, expires_at}. The key is Ed25519-signed and verified offline by
// zhunt (zhunt/licence.py); its expiry follows the subscription's paid-up period.
//
// Environment (Pages > Settings > Variables, all encrypted):
//   STRIPE_SECRET_KEY     restricted key with read access to Checkout Sessions, Subscriptions, Customers
//   LICENCE_PRIVATE_KEY   base64 PKCS8 DER (scripts/licence_tool.py worker-secret)
//   LICENCE_KID           the key id paired with the public key in zhunt/licence.py

const LIVE = new Set(['active', 'trialing']);
const PERIOD_FALLBACK = 31 * 86400; // if Stripe gives no period end, a month

const json = (status, body) => new Response(JSON.stringify(body), {
  status,
  headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
});

const b64url = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const fromB64 = (text) => Uint8Array.from(atob(text), (c) => c.charCodeAt(0));

export async function signLicence(payload, env) {
  const key = await crypto.subtle.importKey('pkcs8', fromB64(env.LICENCE_PRIVATE_KEY), { name: 'Ed25519' }, false, ['sign']);
  const body = `ZHUNT1.${b64url(new TextEncoder().encode(JSON.stringify(payload)))}`;
  const sig = await crypto.subtle.sign({ name: 'Ed25519' }, key, new TextEncoder().encode(body));
  return `${body}.${b64url(sig)}`;
}

// The end of what the customer has paid for (or the trial), across Stripe API versions.
export function periodEnd(sub, now) {
  const ends = [sub.current_period_end, sub.trial_end, ...(sub.items?.data || []).map((i) => i.current_period_end)]
    .filter((x) => typeof x === 'number');
  return ends.length ? Math.max(...ends) : now + PERIOD_FALLBACK;
}

async function stripe(path, env, fetchImpl) {
  const res = await fetchImpl(`https://api.stripe.com/v1/${path}`, { headers: { Authorization: `Bearer ${env.STRIPE_SECRET_KEY}` } });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`stripe ${res.status}`);
  return res.json();
}

export async function handle(request, env, fetchImpl = fetch, now = Math.floor(Date.now() / 1000)) {
  if (!env.STRIPE_SECRET_KEY || !env.LICENCE_PRIVATE_KEY || !env.LICENCE_KID) return json(503, { detail: 'licence service is not configured' });
  const url = new URL(request.url);
  const sessionId = url.searchParams.get('session_id');
  const renew = url.searchParams.get('renew');
  let sub;
  let email;
  try {
    if (sessionId) {
      if (!/^cs_[A-Za-z0-9_]{8,200}$/.test(sessionId)) return json(400, { detail: 'bad checkout session id' });
      const session = await stripe(`checkout/sessions/${sessionId}?expand[]=subscription`, env, fetchImpl);
      if (!session) return json(404, { detail: 'checkout session not found' });
      if (session.status !== 'complete' || !session.subscription) return json(409, { detail: 'checkout is not complete yet; refresh in a moment' });
      sub = session.subscription;
      email = session.customer_details?.email || null;
    } else if (renew) {
      if (!/^sub_[A-Za-z0-9]{8,200}$/.test(renew)) return json(400, { detail: 'bad subscription id' });
      sub = await stripe(`subscriptions/${renew}?expand[]=customer`, env, fetchImpl);
      if (!sub) return json(404, { detail: 'subscription not found' });
      email = sub.customer?.email || null;
      // the id alone is not a secret, so the customer's email must match too
      const asked = (url.searchParams.get('email') || '').trim().toLowerCase();
      if (!email || asked !== email.toLowerCase()) return json(403, { detail: 'that email does not match the subscription' });
    } else {
      return json(400, { detail: 'pass session_id or renew' });
    }
  } catch {
    return json(502, { detail: 'could not reach the payment service; try again shortly' });
  }
  if (!LIVE.has(sub.status)) return json(402, { detail: `subscription is ${sub.status}; restart it on the pricing page` });
  const exp = periodEnd(sub, now);
  const licence = await signLicence({ v: 1, kid: env.LICENCE_KID, plan: 'pro', email, sub: sub.id, iat: now, exp }, env);
  return json(200, { licence, email, expires_at: exp });
}

export const onRequestGet = ({ request, env }) => handle(request, env);
