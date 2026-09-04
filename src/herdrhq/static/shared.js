/* herdr HQ — helpers shared by the dashboard, browse and viewer pages. */

const STATUS_ORDER = ['working', 'blocked', 'idle', 'done', 'unknown'];
const STATUS_LABEL = {
  working: 'working',
  blocked: 'blocked',
  idle: 'idle',
  done: 'done',
  unknown: 'unknown',
};
// icon + label, so status never rides on colour alone
const STATUS_ICON = { working: '▶', blocked: '!', done: '✓', idle: '○', unknown: '?' };

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === null || v === undefined) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child);
  }
  return node;
}

function bytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

function pct(n) {
  return `${(n ?? 0).toFixed(n >= 10 ? 0 : 1)}%`;
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return '–';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

function ago(ts) {
  if (!ts) return 'never';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function fmtDate(t) {
  if (t === null || t === undefined) return '';
  const d = new Date(t * 1000);
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

/** Keep the tail of a path (the part that identifies the project) readable. */
function shortPath(path, keep = 3) {
  if (!path) return '';
  const parts = path.split('/').filter(Boolean);
  const tail = parts.slice(-keep).join('/');
  return parts.length > keep ? `…/${tail}` : path;
}

function qs(params) { return new URLSearchParams(params).toString(); }

async function api(url, opts) {
  const r = await fetch(url, opts);
  let data = null;
  try { data = await r.json(); } catch (_) { /* not JSON */ }
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}

function toast(text) {
  const m = document.getElementById('msg');
  if (!m) return;
  m.textContent = text;
  m.style.display = 'block';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { m.style.display = 'none'; }, 5000);
}

function statusPill(status) {
  return el('span', { class: 'status-pill', 'data-status': status }, [
    el('span', { class: `dot${status === 'working' ? ' is-live' : ''}`, 'data-status': status }),
    el('span', { text: `${STATUS_ICON[status] || ''} ${STATUS_LABEL[status] || status}` }),
  ]);
}

/** Flatten every host's agent panes into one list with host context attached. */
function collectAgents(state) {
  const rows = [];
  for (const host of state.hosts) {
    const data = host.data;
    if (!data) continue;
    const wsLabel = {};
    for (const ws of data.workspaces || []) wsLabel[ws.workspace_id] = ws.label || ws.workspace_id;
    for (const pane of data.panes || []) {
      if (!pane.is_agent) continue;
      rows.push({
        host: host.name,
        hostMeta: host,
        key: `${host.name}/${pane.pane_id}`,
        workspace: wsLabel[pane.workspace_id] || pane.workspace_id,
        history: (host.agent_history || {})[pane.pane_id] || [],
        status: pane.agent_status || 'unknown',
        ...pane,
      });
    }
  }
  return rows;
}

/* Theme: ?theme= wins once, then localStorage, then the OS preference. */
function initTheme() {
  const p = new URLSearchParams(location.search);
  const theme = p.get('theme') || localStorage.getItem('herdr-hq-theme');
  if (theme) document.documentElement.dataset.theme = theme;
}

function bindThemeToggle(btn) {
  if (!btn) return;
  btn.addEventListener('click', () => {
    const current = document.documentElement.dataset.theme
      || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('herdr-hq-theme', next);
  });
}
