"""Vanilla JS (~3 KB): hash routing, pipeline picker, step panel, run filter,
auto-refresh that keeps the current view, selection, filters and scroll position,
and waits while the student reads an opened section or types."""

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
  const PALETTE = ['#4e79a7','#f28e2b','#e15759','#76b7b2','#59a14f','#edc948','#b07aa1','#ff9da7','#9c755f','#bab0ac',
    '#1f77b4','#ff7f0e','#2ca02c','#d62728','#9467bd','#8c564b','#e377c2','#7f7f7f','#bcbd22','#17becf'];
  const decode = s => { try { return decodeURIComponent(s); } catch (e) { return s; } };

  function route() {
    const [view, arg] = (location.hash.slice(1) || 'overview').split('/');
    const name = $$('.view').some(v => v.dataset.view === view) ? view : 'overview';
    $$('.view').forEach(v => v.classList.toggle('active', v.dataset.view === name));
    $$('[data-tab]').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    if (name === 'runs') showRun(arg ? decode(arg) : null);
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
    $$('.cellmap:not(.drawn)', panel).forEach(drawMap);
    if (remember) keep.set('node', key);
  }

  function loadPoints(src, key, done) {
    const have = () => window.SCHUB_PTS && window.SCHUB_PTS[key];
    if (have()) return done(have());
    const tag = document.createElement('script');
    tag.src = src; tag.onload = () => done(have()); tag.onerror = () => done(null);
    document.head.appendChild(tag);
  }

  // A cell map: subsampled UMAP colored by one obs column; click a legend entry to focus it.
  function drawMap(box) {
    box.classList.add('drawn');
    loadPoints(box.dataset.pts, box.dataset.key, data => {
      if (!data) { box.textContent = 'Cell map unavailable.'; return; }
      const select = $('select', box), canvas = $('canvas', box), legend = $('.cm-legend', box);
      const names = Object.keys(data.cols);
      select.replaceChildren(...names.map(n => new Option(n, n)));
      select.hidden = !names.length;
      $('.cm-n', box).textContent = `${data.x.length.toLocaleString()} of ${data.n.toLocaleString()} cells`;
      let focus = -1;
      const paint = () => {
        const col = data.cols[select.value], ratio = window.devicePixelRatio || 1, size = canvas.clientWidth || 300;
        canvas.width = canvas.height = Math.round(size * ratio);
        const g = canvas.getContext('2d'), pad = 6, s = (size - 2 * pad) / 1000, r = data.x.length > 2000 ? 1.6 : 2.4;
        g.scale(ratio, ratio);
        const counts = col ? col.levels.map(() => 0) : [];
        for (let i = 0; i < data.x.length; i++) {
          const c = col ? col.codes[i] : 0;
          if (col && c >= 0) counts[c]++;
          g.globalAlpha = focus < 0 || c === focus ? 0.85 : 0.07;
          g.fillStyle = c < 0 ? '#9a9a9a' : PALETTE[c % PALETTE.length];
          g.fillRect(pad + data.x[i] * s - r / 2, pad + (1000 - data.y[i]) * s - r / 2, r, r);
        }
        legend.replaceChildren(...(col ? col.levels.map((name, k) => {
          const item = document.createElement('button'), swatch = document.createElement('i');
          item.type = 'button'; item.className = 'cm-item' + (focus === k ? ' on' : '');
          swatch.style.background = PALETTE[k % PALETTE.length];
          item.append(swatch, `${name} (${counts[k]})`);
          item.onclick = () => { focus = focus === k ? -1 : k; paint(); };
          return item;
        }) : []));
      };
      select.onchange = () => { focus = -1; paint(); };
      paint();
    });
  }

  function filterRuns() {
    const q = ($('#run-search')?.value || '').toLowerCase();
    const st = $('#run-state')?.value || '';
    keep.set('run-q', q); keep.set('run-st', st);
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
  document.addEventListener('toggle', e => {
    if (e.target.open) $$('.cellmap:not(.drawn)', e.target).forEach(drawMap);
  }, true);

  const auto = () => keep.get('autorefresh', true) !== 'off';
  const button = $('#autorefresh');
  const paint = () => { button.textContent = auto() ? 'Auto-refresh on' : 'Auto-refresh off'; button.setAttribute('aria-pressed', String(auto())); };
  button.addEventListener('click', () => { keep.set('autorefresh', auto() ? 'off' : 'on', true); paint(); });
  // Not while the student types or reads an opened section (a reload would close it).
  // Filters survive reloads, so only recent typing counts, not a lingering focus.
  let typedAt = 0;
  document.addEventListener('input', () => { typedAt = Date.now(); });
  const busy = () => Date.now() - typedAt < 15000 ||
    $$('.view.active details[open], #node-panel details[open]').some(d => d.getClientRects().length);
  setInterval(() => {
    if (auto() && !document.hidden && !busy()) { keep.set('scroll', String(window.scrollY)); location.reload(); }
  }, REFRESH_MS);

  if ($('#run-search')) { $('#run-search').value = keep.get('run-q') || ''; }
  const st = $('#run-state');
  if (st && [...st.options].some(o => o.value === keep.get('run-st'))) st.value = keep.get('run-st');
  filterRuns();
  route(); paint();
  const y = Number(keep.get('scroll') || 0);
  if (y) window.scrollTo(0, y);
  keep.set('scroll', '0');
  restoring = false;
})();
"""
