/* herdr HQ — workspace view.
 *
 * Left: a flat list of checkouts ("repo @ machine" over its branch) and
 * machines holding loose panes. Center: the
 * selected checkout's panes as tabs above a live terminal mirror; agent panes
 * toggle to a chat rendering of their session (markdown + math + highlighted
 * code) with a composer that types into the live pane. Right: files the agent
 * touched, listening ports, live views and tunnels.
 */

const ui = {
  liveState: document.getElementById('liveState'),
  liveLabel: document.getElementById('liveLabel'),
  themeToggle: document.getElementById('themeToggle'),
  projectTree: document.getElementById('projectTree'),
  machineHead: document.getElementById('machineHead'),
  machineTree: document.getElementById('machineTree'),
  workCrumb: document.getElementById('workCrumb'),
  paneTabs: document.getElementById('paneTabs'),
  workModes: document.getElementById('workModes'),
  termCtl: document.getElementById('termCtl'),
  termPanel: document.getElementById('termPanel'),
  termStatus: document.getElementById('termStatus'),
  workInput: document.getElementById('workInput'),
  workSmaller: document.getElementById('workSmaller'),
  workBigger: document.getElementById('workBigger'),
  workMount: document.getElementById('workMount'),
  workNote: document.getElementById('workNote'),
  chatPanel: document.getElementById('chatPanel'),
  chatMeta: document.getElementById('chatMeta'),
  chatScroll: document.getElementById('chatScroll'),
  chatList: document.getElementById('chatList'),
  chatForm: document.getElementById('chatForm'),
  chatStatus: document.getElementById('chatStatus'),
  chatText: document.getElementById('chatText'),
  chatSend: document.getElementById('chatSend'),
  chatNote: document.getElementById('chatNote'),
  filesPanel: document.getElementById('filesPanel'),
  workCrumbs: document.getElementById('workCrumbs'),
  workListing: document.getElementById('workListing'),
  dotfiles: document.getElementById('dotfiles'),
  ctxFiles: document.getElementById('ctxFiles'),
  ctxFileList: document.getElementById('ctxFileList'),
  ctxPorts: document.getElementById('ctxPorts'),
  ctxPortList: document.getElementById('ctxPortList'),
  ctxViews: document.getElementById('ctxViews'),
  ctxViewList: document.getElementById('ctxViewList'),
  ctxTunnels: document.getElementById('ctxTunnels'),
  ctxTunnelList: document.getElementById('ctxTunnelList'),
};

const state = {
  fleet: null,
  model: null,            // {checkouts, projects, machines}
  sel: { host: null, top: null, pane: null, tab: 'term' },
  lastPane: new Map(),    // `${host}|${top}` -> pane_id last used there
  allowInput: true,
  filesPath: null,
  chat: { stamp: null, pinned: true, timer: null, files: [], nodes: null, nodeList: null, sent: null },
  proxied: new Map(),     // `${host}|${port}` -> preview URL
  fetchTimer: null,
  renderTimer: null,
};

const LIVE_RE = /\.(pdf|png|jpe?g|gif|webp|svg|bmp|avif|ico|md|markdown|txt|log|out|err|json|ya?ml|toml|csv|tsv|py|r|jl|js|ts|sh|zsh|bash|tex|bib|sty|cls|rst|org|nix|ini|cfg|conf|sql|lock|service)$/i;
const ICONS = [
  [/\.(png|jpe?g|gif|webp|svg|bmp|avif|ico|tiff?|eps)$/i, '🖼'],
  [/\.pdf$/i, '📕'],
  [/\.(mp4|mov|mkv|avi|webm)$/i, '🎬'],
  [/\.(zip|tar|gz|tgz|bz2|xz|7z|rar|zst|dmg)$/i, '📦'],
  [/\.(csv|tsv|parquet|feather|dta|rds|xls[xm]?)$/i, '📊'],
  [/\.(ya?ml|toml|ini|cfg|conf|json|lock)$/i, '⚙️'],
  [/\.(py|r|jl|js|ts|tsx|jsx|c|h|cpp|hpp|rs|go|java|sh|zsh|bash|sql|html|css|do|m|f90)$/i, '⌨️'],
  [/\.(md|txt|rst|org|log|tex|bib)$/i, '📝'],
];
const fileIcon = (e) => {
  if (e.dir) return '📁';
  for (const [re, ic] of ICONS) if (re.test(e.name)) return ic;
  return '📄';
};

const mirror = createMirror({
  mount: ui.workMount,
  onStatus: (text, kind) => {
    ui.termStatus.textContent = text;
    ui.termStatus.dataset.state = kind;
  },
  onNote: (text) => { ui.workNote.textContent = text; },
  getAllowInput: () => state.allowInput && ui.workInput.checked,
});

/* ------------------------------------------------------------- the model */

