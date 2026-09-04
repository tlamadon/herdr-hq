/* Shared markdown rendering: marked -> DOMPurify -> (optional) relative-link
   rewrite -> highlight.js on code blocks -> KaTeX auto-render for math.
   Used by the chat view (work.js) and the live viewer (viewer.js).

   Sanitizing comes FIRST and is non-negotiable: the input is remote files and
   agent output rendered on the authenticated origin. hljs and KaTeX only
   decorate the already-sanitized DOM with locally generated markup. */

const MD_TEXT_RE = /\.(md|markdown|txt|log|out|err|json|ya?ml|toml|csv|tsv|py|r|jl|js|ts|sh|zsh|bash|tex|bib|sty|cls|rst|org|nix|ini|cfg|conf|sql|lock|service)$/i;

function renderMarkdown(text, { host, baseDir, version } = {}) {
  const body = el('div', { class: 'md-body' });
  body.innerHTML = DOMPurify.sanitize(marked.parse(text));

  if (host && baseDir) rewriteRelativeLinks(body, host, baseDir, version);
  else {
    for (const a of body.querySelectorAll('a[href^="http"]')) {
      a.target = '_blank';
      a.rel = 'noopener';
    }
  }

  if (window.hljs) {
    for (const block of body.querySelectorAll('pre code')) {
      try { hljs.highlightElement(block); } catch (_) { /* unknown language */ }
    }
  }
  if (window.renderMathInElement) {
    try {
      renderMathInElement(body, {
        delimiters: [
          { left: '$$', right: '$$', display: true },
          { left: '\\[', right: '\\]', display: true },
          { left: '$', right: '$', display: false },
          { left: '\\(', right: '\\)', display: false },
        ],
        ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
        throwOnError: false,
      });
    } catch (_) { /* malformed math stays as source */ }
  }
  return body;
}

/** Point the relative links/images a README carries at /api/fs/file on the
    same host, so figures and cross-links resolve. */
function rewriteRelativeLinks(container, host, baseDir, version) {
  const resolve = (rel) => {
    const stack = baseDir.split('/').filter(Boolean);
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
    img.src = `/api/fs/file?${qs({ host, path: resolve(src), v: version || '' })}`;
  }
  for (const a of container.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href');
    if (/^(https?:|mailto:|#|\/)/i.test(href)) {
      if (/^https?:/i.test(href)) { a.target = '_blank'; a.rel = 'noopener'; }
      continue;
    }
    const resolved = resolve(href);
    a.href = MD_TEXT_RE.test(resolved) || /\.(pdf|png|jpe?g|gif|webp|svg)$/i.test(resolved)
      ? `/view?${qs({ host, path: resolved })}`
      : `/api/fs/file?${qs({ host, path: resolved })}`;
    a.target = '_blank';
  }
}
