"""Page script of the Compare view (#experiments): filter, sort, group, fold sweeps, compare.
Filters wait behind one button and the Data, Steps and parameter columns behind another,
so the table first shows only branches, their state and their numbers.

Rows come from <script id="exp-data" type="application/json">; everything is built
with text nodes (never innerHTML), so names and parameters cannot inject markup.
The main script calls window.SCHUB_EXPERIMENTS.route(arg) for #experiments/<arg>.
"""

EXPERIMENTS_SCRIPT = r"""
(() => {
  const source = document.getElementById('exp-data');
  if (!source) return;
  let DATA;
  try { DATA = JSON.parse(source.textContent); } catch (e) {
    const table = document.getElementById('exp-table');
    if (table) table.textContent = 'The experiments list could not be read; the other tabs work.';
    return;
  }
  const ROWS = DATA.rows, METRICS = DATA.metrics, MAX_COMPARE = 5, MAX_PARAM_COLS = 8;
  const byId = new Map(ROWS.map(r => [r.id, r]));
  const STORE = 'exp-state';
  const DEFAULTS = {q: '', project: '', dataset: '', brick: '', status: '', tag: '', sweep: '', period: 'all',
                    hidden: false, group: 'none', fold: true, sort: 'updated', dir: -1, selected: [], open: [],
                    filters: false, details: false};
  let S = (() => { try { return {...DEFAULTS, ...JSON.parse(sessionStorage.getItem(STORE) || '{}')}; } catch (e) { return {...DEFAULTS}; } })();
  const save = () => { try { sessionStorage.setItem(STORE, JSON.stringify(S)); } catch (e) {} };

  function h(tag, props = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else el.setAttribute(k, v === true ? '' : String(v));
    }
    for (const kid of kids.flat(3)) if (kid !== null && kid !== undefined && kid !== false) el.append(kid);
    return el;
  }
  const on = (el, event, fn) => { el.addEventListener(event, fn); return el; };
  const pill = (state, label) => h('span', {class: `pill ${state}`, text: label || DATA.states[state] || state.toLowerCase()});
  const days = stamp => stamp ? (Date.parse(DATA.today + 'T23:59:59Z') - Date.parse(stamp + ':00Z')) / 864e5 : Infinity;
  const inactive = r => !r.pinned && days(r.updated) > 14;
  function ago(stamp) {
    const d = days(stamp);
    if (!isFinite(d)) return '';
    if (d < 1) return 'today ' + stamp.slice(11, 16);
    return d < 2 ? 'yesterday' : d < 30 ? `${Math.floor(d)} days ago` : stamp.slice(0, 10);
  }
  function fmt(m, v) {
    if (v === undefined || v === null) return '';
    if (m.kind === 'pct') return (100 * v).toFixed(1) + '%';
    return m.kind === 'float' ? Number(v).toFixed(1) : Number(v).toLocaleString();
  }
  const byValue = (a, b) => (typeof a === 'number' && typeof b === 'number') ? a - b : String(a).localeCompare(String(b));

  // brick.param -> value as text; a brick used twice in a branch is "Brick#2".
  function paramsOf(row) {
    const seen = {}, out = {};
    for (const s of row.steps) {
      seen[s.short] = (seen[s.short] || 0) + 1;
      const name = seen[s.short] > 1 ? `${s.short}#${seen[s.short]}` : s.short;
      for (const [k, v] of Object.entries(s.params || {})) {
        out[`${name}.${k}`] = v === null ? 'none' : typeof v === 'object' ? JSON.stringify(v) : String(v);
      }
    }
    return out;
  }
  const PARAMS = new Map(ROWS.map(r => [r.id, paramsOf(r)]));

  // Filters kept from an earlier visit may name a project or sweep that no longer exists:
  // drop those instead of showing an empty table.
  const AVAILABLE = {
    project: ROWS.map(r => r.project), dataset: ROWS.flatMap(r => r.datasets), brick: ROWS.flatMap(r => r.steps.map(s => s.short)),
    status: ROWS.map(r => r.state), tag: ROWS.flatMap(r => r.tags), sweep: ROWS.map(r => r.sweep && r.sweep.name),
  };
  for (const [key, values] of Object.entries(AVAILABLE)) if (S[key] && !values.includes(S[key])) S[key] = '';
  S.selected = S.selected.filter(id => byId.has(id));

  function matches(r) {
    const q = S.q.trim().toLowerCase();
    const text = [r.id, r.desc, r.idea || '', r.origin, ...r.tags, r.sweep ? r.sweep.name : ''].join(' ').toLowerCase();
    if (q && !text.includes(q)) return false;
    if (S.project && r.project !== S.project) return false;
    if (S.dataset && !r.datasets.includes(S.dataset)) return false;
    if (S.brick && !r.steps.some(s => s.short === S.brick)) return false;
    if (S.status && r.state !== S.status) return false;
    if (S.tag && !r.tags.includes(S.tag)) return false;
    if (S.sweep && !(r.sweep && r.sweep.name === S.sweep)) return false;
    if (S.period === 'today' && days(r.updated) >= 1) return false;
    if (S.period === 'week' && days(r.updated) >= 7) return false;
    return S.hidden || S.sweep || !(r.archived || inactive(r)) || S.selected.includes(r.id);
  }

  // Only parameters whose values differ between the rows on screen that have them
  // become columns (a step some rows lack is not a difference in its parameters).
  function paramColumns(rows) {
    if (rows.length < 2) return [];
    const keys = new Set(rows.flatMap(r => Object.keys(PARAMS.get(r.id))));
    const found = [];
    for (const key of keys) {
      const values = rows.map(r => PARAMS.get(r.id)[key]).filter(v => v !== undefined);
      if (new Set(values).size > 1) found.push({key, missing: rows.length - values.length});
    }
    found.sort((a, b) => a.missing - b.missing || a.key.localeCompare(b.key));
    return found.slice(0, MAX_PARAM_COLS).map(c => c.key);
  }
  const metricColumns = rows => METRICS.filter(m => rows.some(r => m.key in r.metrics));

  function sortValue(r, key) {
    if (key === 'updated') return r.updated || null;
    if (key === 'branch') return r.id;
    if (key === 'state') return r.state;
    if (key.startsWith('m:')) return r.metrics[key.slice(2)] ?? null;
    const v = PARAMS.get(r.id)[key.slice(2)];
    return v === undefined ? null : (isNaN(Number(v)) ? v : Number(v));
  }
  const sortKey = () => (!S.details && S.sort.startsWith('p:')) ? 'updated' : S.sort;  // a hidden column does not sort
  function sorted(rows) {
    const key = sortKey();
    return [...rows].sort((a, b) => {
      if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
      const x = sortValue(a, key), y = sortValue(b, key);
      if (x === y) return a.id.localeCompare(b.id);
      if (x === null) return 1;
      if (y === null) return -1;
      return (x < y ? -1 : 1) * S.dir;
    });
  }

  function units(rows) {  // a row, or a sweep folded into one unit
    const out = [], sweeps = new Map();
    for (const r of rows) {
      if (!S.fold || !r.sweep || S.sweep) { out.push({row: r}); continue; }
      const key = `${r.project}/${r.sweep.name}`;
      if (!sweeps.has(key)) { sweeps.set(key, {sweep: key, name: r.sweep.name, param: r.sweep.param, step: r.sweep.step, rows: []}); out.push(sweeps.get(key)); }
      sweeps.get(key).rows.push(r);
    }
    for (const u of sweeps.values()) u.rows.sort((a, b) => byValue(a.sweep.value, b.sweep.value));
    return out;
  }
  function section(r) {
    if (S.group === 'project') return r.project;
    if (S.group === 'idea') return r.idea || '(no idea linked)';
    if (S.group === 'dataset') return r.datasets.join(' + ');
    if (S.group === 'tag') return r.tags.length ? r.tags.join(', ') : '(no tags)';
    return '';
  }

  function header(key, label, hint) {
    const mark = sortKey() === key ? (S.dir < 0 ? ' ↓' : ' ↑') : '';
    return h('th', {class: 'sortable', title: hint || 'sort'},
      on(h('button', {type: 'button', text: label + mark}), 'click', () => {
        S.dir = S.sort === key ? -S.dir : (key === 'branch' ? 1 : -1); S.sort = key; save(); renderTable();
      }));
  }

  function select(id, checked) {
    S.selected = checked ? [...new Set([...S.selected, id])].slice(-MAX_COMPARE) : S.selected.filter(x => x !== id);
    save(); renderCompare(); renderTable();
  }

  function rowEl(r, params, metrics, member) {
    const box = on(h('input', {type: 'checkbox', 'aria-label': `compare ${r.id}`}), 'change', e => select(r.id, e.target.checked));
    box.checked = S.selected.includes(r.id);
    const name = r.view ? h('a', {href: `#pipelines/${r.view}`, text: r.branch}) : h('span', {text: r.branch});
    const steps = r.steps.map((s, i) => [i ? ' → ' : '', h('span', {class: `st ${s.state}`, title: `${s.i}. ${s.brick}${s.head ? ': ' + s.head : ''}`, text: s.short})]);
    const issues = r.issues.length ? h('span', {class: 'flag', title: r.issues.map(x => `step ${x.step}: ${x.text}`).join('\n'), text: ' !'}) : null;
    return h('tr', {class: member ? 'member' : null, 'data-id': r.id},
      h('td', {class: 'sel'}, box),
      h('td', {class: 'name'}, r.pinned ? h('span', {class: 'star', title: 'pinned', text: '★ '}) : null, name, issues,
        r.archived ? h('span', {class: 'tag', text: 'archived'}) : null,
        h('div', {class: 'muted small', text: [r.project, r.origin].filter(Boolean).join(' · ')}),
        r.tags.length ? h('div', {class: 'chips'}, r.tags.map(t => h('span', {class: 'chip', text: t}))) : null),
      h('td', {}, pill(r.state, r.state_label)),
      S.details ? h('td', {class: 'small', text: r.datasets.join(' + ')}) : null,
      S.details ? h('td', {class: 'steps small'}, steps) : null,
      params.map(p => h('td', {class: 'small', text: PARAMS.get(r.id)[p] ?? '—'})),
      metrics.map(m => h('td', {class: 'num' + (r.stale.includes(m.key) ? ' stale' : ''),
                                title: r.stale.includes(m.key) ? 'from an older version of this branch' : null, text: fmt(m, r.metrics[m.key])})),
      h('td', {class: 'small muted', text: ago(r.updated)}));
  }

  function range(m, rows) {
    const values = rows.map(r => r.metrics[m.key]).filter(v => v !== undefined && v !== null);
    if (!values.length) return '';
    const lo = Math.min(...values), hi = Math.max(...values);
    return lo === hi ? fmt(m, lo) : `${fmt(m, lo)}–${fmt(m, hi)}`;
  }

  function sweepEl(u, params, metrics) {
    const open = S.open.includes(u.sweep);
    const toggle = on(h('button', {type: 'button', class: 'quiet fold', 'aria-expanded': String(open), text: open ? '▾' : '▸'}), 'click', () => {
      S.open = open ? S.open.filter(x => x !== u.sweep) : [...S.open, u.sweep]; save(); renderTable();
    });
    const all = on(h('input', {type: 'checkbox', 'aria-label': `compare sweep ${u.name}`}), 'change', e => {
      const ids = u.rows.map(r => r.id);
      S.selected = e.target.checked ? [...new Set([...S.selected, ...ids])].slice(-MAX_COMPARE) : S.selected.filter(x => !ids.includes(x));
      save(); renderCompare(); renderTable();
    });
    all.checked = u.rows.every(r => S.selected.includes(r.id));
    const counts = {};
    u.rows.forEach(r => { counts[r.state] = (counts[r.state] || 0) + 1; });
    const latest = u.rows.map(r => r.updated).sort().pop();
    return h('tr', {class: 'sweep'},
      h('td', {class: 'sel'}, all),
      h('td', {class: 'name'}, toggle, h('b', {text: ` sweep ${u.name}`}),
        h('div', {class: 'muted small', text: `step ${u.step} ${u.param}: ${u.rows.map(r => String(r.sweep.value)).join(', ')}`})),
      h('td', {}, Object.entries(counts).map(([s, n]) => pill(s, `${n} ${DATA.states[s] || s.toLowerCase()}`))),
      S.details ? h('td', {class: 'small', text: u.rows[0].datasets.join(' + ')}) : null,
      S.details ? h('td', {class: 'small muted', text: `${u.rows.length} branches`}) : null,
      params.map(() => h('td')),
      metrics.map(m => h('td', {class: 'num', text: range(m, u.rows)})),
      h('td', {class: 'small muted', text: ago(latest)}));
  }

  function renderTable() {
    const target = document.getElementById('exp-table');
    const shown = sorted(ROWS.filter(matches));
    const params = S.details ? paramColumns(shown) : [], metrics = metricColumns(shown);
    const width = (S.details ? 6 : 4) + params.length + metrics.length;
    const body = h('tbody');
    const sections = new Map();
    shown.forEach(r => { const s = section(r); if (!sections.has(s)) sections.set(s, []); sections.get(s).push(r); });
    for (const [name, rows] of [...sections.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      if (S.group !== 'none') body.append(h('tr', {class: 'section'}, h('td', {colspan: width, text: `${name} · ${rows.length}`})));
      for (const u of units(rows)) {
        if (u.row) { body.append(rowEl(u.row, params, metrics, false)); continue; }
        body.append(sweepEl(u, params, metrics));
        if (S.open.includes(u.sweep)) u.rows.forEach(r => body.append(rowEl(r, params, metrics, true)));
      }
    }
    const hidden = ROWS.filter(r => (r.archived || inactive(r)) && !S.hidden).length;
    const note = h('p', {class: 'muted small', text: `${shown.length} of ${ROWS.length} branches` +
      (hidden && !S.hidden ? ` · ${hidden} archived or untouched for 14 days are hidden` : '')});
    if (!shown.length) note.append(' · ', on(h('button', {type: 'button', class: 'quiet', text: 'Show all'}), 'click', () => {
      S = {...DEFAULTS, sort: S.sort, dir: S.dir, filters: S.filters, details: S.details}; save(); renderAll();
    }));
    const head = h('tr', {}, h('th', {class: 'sel'}), header('branch', 'Branch'), header('state', 'State'),
      S.details ? h('th', {text: 'Data'}) : null, S.details ? h('th', {text: 'Steps'}) : null, params.map(p => header('p:' + p, p, 'differs between the rows shown')),
      metrics.map(m => header('m:' + m.key, m.label, m.hint)), header('updated', 'Updated'));
    target.replaceChildren(note, h('div', {class: 'scroll'}, h('table', {class: 'exp-rows'}, h('thead', {}, head), body)));
  }

  function verdicts(rows) {
    const out = [], len = Math.max(...rows.map(r => r.steps.length));
    let shared = 0;
    while (shared < len && rows.every(r => r.steps[shared] && r.steps[shared].key === rows[0].steps[shared].key)) shared++;
    if (len > 0 && shared === len && rows.every(r => r.steps.length === len)) out.push('Identical: every step is the same step (computed once), so the results are the same.');
    else if (shared) out.push(`${shared === 1 ? 'Step 1 is' : `Steps 1–${shared} are`} the same step in all of them (computed once); they part at step ${shared + 1}.`);
    for (const m of METRICS) {
      const values = rows.map(r => r.metrics[m.key]), sources = new Set(rows.map(r => r.sources[m.key]));
      // News only when different steps gave the same number (one shared step giving it is expected).
      if (values.every(v => v !== undefined) && new Set(values).size === 1 && sources.size > 1)
        out.push(`Same ${m.label} in all (${fmt(m, values[0])}) from different steps: check whether what differs can change it (planner notes below).`);
    }
    const notes = new Map();
    rows.forEach(r => r.issues.filter(x => x.code === 'upstream_unused').forEach(x => notes.set(x.text, [...(notes.get(x.text) || []), r.branch])));
    return [...out, ...[...notes].map(([text, branches]) => `${branches.join(', ')}: ${text}`)];
  }

  function stepCell(r, i, base, rows) {
    const s = r.steps[i];
    if (!s) return h('td', {class: 'muted small', text: '—'});
    const b = base.steps[i];
    const shared = x => x !== base && x.steps[i] && x.steps[i].key === s.key;
    const same = r === base ? rows.some(shared) : b && s.key === b.key;
    const diff = b && b.brick === s.brick && !same
      ? Object.keys({...s.params, ...b.params}).filter(k => JSON.stringify(s.params[k]) !== JSON.stringify(b.params[k])).map(k => `${k}=${JSON.stringify(s.params[k] ?? null)}`)
      : [];
    return h('td', {class: 'small' + (same ? ' same' : '')}, h('b', {text: s.short}), same && r !== base ? ' = same step' : '',
      diff.length ? h('div', {text: diff.join(', ')}) : null, s.head ? h('div', {class: 'muted', text: s.head}) : null);
  }

  function renderCompare() {
    const box = document.getElementById('exp-compare');
    const rows = S.selected.map(id => byId.get(id)).filter(Boolean);
    box.hidden = rows.length < 2;
    if (rows.length < 2) { box.replaceChildren(); return; }
    const base = rows[0], len = Math.max(...rows.map(r => r.steps.length));
    const clear = on(h('button', {type: 'button', class: 'quiet', text: 'Clear'}), 'click', () => { S.selected = []; save(); renderCompare(); renderTable(); });
    const heads = h('tr', {}, h('th'), rows.map(r => h('th', {}, r.view ? h('a', {href: `#pipelines/${r.view}`, text: r.branch}) : r.branch, ' ', pill(r.state, r.state_label))));
    const stepRows = [...Array(len).keys()].map(i => h('tr', {}, h('th', {text: `step ${i + 1}`}), rows.map(r => stepCell(r, i, base, rows))));
    const metricRows = METRICS.filter(m => rows.some(r => m.key in r.metrics)).map(m => h('tr', {}, h('th', {text: m.label, title: m.hint || null}),
      rows.map(r => {
        const v = r.metrics[m.key], b = base.metrics[m.key];
        const delta = r !== base && v !== undefined && b !== undefined && v !== b ? ` (${v > b ? '+' : '−'}${fmt(m, Math.abs(v - b))})` : '';
        return h('td', {class: 'num' + (r.stale.includes(m.key) ? ' stale' : ''), text: fmt(m, v) + delta});
      })));
    const figures = [...Array(len).keys()].filter(i => rows.some(r => r.steps[i] && r.steps[i].thumbs.length)).map(i => h('tr', {},
      h('th', {text: `step ${i + 1}`}), rows.map(r => h('td', {}, (r.steps[i] ? r.steps[i].thumbs : []).map(u =>
        h('img', {class: 'thumb', src: u, alt: `${r.branch} step ${i + 1}`, loading: 'lazy'}))))));
    box.replaceChildren(
      h('div', {class: 'graph-head'}, h('h3', {text: `Comparing ${rows.length} branches`}), clear),
      h('ul', {class: 'verdicts'}, verdicts(rows).map(v => h('li', {text: v}))),
      h('div', {class: 'scroll'}, h('table', {class: 'compare'}, h('thead', {}, heads), h('tbody', {}, stepRows,
        metricRows.length ? h('tr', {class: 'section'}, h('td', {colspan: rows.length + 1, text: 'Results'})) : null, metricRows,
        figures.length ? h('tr', {class: 'section'}, h('td', {colspan: rows.length + 1, text: 'Figures'})) : null, figures))));
  }

  // The bar is built once per render of the view: a filter change only repaints the
  // Filters button, so keyboard focus stays on the control the student is using.
  const EMPTY = {period: 'all', group: 'none'};  // what the first option ('Any time', 'No grouping') means
  let bar = {};
  function activeFilters() {
    return ['project', 'dataset', 'brick', 'status', 'tag', 'sweep'].filter(k => S[k]).length
      + (S.period !== 'all') + (S.group !== 'none') + S.hidden + !S.fold;
  }
  function paintBar() {
    const active = activeFilters();
    bar.filters.className = 'toggle' + (S.filters || active ? ' on' : '');
    bar.filters.setAttribute('aria-expanded', String(S.filters));
    bar.filters.textContent = active ? `Filters · ${active}` : 'Filters';
    bar.details.className = 'toggle' + (S.details ? ' on' : '');
    bar.details.setAttribute('aria-pressed', String(S.details));
    bar.row.hidden = !S.filters;
  }
  function changed() { save(); paintBar(); renderTable(); }

  function control(label, key, options) {
    const el = h('select', {'aria-label': label}, h('option', {value: '', text: label}), options.map(([v, t]) => h('option', {value: v, text: t})));
    el.value = S[key] === EMPTY[key] ? '' : S[key];
    return on(el, 'change', () => { S[key] = el.value || EMPTY[key] || ''; changed(); });
  }
  const uniq = values => [...new Set(values)].filter(Boolean).sort().map(v => [v, v]);

  function renderBar() {
    const search = on(h('input', {type: 'search', placeholder: 'Filter: name, tag, idea, sweep', 'aria-label': 'Filter branches'}), 'input', e => { S.q = e.target.value; save(); renderTable(); });
    search.value = S.q;
    const check = (key, text) => { const box = on(h('input', {type: 'checkbox'}), 'change', e => { S[key] = e.target.checked; changed(); }); box.checked = S[key]; return h('label', {class: 'small'}, box, ' ' + text); };
    const reset = on(h('button', {type: 'button', class: 'quiet', text: 'Reset'}), 'click', () => { S = {...DEFAULTS, filters: true, details: S.details}; save(); renderAll(); });
    const sweeps = uniq(ROWS.map(r => r.sweep && r.sweep.name));
    bar = {
      filters: on(h('button', {type: 'button'}), 'click', () => { S.filters = !S.filters; save(); paintBar(); }),
      details: on(h('button', {type: 'button', title: 'Show the data, the steps and the parameters that differ', text: 'Steps & parameters'}),
        'click', () => { S.details = !S.details; changed(); }),
      row: h('div', {class: 'exp-filters'},
        control('All projects', 'project', uniq(ROWS.map(r => r.project))),
        control('All data', 'dataset', uniq(ROWS.flatMap(r => r.datasets))),
        control('Any step', 'brick', uniq(ROWS.flatMap(r => r.steps.map(s => s.short)))),
        control('Any state', 'status', uniq(ROWS.map(r => r.state)).map(([v]) => [v, DATA.states[v] || v.toLowerCase()])),
        control('Any tag', 'tag', uniq(ROWS.flatMap(r => r.tags))),
        sweeps.length ? control('Any sweep', 'sweep', sweeps) : null,
        control('Any time', 'period', [['today', 'today'], ['week', 'last 7 days']]),
        control('No grouping', 'group', [['project', 'by project'], ['idea', 'by idea'], ['dataset', 'by dataset'], ['tag', 'by tag']]),
        check('fold', 'fold sweeps'), check('hidden', 'show archived'), reset),
    };
    document.getElementById('exp-bar').replaceChildren(h('div', {class: 'exp-line'}, search, bar.filters, bar.details), bar.row);
    paintBar();
  }

  function renderAll() { renderBar(); renderCompare(); renderTable(); }

  window.SCHUB_EXPERIMENTS = {
    // #experiments/project=a&sweep=b: open the table filtered (links from graphs and projects)
    route(arg) {
      if (arg) {
        const wanted = new URLSearchParams(arg);
        S = {...S, project: '', sweep: '', tag: '', q: ''};
        for (const key of ['project', 'sweep', 'tag', 'q']) if (wanted.has(key)) S[key] = wanted.get(key);
        save();
      }
      renderAll();
    },
  };
})();
"""