function buildModel(fleet) {
  // every pane tagged with its host — checkouts form around any pane that sits
  // in a repo, agent or not, so a repo with only shells still shows as a project
  const rows = [];
  for (const host of fleet.hosts || []) {
    for (const pane of host.data?.panes || []) {
      rows.push({ ...pane, host: host.name, hostMeta: host });
    }
  }

  const checkouts = new Map();
  for (const row of rows) {
    if (!row.git) continue;
    const key = `${row.host}|${row.git.toplevel}`;
    if (!checkouts.has(key)) {
      checkouts.set(key, {
        key,
        host: row.host,
        hostMeta: row.hostMeta,
        top: row.git.toplevel,
        git: row.git,
        repo: row.git.repo_name,
        panes: [],
      });
    }
  }
  const byHost = {};
  for (const co of checkouts.values()) (byHost[co.host] ||= []).push(co);

  // a checkout owns its herdr workspaces too: herdr groups related panes
  // (workers, reviewers, shells) in one workspace even when their cwds
  // wander, and all of them should be tabs
  const wsCheckouts = new Map(); // `${host}|${workspace_id}` -> Set of checkouts
  for (const row of rows) {
    if (!row.git) continue;
    const co = checkouts.get(`${row.host}|${row.git.toplevel}`);
    const wk = `${row.host}|${row.workspace_id}`;
    if (!wsCheckouts.has(wk)) wsCheckouts.set(wk, new Set());
    wsCheckouts.get(wk).add(co);
  }

  const machines = new Map();
  for (const host of fleet.hosts) {
    machines.set(host.name, {
      host: host.name, hostMeta: host, panes: [], down: host.status !== 'ok',
    });
    const tabLabels = {};
    for (const t of host.data?.tabs || []) tabLabels[t.tab_id] = t.label;
    for (const pane of host.data?.panes || []) {
      const label = tabLabels[pane.tab_id];
      const enriched = {
        ...pane,
        host: host.name,
        hostMeta: host,
        key: `${host.name}/${pane.pane_id}`,
        status: pane.agent_status || 'unknown',
        // herdr's tab name, when someone (or the rename plugin) gave it one
        tabLabel: label && !/^\d+$/.test(label) ? label : null,
      };
      const owners = new Set(wsCheckouts.get(`${host.name}|${pane.workspace_id}`) || []);
      const cwdMatch = (byHost[host.name] || []).filter(
        (co) => pane.cwd && (pane.cwd === co.top || pane.cwd.startsWith(`${co.top}/`)),
      ).sort((a, b) => b.top.length - a.top.length)[0];
      if (cwdMatch) owners.add(cwdMatch);
      if (owners.size) for (const co of owners) co.panes.push(enriched);
      else machines.get(host.name).panes.push(enriched);
    }
  }

  const projects = new Map();
  for (const co of checkouts.values()) {
    if (!projects.has(co.repo)) projects.set(co.repo, []);
    projects.get(co.repo).push(co);
  }
  for (const list of projects.values()) {
    list.sort((a, b) => a.host.localeCompare(b.host)
      || (a.git.branch || '').localeCompare(b.git.branch || ''));
  }
  return { checkouts, projects, machines };
}

function currentPanes() {
  if (!state.model || !state.sel.host) return [];
  if (state.sel.top) {
    return state.model.checkouts.get(`${state.sel.host}|${state.sel.top}`)?.panes || [];
  }
  return state.model.machines.get(state.sel.host)?.panes || [];
}

function currentPane() {
  return currentPanes().find((p) => p.pane_id === state.sel.pane) || null;
}

function filesRoot() {
  if (state.sel.top) return state.sel.top;
  return currentPane()?.cwd || '.';
}

/* -------------------------------------------------------------- sidebar */

/** One dot summarising a whole entry — blocked beats working beats idle —
    so the eye can jump straight to whatever needs attention. */
function aggDot(panes) {
  const agents = panes.filter((p) => p.is_agent);
  let s = 'idle';
  if (agents.some((p) => p.status === 'blocked')) s = 'blocked';
  else if (agents.some((p) => p.status === 'working')) s = 'working';
  return el('span', {
    class: `dot${s === 'working' ? ' is-live' : ''}`,
    'data-status': s,
    title: STATUS_LABEL[s],
  });
}

/** Marker for a plain (non-agent) pane — a prompt glyph, not a dot, so a
    shell never reads as one more gray agent. */
const shellMark = () => el('span', { class: 'shell-mark', text: '❯' });

function statusPills(panes) {
  const counts = {};
  let plain = 0;
  for (const p of panes) {
    if (p.is_agent) counts[p.status] = (counts[p.status] || 0) + 1;
    else plain += 1;
  }
  const pill = (mark, n, title) => el('span', { class: 'tree-pill', title }, [
    mark,
    el('span', { class: 'tree-pill-n', text: String(n) }),
  ]);
  return el('span', { class: 'tree-pills' }, [
    ...STATUS_ORDER.filter((s) => counts[s]).map((s) =>
      pill(el('span', { class: `dot${s === 'working' ? ' is-live' : ''}`, 'data-status': s }),
        counts[s], `${counts[s]} ${STATUS_LABEL[s]} agent${counts[s] === 1 ? '' : 's'}`)),
    plain ? pill(shellMark(), plain, `${plain} plain pane${plain === 1 ? '' : 's'}`) : null,
  ]);
}

function abMarks(g) {
  return [
    g.ahead ? el('span', {
      class: 'tree-ab', text: `↑${g.ahead}`,
      title: `${g.ahead} commit${g.ahead === 1 ? '' : 's'} to push`,
    }) : null,
    g.behind ? el('span', {
      class: 'tree-ab', text: `↓${g.behind}`,
      title: `${g.behind} commit${g.behind === 1 ? '' : 's'} to pull`,
    }) : null,
  ].filter(Boolean);
}

