/* herdr HQ — live viewer: PDF, image, markdown and text files that re-render
   in place when the remote file settles on a new (mtime, size). */

const params = new URLSearchParams(location.search);
const host = params.get('host');
const path = params.get('path');
const scroll = document.getElementById('scroll');
const meta = document.getElementById('meta');
const status = document.getElementById('status');
const statusLabel = document.getElementById('statusLabel');

const base = (path || '').split('/').pop();
document.title = `${base} — herdr HQ`;
document.getElementById('fname').textContent = base;
document.getElementById('fname').title = path || '';
document.getElementById('hostchip').textContent = host || '';
const dirName = (path || '').replace(/\/[^/]+$/, '') || '/';
document.getElementById('folderLink').href = `/browse?${qs({ host, path: dirName })}`;

initTheme();

// Record this view in the shared history (the browse page renders it as
// "Recent views" so closed documents can be reopened).
try {
  const KEY = 'herdr-hq.viewhist';
  const hist = JSON.parse(localStorage.getItem(KEY) || '[]')
    .filter((h) => !(h.host === host && h.path === path));
  hist.unshift({ host, path, t: Date.now() });
  localStorage.setItem(KEY, JSON.stringify(hist.slice(0, 20)));
} catch (_) { /* private mode */ }

const IMAGE_RE = /\.(png|jpe?g|gif|webp|svg|bmp|avif|ico)$/i;
const MD_RE = /\.(md|markdown)$/i;
const TEXT_RE = /\.(txt|log|out|err|json|ya?ml|toml|csv|tsv|py|r|jl|js|ts|sh|zsh|bash|tex|bib|sty|cls|rst|org|nix|ini|cfg|conf|sql|lock|service)$/i;
const kind = IMAGE_RE.test(base) ? 'image'
  : MD_RE.test(base) ? 'markdown'
  : TEXT_RE.test(base) || !base.includes('.') ? 'text'
  : 'pdf';

const pdfjsLib = kind === 'pdf'
  ? await import('/static/vendor/pdfjs/pdf.min.mjs')
  : null;
if (pdfjsLib) {
  pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.min.mjs';
}

const MAX_TEXT = 2 * 1024 * 1024;

let rendering = false;
let queued = null;

// editor state (CodeMirror). Only text/markdown files are editable.
const editable = (kind === 'text' || kind === 'markdown') && !!window.CodeMirror;
let editing = false;
let cm = null;
let baseText = '';
let previewEl = null;

function setStatus(state, label) {
  status.dataset.state = state;
  status.title = label;
  statusLabel.textContent = label;
}

// PDF hyperlinks are annotations, not page graphics; overlay each canvas
// with real <a> elements so they stay clickable.
async function addLinks(doc, page, vp, div, pageDivs) {
  for (const a of await page.getAnnotations({ intent: 'display' })) {
    if (a.subtype !== 'Link' || (!a.url && !a.dest)) continue;
    const [x1, y1, x2, y2] = pdfjsLib.Util.normalizeRect(vp.convertToViewportRectangle(a.rect));
    const link = document.createElement('a');
    link.className = 'link';
    link.style.left = `${x1}px`;
    link.style.top = `${y1}px`;
    link.style.width = `${x2 - x1}px`;
    link.style.height = `${y2 - y1}px`;
    if (a.url) {
      link.href = a.url;
      link.target = '_blank';
      link.rel = 'noopener';
      link.title = a.url;
    } else {
      link.href = '#';
      link.addEventListener('click', async (ev) => {
        ev.preventDefault();
        const dest = typeof a.dest === 'string' ? await doc.getDestination(a.dest) : a.dest;
        if (!dest) return;
        const idx = await doc.getPageIndex(dest[0]);
        pageDivs[idx]?.scrollIntoView({ behavior: 'smooth' });
      });
    }
    div.appendChild(link);
  }
}

