/* herdr HQ — browse view: files, live views, tunnels per host. */

const ui = {
  themeToggle: document.getElementById('themeToggle'),
  hostChips: document.getElementById('hostChips'),
  hostForm: document.getElementById('hostForm'),
  dotfiles: document.getElementById('dotfiles'),
  crumbs: document.getElementById('crumbs'),
  listing: document.getElementById('listing'),
  listingStatus: document.getElementById('listingStatus'),
  agentDirs: document.getElementById('agentDirs'),
  agentDirList: document.getElementById('agentDirList'),
  svcPanel: document.getElementById('svcPanel'),
  svcList: document.getElementById('svcList'),
  viewPanel: document.getElementById('viewPanel'),
  viewList: document.getElementById('viewList'),
  histPanel: document.getElementById('histPanel'),
  histList: document.getElementById('histList'),
  histClear: document.getElementById('histClear'),
  fwList: document.getElementById('fwList'),
  fwForm: document.getElementById('fwForm'),
};

const state = { host: null, path: null, lastListing: null, fleet: null };

const HIST_KEY = 'herdr-hq.viewhist';
const DOTFILES_KEY = 'herdr-hq.dotfiles';

// Files with a live auto-refreshing viewer (/view): documents, images, text.
const LIVE_RE = /\.(pdf|png|jpe?g|gif|webp|svg|bmp|avif|ico|md|markdown|txt|log|out|err|json|ya?ml|toml|csv|tsv|py|r|jl|js|ts|sh|zsh|bash|tex|bib|sty|cls|rst|org|nix|ini|cfg|conf|sql|lock|service)$/i;
const ICONS = [
  [/\.(png|jpe?g|gif|webp|svg|bmp|avif|ico|tiff?|eps)$/i, '🖼'],
  [/\.pdf$/i, '📕'],
  [/\.(mp4|mov|mkv|avi|webm)$/i, '🎬'],
  [/\.(mp3|wav|flac|ogg|m4a)$/i, '🎵'],
  [/\.(zip|tar|gz|tgz|bz2|xz|7z|rar|zst|dmg)$/i, '📦'],
  [/\.(csv|tsv|parquet|feather|dta|rds|xls[xm]?)$/i, '📊'],
  [/\.(ya?ml|toml|ini|cfg|conf|json|lock)$/i, '⚙️'],
  [/\.(py|r|jl|js|ts|tsx|jsx|c|h|cpp|hpp|rs|go|java|sh|zsh|bash|sql|html|css|do|m|f90)$/i, '⌨️'],
  [/\.(md|txt|rst|org|log|tex|bib)$/i, '📝'],
];

function icon(e) {
  if (e.dir) return '📁';
  for (const [re, ic] of ICONS) if (re.test(e.name)) return ic;
  return '📄';
}

/* ---------------------------------------------------------------- urls */

function readUrl() {
  const p = new URLSearchParams(location.search);
  let host = p.get('host');
  let path = p.get('path');
  // old sshpeek bookmarks used #h=...&p=...
  if (!host && location.hash.includes('h=')) {
    const h = new URLSearchParams(location.hash.slice(1));
    host = h.get('h');
    path = h.get('p');
    history.replaceState(null, '', `/browse?${qs({ host: host || '', path: path || '' })}`);
  }
  return { host, path };
}

function writeUrl(push = false) {
  const target = `/browse?${qs({ host: state.host || '', path: state.path || '' })}`;
  if (push) history.pushState({ host: state.host, path: state.path }, '', target);
  else history.replaceState({ host: state.host, path: state.path }, '', target);
  document.title = state.host
    ? `${state.host}:${shortPath(state.path, 2)} — herdr HQ`
    : 'Browse — herdr HQ';
}

/* --------------------------------------------------------------- hosts */

async function loadHosts() {
  try {
    const hosts = await api('/api/hosts');
    ui.hostChips.replaceChildren(...hosts.map((h) => el('button', {
      class: `chip${h.name === state.host ? ' is-on' : ''}`,
      type: 'button',
      'aria-pressed': String(h.name === state.host),
      title: h.connected ? 'connected' : 'not connected yet',
      onclick: () => browse(h.name, null, true),
    }, [
      el('span', { class: 'dot', 'data-status': h.connected ? 'done' : 'unknown' }),
      el('span', { text: h.name }),
      h.agents ? el('span', { class: 'kindtag', text: `${h.agents} agent${h.agents === 1 ? '' : 's'}` }) : null,
      h.kind === 'local' ? el('span', { class: 'kindtag', text: 'local' }) : null,
    ])));
  } catch (e) { toast(e.message); }
}