/** Uncommitted content as a quiet "±N files" mark — dirt is the norm on an
    active checkout, so it stays muted; only conflicts turn it red. */
function dirtyMark(g) {
  if (!g.dirty) return null;
  const bits = [];
  if (g.staged) bits.push(`${g.staged} staged`);
  if (g.unstaged) bits.push(`${g.unstaged} modified`);
  if (g.untracked) bits.push(`${g.untracked} untracked`);
  if (g.conflicts) bits.push(`${g.conflicts} conflicted`);
  const n = (g.staged || 0) + (g.unstaged || 0) + (g.untracked || 0) + (g.conflicts || 0);
  return el('span', {
    class: `tree-dirty${g.conflicts ? ' has-conflicts' : ''}`,
    text: `±${n}`, title: bits.join(' · '),
  });
}

/** Every checkout is one flat two-line entry — "repo @ machine" over the
    branch (its worktree name when that says more); panes live in the tab bar. */
function checkoutRow(co) {
  const onThis = state.sel.host === co.host && state.sel.top === co.top;
  const branch = co.git.branch || co.git.worktree_name || 'detached';
  return el('button', {
    class: `tree-row tree-entry tree-parent${onThis ? ' is-selected' : ''}`,
    type: 'button',
    title: `${co.top} on ${co.host}`,
    onclick: () => select(co.host, co.top, null, null),
  }, [
    el('span', { class: 'tree-line' }, [
      aggDot(co.panes),
      el('span', { class: 'tree-label', text: co.repo }),
      el('span', { class: 'tree-host', text: `@ ${co.host}` }),
      statusPills(co.panes),
    ]),
    el('span', { class: 'tree-sub' }, [
      el('span', { class: 'tree-branch', text: co.git.worktree ? `⌥ ${branch}` : branch }),
      dirtyMark(co.git),
      ...abMarks(co.git),
    ]),
  ]);
}

function machineRow(m) {
  const onThis = state.sel.host === m.host && !state.sel.top;
  return el('button', {
    class: `tree-row tree-parent${onThis ? ' is-selected' : ''}`,
    type: 'button',
    onclick: () => select(m.host, '', null, null),
  }, [
    aggDot(m.panes),
    el('span', { class: 'tree-label', text: m.host }),
    m.down ? el('span', { class: 'tree-extra', text: 'unreachable' }) : null,
    statusPills(m.panes),
  ].filter(Boolean));
}

function renderSidebar() {
  if (!state.model) return;
  const projects = [...state.model.projects.entries()]
    .sort((a, b) => b[1].reduce((s, c) => s + c.panes.length, 0)
      - a[1].reduce((s, c) => s + c.panes.length, 0));
  ui.projectTree.replaceChildren(...projects.flatMap(([, cos]) => cos.map(checkoutRow)));
  if (!projects.length) {
    ui.projectTree.replaceChildren(el('div', { class: 'empty-hint', text: 'No agents in any repository.' }));
  }

  // a machine only earns a row when there is something to say — loose panes,
  // an unreachable host, or the selection currently living there
  const machines = [...state.model.machines.values()].filter(
    (m) => m.down || m.panes.length || (state.sel.host === m.host && !state.sel.top));
  ui.machineHead.hidden = !machines.length;
  ui.machineTree.replaceChildren(...machines.map(machineRow));
}

/* --------------------------------------------------- selection + header */

/** A closed pane or checkout must not strand the center pane: when the fresh
    model no longer resolves the selection, move it somewhere real — the same
    checkout's surviving panes, else the first project, else the machine's
    loose panes. Returns true when it re-selected (and thus re-rendered). */
function revalidateSelection() {
  if (!state.sel.host || !state.model) return false;
  if (state.sel.top && !state.model.checkouts.has(`${state.sel.host}|${state.sel.top}`)) {
    const first = [...state.model.projects.values()][0]?.[0];
    if (first) select(first.host, first.top, null, null);
    else select(state.sel.host, '', null, null);
    return true;
  }
  const panes = currentPanes();
  if ((state.sel.pane && !panes.some((p) => p.pane_id === state.sel.pane))
      || (!state.sel.pane && panes.length)) {
    select(state.sel.host, state.sel.top, state.sel.pane, null);
    return true;
  }
  return false;
}

function select(host, top, pane, tab, { push = true } = {}) {
  const changedTarget = host !== state.sel.host || top !== state.sel.top;
  state.sel.host = host;
  state.sel.top = top;
  const panes = currentPanes();
  // coming back to a place resumes its last pane, not the first agent
  if (!pane) {
    const last = state.lastPane.get(`${host}|${top}`);
    if (last && panes.some((p) => p.pane_id === last)) pane = last;
  }
  if (!pane || !panes.some((p) => p.pane_id === pane)) {
    const firstAgent = panes.find((p) => p.is_agent);
    pane = (firstAgent || panes[0])?.pane_id || null;
  }
  state.sel.pane = pane;
  if (pane) state.lastPane.set(`${host}|${top}`, pane);
  const cur = currentPane();
  if (!tab) tab = state.sel.tab;
  if (tab === 'chat' && !(cur?.is_agent)) tab = 'term';
  state.sel.tab = tab || 'term';
  if (changedTarget) state.filesPath = null;
  if (push) writeUrl();
  renderSidebar();
  renderHeader();
  showPanel();
  loadCtx();
}

