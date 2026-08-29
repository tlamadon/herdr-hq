/* Live pane mirror in xterm.js.
 *
 * herdr exposes no raw output stream, so the server sends whole visible screens
 * (ANSI intact) whenever they change. Painting a frame is therefore a cursor-home
 * redraw with erase-to-end-of-line per row — the same trick `watch` uses, which
 * repaints without the flicker of a full clear. The local cursor is hidden: the
 * real one lives in the pane, and we're never told where it is.
 */

const term = (() => {
  const ui = {
    overlay: document.getElementById('termOverlay'),
    title: document.getElementById('termTitle'),
    where: document.getElementById('termWhere'),
    status: document.getElementById('termStatus'),
    input: document.getElementById('termInput'),
    smaller: document.getElementById('termSmaller'),
    bigger: document.getElementById('termBigger'),
    close: document.getElementById('termClose'),
    mount: document.getElementById('termMount'),
    note: document.getElementById('termNote'),
  };

  const state = {
    xterm: null,
    source: null,
    target: null,      // {host, pane}
    fontSize: 13,
    pending: [],
    flushTimer: null,
    fitTimer: null,
    allowInput: false,
    warnedKeys: false,
  };

  const THEME_DARK = {
    background: '#141413', foreground: '#e6e5dd', cursor: '#3987e5',
    selectionBackground: 'rgba(57,135,229,0.35)',
  };
  const THEME_LIGHT = {
    background: '#fcfcfb', foreground: '#1a1a19', cursor: '#2a78d6',
    selectionBackground: 'rgba(42,120,214,0.25)',
  };

  function isDark() {
    const stamped = document.documentElement.dataset.theme;
    if (stamped) return stamped === 'dark';
    return matchMedia('(prefers-color-scheme: dark)').matches;
  }

  function setStatus(text, kind) {
    ui.status.textContent = text;
    ui.status.dataset.state = kind;
  }

  function ensureTerm() {
    if (state.xterm) return state.xterm;
    state.xterm = new Terminal({
      fontSize: state.fontSize,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
      theme: isDark() ? THEME_DARK : THEME_LIGHT,
      cursorBlink: false,
      convertEol: false,
      scrollback: 0,          // every frame is a full screen; nothing to scroll back to
      disableStdin: false,
      allowProposedApi: true,
    });
    state.xterm.open(ui.mount);
    state.xterm.write('\x1b[?25l');
    state.xterm.onData(onKey);
    return state.xterm;
  }

  /** Redraw one full screen in place. */
  function paint(text) {
    const t = ensureTerm();
    const lines = text.split('\n').map((l) => l.replace(/\r$/, ''));
    let out = '\x1b[H';
    lines.forEach((line, i) => {
      out += line + '\x1b[K';
      if (i < lines.length - 1) out += '\r\n';
    });
    t.write(out + '\x1b[J');
  }

  function resize(cols, rows) {
    const t = ensureTerm();
    if (cols !== t.cols || rows !== t.rows) t.resize(cols, rows);
    scheduleFit();
  }

  /** xterm lays out asynchronously, so measure a couple of frames later. */
  function scheduleFit() {
    clearTimeout(state.fitTimer);
    state.fitTimer = setTimeout(() => requestAnimationFrame(fit), 60);
  }

  /** Panes are wider than the modal; scale down rather than clip. */
  function fit() {
    const el = ui.mount;
    el.style.transform = 'none';
    const box = el.parentElement;
    const pad = getComputedStyle(box);
    const avail = box.clientWidth - parseFloat(pad.paddingLeft) - parseFloat(pad.paddingRight);
    const natural = el.scrollWidth;
    const scale = natural > avail && natural > 0 ? Math.max(0.4, avail / natural) : 1;
    el.style.transform = `scale(${scale})`;
    el.parentElement.style.height = `${el.scrollHeight * scale}px`;
  }

  /* herdr takes named keys, not raw bytes — a literal "\x15" shows up in the pane
     as "^U". So xterm's byte stream is split into printable runs and key names.
     Names verified against herdr 0.8.2: it has no Delete/Home/End/PageUp/PageDown. */

  const MULTI_KEYS = {
    '\x1b[A': 'Up', '\x1b[B': 'Down', '\x1b[C': 'Right', '\x1b[D': 'Left',
    '\x1bOA': 'Up', '\x1bOB': 'Down', '\x1bOC': 'Right', '\x1bOD': 'Left',
    '\x1b[Z': 'Shift+Tab',
    '\x1b[1;5A': 'Ctrl+Up', '\x1b[1;5B': 'Ctrl+Down',
    '\x1b[1;5C': 'Ctrl+Right', '\x1b[1;5D': 'Ctrl+Left',
    '\x1b[1;2A': 'Shift+Up', '\x1b[1;2B': 'Shift+Down',
    '\x1b[1;3C': 'Alt+Right', '\x1b[1;3D': 'Alt+Left',
    '\x1b\x7f': 'Alt+Backspace',
  };
  const SINGLE_KEYS = {
    '\r': 'Enter', '\n': 'Enter', '\t': 'Tab', '\x7f': 'Backspace', '\b': 'Backspace',
  };
  const MULTI_ORDER = Object.keys(MULTI_KEYS).sort((a, b) => b.length - a.length);
  const CSI_RE = /^\x1b(\[[0-9;]*[ -/]*[@-~]|O.)/;

  function toOps(data) {
    const ops = [];
    let text = '';
    const pushText = () => { if (text) { ops.push({ text }); text = ''; } };

    let i = 0;
    outer: while (i < data.length) {
      for (const seq of MULTI_ORDER) {
        if (data.startsWith(seq, i)) {
          pushText();
          ops.push({ key: MULTI_KEYS[seq] });
          i += seq.length;
          continue outer;
        }
      }
      const ch = data[i];
      const code = data.charCodeAt(i);

      if (code === 27) {
        const next = data[i + 1];
        if (next && next !== '[' && next !== 'O' && data.charCodeAt(i + 1) > 32) {
          pushText();
          ops.push({ key: `Alt+${next.toUpperCase()}` });
          i += 2;
        } else {
          const m = CSI_RE.exec(data.slice(i));
          pushText();
          ops.push(m ? { unsupported: m[0] } : { key: 'Escape' });
          i += m ? m[0].length : 1;
        }
        continue;
      }
      if (SINGLE_KEYS[ch]) {
        pushText();
        ops.push({ key: SINGLE_KEYS[ch] });
        i += 1;
        continue;
      }
      if (code >= 1 && code <= 26) {
        pushText();
        ops.push({ key: `Ctrl+${String.fromCharCode(64 + code)}` });
        i += 1;
        continue;
      }
      if (code < 32) { pushText(); ops.push({ unsupported: ch }); i += 1; continue; }
      text += ch;
      i += 1;
    }
    pushText();
    return ops;
  }

  function onKey(data) {
    if (!state.allowInput || !state.target) return;
    state.pending.push(...toOps(data));
    if (state.flushTimer) return;
    // coalesce fast typing and pastes into one request
    state.flushTimer = setTimeout(flush, 15);
  }

  async function flush() {
    state.flushTimer = null;
    const ops = state.pending;
    state.pending = [];
    if (!ops.length || !state.target) return;
    const dropped = ops.filter((o) => o.unsupported).length;
    const sendable = ops.filter((o) => !o.unsupported);
    if (dropped && !state.warnedKeys) {
      state.warnedKeys = true;
      ui.note.textContent = 'That key has no herdr equivalent (Delete, Home, End and Page keys are unsupported).';
    }
    if (!sendable.length) return;
    try {
      const res = await fetch('/api/term/input', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...state.target, ops: sendable }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setStatus(body.error || `input rejected (${res.status})`, 'error');
      }
    } catch (err) {
      setStatus(`input failed: ${err.message}`, 'error');
    }
  }

  function connect(target) {
    if (state.source) state.source.close();
    const qs = new URLSearchParams(target).toString();
    const src = new EventSource(`/api/term/stream?${qs}`);
    state.source = src;
    setStatus('connecting…', 'connecting');

    src.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.t === 'screen') {
        setStatus(`live · ${msg.cols}×${msg.rows}`, 'ok');
        resize(msg.cols, msg.rows);
        paint(new TextDecoder().decode(Uint8Array.from(atob(msg.text), (c) => c.charCodeAt(0))));
      } else if (msg.t === 'error') {
        setStatus(msg.message, 'error');
      } else if (msg.t === 'closed') {
        setStatus(msg.message || 'mirror ended', 'error');
        src.close();
      }
    };
    src.onerror = () => {
      if (src.readyState === EventSource.CLOSED) setStatus('disconnected', 'error');
      else setStatus('reconnecting…', 'connecting');
    };
  }

  function setFont(size) {
    state.fontSize = Math.min(22, Math.max(8, size));
    if (state.xterm) {
      state.xterm.options.fontSize = state.fontSize;
      scheduleFit();
    }
  }

  function open({ host, pane, title, subtitle, canInput = true }) {
    state.target = { host, pane };
    // Writable by default — it's a terminal. Keys only flow once you click into
    // the screen, so the modal can't swallow stray keystrokes on its own.
    state.allowInput = canInput;
    ui.input.checked = canInput;
    ui.input.disabled = !canInput;
    setNote();
    ui.title.textContent = title || pane;
    ui.where.textContent = subtitle || `${host} · ${pane}`;
    ui.overlay.hidden = false;
    document.body.classList.add('is-modal');
    const t = ensureTerm();
    t.options.theme = isDark() ? THEME_DARK : THEME_LIGHT;
    t.write('\x1b[2J\x1b[H\x1b[?25l');
    connect(state.target);
    requestAnimationFrame(fit);
  }

  function close() {
    if (state.source) { state.source.close(); state.source = null; }
    if (state.target) {
      navigator.sendBeacon?.(
        '/api/term/close',
        new Blob([JSON.stringify(state.target)], { type: 'application/json' }),
      );
    }
    state.target = null;
    state.allowInput = false;
    ui.overlay.hidden = true;
    document.body.classList.remove('is-modal');
  }

  ui.close.addEventListener('click', close);
  ui.smaller.addEventListener('click', () => setFont(state.fontSize - 1));
  ui.bigger.addEventListener('click', () => setFont(state.fontSize + 1));
  function setNote() {
    if (!ui.input.checked) {
      ui.note.innerHTML = ui.input.disabled
        ? 'Read-only: input is disabled in <code>config.json</code>.'
        : 'Read-only mirror. Tick <strong>Send keys</strong> to type into this pane.';
      return;
    }
    ui.note.innerHTML = 'Click the screen to type — keys go to the live pane. '
      + 'Untick <strong>Send keys</strong> to make it read-only.';
  }

  ui.input.addEventListener('change', () => {
    state.allowInput = ui.input.checked;
    setNote();
    if (state.allowInput) state.xterm?.focus();
    else state.xterm?.blur();
  });
  ui.overlay.addEventListener('mousedown', (ev) => { if (ev.target === ui.overlay) close(); });
  document.addEventListener('keydown', (ev) => {
    // once keys are being forwarded, Escape belongs to the agent, not the modal
    if (ev.key === 'Escape' && !ui.overlay.hidden && !state.allowInput) close();
  });
  addEventListener('resize', () => { if (!ui.overlay.hidden) fit(); });

  return { open, close };
})();
