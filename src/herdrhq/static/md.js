/* Shared markdown rendering: marked -> DOMPurify -> (optional) relative-link
   rewrite -> highlight.js on code blocks -> KaTeX auto-render for math.
   Used by the chat view (work.js) and the live viewer (viewer.js).

   Sanitizing comes FIRST and is non-negotiable: the input is remote files and
   agent output rendered on the authenticated origin. hljs and KaTeX only
   decorate the already-sanitized DOM with locally generated markup. */

const MD_TEXT_RE = /\.(md|markdown|txt|log|out|err|json|ya?ml|toml|csv|tsv|py|r|jl|js|ts|sh|zsh|bash|tex|bib|sty|cls|rst|org|nix|ini|cfg|conf|sql|lock|service)$/i;

// The common highlight.js build ships without LaTeX; register a compact grammar
// so .tex/.sty/.bib files (and ```latex blocks) get real highlighting: commands,
// %-comments and $…$ math. Runs once, wherever md.js loads (viewer + chat).
if (window.hljs && !window.hljs.getLanguage('latex')) {
  window.hljs.registerLanguage('latex', (hljs) => {
    const COMMAND = { className: 'keyword', begin: /\\[a-zA-Z@]+\*?/ };
    return {
      name: 'LaTeX',
      aliases: ['tex'],
      contains: [
        hljs.COMMENT('%', '$'),
        { className: 'built_in', begin: /\\(begin|end)\b/, end: /\}/, keywords: '', contains: [{ className: 'string', begin: /\{/, end: /\}/, excludeBegin: true, excludeEnd: true }] },
        COMMAND,
        { className: 'string', begin: /\$\$/, end: /\$\$/, contains: [COMMAND] },
        { className: 'string', begin: /\$/, end: /\$/, contains: [COMMAND], illegal: /\n\s*\n/ },
      ],
    };
  });
}

/** Math must not pass through marked: it reads \[, \(, \{ as character
    escapes and strips the backslashes, so KaTeX gets corrupted input (or no
    delimiters at all — GPT-family agents write \[ … \] / \( … \)). Swap the
    unambiguous math spans for atomic placeholder tokens before parsing;
    restoreMath puts the pristine source back into the rendered DOM, where
    the KaTeX auto-render then finds it. Single-$ spans are left alone: they
    are ambiguous ($HOME in inline code) and auto-render already skips code
    tags. Placeholders use private-use characters no real text contains. */
function shieldMath(text) {
  const spans = [];
  const shield = (span) => { spans.push(span); return `${spans.length - 1}`; };
  const shielded = String(text).split(/(```[\s\S]*?(?:```|$))/).map((seg, i) => {
    if (i % 2) return seg; // inside a fence
    return seg
      .replace(/\\\[([\s\S]+?)\\\]/g, (_, b) => shield(`$$${b}$$`))
      .replace(/\\\((.+?)\\\)/g, (_, b) => shield(`$${b}$`))
      .replace(/\$\$([\s\S]+?)\$\$/g, (m) => shield(m));
  }).join('');
  return { shielded, spans };
}

function restoreMath(root, spans) {
  if (!spans.length) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) {
    if (walker.currentNode.nodeValue.includes('')) nodes.push(walker.currentNode);
  }
  for (const node of nodes) {
    node.nodeValue = node.nodeValue.replace(/(\d+)/g, (_, i) => spans[+i] ?? '');
  }
}

function renderMarkdown(text, { host, baseDir, version } = {}) {
  const body = el('div', { class: 'md-body' });
  const { shielded, spans } = shieldMath(text);
  body.innerHTML = DOMPurify.sanitize(marked.parse(shielded));
  restoreMath(body, spans);

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
