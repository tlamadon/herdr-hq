/* herdr HQ dashboard (shared helpers live in shared.js) */

const ui = {
  liveState: document.getElementById('liveState'),
  liveLabel: document.getElementById('liveLabel'),
  markSeen: document.getElementById('markSeen'),
  refreshSelect: document.getElementById('refreshSelect'),
  refreshNow: document.getElementById('refreshNow'),
  themeToggle: document.getElementById('themeToggle'),
  heroAgents: document.getElementById('heroAgents'),
  heroLabel: document.getElementById('heroLabel'),
  tiles: document.getElementById('tiles'),
  hostGrid: document.getElementById('hostGrid'),
  machinesNote: document.getElementById('machinesNote'),
  accountsSection: document.getElementById('accountsSection'),
  accountGrid: document.getElementById('accountGrid'),
  accountsNote: document.getElementById('accountsNote'),
  projectGrid: document.getElementById('projectGrid'),
  projectsNote: document.getElementById('projectsNote'),
  agentsView: document.getElementById('agentsView'),
  search: document.getElementById('search'),
  statusChips: document.getElementById('statusChips'),
  sortSelect: document.getElementById('sortSelect'),
  legend: document.getElementById('legend'),
};

const view = {
  latest: null,
  mode: 'cards',
  query: '',
  statuses: new Set(),
  sort: 'attention',
  openPanes: new Set(),
  timer: null,
  interval: 5000,
  proxied: new Map(),   // `${host}|${port}` -> preview URL from /api/preview
  dupPorts: new Set(),  // `${repo}|${port}` bound in more than one checkout
  seen: {},             // row.key -> state_change_seq at the last look
  activity: new Map(),  // row.key -> ms timestamp of the last observed change
  push: 'off',          // off | live | degraded (set by push.js)
};

/* ------------------------------------------------------- unread / seen */

const SEEN_KEY = 'herdr-hq.seen';
try { view.seen = JSON.parse(localStorage.getItem(SEEN_KEY) || '{}'); } catch (_) { /* fresh */ }

/** Finished or blocked since you last looked at it. herdr's own idle-vs-done
    distinction is the base signal; state_change_seq makes "looked" stick. */
function unread(row) {
  if (row.status !== 'blocked' && row.status !== 'done') return false;
  if (row.state_change_seq === null || row.state_change_seq === undefined) return true;
  return view.seen[row.key] !== row.state_change_seq;
}

function markSeen(row) {
  if (row.state_change_seq === null || row.state_change_seq === undefined) return;
  if (view.seen[row.key] === row.state_change_seq) return;
  view.seen[row.key] = row.state_change_seq;
  try { localStorage.setItem(SEEN_KEY, JSON.stringify(view.seen)); } catch (_) { /* full */ }
  render();
}

function markAllSeen() {
  for (const row of collectAgents(view.latest || { hosts: [] })) {
    if (row.state_change_seq !== null && row.state_change_seq !== undefined) {
      view.seen[row.key] = row.state_change_seq;
    }
  }
  try { localStorage.setItem(SEEN_KEY, JSON.stringify(view.seen)); } catch (_) { /* full */ }
  render();
}

/** Track status transitions we observe (poll or push) for "3m ago" labels. */
function noteActivity(agents) {
  if (!view.prevStatus) view.prevStatus = new Map();
  for (const row of agents) {
    const prev = view.prevStatus.get(row.key);
    if (prev !== undefined && prev !== row.status) view.activity.set(row.key, Date.now());
    view.prevStatus.set(row.key, row.status);
  }
}

/* Git presentation. Ahead/behind come from the local remote-tracking ref — the
   collector never fetches, so "behind" is as of that repo's last fetch. */

function branchName(g) {
  if (!g) return '';
  if (g.detached) return 'detached';
  return g.branch || '(no branch)';
}

function dirtyText(g) {
  if (!g || !g.dirty) return 'clean';
  const bits = [];
  if (g.conflicts) bits.push(`${g.conflicts} conflicted`);
  if (g.staged) bits.push(`${g.staged} staged`);
  if (g.unstaged) bits.push(`${g.unstaged} modified`);
  if (g.untracked) bits.push(`${g.untracked} untracked`);
  return bits.join(' · ');
}

function dirtyCount(g) {
  if (!g) return 0;
  return (g.staged || 0) + (g.unstaged || 0) + (g.untracked || 0) + (g.conflicts || 0);
}

function branchLabel(g) {
  return g.worktree ? `⌥ ${branchName(g)}` : branchName(g);
}

/** ±N ↑a ↓b marks, sidebar-style: quiet dirt, amber sync, red conflicts;
    absence means clean / in sync. */
function gitMarks(g) {
  return [
    g.dirty ? el('span', {
      class: `git-dirty${g.conflicts ? ' has-conflicts' : ''}`,
      text: `±${dirtyCount(g)}`, title: dirtyText(g),
    }) : null,
    g.ahead ? el('span', {
      class: 'git-ab', text: `↑${g.ahead}`,
      title: `${g.ahead} commit${g.ahead === 1 ? '' : 's'} to push`,
    }) : null,
    g.behind ? el('span', {
      class: 'git-ab', text: `↓${g.behind}`,
      title: `${g.behind} commit${g.behind === 1 ? '' : 's'} to pull`,
    }) : null,
  ].filter(Boolean);
}