/** Fill the single slim bar above the center: the selected checkout's panes
    as tabs on the left, a compact repo/branch context, then (in terminal
    mode) the terminal controls, then the mode toggle. */
function renderHeader() {
  const cur = currentPane();
  const co = state.sel.top
    ? state.model?.checkouts.get(`${state.sel.host}|${state.sel.top}`) : null;

  if (!state.sel.host) {
    ui.paneTabs.replaceChildren(el('span', { class: 'empty-hint', text: 'Pick a project or machine on the left.' }));
    ui.workCrumb.replaceChildren();
    ui.workModes.replaceChildren();
    return;
  }

  // tabs — one per pane; duplicated labels get the pane id to tell them apart
  const panes = currentPanes();
  const labelOf = (p) => p.tabLabel || (p.is_agent ? (p.agent || 'agent') : 'shell');
  const counts = {};
  for (const p of panes) counts[labelOf(p)] = (counts[labelOf(p)] || 0) + 1;
  ui.paneTabs.replaceChildren(...(panes.length ? panes.map((p) => {
    const label = labelOf(p);
    const foreign = p.git && state.sel.top && p.git.toplevel !== state.sel.top;
    return el('button', {
      class: `work-tab${p.pane_id === state.sel.pane ? ' is-on' : ''}`,
      type: 'button',
      title: [p.title || label, p.pane_id, foreign ? `in ${p.git.repo_name}` : null]
        .filter(Boolean).join(' · '),
      onclick: () => select(state.sel.host, state.sel.top, p.pane_id,
        state.sel.tab === 'files' ? 'term' : null),
    }, [
      p.is_agent
        ? el('span', { class: `dot${p.status === 'working' ? ' is-live' : ''}`, 'data-status': p.status })
        : shellMark(),
      el('span', { class: 'work-tab-label', text: label }),
      counts[label] > 1 ? el('span', { class: 'work-tab-id', text: p.pane_id }) : null,
    ].filter(Boolean));
  }) : [el('span', { class: 'empty-hint', text: 'No panes here.' })]));
  // reveal the active tab when the selection moves, but don't fight the user's
  // own scrolling on live status re-renders
  if (state.sel.pane && state.sel.pane !== renderHeader.scrolled) {
    renderHeader.scrolled = state.sel.pane;
    ui.paneTabs.querySelector('.is-on')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }

  // compact context — repo @ host · branch ±N ↑↓; the full path lives in Files
  const cg = co?.git;
  ui.workCrumb.replaceChildren(...(co ? [
    el('span', { class: 'crumb-sub', text: `${co.repo} @ ${co.host}` }),
    el('span', { class: 'crumb-sub', text: '·' }),
    el('span', {
      class: 'git-branch',
      text: (cg.worktree ? '⌥ ' : '') + (cg.branch || cg.worktree_name || 'detached'),
    }),
    dirtyMark(cg),
    ...abMarks(cg),
  ].filter(Boolean) : [el('span', { class: 'crumb-sub', text: state.sel.host })]));

  // mode toggle
  const modes = [];
  if (cur?.is_agent) {
    modes.push(el('span', { class: 'viewtoggle' }, [
      el('button', { class: `btn btn-seg${state.sel.tab === 'term' ? ' is-on' : ''}`, type: 'button',
        onclick: () => select(state.sel.host, state.sel.top, state.sel.pane, 'term'), text: 'Terminal' }),
      el('button', { class: `btn btn-seg${state.sel.tab === 'chat' ? ' is-on' : ''}`, type: 'button',
        onclick: () => select(state.sel.host, state.sel.top, state.sel.pane, 'chat'), text: 'Chat' }),
    ]));
  }
  modes.push(el('button', {
    class: `btn btn-seg files-btn${state.sel.tab === 'files' ? ' is-on' : ''}`, type: 'button',
    onclick: () => select(state.sel.host, state.sel.top, state.sel.pane, 'files'),
  }, [el('span', { 'aria-hidden': 'true', text: '🗀' }), el('span', { text: ' Files' })]));
  ui.workModes.replaceChildren(...modes);
}

function showPanel() {
  const tab = state.sel.host ? state.sel.tab : null;
  ui.termPanel.hidden = tab !== 'term';
  ui.chatPanel.hidden = tab !== 'chat';
  ui.filesPanel.hidden = tab !== 'files';
  ui.termCtl.hidden = tab !== 'term';  // terminal controls only in terminal mode

  clearInterval(state.chat.timer);
  state.chat.timer = null;

  if (tab === 'term' && state.sel.pane) {
    const target = mirror.target();
    if (!target || target.host !== state.sel.host || target.pane !== state.sel.pane) {
      mirror.connect({ host: state.sel.host, pane: state.sel.pane });
    }
    // the panel may have been unhidden this very tick; measure after layout
    requestAnimationFrame(() => mirror.fit());
  } else {
    mirror.close();  // one terminal stream at a time; sessions survive on grace
  }

  if (tab === 'chat') {
    state.chat.stamp = null;
    state.chat.nodes = null;
    state.chat.nodeList = null;
    ui.chatMeta.hidden = true;
    ui.chatList.replaceChildren(el('div', { class: 'empty-hint', text: 'loading conversation…' }));
    renderChatStatus();
    loadChat();
    state.chat.timer = setInterval(() => {
      if (!document.hidden) { loadChat(); renderChatStatus(); }
    }, 4000);
    const canSend = state.allowInput && currentPane()?.is_agent;
    ui.chatText.disabled = !canSend;
    ui.chatSend.disabled = !canSend;
    ui.chatNote.textContent = canSend
      ? 'Messages go straight into the live pane, Enter included — same as typing there.'
      : 'Terminal input is disabled in the config; the conversation is read-only.';
  }
  if (tab === 'files') loadFiles(state.filesPath || filesRoot());
}