/* Agent workdirs from the fleet state: the jump-to-checkout shortcuts. */
async function loadAgentDirs() {
  try {
    state.fleet = await api('/api/state');
  } catch (e) {
    ui.agentDirs.hidden = true;
    return;
  }
  const rows = [];
  for (const host of state.fleet.hosts) {
    if (state.host && host.name !== state.host) continue;
    const seen = new Set();
    for (const pane of host.data?.panes || []) {
      if (!pane.is_agent) continue;
      const dir = pane.git?.toplevel || pane.cwd;
      if (!dir || seen.has(dir)) continue;
      seen.add(dir);
      rows.push({ host: host.name, dir, pane });
    }
  }
  if (!rows.length) { ui.agentDirs.hidden = true; return; }
  ui.agentDirs.hidden = false;
  ui.agentDirList.replaceChildren(...rows.map(({ host, dir, pane }) => el('li', { class: 'fw' }, [
    statusPill(pane.agent_status || 'unknown'),
    el('span', { class: 'route' }, [
      el('a', {
        href: `/browse?${qs({ host, path: dir })}`,
        text: pane.git ? `${pane.git.repo_name} · ${pane.git.branch || 'detached'}` : shortPath(dir, 2),
        title: dir,
        onclick: (ev) => { ev.preventDefault(); browse(host, dir, true); },
      }),
      el('span', { class: 'target', text: `${host}:${shortPath(dir, 3)}` }),
    ]),
    el('a', {
      class: 'term-open',
      href: `/?${qs({ termhost: host, termpane: pane.pane_id })}`,
      title: `Open a live terminal on ${host} ${pane.pane_id}`,
    }, [el('span', { 'aria-hidden': 'true', text: '⌨' }), el('span', { class: 'sr-only', text: 'terminal' })]),
  ])));
}

/* --------------------------------------------------------------- files */

async function browse(host, path, push = false) {
  ui.listing.replaceChildren(el('li', { class: 'empty', text: `Connecting to ${host}…` }));
  try {
    const data = await api(`/api/fs/ls?${qs({ host, path: path || '' })}`);
    state.host = data.host;
    state.path = data.path;
    writeUrl(push);
    renderCrumbs(data.path);
    state.lastListing = data;
    renderListing(data);
    const fhost = ui.fwForm.elements.fhost;
    if (!fhost.value) fhost.value = host;
    loadHosts();
    loadAgentDirs();
  } catch (e) {
    ui.listing.replaceChildren(el('li', { class: 'empty', text: e.message }));
    toast(`${host}: ${e.message}`);
  }
}

function renderCrumbs(path) {
  const parts = path.split('/').filter(Boolean);
  const nodes = [el('a', {
    href: `/browse?${qs({ host: state.host, path: '/' })}`,
    text: '/',
    onclick: (ev) => { ev.preventDefault(); browse(state.host, '/', true); },
  })];
  let acc = '';
  parts.forEach((part, i) => {
    acc += `/${part}`;
    const target = acc;
    if (i === parts.length - 1) {
      nodes.push(el('span', { class: 'here', text: part }));
    } else {
      nodes.push(el('a', {
        href: `/browse?${qs({ host: state.host, path: target })}`,
        text: part,
        onclick: (ev) => { ev.preventDefault(); browse(state.host, target, true); },
      }));
      nodes.push(el('span', { class: 'sep', text: '/' }));
    }
  });
  ui.crumbs.replaceChildren(...nodes);
}

function fileRow(e, dirPath) {
  const full = (dirPath === '/' ? '' : dirPath) + '/' + e.name;
  const li = el('li', { class: e.name.startsWith('.') ? 'dotfile' : null });
  li.append(el('span', { class: 'ficon', 'aria-hidden': 'true', text: icon(e) }));
  if (e.dir) {
    li.append(el('a', {
      class: 'fname dir',
      href: `/browse?${qs({ host: state.host, path: full })}`,
      text: `${e.name}/`,
      onclick: (ev) => { ev.preventDefault(); browse(state.host, full, true); },
    }));
  } else if (LIVE_RE.test(e.name)) {
    li.append(el('a', {
      class: 'fname',
      href: `/view?${qs({ host: state.host, path: full })}`,
      target: '_blank',
      text: e.name,
    }));
  } else {
    li.append(el('a', {
      class: 'fname',
      href: `/api/fs/file?${qs({ host: state.host, path: full })}`,
      target: '_blank',
      text: e.name,
    }));
  }
  li.append(el('span', { class: 'leader', 'aria-hidden': 'true' }));
  if (!e.dir) {
    const acts = el('span', { class: 'facts' });
    if (LIVE_RE.test(e.name)) {
      acts.append(el('a', {
        href: `/view?${qs({ host: state.host, path: full })}`,
        target: '_blank', title: 'live view (auto-refresh)', text: 'live',
      }));
    }
    acts.append(el('a', {
      href: `/api/fs/file?${qs({ host: state.host, path: full, dl: 1 })}`,
      title: 'download', text: '↓',
    }));
    li.append(acts);
  }
  li.append(el('span', { class: 'fsize', text: e.dir ? '' : bytes(e.size) }));
  li.append(el('span', { class: 'fdate', text: fmtDate(e.mtime) }));
  return li;
}

