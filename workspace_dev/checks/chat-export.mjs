// Run: node workspace_dev/checks/chat-export.mjs
// The chat export formatter: file names, headings, models, and messages that would break the Markdown.
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const { chatToMarkdown, exportName, titleOf } = await import(pathToFileURL(path.join(here, '../../zhunt/workspace/js/tiles/chat-export.js')).href);

let failed = 0;
const check = (name, ok, detail = '') => { if (ok) console.log('PASS ' + name); else { failed += 1; console.log('FAIL ' + name + ' ' + detail); } };
const day = new Date('2026-11-08T10:00:00Z');

check('file name is a clean slug with the date', exportName('Plan a day in Penang!', day) === 'plan-a-day-in-penang-2026-11-08.md');
check('slashes, dots and control characters never reach the file name', exportName('../../etc/passwd\u0000 ‮ x', day) === 'etc-passwd-x-2026-11-08.md', exportName('../../etc/passwd\u0000 ‮ x', day));
check('empty title falls back to chat', exportName('   ', day) === 'chat-2026-11-08.md' && exportName('日本語', day) === 'chat-2026-11-08.md');
check('long titles are bounded', exportName('a'.repeat(300), day).length <= 40 + '-2026-11-08.md'.length);

const messages = [
  { role: 'user', content: 'Explain\nthis error' },
  { role: 'assistant', content: 'Use this:\n\n```js\nconsole.log(1)\n', model: 'ovh/Qwen3.6-27B' },
  { role: 'user', content: '# not a title\n~~~\nopen tilde' },
];
check('title is the first user line, one line only', titleOf(messages) === 'Explain this error');
const md = chatToMarkdown({ messages, project: 'Trips', now: day });
check('has title, export line and project', md.startsWith('# Explain this error\n\n_Exported from zhunt on 2026-11-08 · project: Trips_\n'), md.slice(0, 90));
check('assistant heading carries the model', md.includes('### zhunt (ovh/Qwen3.6-27B)'));
check('user heading', md.includes('### You\n\nExplain\nthis error'));
check('an unclosed code fence is closed so later messages still render', /console\.log\(1\)\n```\n\n---/.test(md), JSON.stringify(md.slice(md.indexOf('console'), md.indexOf('console') + 40)));
check('an unclosed tilde fence at the end is closed too', md.trimEnd().endsWith('open tilde\n~~~'), JSON.stringify(md.slice(-30)));
check('headings inside a message stay inside it (every message sits under its own ### heading)', (md.match(/^### /gm) || []).length === 3);
check('ends with one newline', md.endsWith('\n') && !md.endsWith('\n\n'));
check('empty chat still exports a title', chatToMarkdown({ messages: [], now: day }).startsWith('# New chat'));

if (failed) { console.log(failed + ' check(s) failed'); process.exit(1); }
console.log('PASS all chat export checks');