/** One compact line: repo · branch ±N ↑a ↓b. */
function gitLine(g) {
  if (!g) return null;
  return el('div', { class: 'git-line' }, [
    el('span', { class: 'git-repo', text: g.repo_name, title: g.repo }),
    el('span', { class: 'git-sep', text: '·' }),
    el('span', {
      class: 'git-branch', text: branchLabel(g),
      title: [g.worktree ? `linked worktree at ${g.toplevel}` : null,
              g.upstream || 'no upstream'].filter(Boolean).join(' — '),
    }),
    ...gitMarks(g),
  ]);
}

/* Listening ports. A port is only clickable when the dashboard can actually reach
   it: anything on this machine, or a remote port bound beyond loopback. */

function hostAddress(host) {
  if (!host || host.transport === 'local') return '127.0.0.1';
  return String(host.target || host.name).split('@').pop();
}

function portHref(sock, host) {
  if (!host) return null;
  if (host.transport === 'local') return `http://127.0.0.1:${sock.port}/`;
  if (sock.scope === 'loopback') return null;
  return `http://${hostAddress(host)}:${sock.port}/`;
}

function wtLabel(sock, row) {
  const g = row?.git;
  if (!g || !view.dupPorts.has(`${g.repo_name}|${sock.port}`)) return null;
  return el('span', { class: 'port-wt', text: g.worktree_name || branchName(g), title: g.toplevel });
}

function portChip(sock, host, row) {
  const where = `${sock.addr}:${sock.port}`;
  const info = `${sock.process} · ${where} · pid ${sock.pid}`;
  const href = portHref(sock, host);
  if (href) {
    return el('a', {
      class: 'port-chip is-open', title: info, text: `:${sock.port}`,
      href, target: '_blank', rel: 'noopener',
    }, [wtLabel(sock, row)]);
  }
  // Loopback-only on a remote machine: the herdr HQ proxy can still reach it.
  const key = `${host?.name}|${sock.port}`;
  const proxied = view.proxied.get(key);
  if (proxied) {
    return el('a', {
      class: 'port-chip is-fwd',
      title: `${info} — proxied through herdr HQ`,
      href: proxied, target: '_blank', rel: 'noopener',
    }, [el('span', { text: `:${sock.port}` }), el('span', { 'aria-hidden': 'true', text: ' ⇢' }), wtLabel(sock, row)]);
  }
  if (host?.name) {
    return el('button', {
      class: 'port-chip is-proxy', type: 'button',
      title: `${info} — loopback only on ${host.name}. Click to open a live preview through the herdr HQ proxy.`,
      onclick: async (ev) => {
        const btn = ev.currentTarget;
        btn.setAttribute('aria-busy', 'true');
        btn.disabled = true;
        try {
          const r = await fetch('/api/preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ host: host.name, port: sock.port }),
          });
          const data = await r.json();
          if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
          view.proxied.set(key, data.url);
          window.open(data.url, '_blank');
          render();
        } catch (e) {
          btn.removeAttribute('aria-busy');
          btn.disabled = false;
          toast(`preview :${sock.port} on ${host.name}: ${e.message}`);
        }
      },
    }, [el('span', { text: `:${sock.port}` }), el('span', { 'aria-hidden': 'true', text: ' ▸' }), wtLabel(sock, row)]);
  }
  return el('span', { class: 'port-chip', title: info, text: `:${sock.port}` });
}

function portsLine(ports, host, label = 'Listening', row = null) {
  if (!ports || !ports.length) return null;
  return el('div', { class: 'ports-line' }, [
    el('span', { class: 'ports-label', text: label }),
    // project rollups mix machines, so each socket may carry its own host
    ...ports.map((s) => portChip(s, s.hostMeta || host, s.row || row)),
  ]);
}

/** Ports bound by more than one checkout of the same repo need telling apart. */
function computeDupPorts(agents) {
  const seen = new Map();  // `${repo}|${port}` -> Set of toplevels
  const dups = new Set();
  for (const row of agents) {
    const g = row.git;
    if (!g) continue;
    for (const sock of row.ports || []) {
      const key = `${g.repo_name}|${sock.port}`;
      const tops = seen.get(key) || new Set();
      tops.add(g.toplevel);
      seen.set(key, tops);
      if (tops.size > 1) dups.add(key);
    }
  }
  return dups;
}

function severity(p) {
  if (p >= 90) return 'critical';
  if (p >= 75) return 'warning';
  return 'normal';
}

const SVG = 'http://www.w3.org/2000/svg';

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

/** Sparkline: recessive line, current value in the accent, optional area fill. */
function sparkline(values, { w = 72, h = 20, area = false, floor = 5 } = {}) {
  const svg = svgEl('svg', {
    class: area ? 'spark spark-wide' : 'spark',
    width: w, height: h, viewBox: `0 0 ${w} ${h}`,
    // wide sparklines stretch to the card; non-scaling strokes keep them 2px
    preserveAspectRatio: area ? 'none' : 'xMidYMid meet',
    role: 'img', 'aria-hidden': 'true',
  });
  const data = (values || []).slice(-40);
  if (data.length < 2) return svg;
  const max = Math.max(floor, ...data);
  const pad = 2;
  const x = (i) => (i / (data.length - 1)) * (w - pad * 2) + pad;
  const y = (v) => h - pad - (v / max) * (h - pad * 2);
  const pts = data.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);

  if (area) {
    svg.append(svgEl('path', {
      d: `M${pad},${h} L${pts.join(' L')} L${(w - pad).toFixed(1)},${h} Z`,
      fill: 'var(--accent)', 'fill-opacity': '0.12', stroke: 'none',
    }));
  }
  svg.append(svgEl('polyline', {
    points: pts.join(' '), fill: 'none', stroke: 'var(--baseline)',
    'stroke-width': '2', 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    'vector-effect': 'non-scaling-stroke',
  }));
  svg.append(svgEl('polyline', {
    points: pts.slice(-2).join(' '), fill: 'none', stroke: 'var(--accent)',
    'stroke-width': '2', 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke',
  }));
  if (!area) {
    // a circle would smear under the stretched aspect ratio of a wide spark
    svg.append(svgEl('circle', {
      cx: x(data.length - 1).toFixed(1), cy: y(data[data.length - 1]).toFixed(1),
      r: '2', fill: 'var(--accent)',
    }));
  }
  return svg;
}