function renderListing(data) {
  const rows = [];
  if (data.path !== '/') {
    const up = el('li', {}, [
      el('span', { class: 'ficon', 'aria-hidden': 'true', text: '📁' }),
      el('a', {
        class: 'fname dir',
        href: '#',
        text: '..',
        onclick: (ev) => {
          ev.preventDefault();
          browse(state.host, data.path.replace(/\/[^/]+$/, '') || '/', true);
        },
      }),
    ]);
    rows.push(up);
  }
  let entries = data.entries;
  if (!ui.dotfiles.checked) entries = entries.filter((e) => !e.name.startsWith('.'));
  if (!entries.length) {
    const hidden = data.entries.length - entries.length;
    rows.push(el('li', {
      class: 'empty',
      text: hidden ? `Only dotfiles here (${hidden} hidden).` : 'Empty directory.',
    }));
  } else {
    for (const e of entries) rows.push(fileRow(e, data.path));
  }
  ui.listing.replaceChildren(...rows);
  ui.listingStatus.textContent = `${entries.length} entries in ${data.path}`;
}

/* ---------------------------------------- services, views, tunnels */

async function loadServices() {
  let svcs = [];
  try {
    svcs = await api('/api/services');
  } catch (_) {
    ui.svcPanel.hidden = true;  // endpoint may not exist yet
    return;
  }
  if (!svcs.length) { ui.svcPanel.hidden = true; return; }
  ui.svcPanel.hidden = false;
  ui.svcList.replaceChildren(...svcs.map((s) => el('li', { class: `fw${s.up ? '' : ' is-down'}` }, [
    el('span', { class: 'jack', 'aria-hidden': 'true' }),
    el('span', { class: 'jack-label', text: s.up ? 'up' : 'starting' }),
    el('span', { class: 'route' }, [
      el('a', { href: s.url, target: '_blank', text: `${s.name}.${s.host}` }),
      el('span', { class: 'target', text: `${s.host} → ${s.target}` }),
    ]),
  ])));
}

function readHist() {
  try { return JSON.parse(localStorage.getItem(HIST_KEY) || '[]'); }
  catch (_) { return []; }
}

