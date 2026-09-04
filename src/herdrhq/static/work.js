/* herdr HQ — workspace view.
 *
 * Left: projects expanding into checkouts (worktree @ machine) and machines
 * with their loose panes. Center: the selected checkout's panes as tabs, each
 * a live terminal mirror; agent panes toggle to a chat rendering of their
 * session (markdown + math + highlighted code) with a composer that types
 * into the live pane. Right: files the agent touched, listening ports,
 * live views and tunnels.
 */

const ui = {
  liveState: document.getElementById('liveState'),
  liveLabel: document.getElementById('liveLabel'),
  themeToggle: document.getElementById('themeToggle'),
  projectTree: document.getElementById('projectTree'),
  machineTree: document.getElementById('machineTree'),
  workCrumb: document.getElementById('workCrumb'),
  paneTabs: document.getElementById('paneTabs'),
  termPanel: document.getElementById('termPanel'),
  termStatus: document.getElementById('termStatus'),
  workInput: document.getElementById('workInput'),
  workSmaller: document.getElementById('workSmaller'),
  workBigger: document.getElementById('workBigger'),
  workMount: document.getElementById('workMount'),
  workNote: document.getElementById('workNote'),
  chatPanel: document.getElementById('chatPanel'),
  chatScroll: document.getElementById('chatScroll'),
  chatList: document.getElementById('chatList'),
  chatForm: document.getElementById('chatForm'),
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
  expanded: new Set(),    // 'proj:<repo>' / 'host:<name>'
  allowInput: true,
  filesPath: null,
  chat: { stamp: null, pinned: true, timer: null, files: [] },
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
  const checkouts = new Map();
  for (const row of collectAgents(fleet)) {
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

  const machines = new Map();
  for (const host of fleet.hosts) {
    machines.set(host.name, {
      host: host.name, hostMeta: host, panes: [], down: host.status !== 'ok',
    });
    for (const pane of host.data?.panes || []) {
      const enriched = {
        ...pane,
        host: host.name,
        hostMeta: host,
        key: `${host.name}/${pane.pane_id}`,
        status: pane.agent_status || 'unknown',
      };
      const owners = (byHost[host.name] || []).filter(
        (co) => pane.cwd && (pane.cwd === co.top || pane.cwd.startsWith(`${co.top}/`)),
      ).sort((a, b) => b.top.length - a.top.length);
      if (owners.length) owners[0].panes.push(enriched);
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

function statusDots(panes) {
  const counts = {};
  for (const p of panes) if (p.is_agent) counts[p.status] = (counts[p.status] || 0) + 1;
  return el('span', { class: 'tree-dots' }, STATUS_ORDER.filter((s) => counts[s]).map((s) =>
    el('span', { class: 'tree-dot-group', title: `${counts[s]} ${STATUS_LABEL[s]}` }, [
      el('span', { class: `dot${s === 'working' ? ' is-live' : ''}`, 'data-status': s }),
      counts[s] > 1 ? el('span', { class: 'tree-dot-n', text: String(counts[s]) }) : null,
    ])));
}

function caretRow({ id, label, extra, dots, children }) {
  const open = state.expanded.has(id);
  const row = el('button', {
    class: 'tree-row tree-parent', type: 'button', 'aria-expanded': String(open),
    onclick: () => {
      if (state.expanded.has(id)) state.expanded.delete(id);
      else state.expanded.add(id);
      renderSidebar();
    },
  }, [
    el('span', { class: 'tree-caret', 'aria-hidden': 'true', text: open ? '▾' : '▸' }),
    el('span', { class: 'tree-label', text: label }),
    extra ? el('span', { class: 'tree-extra', text: extra }) : null,
    dots,
  ]);
  const box = el('div', { class: 'tree-group' }, [row]);
  if (open) box.append(...children);
  return box;
}

function checkoutRow(co) {
  const selected = state.sel.host === co.host && state.sel.top === co.top;
  const label = [co.git.branch || 'detached', co.git.worktree ? '⌥' : null]
    .filter(Boolean).join(' ');
  return el('button', {
    class: `tree-row tree-leaf${selected ? ' is-selected' : ''}`,
    type: 'button',
    title: `${co.top} on ${co.host}`,
    onclick: () => select(co.host, co.top, null, null),
  }, [
    el('span', { class: 'tree-label', text: label }),
    el('span', { class: 'tree-host', text: co.host }),
    statusDots(co.panes),
  ]);
}

function paneLeafRow(host, pane) {
  const selected = state.sel.host === host && !state.sel.top && state.sel.pane === pane.pane_id;
  return el('button', {
    class: `tree-row tree-leaf${selected ? ' is-selected' : ''}`,
    type: 'button',
    title: pane.title || pane.pane_id,
    onclick: () => select(host, '', pane.pane_id, null),
  }, [
    el('span', { class: `dot${pane.status === 'working' ? ' is-live' : ''}`, 'data-status': pane.is_agent ? pane.status : 'unknown' }),
    el('span', { class: 'tree-label', text: pane.is_agent ? (pane.title || pane.agent) : (pane.title || 'shell') }),
    el('span', { class: 'tree-host', text: pane.pane_id }),
  ]);
}

function renderSidebar() {
  if (!state.model) return;
  const projects = [...state.model.projects.entries()]
    .sort((a, b) => b[1].reduce((s, c) => s + c.panes.length, 0)
      - a[1].reduce((s, c) => s + c.panes.length, 0));
  ui.projectTree.replaceChildren(...projects.map(([repo, cos]) => caretRow({
    id: `proj:${repo}`,
    label: repo,
    extra: cos.length > 1 ? `${cos.length}` : null,
    dots: statusDots(cos.flatMap((c) => c.panes)),
    children: cos.map(checkoutRow),
  })));
  if (!projects.length) {
    ui.projectTree.replaceChildren(el('div', { class: 'empty-hint', text: 'No agents in any repository.' }));
  }

  const machines = [...state.model.machines.values()];
  ui.machineTree.replaceChildren(...machines.map((m) => caretRow({
    id: `host:${m.host}`,
    label: m.host,
    extra: m.down ? 'unreachable' : (m.panes.length ? `${m.panes.length}` : null),
    dots: statusDots(m.panes),
    children: m.panes.map((p) => paneLeafRow(m.host, p)),
  })));
}

/* ------------------------------------------------------ selection + tabs */

function select(host, top, pane, tab, { push = true } = {}) {
  const changedTarget = host !== state.sel.host || top !== state.sel.top;
  state.sel.host = host;
  state.sel.top = top;
  const panes = currentPanes();
  if (!pane || !panes.some((p) => p.pane_id === pane)) {
    const firstAgent = panes.find((p) => p.is_agent);
    pane = (firstAgent || panes[0])?.pane_id || null;
  }
  state.sel.pane = pane;
  const cur = currentPane();
  if (!tab) tab = state.sel.tab;
  if (tab === 'chat' && !(cur?.is_agent)) tab = 'term';
  state.sel.tab = tab || 'term';
  if (changedTarget) {
    state.filesPath = null;
    if (top) state.expanded.add(`proj:${state.model?.checkouts.get(`${host}|${top}`)?.repo}`);
    else state.expanded.add(`host:${host}`);
  }
  if (push) writeUrl();
  renderSidebar();
  renderTabs();
  showPanel();
  loadCtx();
}

function renderTabs() {
  const panes = currentPanes();
  const cur = currentPane();
  const co = state.sel.top
    ? state.model?.checkouts.get(`${state.sel.host}|${state.sel.top}`) : null;

  ui.workCrumb.replaceChildren(...(state.sel.host ? [
    el('strong', { text: co ? co.repo : state.sel.host }),
    co ? el('span', { class: 'crumb-sub', text: ` ${co.git.branch || 'detached'}${co.git.worktree ? ' · worktree' : ''} @ ${co.host}` }) : null,
    co?.git.dirty ? el('span', { class: 'git-dirty crumb-sub', text: ' · dirty' }) : null,
    el('span', { class: 'crumb-path', text: filesRoot() || '' }),
  ] : [el('span', { class: 'empty-hint', text: 'Pick a project or machine on the left.' })]).filter(Boolean));

  const tabs = panes.map((p) => el('button', {
    class: `work-tab${p.pane_id === state.sel.pane && state.sel.tab !== 'files' ? ' is-active' : ''}`,
    type: 'button',
    role: 'tab',
    title: `${p.title || ''} (${p.pane_id})`,
    onclick: () => select(state.sel.host, state.sel.top, p.pane_id,
      state.sel.tab === 'files' ? 'term' : state.sel.tab),
  }, [
    el('span', { class: `dot${p.status === 'working' ? ' is-live' : ''}`, 'data-status': p.is_agent ? p.status : 'unknown' }),
    el('span', { text: p.is_agent ? (p.agent || 'agent') : 'shell' }),
    el('span', { class: 'work-tab-id', text: p.pane_id }),
  ]));
  if (state.sel.host) {
    tabs.push(el('button', {
      class: `work-tab work-tab-files${state.sel.tab === 'files' ? ' is-active' : ''}`,
      type: 'button', role: 'tab',
      onclick: () => select(state.sel.host, state.sel.top, state.sel.pane, 'files'),
    }, [el('span', { 'aria-hidden': 'true', text: '🗀' }), el('span', { text: 'Files' })]));
  }
  // the Terminal ⇄ Chat toggle rides at the right edge of the strip
  if (cur?.is_agent && state.sel.tab !== 'files') {
    tabs.push(el('span', { class: 'work-mode viewtoggle' }, [
      el('button', {
        class: `btn btn-seg${state.sel.tab === 'term' ? ' is-on' : ''}`, type: 'button',
        onclick: () => select(state.sel.host, state.sel.top, state.sel.pane, 'term'),
        text: 'Terminal',
      }),
      el('button', {
        class: `btn btn-seg${state.sel.tab === 'chat' ? ' is-on' : ''}`, type: 'button',
        onclick: () => select(state.sel.host, state.sel.top, state.sel.pane, 'chat'),
        text: 'Chat',
      }),
    ]));
  }
  ui.paneTabs.replaceChildren(...tabs);
}

function showPanel() {
  const tab = state.sel.host ? state.sel.tab : null;
  ui.termPanel.hidden = tab !== 'term';
  ui.chatPanel.hidden = tab !== 'chat';
  ui.filesPanel.hidden = tab !== 'files';

  clearInterval(state.chat.timer);
  state.chat.timer = null;

  if (tab === 'term' && state.sel.pane) {
    const target = mirror.target();
    if (!target || target.host !== state.sel.host || target.pane !== state.sel.pane) {
      mirror.connect({ host: state.sel.host, pane: state.sel.pane });
    }
    mirror.fit();
  } else {
    mirror.close();  // one terminal stream at a time; sessions survive on grace
  }

  if (tab === 'chat') {
    state.chat.stamp = null;
    ui.chatList.replaceChildren(el('div', { class: 'empty-hint', text: 'loading conversation…' }));
    loadChat();
    state.chat.timer = setInterval(() => {
      if (!document.hidden) loadChat();
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
  renderChat(data.messages || []);
}

function renderChat(messages) {
  const atBottom = state.chat.pinned;
  const nodes = messages.map((m) => {
    const box = el('div', { class: `chat-msg${m.role === 'user' ? ' is-user' : ''}` });
    if (m.role === 'user') {
      box.append(el('div', { class: 'chat-role', text: 'you' }));
    }
    if (m.tools?.length) {
      box.append(el('div', { class: 'chat-tools' }, m.tools.slice(0, 12).map((t) =>
        el('div', { class: 'chat-tool', title: t.detail || '' }, [
          el('span', { class: 'transcript-tool', text: t.name }),
          t.detail ? el('span', { class: 'chat-tool-detail', text: t.detail }) : null,
        ]))));
      if (m.tools.length > 12) {
        box.append(el('div', { class: 'chat-tool chat-tool-more', text: `… ${m.tools.length - 12} more tool calls` }));
      }
    }
    if (m.text) box.append(renderMarkdown(m.text));
    return box;
  });
  ui.chatList.replaceChildren(...nodes);
  if (atBottom) ui.chatScroll.scrollTop = ui.chatScroll.scrollHeight;
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
  ui.ctxFileList.replaceChildren(...files.slice(0, 15).map((path) => {
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

function renderCtxPorts() {
  const panes = currentPanes();
  const seen = new Set();
  const socks = [];
  for (const p of panes) {
    for (const sock of p.ports || []) {
      const k = `${sock.addr}:${sock.port}`;
      if (!seen.has(k)) { seen.add(k); socks.push({ sock, hostMeta: p.hostMeta }); }
    }
  }
  ui.ctxPorts.hidden = !socks.length;
  ui.ctxPortList.replaceChildren(...socks.map(({ sock, hostMeta }) => portChipLite(sock, hostMeta)));
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
  // selection kept: refresh what depends on the model
  renderSidebar();
  renderTabs();
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
          renderTabs();
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
