/* herdr HQ — push client. Consumes /api/state/stream so agent status flips
   land the moment herdr notices them, instead of on the next poll. Patches
   go straight into view.latest (so the regular poll render stays idempotent)
   followed by a debounced re-render. The poll loop keeps running regardless:
   it owns CPU/RSS/git/ports, and it is the fallback when the stream drops. */

(function pushClient() {
  let renderTimer = null;
  let fetchTimer = null;

  function scheduleRender() {
    if (renderTimer) return;
    renderTimer = setTimeout(() => { renderTimer = null; render(); }, 250);
  }

  function scheduleFetch() {
    // a poll just landed server-side: pick it up soon, once per burst
    if (fetchTimer) return;
    fetchTimer = setTimeout(() => { fetchTimer = null; tick(); }, 400);
  }

  function findPane(host, paneId) {
    const h = view.latest?.hosts?.find((x) => x.name === host);
    const panes = h?.data?.panes || [];
    return panes.find((p) => p.pane_id === paneId);
  }

  function apply(msg) {
    if (msg.type === 'pane') {
      const pane = findPane(msg.host, msg.pane_id);
      if (!pane) return;
      const statusChanged = msg.agent_status && msg.agent_status !== pane.agent_status;
      for (const k of ['agent_status', 'title', 'agent', 'focused']) {
        if (msg[k] !== undefined && msg[k] !== null) pane[k] = msg[k];
      }
      const key = `${msg.host}/${msg.pane_id}`;
      view.activity.set(key, (msg.at ? msg.at * 1000 : Date.now()));
      if (statusChanged && pane.state_change_seq !== null && pane.state_change_seq !== undefined) {
        // the real seq arrives with the next poll; a fractional bump breaks
        // the ==seen match so the unread dot appears immediately
        pane.state_change_seq += 0.5;
      }
      scheduleRender();
    } else if (msg.type === 'host') {
      scheduleFetch();
    } else if (msg.type === 'events') {
      const h = view.latest?.hosts?.find((x) => x.name === msg.host);
      if (h) { h.events = msg.status; scheduleRender(); }
    }
    // 'structure' needs nothing: the debounced re-poll sends 'host' right after
  }

  function setPush(state) {
    if (view.push === state) return;
    view.push = state;
    const label = document.getElementById('pushLabel');
    if (label) {
      label.textContent = state === 'live' ? '· push' : state === 'degraded' ? '· push off' : '';
      label.title = state === 'live'
        ? 'status changes stream in live; resources update on the poll'
        : 'push stream down — falling back to polling alone';
    }
  }

  function connect() {
    const es = new EventSource('/api/state/stream');
    es.onopen = () => setPush('live');
    es.onerror = () => setPush('degraded');  // EventSource retries by itself
    es.onmessage = (ev) => {
      try { apply(JSON.parse(ev.data)); } catch (_) { /* not for us */ }
    };
  }

  connect();
})();
