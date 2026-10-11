// Run: node workspace_dev/checks/team.mjs
// Team chat rules: who a message hands off to, and what each bot sees.
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.location = { hash: '' };
const { mentionsIn, contextFor, systemPrompt, STARTER_BOTS } = await import(pathToFileURL(path.join(here, '../../zhunt/workspace/js/tiles/team.js')).href);

let fail = 0;
const check = (name, ok, extra = '') => { if (!ok) fail += 1; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + extra)); };
const handles = STARTER_BOTS.map((b) => b.handle);
const [researcher, writer] = STARTER_BOTS;

check('mentions are found in order, once each', mentionsIn('@writer then @critic, thanks @writer', handles).join() === 'writer,critic');
check('unknown handles and emails are ignored', mentionsIn('mail bob@example.com or @nobody', handles).length === 0);
check('a bot never hands off to itself', mentionsIn('@researcher here, over to @writer', handles, 'researcher').join() === 'writer');

const transcript = [
  { who: 'you', text: '@researcher compare apps' },
  { who: 'researcher', text: 'Options A and B. @writer please draft' },
  { who: 'writer', text: 'Draft text. @critic check' },
];
const ctxW = contextFor(writer, transcript, STARTER_BOTS);
check('the system prompt names the bot, its job and the team', ctxW[0].role === 'system' && /@writer/.test(ctxW[0].content) && /@critic \(Critic\)/.test(ctxW[0].content));
check('other speakers are labelled user turns, merged when consecutive', ctxW[1].role === 'user' && /\[User\]: @researcher compare apps/.test(ctxW[1].content) && /\[@researcher\]: Options A and B/.test(ctxW[1].content));
check("the bot's own turns are assistant turns", ctxW[2].role === 'assistant' && ctxW[2].content === 'Draft text. @critic check');
const ctxR = contextFor(researcher, [{ who: 'researcher', text: 'stray' }, ...transcript], STARTER_BOTS);
check('the conversation always starts with a user turn', ctxR[1].role === 'user');
check('instructions appear in the system prompt', systemPrompt(researcher, STARTER_BOTS).includes(researcher.instructions));

if (fail) { console.log(fail + ' failed'); process.exit(1); }
console.log('PASS all team checks');
