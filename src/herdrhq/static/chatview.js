/* herdr HQ — chat transcript rendering.
 *
 * Turns /api/transcript/messages typed blocks into DOM. A small per-tool
 * registry decides how each call reads — a one-line row (Read, Grep), a
 * terminal card (Bash), a diff (Edit/Write), a checklist (TodoWrite), a plan
 * card — and everything unmapped still gets a sensible summary line instead
 * of a parameter dump. Fixed shapes for the rest: thinking accordions,
 * slash-command rows, compaction markers, images.
 *
 * Depends on shared.js (el) and md.js (renderMarkdown). work.js owns
 * fetching, caching and scroll; this file is pure payload -> node.
 */

const DIFF_MAX_LINES = 400;   // beyond this, show del-all/add-all without LCS
const DIFF_CTX_FOLD = 8;      // unchanged runs longer than this fold to 3+3
const GROUP_MIN = 3;          // consecutive same-tool calls that collapse

/* ------------------------------------------------------------- summaries */

function fileTail(path) {
  return path ? String(path).split('/').pop() : '';
}

/** mcp__server__tool is accurate and unreadable; the tool is the action. */
function prettyToolName(name) {
  const m = /^mcp__(.+?)__(.+)$/.exec(name);
  return m ? `${m[2]} (${m[1]})` : name;
}

/** First input value that says what an unmapped tool operated on. */
function describeInput(input) {
  for (const key of ['command', 'cmd', 'file_path', 'path', 'pattern', 'query',
    'url', 'prompt', 'description', 'name', 'skill', 'id']) {
    const v = input?.[key];
    if (typeof v === 'string' && v.trim()) return v.replace(/\s+/g, ' ').trim();
  }
  return '';
}

/* Per-tool display config. `line(input)` -> {label, value, secondary} for the
   summary row; `body(input, block)` -> extra node above the output; `output`
   controls the result: 'hide' (noise when it worked), 'term' (mono block),
   'md' (agent answers). Errors always render, whatever `output` says. */
const TOOL_VIEWS = {
  Bash: {
    line: (i) => ({ label: '$', value: i.command || '', secondary: i.description }),
    output: 'term', mono: true,
  },
  Read: {
    line: (i) => ({ label: 'Read', value: i.file_path || '' }),
    path: (i) => i.file_path, output: 'hide',
  },
  Edit: {
    line: (i) => ({ label: 'Edit', value: fileTail(i.file_path), secondary: i.file_path }),
    path: (i) => i.file_path,
    // codex file changes arrive as a ready-made unified diff; claude edits
    // as old/new strings we diff ourselves
    body: (i) => (i.unified_diff
      ? unifiedDiffNode(i.unified_diff)
      : diffNode(i.old_string || '', i.new_string || '')),
    output: 'hide', open: null,
  },
  Write: {
    line: (i) => ({ label: 'Write', value: fileTail(i.file_path), secondary: i.file_path }),
    path: (i) => i.file_path,
    body: (i) => diffNode('', i.content || ''),
    output: 'hide',
  },
  NotebookEdit: {
    line: (i) => ({ label: 'Notebook', value: fileTail(i.notebook_path) }),
    path: (i) => i.notebook_path,
    body: (i) => diffNode('', i.new_source || ''),
    output: 'hide',
  },
  Grep: {
    line: (i) => ({ label: 'Grep', value: i.pattern || '', secondary: i.path ? `in ${i.path}` : '' }),
    output: 'term',
  },
  Glob: {
    line: (i) => ({ label: 'Glob', value: i.pattern || '', secondary: i.path ? `in ${i.path}` : '' }),
    output: 'term',
  },
  WebSearch: { line: (i) => ({ label: 'Search', value: i.query || '' }), output: 'term' },
  WebFetch: { line: (i) => ({ label: 'Fetch', value: i.url || '' }), output: 'term' },
  Skill: { line: (i) => ({ label: 'Skill', value: i.skill || '', secondary: i.args }), output: 'term' },
  TodoWrite: {
    line: (i) => ({ label: 'Todos', value: todoTitle(i.todos) }),
    body: (i) => todoList(i.todos),
    output: 'hide', open: true,
  },
  Task: {
    line: (i) => ({ label: 'Agent', value: i.description || i.subagent_type || 'subagent' }),
    body: (i) => i.prompt ? renderMarkdown(String(i.prompt)) : null,
    output: 'md',
  },
  Agent: {
    line: (i) => ({ label: 'Agent', value: i.description || i.subagent_type || 'subagent' }),
    body: (i) => i.prompt ? renderMarkdown(String(i.prompt)) : null,
    output: 'md',
  },
  ExitPlanMode: {
    line: () => ({ label: 'Plan', value: 'implementation plan' }),
    body: (i) => i.plan ? renderMarkdown(String(i.plan)) : null,
    output: 'hide', open: true, plan: true,
  },
  AskUserQuestion: {
    line: (i) => ({
      label: 'Question',
      value: i.questions?.[0]?.question || 'asking',
      secondary: i.questions?.length > 1 ? `+${i.questions.length - 1} more` : '',
    }),
    body: (i) => questionList(i.questions),
    output: 'term', open: true,
  },
};

