// Export a chat as a Markdown file. Pure functions plus one download helper; nothing is uploaded.

const oneLine = (text) => String(text).replace(/[\u0000-\u001f\u007f]+/g, ' ').replace(/\s+/g, ' ').trim();

export function titleOf(messages) {
  const first = messages.find((m) => m.role === 'user');
  const line = oneLine(first ? first.content : '');
  return line ? (line.length > 60 ? line.slice(0, 60) + '…' : line) : 'New chat';
}

// A file name that is safe on every system: no slashes, control characters or leading dots, bounded length.
export function exportName(title, now = new Date()) {
  const slug = oneLine(title).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40).replace(/-+$/, '');
  return `${slug || 'chat'}-${now.toISOString().slice(0, 10)}.md`;
}

// A message that opens a code fence and never closes it would swallow everything after it when rendered.
function closeOpenFence(text) {
  let open = null;
  for (const line of text.split('\n')) {
    const m = /^ {0,3}(`{3,}|~{3,})/.exec(line);
    if (!m) continue;
    if (!open) open = m[1];
    else if (m[1][0] === open[0] && m[1].length >= open.length) open = null;
  }
  return open ? `${text}\n${open}` : text;
}

export function chatToMarkdown({ messages, project = '', now = new Date() }) {
  const out = [`# ${titleOf(messages)}`, '', `_Exported from zhunt on ${now.toISOString().slice(0, 10)}${project ? ' · project: ' + oneLine(project) : ''}_`];
  for (const m of messages) {
    const who = m.role === 'user' ? 'You' : 'zhunt' + (m.model ? ` (${oneLine(m.model)})` : '');
    out.push('', '---', '', `### ${who}`, '', closeOpenFence(String(m.content).replace(/\s+$/, '')));
  }
  return out.join('\n') + '\n';
}

export function downloadText(filename, text) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