/* ----------------------------------------------------------------- chat */

/** Live status pill beside the prompt box, so it's obvious whether the
    agent is busy, waiting on you, or ready for the next prompt. Right
    after a send herdr hasn't noticed the agent moving yet; a transient
    "sent" pill bridges that gap so the prompt never feels lost. */
function renderChatStatus() {
  if (ui.chatPanel.hidden) return;
  const cur = currentPane();
  const s = cur?.is_agent ? cur.status : null;
  const sent = state.chat.sent;
  if (sent && (cur?.key !== sent.pane || s === 'working' || s === 'blocked'
      || Date.now() - sent.at > 15000)) {
    state.chat.sent = null;
  }
  if (state.chat.sent && s) {
    ui.chatStatus.replaceChildren(el('span', {
      class: 'status-pill', 'data-status': 'sent',
      title: 'prompt delivered — waiting for the agent to pick it up',
    }, [
      el('span', { class: 'dot is-live', 'data-status': 'sent' }),
      el('span', { text: '↑ sent' }),
    ]));
    return;
  }
  ui.chatStatus.replaceChildren(...(s && s !== 'unknown' ? [statusPill(s)] : []));
}

async function loadChat() {
  const pane = currentPane();
  if (!pane?.is_agent) return;
  let data;
  try {
    data = await api(`/api/transcript/messages?${qs({ host: state.sel.host, pane: pane.pane_id })}`);
  } catch (e) {
    ui.chatNote.textContent = e.message;
    return;
  }
  if (!data.available) {
    ui.chatMeta.hidden = true;
    ui.chatList.replaceChildren(el('div', { class: 'empty-hint', text: data.reason || 'no transcript' }));
    state.chat.files = [];
    renderCtxFiles();
    return;
  }
  state.chat.files = data.files || [];
  renderCtxFiles();
  const stamp = `${data.mtime}-${data.size}`;
  if (stamp === state.chat.stamp) return;
  state.chat.stamp = stamp;
  renderChatMeta(data);
  renderChat(data.messages || []);
}

/** Slim header: what this session is, on which model, how full the window. */
function renderChatMeta(data) {
  const s = data.session || {};
  const bits = [];
  if (s.title) bits.push(el('span', { class: 'chat-meta-title', text: s.title }));
  if (s.model) {
    bits.push(el('span', { class: 'chat-meta-fact', text: s.model.replace(/-\d{8}$/, '') }));
  }
  if (s.context) {
    const used = `${Math.round(s.context / 1000)}k`;
    bits.push(el('span', {
      class: 'chat-meta-fact', title: `${s.context.toLocaleString()} tokens in the context window`,
      text: s.window ? `${used} / ${Math.round(s.window / 1000)}k ctx` : `${used} ctx`,
    }));
  }
  if (data.guessed) {
    bits.push(el('span', {
      class: 'chat-meta-guess', text: 'guessed',
      title: 'herdr reported no exact session; this is the newest transcript for the pane\'s directory',
    }));
  }
  ui.chatMeta.title = data.path || '';
  ui.chatMeta.replaceChildren(...bits);
  ui.chatMeta.hidden = !bits.length;
}

/** Viewer URL for a path the transcript mentions, or null when it can't be
    pinned to a real file. Absolute (and home-relative) paths go straight to
    the viewer; bare names like `src/app.py` only link when they match the
    pane's mentioned files — those are stat-verified server-side, so prose
    mentions never become dead links. */
function chatFileHref(path) {
  if (!path || !state.sel.host) return null;
  let p = String(path).trim();
  if (p.startsWith('~/')) p = p.slice(2); // backends resolve home-relative
  if (!p.startsWith('/')) {
    const hit = (state.chat.files || []).find((f) => f === p || f.endsWith(`/${p}`));
    if (!hit) return null;
    p = hit;
  }
  return `/view?${qs({ host: state.sel.host, path: p })}`;
}

/** Re-render keyed by message content: an unchanged message keeps its node,
    so its markdown isn't re-parsed and its open/closed details survive the
    4s poll. The turn still being written rebuilds — aligned from the end so
    its expanded cards stay expanded while it streams. */
