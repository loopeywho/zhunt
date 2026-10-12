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
  var BOTS = {};
  var AVATARS = {};
  // demo pictures: drawn in the page (the real app asks Cloudflare's FLUX model)
  var WALLPAPER = null;
  // demo agent: a scripted run (search -> read -> answer; a save step asks for approval)
  var AGENT = { folders: ['/Users/you/Documents/zhunt-agent'], tasks: {}, order: [], loops: [] };
  function agentStart(body) {
    var id = Math.random().toString(36).slice(2, 10);
    var t = { id: id, goal: body.goal, mode: body.mode || 'task', state: 'running', created: now(), events: [], answer: '', loop_id: null };
    AGENT.tasks[id] = t; AGENT.order.push(id);
    var ev = function (kind, data) { var e = Object.assign({ seq: t.events.length, ts: now(), kind: kind }, data || {}); t.events.push(e); return e; };
    ev('goal', { text: t.goal, mode: t.mode });
    var wantsFile = /\.(md|txt)\b|save|write/i.test(t.goal);
    if (/inbox|email|e-mail|triage/i.test(t.goal)) {
      var m = 'groq/openai/gpt-oss-120b';
      var mailSteps = [
        function () { ev('tool', { name: 'list_emails', args: { unread_only: true, limit: 10 }, call_id: 'm1', ok: true, ms: 820, model: m,
          result: 'INBOX: 4 message(s)\n- id 812 | UNREAD | from Sam Lee | Lunch Friday?\n- id 811 | UNREAD | from Bank | Your statement is ready\n- id 809 | UNREAD | from Priya (client) | Quote for 200 labels\n- id 806 | UNREAD | from Newsletter | 10 AI tools this week' }); },
        function () { ev('tool', { name: 'read_email', args: { id: '809' }, call_id: 'm2', ok: true, ms: 410, model: m,
          result: 'From: Priya <priya@example.com>\nSubject: Quote for 200 labels\n\nHi, could you send a price for 200 chilli sauce labels by Wednesday?' }); },
        function () { t.state = 'waiting'; ev('approval', { name: 'draft_email', args: { to: 'priya@example.com', subject: 'Re: Quote for 200 labels', body: 'Hi Priya,\n\nThanks for asking. I will send the quote for 200 labels by Tuesday.\n\nBest wishes' }, call_id: 'm3', model: m }); },
      ];
      var j = 0;
      var nextMail = function () {
        if (t.state === 'stopped') return;
        if (t.state === 'waiting') { setTimeout(nextMail, 400); return; }
        if (j < mailSteps.length) { mailSteps[j++](); setTimeout(nextMail, 1100); return; }
        var drafted = t.events.some(function (e) { return e.kind === 'decision' && e.approved; });
        t.answer = '**Needs a reply today**\n- Priya (client): quote for 200 labels, due Wednesday. ' + (drafted ? 'Draft reply saved in Drafts.' : 'You declined the draft.') + '\n- Sam: lunch on Friday?\n\n**Read later**\n- Bank: statement ready\n\n**Ignore**\n- Newsletter: 10 AI tools this week';
        ev('answer', { text: t.answer, model: m }); t.state = 'done';
      };
      t._ev = ev;
      setTimeout(nextMail, 600);
      return t;
    }
    var steps = [
      function () { ev('thought', { text: 'I will search the web first, then read the most useful page.', model: 'groq/openai/gpt-oss-120b' }); },
      function () { ev('tool', { name: 'web_search', args: { query: t.goal.slice(0, 60) }, call_id: 'c1', ok: true, ms: 640, model: 'groq/openai/gpt-oss-120b',
        result: 'Search results:\n- 12 Best Free Note-Taking Apps in 2026\n  https://toolradar.com/blog/best-free-note-taking-apps\n- Joplin: open source note taking\n  https://joplinapp.org' }); },
      function () { ev('tool', { name: 'open_page', args: { url: 'https://joplinapp.org' }, call_id: 'c2', ok: true, ms: 910, model: 'groq/openai/gpt-oss-120b',
        result: 'Page: Joplin\nAn open source note taking and to-do application with synchronisation...' }); },
    ];
    if (wantsFile) steps.push(function () { t.state = 'waiting'; ev('approval', { name: 'write_file', args: { path: 'notes-summary.md', content: '# Free note apps\n\n- Joplin: open source, syncs via your own cloud\n- Obsidian: local files, plugins\n- Notion (free plan): easy sharing\n' }, call_id: 'c3', model: 'groq/openai/gpt-oss-120b' }); });
    var i = 0;
    var next = function () {
      if (t.state === 'stopped') return;
      if (t.state === 'waiting') { setTimeout(next, 400); return; }
      if (i < steps.length) { steps[i++](); setTimeout(next, 1100); return; }
      t.answer = '**Joplin** is the best free pick for most people: open source, syncs through a cloud you already use. **Obsidian** suits tinkerers (local files, plugins) and **Notion\'s free plan** is easiest for sharing.\n\nSources: https://toolradar.com/blog/best-free-note-taking-apps, https://joplinapp.org' + (wantsFile ? '\n\n' + (t.events.some(function (e) { return e.kind === 'decision' && e.approved; }) ? 'Saved to notes-summary.md.' : 'You declined saving, so nothing was written.') : '');
      ev('answer', { text: t.answer, model: 'groq/openai/gpt-oss-120b' }); t.state = 'done';
    };
    t._ev = ev;
    setTimeout(next, 600);
    return t;
  }
  function agentDetail(t, since) {
    return { id: t.id, goal: t.goal, mode: t.mode, state: t.state, created: t.created, answer: t.answer, loop_id: t.loop_id,
      steps: t.events.filter(function (e) { return e.kind === 'tool'; }).length, events: t.events.slice(since || 0) };
  }
  function demoPicture(prompt) {
    var wide = /wallpaper/i.test(prompt);
    var c = document.createElement('canvas');
    c.width = wide ? 1280 : 256;
    c.height = wide ? 800 : 256;
    if (wide) return demoLandscape(c, prompt);
    var g = c.getContext('2d');
    var seed = 0;
    for (var i = 0; i < prompt.length; i++) seed = (seed * 31 + prompt.charCodeAt(i)) >>> 0;
    var rnd = function () { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
    var hue = Math.floor(rnd() * 360);
    var grad = g.createLinearGradient(0, 0, 256, 256);
    grad.addColorStop(0, 'hsl(' + hue + ',70%,62%)');
    grad.addColorStop(1, 'hsl(' + ((hue + 70) % 360) + ',70%,42%)');
    g.fillStyle = grad; g.fillRect(0, 0, 256, 256);
    for (var k = 0; k < 6; k++) {
      g.fillStyle = 'hsla(' + ((hue + k * 47) % 360) + ',80%,' + (55 + rnd() * 30) + '%,0.75)';
      g.beginPath(); g.arc(rnd() * 256, rnd() * 256, 20 + rnd() * 70, 0, Math.PI * 2); g.fill();
    }
    g.fillStyle = '#fff'; g.beginPath(); g.arc(98, 112, 14, 0, 7); g.arc(158, 112, 14, 0, 7); g.fill();
    g.strokeStyle = '#fff'; g.lineWidth = 8; g.lineCap = 'round'; g.beginPath(); g.arc(128, 150, 34, 0.2, Math.PI - 0.2); g.stroke();
    return c.toDataURL('image/png');
  }
  function demoLandscape(c, prompt) {
    var g = c.getContext('2d');
    var seed = 7;
    for (var i = 0; i < prompt.length; i++) seed = (seed * 31 + prompt.charCodeAt(i)) >>> 0;
    var rnd = function () { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
    var hue = Math.floor(rnd() * 360);
    var sky = g.createLinearGradient(0, 0, 0, 800);
    sky.addColorStop(0, 'hsl(' + hue + ',45%,12%)');
    sky.addColorStop(1, 'hsl(' + ((hue + 40) % 360) + ',55%,38%)');
    g.fillStyle = sky; g.fillRect(0, 0, 1280, 800);
    g.fillStyle = 'hsla(' + ((hue + 180) % 360) + ',80%,75%,0.9)'; g.beginPath(); g.arc(940, 230, 70, 0, 7); g.fill();
    for (var k = 0; k < 4; k++) {
      g.fillStyle = 'hsla(' + ((hue + k * 25) % 360) + ',40%,' + (30 - k * 6) + '%,0.92)';
      g.beginPath(); g.moveTo(0, 800);
      for (var x = 0; x <= 1280; x += 80) g.lineTo(x, 470 + k * 80 + Math.sin(x / 140 + k * 2 + rnd()) * (70 - k * 10));
      g.lineTo(1280, 800); g.closePath(); g.fill();
    }
    return c.toDataURL('image/jpeg', 0.85);
  }
  var PLAN = 'free';
  var ALLOW_TRAINING = false;
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
    { platform: 'kilo', name: 'Kilo Gateway (no key needed)', key_set: true, keyless: true, key_env: 'KILO_API_KEY', key_url: 'https://app.kilo.ai', free_note: 'Works with no key at all. Its free models may log prompts and use them for training.', models: [
      { id: 'kilo/kilo-auto/free', tiers: ['chat', 'coding', 'reasoning'], headroom: null, reliability: null, train: true },
    ] },
    { platform: 'anthropic', name: 'Anthropic (premium)', key_set: false, key_env: 'ANTHROPIC_API_KEY', key_url: 'https://console.anthropic.com/settings/keys', free_note: 'Paid (Pro): Claude, billed by Anthropic.', models: [
      { id: 'anthropic/claude-sonnet-5-5', tiers: ['chat', 'coding', 'long-context', 'reasoning'], headroom: null, reliability: null, paid: true },
    ] },
  ];

  function view(p, m) {
    var reason = null;
    if (!p.key_set) reason = 'no key';
    else if (m.train && !ALLOW_TRAINING) reason = 'may train on prompts';
    else if (m.paid) reason = PLAN === 'pro' ? 'premium off' : 'pro only';
    var cd = Math.max(0, Math.round((COOLDOWN[m.id] || 0) - now()));
    return { id: m.id, tiers: m.tiers, free: !m.paid, may_train_on_prompts: !!m.train, allowed: reason === null, blocked_reason: reason,
      headroom: m.headroom, cooldown_remaining: cd, reliability: m.reliability, limits: { rpm: null, rpd: null, tpm: null, tpd: null } };
  }
  function status() {
    return { version: 'demo', policy: { allow_paid: false, allow_training: ALLOW_TRAINING, premium_reason: 'pro only' },
      spend: { month: new Date().toISOString().slice(0, 7), spent: 0, cap: 5 },
      providers: CATALOG.map(function (p) { return { platform: p.platform, name: p.name, key_set: p.key_set, key_env: p.key_env, key_url: p.key_url || '', free_note: p.free_note || '', keyless: !!p.keyless, models: p.models.map(function (m) { return view(p, m); }) }; }) };
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

  // team chat: a bot's reply depends on which bot is speaking (named in its system prompt)
  var TEAM = {
    researcher: 'Three free options worth a look:\n\n- **Obsidian**: local Markdown files, huge plugin library, free for personal use.\n- **Joplin**: open source, syncs through your own cloud, has a web clipper.\n- **Notion (free plan)**: easiest to share, but your notes live on their servers.\n\n@writer can you turn this into a short recommendation for a beginner?',
    writer: '**If you want the simplest start, pick Joplin.** It is free, open source and syncs through a cloud you already use. Choose **Obsidian** if you like tinkering with plugins, and **Notion** if you mostly share notes with others.\n\n@critic does this read clearly?',
    critic: 'Clear and fair. One fix: say that Obsidian sync costs extra (the app itself is free), so a beginner is not surprised. Otherwise ready to send.',
  };
  function teamReply(system) {
    var m = /\(@([a-z0-9_-]+)\)/.exec(system || '');
    if (!m) return null;
    return TEAM[m[1]] || 'I am @' + m[1] + ', a bot you made. In the real app I answer with a free model chosen for my job.';
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
    var team = msgs.length && msgs[0].role === 'system' ? teamReply(msgs[0].content) : null;
    var text = team ? team : /failover/i.test(prompt)
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
    if (rest === 'policy/training' && method === 'PUT') {
      ALLOW_TRAINING = !!(body && body.allow === true);
      return Promise.resolve(json(200, { allow_training: ALLOW_TRAINING }));
    }
    if (rest === 'transcribe') {
      // demo: the recording is ignored (never uploaded); a fixed sentence comes back
      return new Promise(function (resolve) { setTimeout(function () { resolve(json(200, { text: 'Write a regex for UK postcodes' })); }, 700); });
    }
    if (rest === 'bots') return Promise.resolve(json(200, { bots: Object.keys(BOTS).sort().map(function (k) { var b = Object.assign({}, BOTS[k]); b.avatar = !!AVATARS[k]; return b; }) }));
    if (rest === 'agent') return Promise.resolve(json(200, { folders: AGENT.folders, loops: AGENT.loops,
      tools: [{ name: 'web_search', risk: 'read' }, { name: 'open_page', risk: 'read' }, { name: 'list_files', risk: 'read' }, { name: 'read_file', risk: 'read' }, { name: 'write_file', risk: 'write' }, { name: 'move_file', risk: 'write' }],
      tasks: AGENT.order.slice().reverse().map(function (id) { return agentDetail(AGENT.tasks[id]); }) }));
    if (rest === 'agent/folders' && method === 'PUT') { AGENT.folders = (body && body.folders) || []; return Promise.resolve(json(200, { folders: AGENT.folders })); }
    if (rest === 'agent/tasks' && method === 'POST') {
      if (!body || !String(body.goal || '').trim()) return Promise.resolve(json(400, { detail: 'tell the agent what to do' }));
      return Promise.resolve(json(200, agentDetail(agentStart(body))));
    }
    if (rest === 'agent/loops' && method === 'POST') {
      var m = /^(\d+)\s*(m|h|d)$/.exec(String((body && body.every) || ''));
      if (!m) return Promise.resolve(json(400, { detail: 'every must look like 30m, 2h or 1d' }));
      var secs = Math.max(300, Number(m[1]) * { m: 60, h: 3600, d: 86400 }[m[2]]);
      var lp = { id: Math.random().toString(36).slice(2, 8), every: secs, goal: body.goal, next_run: now() + secs, runs: 0 };
      AGENT.loops.push(lp); agentStart(body);
      return Promise.resolve(json(200, lp));
    }
    var lm = /^agent\/loops\/([a-z0-9]+)$/.exec(rest);
    if (lm && method === 'DELETE') { AGENT.loops = AGENT.loops.filter(function (l) { return l.id !== lm[1]; }); return Promise.resolve(json(200, { id: lm[1] })); }
    var tm = /^agent\/tasks\/([a-z0-9]+)(\/(decide|stop))?$/.exec(rest);
    if (tm) {
      var task = AGENT.tasks[tm[1]];
      if (!task) return Promise.resolve(json(404, { detail: 'no such task' }));
      if (tm[3] === 'stop') { task.state = 'stopped'; task._ev('stopped'); return Promise.resolve(json(200, agentDetail(task))); }
      if (tm[3] === 'decide') { task._ev('decision', { call_id: body.call_id, approved: !!body.approve }); task.state = 'running'; return Promise.resolve(json(200, { ok: true })); }
      return Promise.resolve(json(200, agentDetail(task, Number(url.searchParams.get('since') || 0))));
    }
    if (rest === 'email/status') return Promise.resolve(json(200, { connected: !!AGENT.mail, address: AGENT.mail || null, presets: {
      gmail: { name: 'Gmail', help: 'https://myaccount.google.com/apppasswords', note: 'Needs 2-Step Verification on, then an app password.' },
      icloud: { name: 'iCloud Mail', help: 'https://account.apple.com', note: 'Sign-In and Security > App-Specific Passwords.' },
      yahoo: { name: 'Yahoo Mail', help: 'https://login.yahoo.com/account/security', note: 'Account Security > Generate app password.' } } }));
    if (rest === 'email/connect' && method === 'PUT') { AGENT.mail = (body && body.address) || 'you@example.com'; return Promise.resolve(json(200, { connected: true, address: AGENT.mail })); }
    if (rest === 'email/connect' && method === 'DELETE') { AGENT.mail = null; return Promise.resolve(json(200, { connected: false })); }
    if (rest === 'wallpaper') {
      if (method === 'PUT') { WALLPAPER = body && body.image; return Promise.resolve(json(200, { wallpaper: true })); }
      if (method === 'DELETE') { WALLPAPER = null; return Promise.resolve(json(200, { wallpaper: false })); }
      return Promise.resolve(WALLPAPER ? json(200, { image: WALLPAPER }) : json(404, { detail: 'no personal wallpaper' }));
    }
    if (rest === 'images/status') return Promise.resolve(json(200, { connected: true, provider: 'Cloudflare Workers AI', key_url: 'https://dash.cloudflare.com/profile/api-tokens', note: 'Demo: pictures are drawn in your browser. The real app uses Cloudflare\'s free FLUX model.' }));
    if (rest === 'images' && method === 'POST') {
      return new Promise(function (resolve) { setTimeout(function () { resolve(json(200, { image: demoPicture(String((body && body.prompt) || '')) })); }, 1200); });
    }
    var av = /^bots\/([a-z0-9_-]+)\/avatar$/.exec(rest);
    if (av) {
      if (method === 'PUT') { AVATARS[av[1]] = body && body.image; return Promise.resolve(json(200, { handle: av[1], avatar: true })); }
      if (method === 'DELETE') { delete AVATARS[av[1]]; return Promise.resolve(json(200, { handle: av[1], avatar: false })); }
      return Promise.resolve(AVATARS[av[1]] ? json(200, { image: AVATARS[av[1]] }) : json(404, { detail: 'this bot has no picture' }));
    }
    if (rest.indexOf('bots/') === 0) {
      var handle = decodeURIComponent(rest.slice(5));
      if (method === 'DELETE') { delete BOTS[handle]; return Promise.resolve(json(200, { handle: handle })); }
      if (method === 'PUT') {
        if (!/^[a-z0-9][a-z0-9_-]{0,39}$/.test(handle) || !body || !String(body.name || '').trim()) return Promise.resolve(json(400, { detail: 'a bot needs a handle and a name' }));
        BOTS[handle] = { handle: handle, name: String(body.name).trim(), instructions: String(body.instructions || ''), alias: body.alias || 'zhunt-auto', color: body.color || '#7aa2f7' };
        return Promise.resolve(json(200, BOTS[handle]));
      }
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
