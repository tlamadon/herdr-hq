/* Live pane mirror in xterm.js — the embeddable core.
 *
 * herdr exposes no raw output stream, so the server sends whole visible screens
 * (ANSI intact) whenever they change. Painting a frame is therefore a cursor-home
 * redraw with erase-to-end-of-line per row — the same trick `watch` uses, which
 * repaints without the flicker of a full clear. The local cursor is hidden: the
 * real one lives in the pane, and we're never told where it is.
 *
 * Used by the dashboard's modal (term.js) and the workspace view (work.js):
 *
 *   const m = createMirror({ mount, onStatus, onNote, getAllowInput });
 *   m.connect({ host, pane });   // reconnects freely; one stream at a time
 *   m.close();                   // stream + server session torn down
 */

/* herdr takes named keys, not raw bytes — a literal "\x15" shows up in the pane
   as "^U". So xterm's byte stream is split into printable runs and key names.
   Names verified against herdr 0.8.2: it has no Delete/Home/End/PageUp/PageDown. */

const TERM_MULTI_KEYS = {
  '\x1b[A': 'Up', '\x1b[B': 'Down', '\x1b[C': 'Right', '\x1b[D': 'Left',
  '\x1bOA': 'Up', '\x1bOB': 'Down', '\x1bOC': 'Right', '\x1bOD': 'Left',
  '\x1b[Z': 'Shift+Tab',
  '\x1b[1;5A': 'Ctrl+Up', '\x1b[1;5B': 'Ctrl+Down',
  '\x1b[1;5C': 'Ctrl+Right', '\x1b[1;5D': 'Ctrl+Left',
  '\x1b[1;2A': 'Shift+Up', '\x1b[1;2B': 'Shift+Down',
  '\x1b[1;3C': 'Alt+Right', '\x1b[1;3D': 'Alt+Left',
  '\x1b\x7f': 'Alt+Backspace',
};
const TERM_SINGLE_KEYS = {
  '\r': 'Enter', '\n': 'Enter', '\t': 'Tab', '\x7f': 'Backspace', '\b': 'Backspace',
};
const TERM_MULTI_ORDER = Object.keys(TERM_MULTI_KEYS).sort((a, b) => b.length - a.length);
const TERM_CSI_RE = /^\x1b(\[[0-9;]*[ -/]*[@-~]|O.)/;

function termToOps(data) {
  const ops = [];
  let text = '';
  const pushText = () => { if (text) { ops.push({ text }); text = ''; } };

  let i = 0;
  outer: while (i < data.length) {
    for (const seq of TERM_MULTI_ORDER) {
      if (data.startsWith(seq, i)) {
        pushText();
        ops.push({ key: TERM_MULTI_KEYS[seq] });
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
        const m = TERM_CSI_RE.exec(data.slice(i));
        pushText();
        ops.push(m ? { unsupported: m[0] } : { key: 'Escape' });
        i += m ? m[0].length : 1;
      }
      continue;
    }
    if (TERM_SINGLE_KEYS[ch]) {
      pushText();
      ops.push({ key: TERM_SINGLE_KEYS[ch] });
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

// The terminal is always dark, independent of the app theme: herdr's panes
// (like any TUI) author their ANSI colours — including background blocks in
// diffs and menus — for a dark terminal, so a light background renders them
// wrong. This is also the conventional terminal feel.
function termTheme() {
  return TERM_THEME_DARK;
}

// Full 16-colour ANSI palettes — without these, every coloured byte an agent
// emits falls back to xterm's stock colours, which clash with our surfaces.
// Warm-neutral dark to match the app; a clean high-contrast light set.
const TERM_THEME_DARK = {
  background: '#161615', foreground: '#e6e5dd',
  cursor: '#e6e5dd', cursorAccent: '#161615',
  selectionBackground: 'rgba(57,135,229,0.32)',
  black: '#2c2c2a', red: '#f7768e', green: '#9ece6a', yellow: '#e0af68',
  blue: '#7aa2f7', magenta: '#bb9af7', cyan: '#7dcfff', white: '#c8c7bd',
  brightBlack: '#585851', brightRed: '#ff8faa', brightGreen: '#b6e389',
  brightYellow: '#f2c67f', brightBlue: '#9db8ff', brightMagenta: '#cfb4ff',
  brightCyan: '#a0e0ff', brightWhite: '#f4f3ec',
};
const TERM_THEME_LIGHT = {
  background: '#fbfbf9', foreground: '#25251f',
  cursor: '#25251f', cursorAccent: '#fbfbf9',
  selectionBackground: 'rgba(42,120,214,0.22)',
  black: '#2e2e2b', red: '#c0392b', green: '#3a8c3a', yellow: '#a6791f',
  blue: '#2a78d6', magenta: '#8250b4', cyan: '#2a8c8c', white: '#c8c7bd',
  brightBlack: '#6a6a63', brightRed: '#d84a3a', brightGreen: '#469a46',
  brightYellow: '#bd8a25', brightBlue: '#3987e5', brightMagenta: '#9660c8',
  brightCyan: '#3a9c9c', brightWhite: '#25251f',
};

function createMirror({ mount, onStatus, onNote, getAllowInput }) {
  const state = {
    xterm: null,
    fit: null,         // xterm FitAddon: sizes the grid to the container
    source: null,
    target: null,      // {host, pane}
    reqCols: 0,        // size the observe stream was requested at
    reqRows: 0,
    fontSize: 13,
    pending: [],
    flushTimer: null,
    fitTimer: null,
    warnedKeys: false,
    decoder: new TextDecoder(),
  };

  const setStatus = (text, kind) => onStatus?.(text, kind);

  function ensureTerm() {
    if (state.xterm) return state.xterm;
    state.xterm = new Terminal({
      fontSize: state.fontSize,
      // "Nerd Symbols" (bundled, icons-only) sits late so text and box-drawing
      // stay in the primary/system mono; only true icon glyphs fall through to it.
      fontFamily: '"JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, "Cascadia Code", Menlo, Monaco, "Nerd Symbols", monospace',
      fontWeight: 400,
      fontWeightBold: 600,
      lineHeight: 1.0,
      letterSpacing: 0,
      theme: termTheme(),
      cursorBlink: false,
      convertEol: false,
      scrollback: 0,          // herdr streams the visible screen, not a scroll log
      drawBoldTextInBrightColors: true,
      minimumContrastRatio: 1,  // trust the agent's colours; don't auto-nudge
      disableStdin: false,
      allowProposedApi: true,
    });
    state.fit = new FitAddon.FitAddon();
    state.xterm.loadAddon(state.fit);
    state.xterm.open(mount);
    state.xterm.onData(onKey);
    attachWheel();
    return state.xterm;
  }

  // Each wheel notch is only ~3 lines, and every batch is a network round-trip
  // to herdr, so 1:1 scrolling crawls. Amplify the line count so a notch moves
  // a meaningful chunk — this is the knob to turn if scrolling feels off.
  const SCROLL_SPEED = 3;

  /** xterm keeps no scrollback (herdr owns the history), so the wheel scrolls
      the pane's own viewport through the control stream. We use xterm's own
      wheel-intercept hook (a plain DOM listener gets swallowed by xterm) and
      return false so it doesn't try to scroll its empty local buffer. Deltas
      are coalesced into one scroll request. */
  function attachWheel() {
    const t = state.xterm;
    if (!t || !t.attachCustomWheelEventHandler) return;
    let accum = 0;
    let timer = null;
    t.attachCustomWheelEventHandler((ev) => {
      if (!state.target) return true;  // not connected — let xterm have it
      const unit = ev.deltaMode === 1 ? 1
        : ev.deltaMode === 2 ? (t.rows || 24)
        : 1 / 24;  // pixels → rough lines
      accum += ev.deltaY * unit;
      if (!timer) {
        timer = setTimeout(() => {
          timer = null;
          const dir = accum < 0 ? 'up' : 'down';
          const lines = Math.max(1, Math.min(200, Math.round(Math.abs(accum) * SCROLL_SPEED)));
          accum = 0;
          sendScroll(dir, lines);
        }, 24);
      }
      return false;  // handled here; don't scroll the empty local buffer
    });
  }

  async function sendScroll(dir, lines) {
    if (!state.target) return;
    try {
      await fetch('/api/term/scroll', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...state.target, dir, lines }),
      });
    } catch (_) { /* scrolling back is best-effort */ }
  }

  /** Size the grid to the container (FitAddon), and if the fitted column/row
      count changed materially, re-request the control stream at the new size —
      herdr resizes the pane to whatever size we ask for, so the grid always
      fills the panel exactly, no CSS scaling, no clipping. */
  function fit() {
    if (!state.xterm || !state.fit) return;
    try { state.fit.fit(); } catch (_) { return; }
    const { cols, rows } = state.xterm;
    if (!cols || !rows) return;
    if (state.target && (Math.abs(cols - state.reqCols) > 1 || Math.abs(rows - state.reqRows) > 1)) {
      openStream(cols, rows);
    }
  }

  function scheduleFit() {
    clearTimeout(state.fitTimer);
    state.fitTimer = setTimeout(() => requestAnimationFrame(fit), 80);
  }

  // the container resizes without a window resize (panels unhiding, sidebar
  // folding); refit — and re-request at the new size — whenever it moves
  if (typeof ResizeObserver !== 'undefined' && mount.parentElement) {
    new ResizeObserver(scheduleFit).observe(mount.parentElement);
  }

  function writeFrame(msg) {
    const t = state.xterm;
    if (!t) return;
    if (msg.cols && (msg.cols !== t.cols || msg.rows !== t.rows)) {
      t.resize(msg.cols, msg.rows);
    }
    if (msg.full) t.reset();  // a full repaint starts from a clean slate
    if (msg.bytes) {
      t.write(Uint8Array.from(atob(msg.bytes), (c) => c.charCodeAt(0)));
    }
  }

  function onKey(data) {
    if (!getAllowInput?.() || !state.target) return;
    state.pending.push(...termToOps(data));
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
      onNote?.('That key has no herdr equivalent (Delete, Home, End and Page keys are unsupported).');
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

  /** (Re)open the observe stream at cols×rows. Called on connect and whenever
      the fitted size changes; the old stream is replaced. */
  function openStream(cols, rows) {
    if (!state.target) return;
    if (state.source) state.source.close();
    state.reqCols = cols;
    state.reqRows = rows;
    const qs = new URLSearchParams({ ...state.target, cols, rows }).toString();
    const src = new EventSource(`/api/term/stream?${qs}`);
    state.source = src;
    setStatus('connecting…', 'connecting');

    src.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.t === 'frame') {
        setStatus(`live · ${msg.cols}×${msg.rows}`, 'ok');
        writeFrame(msg);
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

  function connect(target) {
    state.target = { host: target.host, pane: target.pane };
    const t = ensureTerm();
    t.options.theme = termTheme();
    t.reset();
    // size to the container first, then open the stream at that size
    try { state.fit?.fit(); } catch (_) { /* not laid out yet */ }
    openStream(t.cols || 200, t.rows || 50);
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
  }

  function setFont(size) {
    state.fontSize = Math.min(22, Math.max(8, size));
    if (state.xterm) {
      state.xterm.options.fontSize = state.fontSize;
      scheduleFit();  // a different font means a different fit → re-request size
    }
  }

  return {
    connect,
    close,
    fit,
    setFont,
    fontSize: () => state.fontSize,
    focus: () => state.xterm?.focus(),
    blur: () => state.xterm?.blur(),
    target: () => state.target,
  };
}
