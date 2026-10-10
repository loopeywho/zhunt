// Run: node workspace_dev/checks/md.mjs
// Each hazardous input runs in a child process with a hard timeout so a regression (infinite loop,
// catastrophic backtracking) fails the check instead of hanging it.
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const child = process.argv[2];

if (child) {
  const { installDom, countNodes } = await import('./_dom.mjs');
  installDom();
  const { renderMarkdown } = await import('../../zhunt/workspace/js/md.js');
  const src = eval(child); // eslint-disable-line no-eval
  const root = document.createElement('div');
  const t0 = performance.now();
  renderMarkdown(src, root);
  console.log(JSON.stringify({ ms: Math.round(performance.now() - t0), nodes: countNodes(root), text: root.textContent.slice(0, 40) }));
  process.exit(0);
}

const cases = [
  ['heading with U+2028', "'# Title\\u2028more'", 1500, (r) => r.nodes < 10],
  ['heading with U+2029', "'## Title\\u2029more\\nnext'", 1500, (r) => r.nodes < 10],
  ['bullet-looking line after U+2028 heading', "'# a\\u2028- b'", 1500, (r) => r.nodes < 10],
  ['heading + 4000 spaces + b', "'# a' + ' '.repeat(4000) + 'b'", 1500, (r) => r.nodes < 10],
  ['heading + 40000 spaces + b', "'# a' + ' '.repeat(40000) + 'b'", 1500, (r) => r.nodes < 10],
  ['heading with trailing hashes', "'## Hello ##'", 1500, (r) => r.text === 'Hello'],
  ['heading hashes inside word', "'# C#'", 1500, (r) => r.text === 'C#'],
  ['quadratic emphasis', "'**a '.repeat(16000)", 4000, () => true],
];
let fail = 0;
for (const [name, expr, limit, ok] of cases) {
  const r = spawnSync(process.execPath, [path.join(here, 'md.mjs'), expr], { timeout: limit + 3000, encoding: 'utf8' });
  let res = null;
  try { res = JSON.parse(r.stdout.trim()); } catch { /* timeout or crash */ }
  const pass = res && res.ms <= limit && ok(res);
  if (!pass) fail += 1;
  console.log((pass ? 'PASS ' : 'FAIL ') + name + (res ? ` (${res.ms}ms, ${res.nodes} nodes)` : ' (timeout/crash)'));
}
process.exit(fail ? 1 : 0);
