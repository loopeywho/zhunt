# Third-party notices

## FreeLLMAPI

Zhunt's model-selection engine contains logic adapted (re-implemented in Python,
not copied) from FreeLLMAPI, https://github.com/tashfeenahmed/freellmapi.

Files containing logic adapted from FreeLLMAPI:

- `zhunt/state.py`: SQLite persistence of usage, cooldowns and learned limits.
- `zhunt/quota.py`: transient vs daily-exhausted 429 cooldowns, the guessed
  exhaustion cap for providers with no daily limit, daily-reset benches
  (UTC / midnight Pacific), fixed 401/402/403/404 benches, the model failure
  streak bench, and learned limits that only ever lower a cap.
- `zhunt/engine.py`: daily-quota error detection, parsing limits from provider
  error bodies and rate-limit headers.
- `zhunt/selection.py`: the headroom ramp and the decaying failure penalty
  used in scoring.

FreeLLMAPI is distributed under the following licence:

```
MIT License

Copyright (c) 2026 Tashfeen Ahmed

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## xterm.js and FitAddon (Engineering terminals)

Vendored browser distributions: `@xterm/xterm` 6.0.0 and `@xterm/addon-fit`
0.11.0 from npm. MIT licenses are included beside the files in
`zhunt/workspace/vendor/`. Upstream: https://github.com/xtermjs/xterm.js.
These files are served locally; the terminal loads no CDN scripts.