function meterRow(label, value, text) {
  const fill = el('div', { class: 'meter-fill' });
  fill.style.width = `${Math.min(100, Math.max(0, value))}%`;
  const sev = severity(value);
  if (sev !== 'normal') fill.dataset.sev = sev;
  return el('div', { class: 'meter-row' }, [
    el('span', { class: 'meter-label', text: label }),
    el('div', { class: 'meter', role: 'img', 'aria-label': `${label} ${pct(value)}` }, [fill]),
    el('span', { class: 'meter-value', text }),
  ]);
}

/* "session" / "week" / "week (all models)" -> the short label on a meter row */
function usageWindowLabel(label) {
  if (!label) return '?';
  if (label === 'session') return '5h';
  if (label === 'week') return 'Week';
  const m = label.match(/^week \((.+)\)$/);
  if (m) return m[1] === 'all models' ? 'Week' : `Wk ${m[1]}`;
  return label;
}

/* the meter-value column is narrow: time when under a day away, date beyond */
function fmtWhen(epoch) {
  const d = new Date(epoch * 1000);
  if (d - Date.now() < 20 * 3600e3)
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

/* claude resets come as host-local text ("Sep 9 at 6:09pm (America/Chicago)");
   shed the parenthetical and apply the same near/far split as fmtWhen */
function compactResets(s) {
  s = s.replace(/\s*\([^)]*\)\s*$/, '').replace(/\s+at\s+/, ' ');
  const m = s.match(/^([A-Z][a-z]{2,} \d{1,2}),?\s+(.+)$/);
  if (!m) return s;
  const today = new Date().toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  return m[1] === today ? m[2] : m[1];
}

function usageMeterRow(provider, w) {
  const resets = w.resets_at ? fmtWhen(w.resets_at) : compactResets(w.resets || '');
  const row = meterRow(usageWindowLabel(w.label), w.pct,
    resets ? `${w.pct}% · resets ${resets}` : `${w.pct}%`);
  row.title = `${provider} ${w.label}` + (resets ? `: resets ${w.resets || resets}` : '');
  return row;
}

/* One entry per distinct provider+account across the fleet: the freshest
   host's snapshot wins (the limits are account-wide, so hosts only disagree
   by probe timing). Hosts that can't produce a snapshot become issue lines. */
function collectAccounts(state) {
  const groups = new Map();
  const issues = [];
  for (const host of state.hosts) {
    const au = host.agent_usage;
    if (!au) continue;
    if (au.error) {
      if (host.status === 'ok') issues.push(`${host.name}: ${au.error}`);
      continue;
    }
    for (const [provider, key] of [['Claude', 'claude'], ['Codex', 'codex']]) {
      const sec = au[key];
      if (!sec) continue;
      if (sec.error && !(sec.windows || []).length) {
        issues.push(`${host.name}: ${provider} ${sec.error}`);
        continue;
      }
      const email = sec.account?.email;
      const gkey = `${key}:${email || `@${host.name}`}`; // no email -> can't merge
      const fresh = sec.as_of ?? au.checked_at ?? 0;
      let g = groups.get(gkey);
      if (!g) { g = { provider, hosts: [], freshest: -1, best: null }; groups.set(gkey, g); }
      g.hosts.push(host.name);
      if (fresh > g.freshest) { g.freshest = fresh; g.best = { sec, checkedAt: au.checked_at, host: host.name }; }
    }
  }
  return { accounts: [...groups.values()], issues };
}

function renderAccounts(state) {
  const { accounts, issues } = collectAccounts(state);
  ui.accountsSection.hidden = !accounts.length && !issues.length;
  const bits = [];
  if (accounts.length) bits.push(`${accounts.length} account${accounts.length === 1 ? '' : 's'}`);
  bits.push(...issues);
  ui.accountsNote.textContent = bits.join(' · ');

  ui.accountGrid.replaceChildren(...accounts.map((g) => {
    const { sec, checkedAt, host } = g.best;
    const acct = sec.account || {};
    // codex data is only as fresh as its last session; claude is probed live
    const when = sec.as_of ? `as of ${ago(sec.as_of)}` : `checked ${ago(checkedAt)}`;
    return el('div', { class: 'host-card account-card' }, [
      el('div', { class: 'host-head' }, [
        el('span', { class: 'host-name', text: g.provider }),
        el('span', { class: 'host-badges' },
          acct.plan ? [el('span', { class: 'badge', text: acct.plan })] : []),
      ]),
      el('div', { class: 'host-sub', text: acct.email || 'account unknown' }),
      el('div', { class: 'meters' }, (sec.windows || []).map((w) => usageMeterRow(g.provider, w))),
      el('div', { class: 'account-foot' }, [
        el('span', { text: when }),
        el('span', { class: 'account-hosts', text: `on ${g.hosts.join(', ')}` }),
      ]),
    ]);
  }));
}

/* ---------------------------------------------------------- data shaping */

