"""Vanilla JS (~3 KB): hash routing, pipeline picker, step panel, run filter,
auto-refresh that keeps the current view, selection and scroll position."""

SCRIPT = r"""
(() => {
  const REFRESH_MS = 60000;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const sel = (attr, v) => `[${attr}="${CSS.escape(v)}"]`;
  const keep = {
    get: (k, local) => { try { return (local ? localStorage : sessionStorage).getItem(k); } catch (e) { return null; } },
    set: (k, v, local) => { try { (local ? localStorage : sessionStorage).setItem(k, v); } catch (e) {} },
  };
  let restoring = true;

  function route() {
    const [view, arg] = (location.hash.slice(1) || 'overview').split('/');
    const name = $$('.view').some(v => v.dataset.view === view) ? view : 'overview';
    $$('.view').forEach(v => v.classList.toggle('active', v.dataset.view === name));
    $$('[data-tab]').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    if (name === 'runs') showRun(arg ? decodeURIComponent(arg) : null);
    if (name === 'pipelines') selectPipe(arg || keep.get('pipe') || 'v-all');
    if (!restoring) window.scrollTo(0, 0);
  }

  function showRun(id) {
    const found = id && $(sel('data-run', id));
    $$('[data-run]').forEach(r => { r.hidden = r !== found; });
    $('#run-list').hidden = !!found;
  }

  function selectPipe(id) {
    if (!$(sel('data-pipe-view', id))) id = 'v-all';
    $$('[data-pipe-view]').forEach(v => { v.hidden = v.dataset.pipeView !== id; });
    $$('[data-pipe]').forEach(b => b.classList.toggle('active', b.dataset.pipe === id));
    keep.set('pipe', id);
    const node = keep.get('node');
    if (node) selectNode(node, false);
  }

  function selectNode(key, remember = true) {
    const tpl = $(sel('data-node', key));
    const panel = $('#node-panel');
    if (!tpl || !panel) return;
    panel.replaceChildren(tpl.content.cloneNode(true));
    panel.classList.remove('filled'); void panel.offsetWidth; panel.classList.add('filled');
    const view = $('[data-pipe-view]:not([hidden])');
    if (view) {
      const svg = $('svg', view);
      $$('.node', view).forEach(n => n.classList.remove('selected', 'on-path'));
      const hit = $('.node' + sel('data-key', key), view);
      if (svg) svg.classList.toggle('focused', !!hit);
      if (hit) {
        hit.classList.add('selected');
        (hit.dataset.path || '').split(' ').forEach(k => {
          const n = k && $('.node' + sel('data-key', k), view);
          if (n) n.classList.add('on-path');
        });
      }
    }
    if (remember) keep.set('node', key);
  }

  function filterRuns() {
    const q = ($('#run-search')?.value || '').toLowerCase();
    const st = $('#run-state')?.value || '';
    $$('#run-list tbody tr').forEach(r => {
      r.hidden = (st && r.dataset.state !== st) || (q && !r.dataset.text.includes(q));
    });
  }

  document.addEventListener('click', e => {
    const node = e.target.closest('.node[data-key]');
    if (node && !node.classList.contains('ds')) return selectNode(node.dataset.key);
    const pipe = e.target.closest('[data-pipe]');
    if (pipe) { location.hash = 'pipelines/' + pipe.dataset.pipe; return; }
    const metric = e.target.closest('[data-filter-state]');
    if (metric && $('#run-state')) { $('#run-state').value = metric.dataset.filterState; setTimeout(filterRuns); }
    const row = e.target.closest('tr[data-href]');
    if (row && !e.target.closest('a')) location.hash = row.dataset.href;
  });
  document.addEventListener('keydown', e => {
    const node = e.key === 'Enter' && e.target.closest && e.target.closest('.node[data-key]');
    if (node && !node.classList.contains('ds')) selectNode(node.dataset.key);
  });
  ['input', 'change'].forEach(t => document.addEventListener(t, e => { if (e.target.closest('#run-filters')) filterRuns(); }));
  window.addEventListener('hashchange', route);

  const auto = () => keep.get('autorefresh', true) !== 'off';
  const button = $('#autorefresh');
  const paint = () => { button.textContent = auto() ? 'Auto-refresh on' : 'Auto-refresh off'; button.setAttribute('aria-pressed', String(auto())); };
  button.addEventListener('click', () => { keep.set('autorefresh', auto() ? 'off' : 'on', true); paint(); });
  setInterval(() => {
    if (auto() && !document.hidden) { keep.set('scroll', String(window.scrollY)); location.reload(); }
  }, REFRESH_MS);

  route(); paint();
  const y = Number(keep.get('scroll') || 0);
  if (y) window.scrollTo(0, y);
  keep.set('scroll', '0');
  restoring = false;
})();
"""
