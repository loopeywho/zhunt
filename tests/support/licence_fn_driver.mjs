// Drives site/functions/api/licence.js with a fake Stripe for tests/test_licence_function.py.
// Prints one JSON object: {case: {status, body}}.
import { handle } from '../../site/functions/api/licence.js';

const env = { STRIPE_SECRET_KEY: 'sk_test_x', LICENCE_PRIVATE_KEY: process.env.PRIV, LICENCE_KID: 'test' };
const NOW = 1_800_000_000;
const sub = (status, extra = {}) => ({ id: 'sub_ABCDEFGH12', status, items: { data: [{ current_period_end: NOW + 30 * 86400 }] }, ...extra });
const STRIPE = {
  'checkout/sessions/cs_test_complete1?expand[]=subscription': { status: 'complete', customer_details: { email: 'buyer@example.com' }, subscription: sub('trialing', { trial_end: NOW + 7 * 86400 }) },
  'checkout/sessions/cs_test_open00001?expand[]=subscription': { status: 'open', subscription: null },
  'checkout/sessions/cs_test_canceled1?expand[]=subscription': { status: 'complete', customer_details: { email: 'x@example.com' }, subscription: sub('canceled') },
  'subscriptions/sub_ABCDEFGH12?expand[]=customer': sub('active', { customer: { email: 'Buyer@Example.com' } }),
};
const fakeFetch = async (url, opts) => {
  if (opts.headers.Authorization !== 'Bearer sk_test_x') return new Response('{}', { status: 401 });
  const body = STRIPE[url.replace('https://api.stripe.com/v1/', '')];
  return body ? new Response(JSON.stringify(body)) : new Response('{}', { status: 404 });
};
const run = async (qs, e = env) => {
  const res = await handle(new Request(`https://zhunt.test/api/licence?${qs}`), e, fakeFetch, NOW);
  return { status: res.status, body: await res.json() };
};
console.log(JSON.stringify({
  purchase: await run('session_id=cs_test_complete1'),
  incomplete: await run('session_id=cs_test_open00001'),
  canceled: await run('session_id=cs_test_canceled1'),
  unknown: await run('session_id=cs_test_missing01'),
  malformed: await run('session_id=../../charges'),
  renew: await run('renew=sub_ABCDEFGH12&email=buyer@example.com'),
  renewWrongEmail: await run('renew=sub_ABCDEFGH12&email=attacker@example.com'),
  unconfigured: await run('session_id=cs_test_complete1', {}),
}));