/** Roll agents up by repository. A project spans machines; a checkout doesn't. */
function collectProjects(agents) {
  const projects = new Map();
  for (const row of agents) {
    const g = row.git;
    const key = g ? g.repo_name : ' no-repo';
    let p = projects.get(key);
    if (!p) {
      p = {
        key,
        name: g ? g.repo_name : 'Outside a repository',
        hasGit: !!g,
        agents: [],
        hosts: new Set(),
        checkouts: new Map(),
        ports: new Map(),
        counts: {},
        cpu: 0,
        rss: 0,
      };
      projects.set(key, p);
    }
    p.agents.push(row);
    p.hosts.add(row.host);
    p.counts[row.status] = (p.counts[row.status] || 0) + 1;
    p.cpu += row.usage?.cpu_pct || 0;
    p.rss += row.usage?.rss || 0;
    for (const sock of row.ports || []) {
      p.ports.set(`${row.host}|${sock.addr}:${sock.port}`, { ...sock, hostMeta: row.hostMeta, row });
    }
    if (g) {
      // the main checkout and each linked worktree are separate working copies
      const ck = `${row.host}|${g.toplevel}`;
      const existing = p.checkouts.get(ck);
      if (existing) existing.agents += 1;
      else p.checkouts.set(ck, { host: row.host, git: g, agents: 1 });
    }
  }
  return [...projects.values()].sort((a, b) => {
    if (a.hasGit !== b.hasGit) return a.hasGit ? -1 : 1;
    return b.agents.length - a.agents.length || b.cpu - a.cpu;
  });
}

function matches(row) {
  if (view.statuses.size && !view.statuses.has(row.status)) return false;
  if (!view.query) return true;
  const g = row.git;
  const hay = [row.title, row.cwd, row.host, row.workspace, row.agent, row.pane_id,
               g?.repo_name, g?.branch, g?.worktree_name, g?.upstream]
    .filter(Boolean).join(' ').toLowerCase();
  return hay.includes(view.query);
}

/** blocked, then unseen-done, then everything else by the usual status order. */
function attentionRank(r) {
  if (r.status === 'blocked') return 0;
  if (r.status === 'done' && unread(r)) return 1;
  const i = STATUS_ORDER.indexOf(r.status);
  return 2 + (i < 0 ? STATUS_ORDER.length : i);
}

function sortRows(rows) {
  const byStatus = (r) => {
    const i = STATUS_ORDER.indexOf(r.status);
    return i < 0 ? STATUS_ORDER.length : i;
  };
  const cpu = (r) => r.usage?.cpu_pct || 0;
  const mem = (r) => r.usage?.rss || 0;
  const cmp = {
    attention: (a, b) => attentionRank(a) - attentionRank(b) || cpu(b) - cpu(a),
    status: (a, b) => byStatus(a) - byStatus(b) || cpu(b) - cpu(a),
    cpu: (a, b) => cpu(b) - cpu(a),
    mem: (a, b) => mem(b) - mem(a),
    title: (a, b) => (a.title || '').localeCompare(b.title || ''),
  }[view.sort] || ((a, b) => byStatus(a) - byStatus(b));
  return rows.slice().sort(cmp);
}

/* -------------------------------------------------------------- rendering */

function renderSummary(state, agents) {
  const online = state.hosts.filter((h) => h.status === 'ok').length;
  const counts = Object.fromEntries(STATUS_ORDER.map((s) => [s, 0]));
  let cpu = 0;
  let rss = 0;
  for (const a of agents) {
    counts[a.status] = (counts[a.status] || 0) + 1;
    cpu += a.usage?.cpu_pct || 0;
    rss += a.usage?.rss || 0;
  }

  ui.heroAgents.textContent = agents.length;
  ui.heroLabel.textContent = `agents on ${online} of ${state.hosts.length} machine${state.hosts.length === 1 ? '' : 's'}`;

  const unseenDone = agents.filter((a) => a.status === 'done' && unread(a)).length;
  const tiles = [
    { label: 'Working', status: 'working', value: counts.working, sub: 'actively running' },
    { label: 'Blocked', status: 'blocked', value: counts.blocked, sub: 'waiting on you' },
    { label: 'Done · unseen', status: 'done', value: unseenDone, sub: 'finished while away' },
    { label: 'Idle', status: 'idle', value: counts.idle + counts.done - unseenDone, sub: 'ready or already seen' },
    { label: 'Agent CPU', value: (cpu / 100).toFixed(1), sub: 'cores across the fleet' },
    { label: 'Agent memory', value: bytes(rss).split(' ')[0], sub: `${bytes(rss).split(' ')[1]} resident` },
  ];

  ui.tiles.replaceChildren(...tiles.map((t) => el('div', { class: 'tile' }, [
    el('div', { class: 'tile-label' }, [
      t.status ? el('span', { class: 'dot', 'data-status': t.status }) : null,
      el('span', { text: t.label }),
    ]),
    el('div', { class: 'tile-value', text: String(t.value) }),
    el('div', { class: 'tile-sub', text: t.sub }),
  ])));
}

