// Public demo of the zhunt workspace (zhunt.kindredwildcat.com/demo): the real UI with this
// in-page stand-in for the local daemon, so it runs on a static site with no keys and no server.
// Loaded as a classic script before the workspace's modules (scripts/build_site.py injects it).
// Replies are scripted: four suggestion prompts get real-looking answers, anything else is told
// plainly that this is a demo. Nothing leaves the browser.
(function () {
  'use strict';
  try { sessionStorage.setItem('zhunt.key', 'demo'); } catch (e) { /* storage blocked: auth screen shows */ }

  document.addEventListener('DOMContentLoaded', function () {
    var bar = document.createElement('div');
    bar.className = 'demo-banner';
    bar.setAttribute('role', 'note');
    var text = document.createElement('span');
    text.textContent = 'zhunt demo: replies are scripted.';
    var install = document.createElement('a');
    install.href = '/#install';
    install.textContent = 'Install for real answers';
    var home = document.createElement('a');
    home.href = '/';
    home.textContent = 'zhunt home';
    bar.append(text, install, home);
    document.body.append(bar);
  });

  var now = function () { return Date.now() / 1000; };
  var COOLDOWN = { 'cerebras/gpt-oss-120b': now() + 47 };
  var DECISIONS = [];
  var LAYOUTS = {};
  var PLAN = 'free';
  var THEMES = ['catppuccin', 'catppuccin-latte', 'dark', 'everforest', 'flexoki-light', 'gruvbox', 'kanagawa', 'light', 'matte-black', 'nord', 'rose-pine', 'tokyo-night'];

  var CATALOG = [
    { platform: 'groq', name: 'Groq', key_set: true, key_env: 'GROQ_API_KEY', key_url: 'https://console.groq.com/keys', free_note: 'Free tier, no card: fast open models (Llama, GPT-OSS, Qwen).', models: [
      { id: 'groq/openai/gpt-oss-120b', tiers: ['chat', 'coding', 'reasoning'], headroom: 0.82, reliability: 0.91 },
      { id: 'groq/llama-3.3-70b-versatile', tiers: ['chat', 'coding'], headroom: 0.45, reliability: 0.88 },
      { id: 'groq/qwen/qwen3.8-27b', tiers: ['chat', 'coding'], headroom: 0.15, reliability: 0.8 },
    ] },
    { platform: 'cerebras', name: 'Cerebras', key_set: false, key_env: 'CEREBRAS_API_KEY', key_url: 'https://cloud.cerebras.ai', free_note: 'Free tier, no card: very fast GPT-OSS and Qwen.', models: [
      { id: 'cerebras/gpt-oss-120b', tiers: ['chat', 'coding'], headroom: 0.05, reliability: 0.79, train: true },
    ] },
    { platform: 'google', name: 'Google AI Studio', key_set: true, key_env: 'GEMINI_API_KEY', key_url: 'https://aistudio.google.com/apikey', free_note: 'Free tier with a Google account.', models: [
      { id: 'google/gemini-3.8-flash', tiers: ['chat', 'long-context', 'reasoning'], headroom: 0.66, reliability: 0.93 },
    ] },
    { platform: 'anthropic', name: 'Anthropic (premium)', key_set: false, key_env: 'ANTHROPIC_API_KEY', key_url: 'https://console.anthropic.com/settings/keys', free_note: 'Paid (Pro): Claude, billed by Anthropic.', models: [
      { id: 'anthropic/claude-sonnet-5-5', tiers: ['chat', 'coding', 'long-context', 'reasoning'], headroom: null, reliability: null, paid: true },
    ] },
  ];

  function view(p, m) {
    var reason = null;
    if (!p.key_set) reason = 'no key';
    else if (m.train) reason = 'may train on prompts';
    else if (m.paid) reason = PLAN === 'pro' ? 'premium off' : 'pro only';
    var cd = Math.max(0, Math.round((COOLDOWN[m.id] || 0) - now()));
    return { id: m.id, tiers: m.tiers, free: !m.paid, may_train_on_prompts: !!m.train, allowed: reason === null, blocked_reason: reason,
      headroom: m.headroom, cooldown_remaining: cd, reliability: m.reliability, limits: { rpm: null, rpd: null, tpm: null, tpd: null } };
  }
  function status() {
    return { version: 'demo', policy: { allow_paid: false, allow_training: false, premium_reason: 'pro only' },
      spend: { month: new Date().toISOString().slice(0, 7), spent: 0, cap: 5 },
      providers: CATALOG.map(function (p) { return { platform: p.platform, name: p.name, key_set: p.key_set, key_env: p.key_env, key_url: p.key_url || '', free_note: p.free_note || '', models: p.models.map(function (m) { return view(p, m); }) }; }) };
  }
  function usable(tier) {
    var out = [];
    CATALOG.forEach(function (p) { p.models.forEach(function (m) { var v = view(p, m); if (v.allowed && !v.cooldown_remaining && v.tiers.indexOf(tier) >= 0) out.push(v); }); });
    return out;
  }
  function record(d) {
    var base = { ts: now(), session: null, requested_alias: 'zhunt-auto', tier: 'chat', model: null, degraded: false, attempt: 1, success: true, failure: null, input_tokens: 0, output_tokens: 0, latency_ms: 0 };
    for (var k in d) base[k] = d[k];
    DECISIONS.push(base);
    if (DECISIONS.length > 200) DECISIONS.shift();
  }
  // a little history so the routing, logs and panels windows have something to show
  record({ ts: now() - 900, session: 'ui-seed1', tier: 'coding', model: 'groq/openai/gpt-oss-120b', input_tokens: 812, output_tokens: 240, latency_ms: 1430 });
  record({ ts: now() - 600, session: 'ui-seed1', tier: 'chat', model: 'cerebras/gpt-oss-120b', success: false, failure: 'rate-limit', latency_ms: 210 });
  record({ ts: now() - 599, session: 'ui-seed1', tier: 'chat', model: 'groq/llama-3.3-70b-versatile', attempt: 2, input_tokens: 90, output_tokens: 64, latency_ms: 880 });

  function classify(prompt) {
    var p = prompt.toLowerCase();
    if (/code|function|python|bug|regex|error/.test(p)) return 'coding';
    if (/reason|prove|why |step by step|plan/.test(p)) return 'reasoning';
    if (prompt.length > 600 || /summar|long/.test(p)) return 'long-context';
    return 'chat';
  }

  var CANNED = [
    [/regex.*postcode|postcode.*regex/i, 'Here is a practical regex for UK postcodes (case-insensitive, optional space):\n\n```regex\n^([A-Z]{1,2}\\d[A-Z\\d]?) ?(\\d[A-Z]{2})$\n```\n\n- `SW1A 1AA`, `M1 1AE`, `B33 8TH` match\n- It checks the **shape**, not whether the postcode exists; use the Royal Mail PAF or a postcode API for that.\n- Add the `i` flag, or uppercase the input first.'],
    [/error message/i, 'Paste the error and I will walk through it. A good pattern:\n\n1. **Read the last line first**: it names the exception and usually the cause.\n2. **Find the first line that is your code** in the stack trace; that is where to look.\n3. **Check the inputs** at that line: a `None`, an empty list or a wrong type causes most errors.\n\nFor example `KeyError: \'email\'` means the dict had no `email` key: use `data.get("email")` or check where the dict is built.'],
    [/penang/i, 'A relaxed 3 days in Penang:\n\n**Day 1 - George Town:** street-art walk on Armenian Street, Khoo Kongsi clan house, then dinner at Gurney Drive hawker centre (char kway teow, asam laksa).\n\n**Day 2 - Hills and temples:** funicular up Penang Hill in the morning, Kek Lok Si temple after, night market at Batu Ferringhi.\n\n**Day 3 - Coast:** Penang National Park trek to Monkey Beach, then cendol at Penang Road before you leave.\n\nTip: Grab is cheap and reliable; carry cash for hawker stalls.'],
    [/summari[sz]e/i, 'Paste the article (or its text) and I will give you:\n\n- a **3-line summary**,\n- the **key claims** with any numbers,\n- and **what is missing** or worth checking.\n\nLong articles go to a long-context model automatically: zhunt classified this request as `long-context`.'],
  ];
  function replyFor(prompt, model, tier) {
    for (var i = 0; i < CANNED.length; i++) if (CANNED[i][0].test(prompt)) return CANNED[i][1];
    return 'This is the **zhunt demo**, so this reply is scripted. In the real app, zhunt would send your message to `' + model + '` (a free model it picked for the `' + tier + '` tier) and stream its answer here.\n\nTry one of the suggestion buttons, or type **failover** to watch zhunt switch models when one hits its limit. To get real answers, install zhunt and add a free Groq key: it takes about a minute.';
  }

  function json(status, body) { return new Response(JSON.stringify(body), { status: status, headers: { 'content-type': 'application/json' } }); }
  function sseStream(model, text, onDone) {
    var words = text.split(/(\s+)/);
    var enc = new TextEncoder();
    return new Response(new ReadableStream({
      start: function (controller) {
        var i = 0;
        (function tick() {
          if (i >= words.length) {
            controller.enqueue(enc.encode('data: [DONE]\n\n'));
            controller.close();
            onDone(Math.ceil(text.length / 4));
            return;
          }
          var chunk = { id: 'demo', object: 'chat.completion.chunk', model: model, choices: [{ index: 0, delta: { content: words.slice(i, i + 2).join('') } }] };
          i += 2;
          controller.enqueue(enc.encode('data: ' + JSON.stringify(chunk) + '\n\n'));
          setTimeout(tick, 28);
        })();
      },
    }), { status: 200, headers: { 'content-type': 'text/event-stream' } });
  }

  function chat(body, headers) {
    var msgs = body.messages || [];
    var prompt = msgs.length ? String(msgs[msgs.length - 1].content || '') : '';
    var session = headers.get('x-session-id');
    var tier = body.model && body.model !== 'zhunt-auto' ? body.model.replace('zhunt-', '') : classify(prompt);
    var t0 = Date.now();
    var attempt = 1;
    if (/failover/i.test(prompt)) {
      record({ session: session, tier: tier, model: 'cerebras/gpt-oss-120b', success: false, failure: 'rate-limit', latency_ms: 210 });
      COOLDOWN['cerebras/gpt-oss-120b'] = now() + 60;
      attempt = 2;
    }
    var pool = usable(tier);
    if (!pool.length) pool = usable('chat');
    var model = pool[0].id;
    var text = /failover/i.test(prompt)
      ? 'The first model zhunt tried (`cerebras/gpt-oss-120b`) answered **429 rate limited**, so zhunt rested it and retried on `' + model + '` before you saw an error. The line under this reply shows both attempts; the Routing window explains why each model was chosen.'
      : replyFor(prompt, model, tier);
    var inTok = Math.max(1, Math.round(prompt.length / 4));
    return sseStream(model, text, function (outTok) {
      record({ session: session, tier: tier, model: model, attempt: attempt, input_tokens: inTok, output_tokens: outTok, latency_ms: Date.now() - t0 });
    });
  }

  var realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var url = new URL(typeof input === 'string' ? input : input.url, location.href);
    init = init || {};
    var method = (init.method || 'GET').toUpperCase();
    var headers = new Headers(init.headers || {});
    var path = url.pathname;
    var body = null;
    try { body = init.body ? JSON.parse(init.body) : null; } catch (e) { body = null; }
    if (path === '/v1/chat/completions') return Promise.resolve(chat(body || {}, headers));
    if (path.indexOf('/zhunt/api/') !== 0) return realFetch(input, init);
    var rest = path.slice('/zhunt/api/'.length);
    if (rest === 'status') return Promise.resolve(json(200, status()));
    if (rest === 'decisions') {
      var limit = Number(url.searchParams.get('limit') || 50);
      var s = url.searchParams.get('session');
      var list = DECISIONS.slice().reverse().filter(function (d) { return !s || d.session === s; }).slice(0, limit);
      return Promise.resolve(json(200, { decisions: list }));
    }
    if (rest === 'plan') {
      return Promise.resolve(json(200, { plan: PLAN, email: null, expires_at: null, in_grace: false, problem: null,
        features: [],
        feature_labels: { premium_fallback: 'Premium models (GPT, Claude, Gemini Pro) when free models run out, under your monthly cap' },
        upgrade_url: 'https://zhunt.kindredwildcat.com/#pricing' }));
    }
    if (rest === 'themes') {
      return Promise.resolve(json(200, { themes: THEMES.map(function (n) { return { name: n, builtin: true }; }) }));
    }
    if (rest.indexOf('themes/') === 0) return realFetch('css/themes/' + encodeURIComponent(rest.slice(7)) + '.css');
    if (rest === 'keys' && method === 'POST') {
      var prov = CATALOG.filter(function (p) { return body && p.platform === body.platform; })[0];
      if (!prov) return Promise.resolve(json(404, { detail: 'unknown provider' }));
      if (!body.key || String(body.key).trim().length < 8 || /\s/.test(String(body.key).trim())) return Promise.resolve(json(400, { detail: 'that does not look like an API key (one line, no spaces)' }));
      prov.key_set = true; // demo: the key is not kept anywhere
      return Promise.resolve(json(200, { platform: prov.platform, key_set: true }));
    }
    if (rest.indexOf('keys/') === 0 && method === 'DELETE') {
      var gone = CATALOG.filter(function (p) { return p.platform === rest.slice(5); })[0];
      if (gone) gone.key_set = false;
      return Promise.resolve(json(200, { platform: rest.slice(5), key_set: false }));
    }
    if (rest === 'transcribe') {
      // demo: the recording is ignored (never uploaded); a fixed sentence comes back
      return new Promise(function (resolve) { setTimeout(function () { resolve(json(200, { text: 'Write a regex for UK postcodes' })); }, 700); });
    }
    if (rest === 'layouts') return Promise.resolve(json(200, { layouts: Object.keys(LAYOUTS).filter(function (n) { return n !== 'last'; }) }));
    if (rest.indexOf('layouts/') === 0) {
      var name = decodeURIComponent(rest.slice(8));
      if (method === 'PUT') { LAYOUTS[name] = body; return Promise.resolve(json(200, { name: name })); }
      if (method === 'DELETE') { delete LAYOUTS[name]; return Promise.resolve(json(200, { name: name })); }
      return Promise.resolve(LAYOUTS[name] ? json(200, LAYOUTS[name]) : json(404, { detail: 'no layout named ' + name }));
    }
    if (rest === 'compare') {
      var prompt = body && body.messages ? String(body.messages[body.messages.length - 1].content || '') : '';
      var st = status();
      var byId = {};
      st.providers.forEach(function (p) { p.models.forEach(function (m) { byId[m.id] = m; }); });
      return new Promise(function (resolve) {
        setTimeout(function () {
          resolve(json(200, { results: (body.models || []).map(function (id) {
            var m = byId[id];
            if (!m) return { model: id, ok: false, error: 'unknown model' };
            if (!m.allowed) return { model: id, ok: false, text: null, error: 'blocked: ' + m.blocked_reason };
            if (m.cooldown_remaining) return { model: id, ok: false, text: null, error: '429 rate limited (cooldown ' + m.cooldown_remaining + 's)', latency_ms: 180 };
            return { model: id, ok: true, text: replyFor(prompt, id, 'compare'), error: null, latency_ms: 600 + (id.length * 37) % 900, input_tokens: Math.round(prompt.length / 4), output_tokens: 120 };
          }) }));
        }, 900);
      });
    }
    return Promise.resolve(json(404, { detail: 'not in the demo' }));
  };
})();
