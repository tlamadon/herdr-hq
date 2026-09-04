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
  if (rendering) { queued = version; return; }
  rendering = true;
  try {
    const url = `/api/fs/file?${qs({ host, path, v: version })}`;
    const what = kind === 'image' ? await renderImage(url)
      : kind === 'markdown' ? await renderMarkdown(url)
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

/** Rewrite the relative links/images a README points at into /api/fs/file
    URLs on the same host, so figures and cross-links resolve. */
function rewriteRelative(container, version) {
  const resolve = (rel) => {
    const stack = dirName.split('/').filter(Boolean);
    for (const part of rel.split('/')) {
      if (part === '' || part === '.') continue;
      else if (part === '..') stack.pop();
      else stack.push(part);
    }
    return `/${stack.join('/')}`;
  };
  for (const img of container.querySelectorAll('img[src]')) {
    const src = img.getAttribute('src');
    if (/^(https?:|data:|\/)/i.test(src)) continue;
    img.src = `/api/fs/file?${qs({ host, path: resolve(src), v: version })}`;
  }
  for (const a of container.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href');
    if (/^(https?:|mailto:|#|\/)/i.test(href)) {
      if (/^https?:/i.test(href)) { a.target = '_blank'; a.rel = 'noopener'; }
      continue;
    }
    const resolved = resolve(href);
    a.href = MD_RE.test(resolved) || TEXT_RE.test(resolved)
      ? `/view?${qs({ host, path: resolved })}`
      : `/api/fs/file?${qs({ host, path: resolved })}`;
    a.target = '_blank';
  }
}

async function renderMarkdown(url) {
  const text = await fetchText(url);
  const body = el('div', { class: 'md-body' });
  // marked does not sanitize; these are remote files rendered on the
  // authenticated origin, so DOMPurify is non-negotiable.
  body.innerHTML = DOMPurify.sanitize(marked.parse(text));
  rewriteRelative(body, url.split('v=').pop());
  const next = el('div', { class: 'pageset' }, [body]);
  swap(next);
  return `${bytes(text.length)} markdown`;
}

async function renderText(url) {
  const text = await fetchText(url);
  const pre = el('pre', { class: 'code-body' });
  pre.textContent = text;
  const next = el('div', { class: 'pageset' }, [pre]);
  swap(next);
  return `${text.split('\n').length} lines`;
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
