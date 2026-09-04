/* The dashboard's terminal modal — chrome around the createMirror core
   (termcore.js), which owns the xterm, the frame painting and the key
   translation. */

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

  const state = { allowInput: false };

  const mirror = createMirror({
    mount: ui.mount,
    onStatus: (text, kind) => {
      ui.status.textContent = text;
      ui.status.dataset.state = kind;
    },
    onNote: (text) => { ui.note.textContent = text; },
    getAllowInput: () => state.allowInput,
  });

  function setNote() {
    if (!ui.input.checked) {
      ui.note.innerHTML = ui.input.disabled
        ? 'Read-only: input is disabled in the config.'
        : 'Read-only mirror. Tick <strong>Send keys</strong> to type into this pane.';
      return;
    }
    ui.note.innerHTML = 'Click the screen to type — keys go to the live pane. '
      + 'Untick <strong>Send keys</strong> to make it read-only.';
  }

  function open({ host, pane, title, subtitle, canInput = true }) {
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
    mirror.connect({ host, pane });
  }

  function close() {
    mirror.close();
    state.allowInput = false;
    ui.overlay.hidden = true;
    document.body.classList.remove('is-modal');
  }

  ui.close.addEventListener('click', close);
  ui.smaller.addEventListener('click', () => mirror.setFont(mirror.fontSize() - 1));
  ui.bigger.addEventListener('click', () => mirror.setFont(mirror.fontSize() + 1));

  ui.input.addEventListener('change', () => {
    state.allowInput = ui.input.checked;
    setNote();
    if (state.allowInput) mirror.focus();
    else mirror.blur();
  });
  ui.overlay.addEventListener('mousedown', (ev) => { if (ev.target === ui.overlay) close(); });
  document.addEventListener('keydown', (ev) => {
    // once keys are being forwarded, Escape belongs to the agent, not the modal
    if (ev.key === 'Escape' && !ui.overlay.hidden && !state.allowInput) close();
  });
  addEventListener('resize', () => { if (!ui.overlay.hidden) mirror.fit(); });

  return { open, close };
})();