function renderHosts(state) {
  const cards = state.hosts.map((host) => {
    const data = host.data;
    const m = data?.machine || {};
    const agentCount = (data?.panes || []).filter((p) => p.is_agent).length;
    const badges = [
      el('span', { class: 'badge', text: host.transport === 'local' ? 'local' : `ssh ${host.target}` }),
      data?.herdr?.version ? el('span', { class: 'badge', text: `herdr ${data.herdr.version}` }) : null,
      el('span', {
        class: `badge ${host.status === 'ok' ? 'is-good' : 'is-bad'}`,
        text: host.status === 'ok' ? 'online' : 'unreachable',
      }),
    ];

    const body = [];
    if (host.status === 'error') {
      body.push(el('div', { class: 'host-error', text: host.error || 'unknown error' }));
    }
    if (data) {
      body.push(el('div', { class: 'meters' }, [
        meterRow('CPU', m.cpu_pct, `${pct(m.cpu_pct)} of ${m.cpu_count} cores`),
        meterRow('RAM', m.mem_pct, `${bytes(m.mem_used)} / ${bytes(m.mem_total)}`),
        m.disk_total ? meterRow('Disk', (m.disk_used / m.disk_total) * 100,
          `${bytes(m.disk_used)} / ${bytes(m.disk_total)}`) : null,
      ].filter(Boolean)));

      body.push(sparkline(host.history?.cpu, { w: 268, h: 30, area: true, floor: 10 }));

      body.push(el('div', { class: 'host-stats' }, [
        el('div', {}, [
          el('div', { class: 'host-stat-label', text: 'Load' }),
          el('div', { class: 'host-stat-value', text: `${m.load1 ?? '–'}` }),
        ]),
        el('div', {}, [
          el('div', { class: 'host-stat-label', text: 'Uptime' }),
          el('div', { class: 'host-stat-value', text: duration(m.uptime_s) }),
        ]),
        el('div', {}, [
          el('div', { class: 'host-stat-label', text: 'Agents' }),
          el('div', { class: 'host-stat-value', text: String(agentCount) }),
        ]),
      ]));

      // every listening port under a herdr pane, agent or not
      const seen = new Set();
      const allPorts = [];
      for (const pane of data.panes || []) {
        for (const sock of pane.ports || []) {
          const key = `${sock.addr}:${sock.port}`;
          if (!seen.has(key)) { seen.add(key); allPorts.push(sock); }
        }
      }
      body.push(portsLine(allPorts, host, 'Pane ports'));
    }

    const sub = data
      ? `${m.os} ${m.os_release} · ${m.arch} · ${m.proc_count} procs · polled ${ago(host.last_ok)} in ${host.latency_ms} ms`
      : host.status === 'error'
        ? `never polled successfully · last tried ${ago(host.last_try)}`
        : 'waiting for first poll…';

    return el('div', { class: `host-card${host.status === 'error' ? ' is-error' : ''}` }, [
      el('div', { class: 'host-head' }, [
        el('span', { class: 'host-name', text: host.name }),
        el('span', { class: 'host-badges' }, badges.filter(Boolean)),
      ]),
      el('div', { class: 'host-sub', text: sub }),
      ...body,
    ]);
  });
  ui.hostGrid.replaceChildren(...cards);

  const errs = state.hosts.filter((h) => h.status === 'error');
  ui.machinesNote.textContent = errs.length
    ? `${errs.length} unreachable`
    : `all ${state.hosts.length} reachable`;
}

function statusRow(counts) {
  const present = STATUS_ORDER.filter((s) => counts[s]);
  return el('div', { class: 'status-row' }, present.map((s) => el('span', { class: 'status-item' }, [
    el('span', { class: `dot${s === 'working' ? ' is-live' : ''}`, 'data-status': s }),
    el('span', { text: `${counts[s]} ${STATUS_LABEL[s]}` }),
  ])));
}

function checkoutRow(c, showHost) {
  const g = c.git;
  return el('div', { class: 'checkout-row' }, [
    el('span', { class: 'git-branch', text: branchLabel(g), title: g.toplevel }),
    showHost ? el('span', { class: 'badge', text: c.host }) : null,
    el('span', { class: 'checkout-spacer' }),
    ...gitMarks(g),
    el('span', { class: 'checkout-agents', text: `${c.agents} ${c.agents === 1 ? 'agent' : 'agents'}` }),
  ].filter(Boolean));
}

function projectCard(p) {
  const multiHost = p.hosts.size > 1;
  const checkouts = [...p.checkouts.values()]
    .sort((a, b) => b.agents - a.agents || branchName(a.git).localeCompare(branchName(b.git)));
  const dirtyCheckouts = checkouts.filter((c) => c.git.dirty).length;
  const unpushed = checkouts.reduce((s, c) => s + (c.git.ahead || 0), 0);

  const summary = [`${p.agents.length} agent${p.agents.length === 1 ? '' : 's'}`];
  if (p.hasGit) {
    summary.push(`${checkouts.length} checkout${checkouts.length === 1 ? '' : 's'}`);
  }
  summary.push(`${pct(p.cpu)} cpu`, bytes(p.rss));

  const flags = [];
  if (unpushed) flags.push(el('span', { class: 'badge', text: `↑${unpushed} unpushed` }));
  if (dirtyCheckouts) {
    flags.push(el('span', {
      class: 'badge',
      text: `${dirtyCheckouts} dirty`,
      title: `${dirtyCheckouts} of ${checkouts.length} checkouts have uncommitted changes`,
    }));
  }

  return el('div', { class: 'host-card project-card' }, [
    el('div', { class: 'host-head' }, [
      el('button', {
        class: 'project-name',
        type: 'button',
        title: 'Filter the agent list to this project',
        onclick: () => {
          view.query = p.hasGit ? p.name.toLowerCase() : '';
          ui.search.value = p.hasGit ? p.name : '';
          render();
          document.getElementById('agents-h')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        },
        text: p.name,
      }),
      el('span', { class: 'host-badges' }, [
        ...flags,
        multiHost
          ? el('span', { class: 'badge', text: `${p.hosts.size} machines`, title: [...p.hosts].join(', ') })
          : el('span', { class: 'badge', text: [...p.hosts][0] }),
      ]),
    ]),
    el('div', { class: 'host-sub', text: summary.join(' · ') }),
    statusRow(p.counts),
    checkouts.length
      ? el('div', { class: 'checkouts' }, checkouts.map((c) => checkoutRow(c, multiHost)))
      : null,
    p.ports.size
      ? portsLine([...p.ports.values()], null, 'Listening')
      : null,
  ].filter(Boolean));
}