function todoTitle(todos) {
  if (!Array.isArray(todos) || !todos.length) return 'updating list';
  const done = todos.filter((t) => t?.status === 'completed').length;
  const active = todos.find((t) => t?.status === 'in_progress');
  if (active) return `${active.activeForm || active.content} — ${done}/${todos.length}`;
  return done === todos.length ? `done — ${done}/${todos.length}` : `${done}/${todos.length} done`;
}

function todoList(todos) {
  if (!Array.isArray(todos) || !todos.length) return null;
  return el('ul', { class: 'chat-todos' }, todos.map((t) => el('li', {
    class: `todo-${t?.status || 'pending'}`,
    text: t?.status === 'in_progress' ? (t.activeForm || t.content) : (t?.content || ''),
  })));
}

function questionList(questions) {
  if (!Array.isArray(questions) || !questions.length) return null;
  return el('div', { class: 'chat-questions' }, questions.map((q) => el('div', {}, [
    el('div', { class: 'chat-q', text: q?.question || '' }),
    Array.isArray(q?.options)
      ? el('ul', {}, q.options.map((o) => el('li', { text: o?.label || String(o) })))
      : null,
  ])));
}

/* ------------------------------------------------------------------ diff */

/** Line-level LCS diff -> [{k: 'ctx'|'del'|'add', text}]. Inputs are already
    server-capped; anything still huge degrades to del-all/add-all. */
function lineDiff(oldText, newText) {
  const a = oldText === '' ? [] : oldText.split('\n');
  const b = newText === '' ? [] : newText.split('\n');
  if (a.length * b.length > DIFF_MAX_LINES * DIFF_MAX_LINES) {
    return [...a.map((t) => ({ k: 'del', text: t })), ...b.map((t) => ({ k: 'add', text: t }))];
  }
  // dp[i][j] = LCS length of a[i:], b[j:]
  const w = b.length + 1;
  const dp = new Uint16Array((a.length + 1) * w);
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      dp[i * w + j] = a[i] === b[j]
        ? dp[(i + 1) * w + j + 1] + 1
        : Math.max(dp[(i + 1) * w + j], dp[i * w + j + 1]);
    }
  }
  const rows = [];
  let i = 0; let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { rows.push({ k: 'ctx', text: a[i] }); i++; j++; }
    else if (dp[(i + 1) * w + j] >= dp[i * w + j + 1]) rows.push({ k: 'del', text: a[i++] });
    else rows.push({ k: 'add', text: b[j++] });
  }
  while (i < a.length) rows.push({ k: 'del', text: a[i++] });
  while (j < b.length) rows.push({ k: 'add', text: b[j++] });
  return rows;
}

function diffNode(oldText, newText) {
  const rows = lineDiff(oldText, newText);
  if (!rows.length) return null;
  const out = [];
  // fold long unchanged runs — the change is the point, not the file
  let run = [];
  const flush = () => {
    if (run.length > DIFF_CTX_FOLD) {
      out.push(...run.slice(0, 3));
      out.push({ k: 'fold', text: `⋯ ${run.length - 6} unchanged lines` });
      out.push(...run.slice(-3));
    } else out.push(...run);
    run = [];
  };
  for (const row of rows) {
    if (row.k === 'ctx') run.push(row);
    else { flush(); out.push(row); }
  }
  flush();
  const MARK = { del: '−', add: '+', ctx: ' ', fold: '' };
  return el('div', { class: 'chat-diff' }, out.map((r) => el('div', {
    class: `diff-${r.k}`,
  }, [
    el('span', { class: 'diff-mark', text: MARK[r.k] }),
    el('span', { class: 'diff-text', text: r.text }),
  ])));
}

