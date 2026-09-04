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
    source: null,
    target: null,      // {host, pane}
    fontSize: 13,
    autoFit: true,     // pick the font size that fills the box; A+/A- overrides
    pending: [],
    flushTimer: null,
    fitTimer: null,
    warnedKeys: false,
  };

  const setStatus = (text, kind) => onStatus?.(text, kind);

  function ensureTerm() {
    if (state.xterm) return state.xterm;
    state.xterm = new Terminal({
      fontSize: state.fontSize,
      fontFamily: '"JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, "Cascadia Code", Menlo, Monaco, monospace',
      fontWeight: 400,
      fontWeightBold: 600,
      lineHeight: 1.0,
      letterSpacing: 0,
      theme: termTheme(),
      cursorBlink: false,
      convertEol: false,
      scrollback: 0,          // every frame is a full screen; nothing to scroll back to
      drawBoldTextInBrightColors: true,
      minimumContrastRatio: 1,  // trust the agent's colours; don't auto-nudge
      disableStdin: false,
      allowProposedApi: true,
    });
    state.xterm.open(mount);
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

  /** Fill the available width by choosing the font size, not by CSS-scaling:
      text stays crisp, the box height is honest, and reflows don't jump.
      The target font comes straight from the measured width-per-column —
      one shot, no iteration, so racing frames can't thrash it. A transform
      scale-down remains only as a last resort at the minimum font size.
      Manual A+/A- turns auto-fitting off until the next pane. */
  const MIN_AUTO_FONT = 10;  // below this the pane scrolls rather than shrinks

  function boxWidth() {
    // the scroll container: unlike wrapper divs, its width never stretches
    // with overflowing content, so this measurement is trustworthy
    const box = mount.parentElement;
    if (!box) return 0;
    const pad = getComputedStyle(box);
    return box.clientWidth - parseFloat(pad.paddingLeft) - parseFloat(pad.paddingRight);
  }

  /** True on-screen width of the rendered grid. NOT mount.scrollWidth — the
      canvas renderer's backing store carries a device-pixel width that leaks
      into scrollWidth and reads far too wide. The .xterm-screen element is
      the honest CSS width. */
  function gridWidth() {
    const screen = mount.querySelector('.xterm-screen');
    return screen ? screen.clientWidth : 0;
  }

  /** Fill the width by choosing the font size for the pane's fixed column
      count (herdr dictates cols; we can't reflow them). Font scales linearly
      with rendered width, so one measurement gives the target directly — no
      transform, no iteration. A very wide pane (many columns) bottoms out at
      a readable floor and scrolls horizontally instead of shrinking to a
      blur. Manual A+/A- turns auto-fitting off until the next pane. */
  function fit() {
    if (!state.autoFit || !state.xterm) return;
    const avail = boxWidth();
    const w = gridWidth();
    if (!w || avail <= 0) return;
    const want = Math.min(22, Math.max(MIN_AUTO_FONT, Math.round(state.fontSize * avail / w)));
    if (want !== state.fontSize) {
      state.fontSize = want;
      state.xterm.options.fontSize = want;
    }
  }

  // layout transients (panels unhiding, columns settling, sidebars folding)
  // resize the container without a window resize; refit whenever it moves
  if (typeof ResizeObserver !== 'undefined' && mount.parentElement) {
    new ResizeObserver(scheduleFit).observe(mount.parentElement);
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

  function connect(target) {
    if (state.source) state.source.close();
    state.target = { host: target.host, pane: target.pane };
    state.autoFit = true;  // a fresh pane goes back to filling the box
    const t = ensureTerm();
    t.options.theme = termTheme();
    t.write('\x1b[2J\x1b[H\x1b[?25l');
    const qs = new URLSearchParams(state.target).toString();
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
    state.autoFit = false;  // the user chose; stop second-guessing them
    state.fontSize = Math.min(22, Math.max(8, size));
    if (state.xterm) {
      state.xterm.options.fontSize = state.fontSize;
      scheduleFit();
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