function renderProjects(agents) {
  const projects = collectProjects(agents);
  ui.projectGrid.replaceChildren(...projects.map(projectCard));
  const repos = projects.filter((p) => p.hasGit).length;
  ui.projectsNote.textContent = repos
    ? `${repos} ${repos === 1 ? 'repository' : 'repositories'} in play`
    : 'no repositories detected';
}

function openTerminal(row) {
  markSeen(row);
  term.open({
    host: row.host,
    pane: row.pane_id,
    title: row.title || row.pane_id,
    subtitle: `${row.host} · ${row.workspace} · ${row.pane_id}`,
    canInput: view.latest?.terminal?.input !== false,
  });
}

function terminalButton(row, label = '⌨') {
  if (!view.latest?.terminal?.enabled) return null;
  return el('button', {
    class: 'term-open',
    type: 'button',
    title: `Open a live terminal on ${row.host} ${row.pane_id}`,
    text: label,
    onclick: () => openTerminal(row),
  });
}

/** Jump to the workspace view focused on this agent's checkout and pane. */
function workspaceLink(row) {
  const q = { host: row.host, pane: row.pane_id };
  if (row.git?.toplevel) q.top = row.git.toplevel;
  return `/work?${qs(q)}`;
}

function filesButton(row) {
  if (!row.git?.toplevel && !row.cwd) return null;
  return el('a', {
    class: 'term-open files-open',
    href: workspaceLink(row),
    title: `Open ${row.git?.repo_name || row.cwd} in the workspace view`,
  }, [el('span', { 'aria-hidden': 'true', text: '🗀' }), el('span', { class: 'sr-only', text: 'workspace' })]);
}

/** One line of what the agent is actually doing, expandable for the detail.
    Renders nothing when the host doesn't report agent sessions. */
function transcriptLine(row) {
  const t = row.transcript;
  if (!t || !t.available) return null;
  const bits = [];
  if (t.current_tool) {
    bits.push(el('span', { class: 'transcript-tool', text: t.current_tool.name }));
    if (t.current_tool.detail) bits.push(el('span', { class: 'transcript-sep', text: '·' }));
    if (t.current_tool.detail) bits.push(el('span', { class: 'transcript-detail', text: t.current_tool.detail }));
  } else if (t.last_assistant) {
    bits.push(el('span', { class: 'transcript-detail', text: `“${t.last_assistant}”` }));
  }
  if (!bits.length) return null;
  const details = el('details', { class: 'transcript-line' }, [
    el('summary', {}, bits),
    el('div', { class: 'transcript-full' }, [
      t.last_prompt ? el('div', { class: 'transcript-block' }, [
        el('span', { class: 'transcript-label', text: 'asked' }),
        el('span', { text: t.last_prompt }),
      ]) : null,
      t.last_assistant ? el('div', { class: 'transcript-block' }, [
        el('span', { class: 'transcript-label', text: 'said' }),
        el('span', { text: t.last_assistant }),
      ]) : null,
      t.current_tool ? el('div', { class: 'transcript-block' }, [
        el('span', { class: 'transcript-label', text: 'running' }),
        el('span', { text: `${t.current_tool.name} ${t.current_tool.detail || ''}` }),
      ]) : null,
    ]),
  ]);
  if (view.openTranscripts?.has(row.key)) details.open = true;
  details.addEventListener('toggle', () => {
    if (!view.openTranscripts) view.openTranscripts = new Set();
    if (details.open) { view.openTranscripts.add(row.key); markSeen(row); }
    else view.openTranscripts.delete(row.key);
  });
  return details;
}

function unreadDot(row) {
  if (!unread(row)) return null;
  return el('span', { class: 'unread-dot', title: 'new activity since you last looked' }, [
    el('span', { class: 'sr-only', text: 'unread' }),
  ]);
}

function activityAge(row) {
  const t = view.activity.get(row.key);
  return t ? ago(t / 1000) : null;
}

function agentCard(row, showHost = false) {
  const u = row.usage || {};
  const procs = (u.procs || []).map((p) => el('div', { class: 'proc-row' }, [
    el('span', { text: p.cmdline || p.name, title: p.cmdline || p.name }),
    el('span', { text: pct(p.cpu_pct) }),
    el('span', { text: bytes(p.rss) }),
  ]));

  const details = el('details', { class: 'agent-procs' }, [
    el('summary', { text: `${u.proc_count || 0} processes · pid ${row.shell_pid ?? '?'}` }),
    ...procs,
  ]);
  if (view.openPanes.has(row.key)) details.open = true;
  details.addEventListener('toggle', () => {
    if (details.open) view.openPanes.add(row.key);
    else view.openPanes.delete(row.key);
  });

  const age = activityAge(row);
  const kind = [row.agent || 'agent', row.pane_id, age].filter(Boolean).join(' · ');
  const card = el('div', {
    class: 'agent-card',
    'data-status': row.status,
    'data-unread': unread(row) ? '1' : null,
  }, [
    el('div', { class: 'agent-top' }, [
      unreadDot(row),
      statusPill(row.status),
      row.focused ? el('span', { class: 'badge', text: 'focused' }) : null,
      showHost ? el('span', { class: 'badge', text: row.host }) : null,
      el('span', { class: 'agent-kind', text: kind }),
      filesButton(row),
      terminalButton(row),
    ]),
    el('div', { class: 'agent-title', text: row.title || row.workspace, title: row.title || '' }),
    el('div', { class: 'agent-path', text: shortPath(row.cwd), title: row.cwd || '' }),
    gitLine(row.git),
    transcriptLine(row),
    portsLine(row.ports, row.hostMeta, 'Listening', row),
    el('div', { class: 'agent-usage' }, [
      el('span', { class: 'usage-num' }, [
        el('b', { text: pct(u.cpu_pct) }), el('span', { text: ' cpu' }),
      ]),
      el('span', { class: 'usage-num' }, [
        el('b', { text: bytes(u.rss) }), el('span', { text: ' rss' }),
      ]),
      sparkline(row.history, { w: 76, h: 20, floor: 10 }),
    ]),
    details,
  ]);
  if (unread(row)) card.addEventListener('click', () => markSeen(row));
  return card;
}