/** A pre-formed unified diff, colored line by line. */
function unifiedDiffNode(diffText) {
  const rows = String(diffText).split('\n').map((line) => {
    if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')) {
      return { k: 'fold', text: line };
    }
    if (line.startsWith('+')) return { k: 'add', text: line.slice(1) };
    if (line.startsWith('-')) return { k: 'del', text: line.slice(1) };
    return { k: 'ctx', text: line.startsWith(' ') ? line.slice(1) : line };
  });
  const MARK = { del: '−', add: '+', ctx: ' ', fold: '' };
  return el('div', { class: 'chat-diff' }, rows.map((r) => el('div', {
    class: `diff-${r.k}`,
  }, [
    el('span', { class: 'diff-mark', text: MARK[r.k] }),
    el('span', { class: 'diff-text', text: r.text }),
  ])));
}

/* ----------------------------------------------------------- tool blocks */

function summaryRow(cfg, name, line, block, ctx) {
  const state = block.error ? 'error' : (block.pending ? 'pending' : 'done');
  const valueClass = `chat-tool-value${cfg.mono === false ? '' : ' is-mono'}`;
  // a tool that names a file gets its value linked into the viewer; the
  // stopPropagation keeps the click from also toggling the details row
  const href = cfg.path && ctx?.fileHref ? ctx.fileHref(cfg.path(block.input || {})) : null;
  const value = href
    ? el('a', {
      class: `${valueClass} chat-file`, href, target: '_blank',
      text: line.value || '', onclick: (ev) => ev.stopPropagation(),
    })
    : el('span', { class: valueClass, text: line.value || '' });
  return el('span', { class: 'chat-tool-line', 'data-state': state }, [
    el('span', { class: 'chat-tool-name', text: line.label || prettyToolName(name) }),
    value,
    line.secondary ? el('span', { class: 'chat-tool-sub', text: line.secondary }) : null,
    block.pending ? el('span', { class: 'chat-tool-sub', text: 'running…' }) : null,
  ]);
}

function renderToolBlock(block, ctx) {
  const name = block.name || 'tool';
  const cfg = TOOL_VIEWS[name] || {
    line: (i) => ({ label: prettyToolName(name), value: describeInput(i) }),
    output: 'term',
  };
  const input = (block.input && typeof block.input === 'object') ? block.input : {};
  const line = cfg.line(input);
  const row = summaryRow(cfg, name, line, block, ctx);

  const parts = [];
  const body = cfg.body ? cfg.body(input, block) : null;
  if (body) parts.push(body);
  const result = block.result || '';
  if (block.error) {
    parts.push(el('pre', { class: 'chat-out is-error', text: result || 'failed' }));
  } else if (result && cfg.output !== 'hide') {
    parts.push(cfg.output === 'md'
      ? renderMarkdown(result)
      : el('pre', { class: 'chat-out', text: result }));
  }

  if (!parts.length) {
    return el('div', { class: `chat-tool${block.error ? ' is-error' : ''}` }, [row]);
  }
  const open = block.error ? false : (cfg.open ?? false);
  return el('details', {
    class: `chat-tool is-card${block.error ? ' is-error' : ''}${cfg.plan ? ' is-plan' : ''}`,
    ...(open ? { open: '' } : {}),
  }, [el('summary', {}, [row]), el('div', { class: 'chat-tool-body' }, parts)]);
}

/** Runs of the same tool fold into one row — five Reads are one fact. */
function groupToolNodes(blocks) {
  const out = [];
  let i = 0;
  while (i < blocks.length) {
    const b = blocks[i];
    if (b.t !== 'tool') { out.push(b); i++; continue; }
    let j = i;
    while (j < blocks.length && blocks[j].t === 'tool' && blocks[j].name === b.name) j++;
    const run = blocks.slice(i, j);
    if (run.length >= GROUP_MIN && !run.some((x) => x.error || x.pending)) {
      const cfg = TOOL_VIEWS[b.name];
      const preview = run.slice(0, 2)
        .map((x) => (cfg ? cfg.line(x.input || {}).value : describeInput(x.input)) || '')
        .filter(Boolean).join(', ');
      out.push({
        t: '_group',
        name: b.name,
        run,
        preview: preview + (run.length > 2 ? `, +${run.length - 2} more` : ''),
      });
    } else out.push(...run);
    i = j;
  }
  return out;
}