function renderChat(messages) {
  const atBottom = state.chat.pinned;
  const prev = state.chat.nodes || new Map();
  const prevList = state.chat.nodeList || [];
  const next = new Map();
  const nodes = messages.map((m, idx) => {
    const base = JSON.stringify(m);
    let key = base;
    for (let n = 2; next.has(key); n += 1) key = `${base}#${n}`;
    let node = prev.get(key);
    if (!node) {
      node = renderChatMessage(m, { fileHref: chatFileHref });
      const old = prevList[prevList.length - (messages.length - idx)];
      if (old) copyOpenState(old, node);
    }
    next.set(key, node);
    return node;
  });
  state.chat.nodes = next;
  state.chat.nodeList = nodes;
  ui.chatList.replaceChildren(...nodes);
  if (atBottom) ui.chatScroll.scrollTop = ui.chatScroll.scrollHeight;
}

/** Mirror expanded/collapsed cards from a message's previous render; blocks
    added since keep their defaults. */
function copyOpenState(oldNode, newNode) {
  const from = oldNode.querySelectorAll('details');
  const to = newNode.querySelectorAll('details');
  from.forEach((d, i) => { if (to[i]) to[i].open = d.open; });
}

ui.chatScroll.addEventListener('scroll', () => {
  const s = ui.chatScroll;
  state.chat.pinned = s.scrollHeight - s.scrollTop - s.clientHeight < 60;
});

ui.chatForm.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const text = ui.chatText.value;
  const pane = currentPane();
  if (!text.trim() || !pane) return;
  try {
    await api('/api/term/input', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        host: state.sel.host, pane: pane.pane_id,
        ops: [{ text }, { key: 'Enter' }],
      }),
    });
    ui.chatText.value = '';
    state.chat.pinned = true;
    state.chat.sent = { pane: pane.key, at: Date.now() };
    renderChatStatus();
    ui.chatNote.textContent = 'sent — the agent sees it as typed input';
    setTimeout(loadChat, 1500);
  } catch (e) {
    ui.chatNote.textContent = `send failed: ${e.message}`;
  }
});

ui.chatText.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' && !ev.shiftKey) {
    ev.preventDefault();
    ui.chatForm.requestSubmit();
  }
});

/* ---------------------------------------------------------------- files */

async function loadFiles(path) {
  ui.workListing.replaceChildren(el('li', { class: 'empty', text: 'loading…' }));
  let data;
  try {
    data = await api(`/api/fs/ls?${qs({ host: state.sel.host, path })}`);
  } catch (e) {
    ui.workListing.replaceChildren(el('li', { class: 'empty', text: e.message }));
    return;
  }
  state.filesPath = data.path;
  renderFileCrumbs(data.path);
  renderListing(data);
}

function renderFileCrumbs(path) {
  const parts = path.split('/').filter(Boolean);
  const nodes = [el('a', {
    href: '#', text: '/',
    onclick: (ev) => { ev.preventDefault(); loadFiles('/'); },
  })];
  let acc = '';
  parts.forEach((part, i) => {
    acc += `/${part}`;
    const target = acc;
    if (i === parts.length - 1) nodes.push(el('span', { class: 'here', text: part }));
    else {
      nodes.push(el('a', {
        href: '#', text: part,
        onclick: (ev) => { ev.preventDefault(); loadFiles(target); },
      }));
      nodes.push(el('span', { class: 'sep', text: '/' }));
    }
  });
  ui.workCrumbs.replaceChildren(...nodes);
}

function renderListing(data) {
  const rows = [];
  if (data.path !== '/') {
    rows.push(el('li', {}, [
      el('span', { class: 'ficon', 'aria-hidden': 'true', text: '📁' }),
      el('a', {
        class: 'fname dir', href: '#', text: '..',
        onclick: (ev) => {
          ev.preventDefault();
          loadFiles(data.path.replace(/\/[^/]+$/, '') || '/');
        },
      }),
    ]));
  }
  let entries = data.entries;
  if (!ui.dotfiles.checked) entries = entries.filter((e) => !e.name.startsWith('.'));
  if (!entries.length) {
    rows.push(el('li', { class: 'empty', text: 'Empty directory.' }));
  }
  for (const e of entries) {
    const full = (data.path === '/' ? '' : data.path) + '/' + e.name;
    const li = el('li', { class: e.name.startsWith('.') ? 'dotfile' : null });
    li.append(el('span', { class: 'ficon', 'aria-hidden': 'true', text: fileIcon(e) }));
    if (e.dir) {
      li.append(el('a', {
        class: 'fname dir', href: '#', text: `${e.name}/`,
        onclick: (ev) => { ev.preventDefault(); loadFiles(full); },
      }));
    } else {
      const viewable = LIVE_RE.test(e.name) || !e.name.includes('.');
      li.append(el('a', {
        class: 'fname',
        href: viewable
          ? `/view?${qs({ host: state.sel.host, path: full })}`
          : `/api/fs/file?${qs({ host: state.sel.host, path: full })}`,
        target: '_blank',
        text: e.name,
      }));
    }
    li.append(el('span', { class: 'leader', 'aria-hidden': 'true' }));
    if (!e.dir) {
      li.append(el('span', { class: 'facts' }, [
        el('a', {
          href: `/api/fs/file?${qs({ host: state.sel.host, path: full, dl: 1 })}`,
          title: 'download', text: '↓',
        }),
      ]));
    }
    li.append(el('span', { class: 'fsize', text: e.dir ? '' : bytes(e.size) }));
    li.append(el('span', { class: 'fdate', text: fmtDate(e.mtime) }));
    rows.push(li);
  }
  ui.workListing.replaceChildren(...rows);
}