function fmtAge(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`;
  return `${Math.round(s / 86400)}d`;
}

function renderHistory(open) {
  const openSet = new Set(open.map((v) => `${v.host}:${v.path}`));
  const hist = readHist().filter((h) => !openSet.has(`${h.host}:${h.path}`));
  if (!hist.length) { ui.histPanel.hidden = true; return; }
  ui.histPanel.hidden = false;
  ui.histList.replaceChildren(...hist.map((h) => el('li', { class: 'fw is-hist' }, [
    el('span', { class: 'route' }, [
      el('a', {
        href: `/view?${qs({ host: h.host, path: h.path })}`,
        target: '_blank', text: h.path.split('/').pop(), title: 'reopen live view',
      }),
      el('span', { class: 'target', text: `${h.host}:${h.path} · ${fmtAge(h.t / 1000)} ago` }),
    ]),
    el('button', {
      class: 'btn btn-mini', type: 'button', text: '✕', title: 'forget',
      onclick: () => {
        localStorage.setItem(HIST_KEY, JSON.stringify(
          readHist().filter((x) => !(x.host === h.host && x.path === h.path))));
        renderHistory(open);
      },
    }),
  ])));
}

async function loadViews() {
  let views = [];
  try {
    views = await api('/api/views');
  } catch (e) { toast(e.message); return; }
  renderHistory(views);
  if (!views.length) { ui.viewPanel.hidden = true; return; }
  ui.viewPanel.hidden = false;
  ui.viewList.replaceChildren(...views.map((v) => el('li', { class: 'fw' }, [
    el('span', { class: 'jack', 'aria-hidden': 'true' }),
    el('span', { class: 'jack-label', text: 'live' }),
    el('span', { class: 'route' }, [
      el('a', {
        href: `/view?${qs({ host: v.host, path: v.path })}`,
        target: '_blank', text: v.path.split('/').pop(),
      }),
      el('span', { class: 'target', text: `${v.host}:${v.path} · open ${fmtAge(v.started)}` }),
    ]),
    el('button', {
      class: 'btn btn-mini', type: 'button', text: '✕', title: 'close this live view',
      onclick: async () => {
        try {
          await api(`/api/views/${v.id}`, { method: 'DELETE' });
          setTimeout(loadViews, 500);   // the stream unwinds on its next tick
        } catch (e) { toast(e.message); }
      },
    }),
  ])));
}

async function loadForwards() {
  let fws = [];
  try {
    fws = await api('/api/forwards');
  } catch (_) {
    return;  // endpoint may not exist yet
  }
  if (!fws.length) {
    ui.fwList.replaceChildren(el('li', { class: 'empty', text: 'No tunnels yet.' }));
    return;
  }
  ui.fwList.replaceChildren(...fws.map((f) => {
    const target = f.remote_host === 'localhost'
      ? `:${f.remote_port}` : `${f.remote_host}:${f.remote_port}`;
    return el('li', { class: `fw${f.up ? '' : ' is-down'}` }, [
      el('span', { class: 'jack', 'aria-hidden': 'true' }),
      el('span', { class: 'jack-label', text: f.up ? 'up' : 'down' }),
      el('span', { class: 'route' }, [
        el('span', { text: `${f.host} ${target} ` }),
        el('span', { class: 'arrow', 'aria-hidden': 'true', text: '⇢ ' }),
        f.local_port
          ? el('a', { href: `http://127.0.0.1:${f.local_port}`, target: '_blank', text: `127.0.0.1:${f.local_port}` })
          : el('span', { text: '—' }),
      ]),
      f.declared
        ? el('span', { class: 'pin', title: 'declared in herdr-hq.yaml', text: '⚲ pinned' })
        : el('button', {
          class: 'btn btn-mini', type: 'button', text: '✕', title: 'remove tunnel',
          onclick: async () => {
            try {
              await api(`/api/forwards/${encodeURIComponent(f.id)}`, { method: 'DELETE' });
              loadForwards();
            } catch (e) { toast(e.message); }
          },
        }),
    ]);
  }));
}

/* -------------------------------------------------------------- wiring */

ui.hostForm.addEventListener('submit', (ev) => {
  ev.preventDefault();
  const h = ui.hostForm.elements.h.value.trim();
  if (h) browse(h, null, true);
});

ui.fwForm.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const host = ui.fwForm.elements.fhost.value.trim();
  const port = parseInt(ui.fwForm.elements.fport.value, 10);
  const local = parseInt(ui.fwForm.elements.flocal.value, 10) || 0;
  if (!host || !port) { toast('need a host and a port'); return; }
  try {
    await api('/api/forwards', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host, port, local }),
    });
    ui.fwForm.elements.fport.value = '';
    ui.fwForm.elements.flocal.value = '';
    loadForwards();
    loadHosts();
  } catch (e) { toast(e.message); }
});

ui.histClear.addEventListener('click', () => {
  localStorage.removeItem(HIST_KEY);
  renderHistory([]);
});

// Viewer tabs write the history; re-render when they do.
window.addEventListener('storage', (ev) => {
  if (ev.key === HIST_KEY) loadViews();
});

ui.dotfiles.checked = localStorage.getItem(DOTFILES_KEY) === '1';
ui.dotfiles.addEventListener('change', () => {
  localStorage.setItem(DOTFILES_KEY, ui.dotfiles.checked ? '1' : '0');
  if (state.lastListing) renderListing(state.lastListing);
});

window.addEventListener('popstate', () => {
  const { host, path } = readUrl();
  if (host && (host !== state.host || path !== state.path)) browse(host, path);
  else if (!host) { state.host = null; state.path = null; loadHosts(); }
});

initTheme();
bindThemeToggle(ui.themeToggle);
loadHosts();
loadAgentDirs();
loadForwards();
loadServices();
loadViews();
setInterval(() => { loadForwards(); loadServices(); loadViews(); }, 15000);

const init = readUrl();
if (init.host) browse(init.host, init.path);