function renderCards(rows) {
  const groups = [];
  // With the default sort, agents that need you come first, across hosts.
  if (view.sort === 'attention') {
    const urgent = rows.filter((r) => attentionRank(r) < 2);
    if (urgent.length) {
      rows = rows.filter((r) => attentionRank(r) >= 2);
      groups.push(el('div', { class: 'host-group attention-band' }, [
        el('div', { class: 'host-group-head' }, [
          el('strong', { text: 'Needs attention' }),
          el('span', { text: `${urgent.length} agent${urgent.length === 1 ? '' : 's'} waiting on you` }),
        ]),
        el('div', { class: 'agent-grid' }, urgent.map((r) => agentCard(r, true))),
      ]));
    }
  }
  const byHost = new Map();
  for (const row of rows) {
    if (!byHost.has(row.host)) byHost.set(row.host, []);
    byHost.get(row.host).push(row);
  }
  for (const [host, hostRows] of byHost) {
    const cpu = hostRows.reduce((s, r) => s + (r.usage?.cpu_pct || 0), 0);
    const rss = hostRows.reduce((s, r) => s + (r.usage?.rss || 0), 0);
    groups.push(el('div', { class: 'host-group' }, [
      el('div', { class: 'host-group-head' }, [
        el('strong', { text: host }),
        el('span', { text: `${hostRows.length} agent${hostRows.length === 1 ? '' : 's'} · ${pct(cpu)} cpu · ${bytes(rss)} rss` }),
      ]),
      el('div', { class: 'agent-grid' }, hostRows.map(agentCard)),
    ]));
  }
  return groups;
}

function renderTable(rows) {
  const head = ['Host', 'Workspace', 'Agent', 'Status', 'Activity', 'Title', 'Last message',
                'Repo', 'Branch', 'Sync',
                'Working tree', 'Ports', 'Working dir', 'CPU', 'RSS', 'Procs', 'Pane', 'PID'];
  const thead = el('thead', {}, [el('tr', {}, head.map((h) => el('th', { text: h })))]);
  const tbody = el('tbody', {}, rows.map((r) => {
    const g = r.git;
    const sync = !g ? '' : [g.ahead ? `↑${g.ahead}` : '', g.behind ? `↓${g.behind}` : '']
      .filter(Boolean).join(' ') || (g.upstream ? 'in sync' : 'no upstream');
    return el('tr', { 'data-unread': unread(r) ? '1' : null }, [
    el('td', { text: r.host }),
    el('td', { text: r.workspace }),
    el('td', { text: r.agent || '–' }),
    el('td', {}, [unreadDot(r), statusPill(r.status)]),
    el('td', { class: 'num', text: activityAge(r) || '' }),
    el('td', { text: r.title || '', title: r.title || '' }),
    el('td', {
      class: 'transcript-cell',
      text: r.transcript?.current_tool
        ? `▸ ${r.transcript.current_tool.name} ${r.transcript.current_tool.detail || ''}`
        : r.transcript?.last_assistant || '',
      title: r.transcript?.last_assistant || '',
    }),
    el('td', { text: g ? g.repo_name : '–', title: g ? g.repo : '' }),
    el('td', {}, [
      el('span', { class: 'git-branch', text: g ? branchLabel(g) : '–',
        title: g?.worktree ? g.toplevel : '' }),
    ]),
    el('td', { class: 'num', text: sync }),
    el('td', {
      class: g?.dirty ? `git-dirty${g.conflicts ? ' has-conflicts' : ''}` : 'git-muted',
      text: g?.dirty ? `±${dirtyCount(g)}` : '',
      title: g ? dirtyText(g) : '',
    }),
    el('td', {}, (r.ports || []).map((s) => portChip(s, r.hostMeta, r))),
    el('td', { class: 'path' }, r.cwd ? [el('a', {
      href: workspaceLink(r),
      title: `Open in the workspace view`, text: r.cwd,
    })] : []),
    el('td', { class: 'num', text: pct(r.usage?.cpu_pct) }),
    el('td', { class: 'num', text: bytes(r.usage?.rss) }),
    el('td', { class: 'num', text: String(r.usage?.proc_count ?? 0) }),
    el('td', {}, [el('span', { text: `${r.pane_id} ` }), terminalButton(r)].filter(Boolean)),
    el('td', { class: 'num', text: String(r.shell_pid ?? '–') }),
    ]);
  }));
  return [el('div', { class: 'table-wrap' }, [el('table', {}, [thead, tbody])])];
}