ui.dotfiles.checked = localStorage.getItem('herdr-hq.dotfiles') === '1';
ui.dotfiles.addEventListener('change', () => {
  localStorage.setItem('herdr-hq.dotfiles', ui.dotfiles.checked ? '1' : '0');
  if (state.filesPath) loadFiles(state.filesPath);
});

/* --------------------------------------------------------- context panel */

function renderCtxFiles() {
  const files = state.chat.files;
  const cur = currentPane();
  ui.ctxFiles.hidden = !files.length || !cur?.is_agent;
  if (ui.ctxFiles.hidden) return;
  const top = state.sel.top;
  ui.ctxFileList.replaceChildren(...files.slice(0, 30).map((path) => {
    const display = top && path.startsWith(`${top}/`) ? path.slice(top.length + 1) : shortPath(path, 3);
    return el('li', {}, [el('a', {
      href: `/view?${qs({ host: state.sel.host, path })}`,
      target: '_blank', text: display, title: path,
    })]);
  }));
}

function portChipLite(sock, hostMeta) {
  const info = `${sock.process} · ${sock.addr}:${sock.port} · pid ${sock.pid}`;
  const direct = hostMeta?.transport === 'local'
    ? `http://127.0.0.1:${sock.port}/`
    : (sock.scope !== 'loopback'
      ? `http://${String(hostMeta?.target || hostMeta?.name).split('@').pop()}:${sock.port}/` : null);
  if (direct) {
    return el('a', {
      class: 'port-chip is-open', title: info, text: `:${sock.port}`,
      href: direct, target: '_blank', rel: 'noopener',
    });
  }
  const key = `${hostMeta?.name}|${sock.port}`;
  const proxied = state.proxied.get(key);
  if (proxied) {
    return el('a', {
      class: 'port-chip is-fwd', title: `${info} — proxied through herdr HQ`,
      href: proxied, target: '_blank', rel: 'noopener', text: `:${sock.port} ⇢`,
    });
  }
  return el('button', {
    class: 'port-chip is-proxy', type: 'button',
    title: `${info} — click to open a live preview through the herdr HQ proxy`,
    text: `:${sock.port} ▸`,
    onclick: async (ev) => {
      const btn = ev.currentTarget;
      btn.setAttribute('aria-busy', 'true');
      try {
        const data = await api('/api/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ host: hostMeta.name, port: sock.port }),
        });
        state.proxied.set(key, data.url);
        window.open(data.url, '_blank');
        loadCtx();
      } catch (e) {
        btn.removeAttribute('aria-busy');
        toast(`preview :${sock.port}: ${e.message}`);
      }
    },
  });
}

function collectPorts(panes) {
  const seen = new Set();
  const socks = [];
  for (const p of panes) {
    for (const sock of p.ports || []) {
      const k = `${sock.addr}:${sock.port}`;
      if (!seen.has(k)) { seen.add(k); socks.push({ sock, hostMeta: p.hostMeta }); }
    }
  }
  return socks.sort((a, b) => a.sock.port - b.sock.port);
}

function renderCtxPorts() {
  // the checkout's own ports first; when it has none, everything the host's
  // herdr panes are listening on still shows (labelled), so the panel is
  // reliably where you look for "what can I open"
  let socks = collectPorts(currentPanes());
  let fallback = false;
  if (!socks.length && state.sel.host) {
    const h = state.fleet?.hosts?.find((x) => x.name === state.sel.host);
    const all = (h?.data?.panes || []).map((p) => ({ ...p, hostMeta: h }));
    socks = collectPorts(all);
    fallback = true;
  }
  ui.ctxPorts.hidden = !socks.length;
  if (ui.ctxPorts.hidden) return;
  const rows = socks.map(({ sock, hostMeta }) => el('div', { class: 'ctx-port-row' }, [
    portChipLite(sock, hostMeta),
    el('span', { class: 'ctx-port-proc', text: sock.process || '', title: `pid ${sock.pid}` }),
  ]));
  if (fallback) {
    rows.unshift(el('div', {
      class: 'ctx-hint',
      text: `none in this checkout — everything on ${state.sel.host}:`,
    }));
  }
  ui.ctxPortList.replaceChildren(...rows);
}

async function renderCtxViewsTunnels() {
  try {
    const views = await api('/api/views');
    ui.ctxViews.hidden = !views.length;
    ui.ctxViewList.replaceChildren(...views.map((v) => el('li', {}, [
      el('a', {
        href: `/view?${qs({ host: v.host, path: v.path })}`,
        target: '_blank', text: v.path.split('/').pop(), title: `${v.host}:${v.path}`,
      }),
      el('button', {
        class: 'btn btn-mini', type: 'button', text: '✕', title: 'close this live view',
        onclick: async () => {
          await api(`/api/views/${v.id}`, { method: 'DELETE' }).catch(() => {});
          setTimeout(renderCtxViewsTunnels, 500);
        },
      }),
    ])));
  } catch (_) { ui.ctxViews.hidden = true; }
  try {
    const fws = await api('/api/forwards');
    ui.ctxTunnels.hidden = !fws.length;
    ui.ctxTunnelList.replaceChildren(...fws.map((f) => el('li', {}, [
      el('a', {
        href: f.local_port ? `http://127.0.0.1:${f.local_port}` : '#',
        target: '_blank',
        text: `${f.host}:${f.remote_port} ⇢ :${f.local_port ?? '—'}`,
        title: f.up ? 'up' : 'connection down',
      }),
      f.declared ? el('span', { class: 'pin', text: '⚲' }) : el('button', {
        class: 'btn btn-mini', type: 'button', text: '✕', title: 'remove tunnel',
        onclick: async () => {
          await api(`/api/forwards/${encodeURIComponent(f.id)}`, { method: 'DELETE' }).catch(() => {});
          renderCtxViewsTunnels();
        },
      }),
    ])));
  } catch (_) { ui.ctxTunnels.hidden = true; }
}