function renderGroup(g, ctx) {
  return el('details', { class: 'chat-tool is-card is-group' }, [
    el('summary', {}, [el('span', { class: 'chat-tool-line', 'data-state': 'done' }, [
      el('span', { class: 'chat-tool-name', text: `${prettyToolName(g.name)} ×${g.run.length}` }),
      el('span', { class: 'chat-tool-value is-mono', text: g.preview }),
    ])]),
    el('div', { class: 'chat-tool-body' }, g.run.map((b) => renderToolBlock(b, ctx))),
  ]);
}

/* ------------------------------------------------------- prose file links */

// something worth trying as a path: no spaces, path-safe characters, and
// either directories or a real extension (one with a letter — "v0.1.15" is
// a version, not a file)
const PATHISH_RE = /^(~?\/)?[\w.@%+=-]+(\/[\w.@%+=-]+)*$/;
const FILE_EXT_RE = /\.[A-Za-z][A-Za-z0-9]{0,7}$/;

function pathCandidate(text) {
  if (!text || text.length > 200 || /\s/.test(text)) return null;
  if (!PATHISH_RE.test(text)) return null;
  if (!text.includes('/') && !FILE_EXT_RE.test(text)) return null;
  return text;
}

/** Turn path-looking inline code in rendered markdown into viewer links.
    Block code is left alone — a script is not a file mention. */
function linkifyPaths(body, ctx) {
  if (!ctx?.fileHref) return;
  for (const code of body.querySelectorAll('code')) {
    if (code.closest('pre') || code.closest('a')) continue;
    const candidate = pathCandidate(code.textContent);
    const href = candidate && ctx.fileHref(candidate);
    if (!href) continue;
    const a = el('a', { class: 'chat-file', href, target: '_blank' });
    code.replaceWith(a);
    a.append(code);
  }
  // agents also write [label](/abs/path.pdf) markdown links to host files —
  // dead in a browser; route them through the viewer when they resolve
  for (const a of body.querySelectorAll('a[href]')) {
    let href = a.getAttribute('href');
    if (/^(https?:|mailto:|#|\/view\?|\/api\/)/i.test(href)) continue;
    href = href.replace(/^file:\/\//i, '');
    let target = null;
    try { target = ctx.fileHref(decodeURI(href)); } catch (_) { /* bad escape */ }
    if (target) {
      a.href = target;
      a.target = '_blank';
    }
  }
}

/* --------------------------------------------------------------- blocks */

function renderBlock(block, ctx) {
  switch (block.t) {
    case 'text': {
      const body = renderMarkdown(block.text || '');
      linkifyPaths(body, ctx);
      return body;
    }
    case 'thinking':
      return el('details', { class: 'chat-thinking' }, [
        el('summary', { text: 'thinking' }),
        renderMarkdown(block.text || ''),
      ]);
    case 'tool':
      return renderToolBlock(block, ctx);
    case '_group':
      return renderGroup(block, ctx);
    case 'command': {
      const label = [block.name, block.args].filter(Boolean).join(' ');
      const line = el('code', { text: label || 'command output' });
      if (!block.output) return el('div', { class: 'chat-cmd' }, [line]);
      return el('details', { class: 'chat-cmd' }, [
        el('summary', {}, [line]),
        el('pre', { class: 'chat-out', text: block.output }),
      ]);
    }
    case 'compact':
      return el('details', { class: 'chat-compact' }, [
        el('summary', { text: 'context compacted — summary' }),
        renderMarkdown(block.text || ''),
      ]);
    case 'image':
      return el('img', { class: 'chat-img', src: block.src, alt: 'attached image' });
    default:
      return null;
  }
}

/** One transcript entry -> its .chat-msg node. ctx.fileHref, when given,
    resolves a path the transcript mentions to a viewer URL (or null). */
function renderChatMessage(m, ctx) {
  const cls = m.role === 'user' ? 'chat-msg is-user'
    : m.role === 'system' ? 'chat-msg is-system' : 'chat-msg';
  const box = el('div', { class: cls });
  if (m.role === 'user' && m.blocks.some((b) => b.t === 'text' || b.t === 'image')) {
    box.append(el('div', { class: 'chat-role', text: 'you' }));
  }
  for (const block of groupToolNodes(m.blocks || [])) {
    const node = renderBlock(block, ctx);
    if (node) box.append(node);
  }
  return box;
}