function renderChips(agents) {
  const counts = {};
  for (const a of agents) counts[a.status] = (counts[a.status] || 0) + 1;
  const present = STATUS_ORDER.filter((s) => counts[s]);
  const chips = present.map((s) => {
    const on = view.statuses.has(s);
    return el('button', {
      class: `chip${on ? ' is-on' : ''}`, type: 'button', 'aria-pressed': String(on),
      onclick: () => {
        if (view.statuses.has(s)) view.statuses.delete(s);
        else view.statuses.add(s);
        render();
      },
    }, [
      el('span', { class: 'dot', 'data-status': s }),
      el('span', { text: `${STATUS_LABEL[s]} ${counts[s]}` }),
    ]);
  });
  if (view.statuses.size) {
    chips.push(el('button', {
      class: 'chip', type: 'button',
      onclick: () => { view.statuses.clear(); render(); },
    }, [el('span', { text: 'clear' })]));
  }
  ui.statusChips.replaceChildren(...chips);
}

function renderLegend() {
  ui.legend.replaceChildren(
    ...STATUS_ORDER.map((s) => el('li', {}, [
      el('span', { class: 'dot', 'data-status': s }),
      el('span', { text: `${STATUS_ICON[s]} ${STATUS_LABEL[s]}` }),
    ])),
    el('li', {}, [
      el('span', { class: 'unread-dot', 'aria-hidden': 'true' }),
      el('span', { text: 'unread — blocked or finished since you last looked' }),
    ]),
  );
}

function render() {
  const state = view.latest;
  if (!state) return;
  const agents = collectAgents(state);
  noteActivity(agents);
  view.dupPorts = computeDupPorts(agents);
  renderSummary(state, agents);
  renderHosts(state);
  renderAccounts(state);
  renderProjects(agents);
  renderChips(agents);
  writeUrl();

  const rows = sortRows(agents.filter(matches));
  if (!rows.length) {
    ui.agentsView.replaceChildren(el('div', {
      class: 'empty',
      text: agents.length ? 'No agents match the current filter.' : 'No agents running on any machine.',
    }));
    return;
  }
  ui.agentsView.replaceChildren(...(view.mode === 'table' ? renderTable(rows) : renderCards(rows)));
}

/* ------------------------------------------------------------- polling */

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function tick(force = false) {
  try {
    if (force) {
      ui.liveLabel.textContent = 'polling machines…';
      await fetch('/api/refresh', { method: 'POST' });
      // pollers wake asynchronously and each sample takes ~1s on the far end
      await sleep(1400);
    }
    const res = await fetch('/api/state', { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    view.latest = await res.json();
    const bad = view.latest.hosts.filter((h) => h.status === 'error').length;
    ui.liveState.dataset.state = bad === view.latest.hosts.length && bad > 0 ? 'error' : 'ok';
    ui.liveLabel.textContent = `updated ${new Date().toLocaleTimeString()}`;
    render();
    if (view.autoTerm && view.latest.terminal?.enabled) {
      const { host, pane } = view.autoTerm;
      view.autoTerm = null;
      const row = collectAgents(view.latest).find((r) => r.host === host && r.pane_id === pane);
      term.open({
        host, pane,
        title: row?.title || pane,
        subtitle: `${host} · ${pane}`,
        canInput: view.latest.terminal.input !== false,
      });
    }
  } catch (err) {
    ui.liveState.dataset.state = 'error';
    ui.liveLabel.textContent = `server unreachable (${err.message})`;
  }
}

function schedule() {
  clearInterval(view.timer);
  if (view.interval > 0) view.timer = setInterval(() => tick(), view.interval);
  if (view.interval === 0) {
    ui.liveState.dataset.state = 'paused';
    ui.liveLabel.textContent = 'auto-refresh paused';
  }
}

/* --------------------------------------------------------------- wiring */

/** Views are shareable: ?view=table&q=overleaf&status=working,blocked&theme=dark */
function readUrl() {
  const p = new URLSearchParams(location.search);
  if (p.get('view') === 'table') view.mode = 'table';
  if (p.get('q')) { view.query = p.get('q').toLowerCase(); ui.search.value = p.get('q'); }
  if (p.get('sort')) { view.sort = p.get('sort'); ui.sortSelect.value = view.sort; }
  for (const s of (p.get('status') || '').split(',').filter(Boolean)) view.statuses.add(s);
  // deep link straight to a pane's terminal: ?termhost=nixos-ultra&termpane=wB:p1
  if (p.get('termhost') && p.get('termpane')) {
    view.autoTerm = { host: p.get('termhost'), pane: p.get('termpane') };
  }
  initTheme();
  for (const btn of document.querySelectorAll('[data-view]')) {
    btn.classList.toggle('is-on', btn.dataset.view === view.mode);
  }
}

function writeUrl() {
  const p = new URLSearchParams();
  if (view.mode !== 'cards') p.set('view', view.mode);
  if (view.query) p.set('q', view.query);
  if (view.sort !== 'attention') p.set('sort', view.sort);
  if (view.statuses.size) p.set('status', [...view.statuses].join(','));
  const qs = p.toString();
  history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
}

ui.refreshSelect.addEventListener('change', () => {
  view.interval = Number(ui.refreshSelect.value);
  schedule();
  if (view.interval) tick();
});
ui.refreshNow.addEventListener('click', () => tick(true));
ui.markSeen.addEventListener('click', markAllSeen);
ui.search.addEventListener('input', () => {
  view.query = ui.search.value.trim().toLowerCase();
  render();
});
ui.sortSelect.addEventListener('change', () => {
  view.sort = ui.sortSelect.value;
  render();
});
for (const btn of document.querySelectorAll('[data-view]')) {
  btn.addEventListener('click', () => {
    view.mode = btn.dataset.view;
    for (const other of document.querySelectorAll('[data-view]')) {
      other.classList.toggle('is-on', other === btn);
    }
    render();
  });
}

bindThemeToggle(ui.themeToggle);

readUrl();
renderLegend();
tick();
schedule();