function loadCtx() {
  renderCtxPorts();
  renderCtxFiles();
  const pane = currentPane();
  if (pane?.is_agent && state.sel.tab !== 'chat') loadChat();  // feeds mentioned files
}

/* --------------------------------------------------------- state + push */

async function fetchState() {
  try {
    state.fleet = await api('/api/state');
    setBrandVersion(state.fleet.version);
    ui.liveState.dataset.state = 'ok';
    ui.liveLabel.textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    ui.liveState.dataset.state = 'error';
    ui.liveLabel.textContent = `server unreachable (${e.message})`;
    return;
  }
  state.model = buildModel(state.fleet);
  state.allowInput = state.fleet.terminal?.input !== false;
  if (!state.sel.host) {
    const fromUrl = readUrl();
    if (fromUrl.host) {
      select(fromUrl.host, fromUrl.top || '', fromUrl.pane, fromUrl.tab, { push: false });
      return;
    }
    const first = [...state.model.projects.values()][0]?.[0];
    if (first) { select(first.host, first.top, null, 'term', { push: false }); return; }
    const firstHost = [...state.model.machines.values()].find((m) => m.panes.length);
    if (firstHost) { select(firstHost.host, '', null, 'term', { push: false }); return; }
  }
  if (revalidateSelection()) return;  // select() already re-rendered
  // selection kept: refresh what depends on the model
  renderSidebar();
  renderHeader();
  renderChatStatus();
  renderCtxPorts();
}

function scheduleFetch() {
  if (state.fetchTimer) return;
  state.fetchTimer = setTimeout(() => { state.fetchTimer = null; fetchState(); }, 500);
}

function connectPush() {
  const es = new EventSource('/api/state/stream');
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'pane') {
      const h = state.fleet?.hosts?.find((x) => x.name === msg.host);
      const pane = h?.data?.panes?.find((p) => p.pane_id === msg.pane_id);
      if (pane) {
        for (const k of ['agent_status', 'title', 'agent', 'focused']) {
          if (msg[k] !== undefined && msg[k] !== null) pane[k] = msg[k];
        }
        if (state.renderTimer) return;
        state.renderTimer = setTimeout(() => {
          state.renderTimer = null;
          state.model = buildModel(state.fleet);
          renderSidebar();
          renderHeader();
          renderChatStatus();
        }, 300);
      }
    } else if (msg.type === 'host') {
      scheduleFetch();
    }
  };
}

/* ------------------------------------------------------------------ url */

function readUrl() {
  const p = new URLSearchParams(location.search);
  return {
    host: p.get('host'),
    top: p.get('top'),
    pane: p.get('pane'),
    tab: ['term', 'chat', 'files'].includes(p.get('tab')) ? p.get('tab') : null,
  };
}

function writeUrl() {
  const p = new URLSearchParams();
  if (state.sel.host) p.set('host', state.sel.host);
  if (state.sel.top) p.set('top', state.sel.top);
  if (state.sel.pane) p.set('pane', state.sel.pane);
  if (state.sel.tab !== 'term') p.set('tab', state.sel.tab);
  history.replaceState(null, '', p.toString() ? `/work?${p}` : '/work');
  const co = state.sel.top ? state.model?.checkouts.get(`${state.sel.host}|${state.sel.top}`) : null;
  document.title = co ? `${co.repo} · ${co.git.branch || ''} — herdr HQ` : 'Workspace — herdr HQ';
}

/* --------------------------------------------------------------- wiring */

ui.workSmaller.addEventListener('click', () => mirror.setFont(mirror.fontSize() - 1));
ui.workBigger.addEventListener('click', () => mirror.setFont(mirror.fontSize() + 1));
ui.workInput.addEventListener('change', () => {
  if (ui.workInput.checked) mirror.focus();
  else mirror.blur();
});
addEventListener('resize', () => { if (!ui.termPanel.hidden) mirror.fit(); });
// leaving the page: release the pane now so herdr resizes it back promptly
addEventListener('pagehide', () => mirror.close());
window.addEventListener('popstate', () => {
  const u = readUrl();
  if (u.host) select(u.host, u.top || '', u.pane, u.tab, { push: false });
});

initTheme();
bindThemeToggle(ui.themeToggle);
fetchState();
connectPush();
setInterval(fetchState, 15000);
setInterval(renderCtxViewsTunnels, 15000);
renderCtxViewsTunnels();