async function render(version) {
  if (editing) return;  // the editor owns the surface; don't re-render under it
  if (rendering) { queued = version; return; }
  rendering = true;
  try {
    const url = `/api/fs/file?${qs({ host, path, v: version })}`;
    const what = kind === 'image' ? await renderImage(url)
      : kind === 'markdown' ? await renderMarkdownFile(url)
      : kind === 'text' ? await renderText(url)
      : await renderPdf(url);
    meta.textContent = `${what} · updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    // Keep the last good render; a half-written or unreadable file just
    // reports itself in the header and we wait for the next change event.
    meta.textContent = `refresh failed: ${e.message || e}`;
  } finally {
    rendering = false;
    if (queued !== null) { const v = queued; queued = null; render(v); }
  }
}

// Render into a fresh container, then swap it in: no white flash, and the
// scroll offset carries over.
function swap(next) {
  const y = scroll.scrollTop;
  document.querySelector('.pageset').replaceWith(next);
  scroll.scrollTop = y;
}

async function renderImage(url) {
  const img = new Image();
  await new Promise((ok, fail) => {
    img.onload = ok;
    img.onerror = () => fail(new Error('could not load image'));
    img.src = url;
  });
  const next = el('div', { class: 'pageset' }, [img]);
  swap(next);
  return `${img.naturalWidth}×${img.naturalHeight}`;
}

async function fetchText(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const len = Number(r.headers.get('content-length') || 0);
  if (len > MAX_TEXT) {
    throw new Error(`file is ${bytes(len)} — too large to render; use download instead`);
  }
  return r.text();
}

async function renderMarkdownFile(url) {
  const text = await fetchText(url);
  // the shared pipeline (md.js): marked -> DOMPurify -> link rewrite -> hljs -> KaTeX
  const body = renderMarkdown(text, { host, baseDir: dirName, version: url.split('v=').pop() });
  const next = el('div', { class: 'pageset' }, [body]);
  swap(next);
  return `${bytes(text.length)} markdown`;
}

// file extension -> highlight.js language id (only for langs the build carries;
// anything unmapped, e.g. LaTeX in a common build, falls back to auto-detect)
const EXT_LANG = {
  py: 'python', r: 'r', jl: 'julia', js: 'javascript', mjs: 'javascript', ts: 'typescript',
  sh: 'bash', zsh: 'bash', bash: 'bash', json: 'json', yaml: 'yaml', yml: 'yaml',
  toml: 'ini', ini: 'ini', cfg: 'ini', conf: 'ini', service: 'ini',
  tex: 'latex', bib: 'latex', sty: 'latex', cls: 'latex',
  rs: 'rust', go: 'go', c: 'c', h: 'c', cpp: 'cpp', hpp: 'cpp', java: 'java',
  rb: 'ruby', lua: 'lua', sql: 'sql', nix: 'nix', css: 'css', scss: 'scss',
  html: 'xml', xml: 'xml', md: 'markdown', diff: 'diff', patch: 'diff',
};

async function renderText(url) {
  const raw = await fetchText(url);
  const src = raw.replace(/\n$/, '');  // drop one trailing newline — no phantom last line
  const ext = (base.split('.').pop() || '').toLowerCase();
  const want = EXT_LANG[ext];
  let html = null;
  let lang = 'text';
  try {
    if (want && window.hljs?.getLanguage(want)) {
      html = hljs.highlight(src, { language: want, ignoreIllegals: true }).value;
      lang = want;
    } else if (window.hljs) {
      const auto = hljs.highlightAuto(src);
      html = auto.value;
      lang = auto.language || 'text';
    }
  } catch (_) { html = null; lang = 'text'; }

  const nLines = src.split('\n').length;
  const gutter = el('pre', { class: 'code-gutter', 'aria-hidden': 'true' });
  gutter.textContent = Array.from({ length: nLines }, (_, i) => i + 1).join('\n');
  const code = el('code', { class: `hljs language-${lang}` });
  if (html != null) code.innerHTML = html; else code.textContent = src;  // hljs output is escaped
  const view = el('div', { class: 'code-view' }, [gutter, el('pre', { class: 'code-body' }, [code])]);
  swap(el('div', { class: 'pageset' }, [view]));
  return `${nLines} lines · ${lang}`;
}

async function renderPdf(url) {
  const doc = await pdfjsLib.getDocument(url).promise;
  const next = el('div', { class: 'pageset' });
  const width = Math.min(scroll.clientWidth - 36, 1000);
  const dpr = window.devicePixelRatio || 1;
  const pageDivs = [];
  for (let i = 1; i <= doc.numPages; i++) {
    const page = await doc.getPage(i);
    const v1 = page.getViewport({ scale: 1 });
    const scale = width / v1.width;
    const vp = page.getViewport({ scale: scale * dpr });
    const cssVp = page.getViewport({ scale });
    const div = el('div', { class: 'page' });
    const c = document.createElement('canvas');
    c.width = vp.width;
    c.height = vp.height;
    c.style.width = `${cssVp.width}px`;
    c.style.height = `${cssVp.height}px`;
    await page.render({ canvasContext: c.getContext('2d'), viewport: vp }).promise;
    div.appendChild(c);
    await addLinks(doc, page, cssVp, div, pageDivs);
    next.appendChild(div);
    pageDivs.push(div);
  }
  swap(next);
  return `${doc.numPages} page${doc.numPages === 1 ? '' : 's'}`;
}

/* ------------------------------------------------- editor (CodeMirror) */

const actions = document.getElementById('editActions');

function actionBtn(label, onClick, cls) {
  const b = el('button', { class: `viewer-btn${cls ? ' ' + cls : ''}`, type: 'button' });
  b.textContent = label;
  b.addEventListener('click', onClick);
  return b;
}

function renderActions() {
  if (!actions) return;
  actions.replaceChildren();
  if (!editable) return;
  if (!editing) {
    actions.append(actionBtn('✎ Edit', enterEdit, 'edit'));
    return;
  }
  if (kind === 'markdown') {
    actions.append(actionBtn(previewEl ? '◂ Edit' : 'Preview ▸', togglePreview));
  }
  actions.append(actionBtn('Save', save, 'primary'));
  actions.append(actionBtn('Cancel', cancel));
}

function modeForFile() {
  const info = (CodeMirror.findModeByFileName && CodeMirror.findModeByFileName(base))
    || (CodeMirror.findModeByExtension && CodeMirror.findModeByExtension((base.split('.').pop() || '').toLowerCase()));
  return info ? (info.mime || info.mode) : (kind === 'markdown' ? 'markdown' : null);
}

function isDirty() { return !!cm && cm.getValue() !== baseText; }
function updateDirty() { meta.textContent = isDirty() ? '● unsaved changes' : 'saved'; }

async function enterEdit() {
  let text;
  try {
    text = await fetchText(`/api/fs/file?${qs({ host, path, v: Date.now() })}`);
  } catch (e) {
    setStatus('error', `can't open for edit: ${e.message || e}`);
    return;
  }
  if (es) { es.close(); es = null; }         // pause live-watch while editing
  clearInterval(pollTimer); pollTimer = null;
  editing = true; previewEl = null; baseText = text;
  setStatus('paused', 'editing');
  document.body.classList.add('editing');
  const wrap = el('div', { class: 'pageset cm-wrap' });  // keep .pageset so swap() can replace it on exit
  swap(wrap);
  cm = CodeMirror(wrap, {
    value: text,
    mode: modeForFile(),
    lineNumbers: true,
    lineWrapping: false,
    matchBrackets: true,
    autoCloseBrackets: true,
    styleActiveLine: true,
    indentUnit: 2,
    tabSize: 2,
    theme: 'herdr',
    extraKeys: {
      'Cmd-S': save,
      'Ctrl-S': save,
      Tab: (i) => i.execCommand('insertSoftTab'),
    },
  });
  cm.on('change', updateDirty);
  cm.focus();
  renderActions();
  updateDirty();
}

