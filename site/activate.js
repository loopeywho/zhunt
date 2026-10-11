// After Stripe checkout: exchange ?session_id for a signed licence key (functions/api/licence.js).
(function () {
  var title = document.getElementById('title');
  var detail = document.getElementById('detail');
  var session = new URLSearchParams(location.search).get('session_id');
  // keep the session id out of the history and any later Referer
  if (session) history.replaceState(null, '', location.pathname);

  function fail(message) {
    title.textContent = 'We could not fetch your key';
    detail.textContent = message + ' If you were charged and this keeps happening, email kwokwingfoods@gmail.com with the email you used at checkout (never your card details).';
  }

  if (!session) {
    title.textContent = 'Activate zhunt Pro';
    detail.textContent = 'This page shows your licence key right after checkout. Start the 7-day trial from the pricing page; you will be brought back here.';
    return;
  }

  var tries = 0;
  (function load() {
    fetch('/api/licence?session_id=' + encodeURIComponent(session), { cache: 'no-store' })
      .then(function (res) { return res.json().then(function (body) { return { status: res.status, body: body }; }); })
      .then(function (r) {
        if (r.status === 409 && tries < 5) { tries += 1; setTimeout(load, 2000); return; } // Stripe still finishing
        if (r.status !== 200 || !r.body.licence) { fail((r.body && r.body.detail ? r.body.detail : 'HTTP ' + r.status) + '.'); return; }
        title.textContent = 'Your Pro licence key';
        detail.textContent = 'For ' + (r.body.email || 'your account') + '. Copy it now and keep it somewhere safe.';
        document.getElementById('licence').textContent = r.body.licence;
        document.getElementById('result').hidden = false;
        document.getElementById('next').hidden = false;
        document.getElementById('copy').addEventListener('click', function () {
          var button = this;
          navigator.clipboard.writeText(r.body.licence).then(function () { button.textContent = 'Copied'; }, function () { button.textContent = 'Select the key and copy it'; });
        });
      })
      .catch(function () { fail('The licence service did not answer.'); });
  })();
})();
