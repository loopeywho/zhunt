// Run: node workspace_dev/checks/agent.mjs
// Agent window: slash command parsing and the command list.
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.location = { hash: '' };
const { parseCommand, COMMANDS, parseBriefsArgs, briefItemLine, caughtUpLine } = await import(pathToFileURL(path.join(here, '../../zhunt/workspace/js/tiles/agent.js')).href);

let fail = 0;
const check = (name, ok, extra = '') => { if (!ok) fail += 1; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + extra)); };
const loop = parseCommand('/loop 2h check the news');
check('a command and its arguments are split', loop.cmd === 'loop' && loop.args === '2h check the news');
check('plain text is a task, not a command', parseCommand('find me a recipe').cmd === null);
check('a slash later in the text is not a command', parseCommand('compare a/b testing tools').cmd === null);
check('multi-line goals keep their lines', parseCommand('/goal line one\nline two').args === 'line one\nline two');
check('every advertised command starts with /', COMMANDS.every(([c]) => c.startsWith('/')));
check('goal, loop, stop and help exist', ['/goal', '/loop', '/stop', '/help'].every((c) => COMMANDS.some(([x]) => x === c)));
check('/briefs on and off switch briefs', parseBriefsArgs('on').enabled === true && parseBriefsArgs(' OFF ').enabled === false);
check('/briefs with times sets the times', JSON.stringify(parseBriefsArgs('07:30, 18:00')) === '{"times":["07:30","18:00"]}');
check('/briefs alone just shows the latest', Object.keys(parseBriefsArgs('')).length === 0);
check('/briefs nonsense is refused', parseBriefsArgs('8am') === null && parseBriefsArgs('25:00') === null);
check('a brief item without a due date is just its summary', briefItemLine({ summary: 'Sam needs the deck', due: null }) === 'Sam needs the deck');
check('a brief item shows its due date', /^Sam needs the deck · due .*16/.test(briefItemLine({ summary: 'Sam needs the deck', due: '2026-10-16' })));
check('a catch-up brief says what it covers', caughtUpLine(Date.UTC(2026, 9, 12, 17) / 1000).startsWith('caught up: covers since '));
check('brief commands are advertised', ['/brief', '/briefs'].every((c) => COMMANDS.some(([x]) => x === c)));
if (fail) { console.log(fail + ' failed'); process.exit(1); }
console.log('PASS all agent checks');