async function save() {
  if (!cm) return;
  const content = cm.getValue();
  setStatus('paused', 'saving…');
  try {
    const r = await fetch('/api/fs/write', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host, path, content }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok || !body.ok) throw new Error(body.error || `save failed (${r.status})`);
    baseText = content;
    setStatus('ok', 'editing');
    meta.textContent = `saved · ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    setStatus('error', 'save failed');
    meta.textContent = `save failed: ${e.message || e}`;
  }
}

function cancel() {
  if (isDirty() && !window.confirm('Discard unsaved changes?')) return;
  editing = false;
  cm = null;
  previewEl = null;
  document.body.classList.remove('editing');
  renderActions();
  connect();                              // resume the live-watch
  render(`edit-exit-${Date.now()}`);
}

// markdown-only: flip between the editor and a rendered preview of the buffer
function togglePreview() {
  if (!cm) return;
  const wrap = document.querySelector('.cm-wrap');
  if (!previewEl) {
    previewEl = renderMarkdown(cm.getValue(), { host, baseDir: dirName });
    previewEl.classList.add('cm-preview');
    cm.getWrapperElement().style.display = 'none';
    wrap.append(previewEl);
  } else {
    previewEl.remove();
    previewEl = null;
    cm.getWrapperElement().style.display = '';
    cm.refresh();
    cm.focus();
  }
  renderActions();
}

// warn before leaving with unsaved edits
window.addEventListener('beforeunload', (e) => {
  if (isDirty()) { e.preventDefault(); e.returnValue = ''; }
});

renderActions();

/* ------------------------------------------------- change stream */

let es = null;
let pollTimer = null;
let sawOpen = false;

function connect() {
  es = new EventSource(`/api/fs/watch?${qs({ host, path })}`);
  es.onopen = () => {
    sawOpen = true;
    clearInterval(pollTimer);
    pollTimer = null;
    setStatus('ok', 'watching');
  };
  es.onerror = () => {
    setStatus('error', 'reconnecting…');
    // If the stream never comes up (connection-per-origin budget spent,
    // proxy in the way), fall back to blind polling so the view still works.
    if (!pollTimer) {
      pollTimer = setInterval(() => {
        setStatus('paused', 'polling');
        render(`poll-${Date.now()}`);
      }, 5000);
    }
  };
  es.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    if (d.type === 'bye') {          // detached from the Live views panel
      es.close();
      window.close();                // works when this tab was opened by the UI
      setStatus('error', 'detached');
      meta.textContent = 'live view detached — safe to close this tab';
      return;
    }
    render(`${d.mtime}-${d.size || 0}`);
  };
}

// A parked tab gives its SSE slot back (browsers cap connections per origin);
// on return it reconnects and re-renders in case the file moved on.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    if (es) { es.close(); es = null; }
    clearInterval(pollTimer);
    pollTimer = null;
    setStatus('paused', 'paused (tab hidden)');
  } else if (!es) {
    connect();
    render(`resume-${Date.now()}`);
  }
});

connect();
setTimeout(() => {
  if (!sawOpen && !pollTimer) {
    setStatus('paused', 'polling');
    pollTimer = setInterval(() => render(`poll-${Date.now()}`), 5000);
    render(`poll-${Date.now()}`);
  }
}, 8000);

// Images and text scale/reflow in CSS; only PDF pages need a re-render on resize.
let resizeT;
if (kind === 'pdf') {
  window.addEventListener('resize', () => {
    clearTimeout(resizeT);
    resizeT = setTimeout(() => render(`resize-${Date.now()}`), 300);
  });
}
