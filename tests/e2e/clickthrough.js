// Click-through test of the sc-hub dashboard: every control, with real mouse clicks,
// checking what each one does (not only how it looks). Run it with tests/e2e/run.sh, or:
// node clickthrough.js file:///path/to/view/index.html <out dir>   (file:// only: it reads br/ next to the page)
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');
const { fileURLToPath } = require('url');

const BASE = process.argv[2];
const OUT = process.argv[3];
fs.mkdirSync(OUT, { recursive: true });
const results = [];
const errors = [];
let section = '';

async function check(name, fn) {
  try {
    const note = await fn();
    results.push({ section, name, ok: true, note: note || '' });
  } catch (e) {
    results.push({ section, name, ok: false, note: String(e.message || e).split('\n')[0].slice(0, 300) });
  }
}
function assert(cond, msg) { if (!cond) throw new Error(msg); }

async function main() {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark', acceptDownloads: true });
  // Record what the copy buttons put on the clipboard; window.__refuse simulates a page that may not copy.
  await context.addInitScript(() => {
    window.__copied = [];
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
      writeText: async t => { if (window.__refuse) throw new Error('refused'); window.__copied.push(t); } } });
    const exec = document.execCommand.bind(document);
    document.execCommand = (c, ...a) => (window.__refuse ? false : exec(c, ...a));
  });
  const page = await context.newPage();
  page.on('pageerror', e => errors.push(`${section}: pageerror ${e.message}`));
  page.on('console', m => { if (m.type() === 'error') errors.push(`${section}: console ${m.text()}`); });

  const go = async (hash, fresh = false) => {
    await page.goto('about:blank');  // a full load every time: no hash navigation left pending
    await page.goto(`${BASE}#${hash}`);
    if (fresh) { await page.evaluate(() => sessionStorage.clear()); await page.reload(); }
    await page.waitForTimeout(250);
  };
  const hash = () => page.evaluate(() => location.hash);
  const vis = sel => page.locator(sel).first().isVisible();
  const copied = () => page.evaluate(() => window.__copied[window.__copied.length - 1] || '');
  const shot = name => page.screenshot({ path: path.join(OUT, `${name}.png`) });
  const view = name => `.view[data-view="${name}"]`;
  const hintsFit = async scope => {  // every visible '?' opens a tip that stays on screen
    const bad = await page.evaluate(sel => {
      const out = [];
      for (const h of document.querySelectorAll(`${sel} .hint`)) {
        if (!h.getClientRects().length) continue;
        h.focus();
        const r = h.querySelector('.tip').getBoundingClientRect();
        if (r.width === 0 || r.left < 0 || r.right > innerWidth) out.push(`${h.getAttribute('aria-label').slice(0, 40)} [${Math.round(r.left)}..${Math.round(r.right)}]`);
        h.blur();
      }
      return out;
    }, scope);
    assert(!bad.length, `tips off screen: ${bad.join('; ')}`);
  };
  const download = async (clickSel) => {
    const [dl] = await Promise.all([page.waitForEvent('download', { timeout: 5000 }), page.click(clickSel)]);
    const file = path.join(OUT, dl.suggestedFilename());
    await dl.saveAs(file);
    const nb = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert(nb.nbformat === 4 && nb.cells.length > 2, `not a notebook: ${dl.suggestedFilename()}`);
    return `${dl.suggestedFilename()} (${nb.cells.length} cells)`;
  };

  // ---------------------------------------------------------------- header
  section = 'Header';
  await go('journal', true);
  await shot('01-journal');
  await check('one tab, the Journal, active', async () => {
    const tabs = await page.$$eval('[data-tab]', ts => ts.map(t => `${t.textContent}${t.classList.contains('active') ? '*' : ''}`));
    assert(tabs.join() === 'Journal*', tabs.join());
  });
  await check('the page opens on the Journal', async () => {
    await go('', true);
    assert(await vis(view('journal')), 'journal hidden');
  });
  for (const old of ['projects', 'pipelines', 'experiments', 'overview']) {
    await check(`the old address #${old} opens the Journal`, async () => {
      await go(old);
      await page.waitForTimeout(150);
      assert(await vis(view('journal')), 'journal hidden');
    });
  }
  await check('sc-hub menu opens, closes on an outside click', async () => {
    await page.click('summary.brand');
    assert(await vis('.account .menu'), 'menu did not open');
    await page.mouse.click(900, 700);
    assert(!(await vis('.account .menu')), 'menu stayed open');
  });
  await check('Escape closes the menu and gives focus back', async () => {
    await page.click('summary.brand');
    await page.keyboard.press('Escape');
    assert(!(await vis('.account .menu')), 'still open');
    assert(await page.evaluate(() => document.activeElement.matches('summary.brand')), 'focus lost');
  });
  for (const [label, want, key] of [['Runs', '#runs', 'runs'], ['Library', '#library', 'library'],
    ['Interactive sessions', '#runs/sessions', 'runs'], ['Cluster overview', '#cluster', 'cluster']]) {
    await check(`menu → ${label}`, async () => {
      await page.click('summary.brand');
      await page.click(`.account .menu a:has-text("${label}")`);
      await page.waitForTimeout(150);
      assert((await hash()) === want, `hash ${await hash()}`);
      assert(await vis(view(key)), 'view hidden');
      assert(!(await vis('.account .menu')), 'menu stayed open');
      assert(await page.$eval('summary.brand', b => b.classList.contains('here')), 'sc-hub button not marked as the place');
      if (label === 'Interactive sessions') {
        const sub = await page.$eval('[data-subtabs="runs"] button.active', b => b.textContent);
        assert(sub.startsWith('Sessions'), `subtab ${sub}`);
      }
    });
  }
  await shot('02-cluster');
  await check('auto-refresh switch turns off and on', async () => {
    await page.click('summary.brand');
    await page.click('#autorefresh');
    assert((await page.getAttribute('#autorefresh', 'aria-checked')) === 'false', 'still on');
    assert(await vis('.paused'), 'no "auto-refresh off" badge');
    await page.click('#autorefresh');
    assert((await page.getAttribute('#autorefresh', 'aria-checked')) === 'true', 'still off');
    await page.keyboard.press('Escape');
  });
  await check('status chip opens what runs', async () => {
    const target = await page.getAttribute('header .status', 'href');
    await page.click('header .status');
    await page.waitForTimeout(150);
    assert((await hash()) === target, `${await hash()} vs ${target}`);
    const sub = await page.$eval('[data-subtabs="runs"] button.active', b => b.textContent);
    assert(sub.toLowerCase().startsWith(target.split('/')[1]), `subtab ${sub}`);
    return `${await page.textContent('header .status')} → ${target}`;
  });

  // ---------------------------------------------------------------- journal
  section = 'Journal';
  await go('journal', true);
  await page.waitForSelector('#jpage .jp');
  await shot('00-journal');
  const shownProject = () => page.$eval('#jpage .jp', a => a.dataset.project);
  const visibleRows = sel => page.$$eval(sel, rs => rs.filter(r => r.getClientRects().length).length);
  await check('the navigator groups 65 projects by where they stand', async () => {
    const groups = await page.$$eval('.jgroup', gs => gs.map(g => `${g.dataset.group}:${g.querySelector('summary .count').textContent}`));
    assert(groups.join(' ') === 'blocked:3 working:7 done:34 idle:4 eval:14', groups.join(' '));
    const shownDone = await visibleRows('.jgroup[data-group="done"] > .jnode');
    assert(shownDone === 12, `${shownDone} done projects shown before "Show all"`);
    await page.click('.jgroup[data-group="done"] .jmore');
    assert((await visibleRows('.jgroup[data-group="done"] > .jnode')) === 34, 'Show all did not show all');
    const closed = await page.$$eval('.jgroup:not([open])', gs => gs.map(g => g.dataset.group).join(','));
    assert(closed === 'idle,eval', `closed groups ${closed}`);
    return `${groups.join(', ')}; page ${await shownProject()}`;
  });
  await check('search finds projects by name and by the question of a variant', async () => {
    await page.fill('#jnav-q', 'hct116');
    const hits = await page.$$eval('.jitem', is => is.filter(i => i.getClientRects().length).map(i => i.dataset.path));
    assert(hits.length > 0 && hits.every(h => h.includes('hct116')), hits.join(' '));
    assert(await vis('.jgroup[data-group="eval"] .jitem[data-path^="hct116"]'), 'a closed group did not open for a hit');
    await page.fill('#jnav-q', 'stricter qc');
    const strict = await page.$$eval('.jitem', is => is.filter(i => i.getClientRects().length).map(i => i.dataset.path));
    assert(strict.join(' ') === 'k562-qc k562-qc/strict', strict.join(' '));
    await page.fill('#jnav-q', 'no such thing');
    assert(await vis('.jnone'), 'no "No project matches"');
    await page.fill('#jnav-q', '');
    return `${hits.length} for hct116`;
  });
  await check('a variant opens from the tree, with a path back to its project', async () => {
    await page.click('.jitem[data-path="k562-qc/strict/min50"]');
    await page.waitForFunction(() => document.querySelector('#jpage .jp')?.dataset.project === 'k562-qc/strict/min50');
    assert((await hash()) === '#journal/k562-qc/strict/min50', await hash());
    await page.click('#jpage .jp-crumbs a:has-text("k562-qc")');
    await page.waitForFunction(() => document.querySelector('#jpage .jp')?.dataset.project === 'k562-qc');
    const variants = await page.$$eval('#jpage .jp-section .jrow', rs => rs.map(r => r.querySelector('.jrow-name').textContent));
    assert(variants.join(',') === 'strict,lenient' || variants.join(',') === 'lenient,strict', variants.join(','));
  });
  await check('the page starts with the outcome and its links', async () => {
    const text = await page.textContent('#jpage .jp-outcome');
    assert(text.includes('knockdown in 41 of 50 targets') && text.includes('Protocol notebook'), text.slice(0, 160));
  });
  await check('the report opens without code and its notebook downloads', async () => {
    const href = await page.getAttribute('#jpage .jp-links a', 'href');
    assert(href && href.endsWith('/report.html'), `link ${href}`);
    const report = await page.context().newPage();
    await report.goto(new URL(href, page.url()).href);
    const text = await report.textContent('body');
    await report.close();
    assert(text.includes('good enough to model') && text.includes('Notes on this report'), text.slice(0, 120));
    assert(!text.includes("print('x')"), 'the report page shows code');
    return download('#jpage .jp-links [data-jnb^="report:"]');
  });
  await check('the protocol notebook downloads', () => download('#jpage .jp-links [data-jnb="k562-qc"]'));
  await check('steps are one line each, oldest first; a step opens with its output and figure', async () => {
    const cids = await page.$$eval('#jpage .jsteps .jcid', cs => cs.map(c => c.textContent));
    assert(cids.join(',') === 'c0001,c0002,c0003,c0004', cids.join(','));
    const tall = await page.$$eval('#jpage .jsteps > details > summary', ss => Math.max(...ss.map(s => s.getBoundingClientRect().height)));
    assert(tall < 48, `a closed step is ${Math.round(tall)} px tall`);
    await page.click('#jpage .jsteps details.jstep >> nth=1 >> summary');
    await page.waitForTimeout(150);
    const ok = await page.$eval('#jpage .jsteps details.jstep[open] img.jfig', i => i.complete && i.naturalWidth > 0);
    assert(ok, 'figure did not load');
    await page.click('#jpage .jsteps details.jstep[open] details.fold > summary');
    assert(await vis('#jpage .jsteps details.jstep[open] details.fold[open] pre.code'), 'code hidden');
  });
  await check('a step copies its reference', async () => {
    await page.click('#jpage .jsteps details.jstep[open] [data-copy]');
    assert((await copied()).startsWith('k562-qc#c0'), await copied());
  });
  await check('filters: failed, with figures, one engine, then all; the order flips', async () => {
    const rows = () => visibleRows('#jpage .jsteps > details');
    await page.click('#jpage [data-step-filter="failed"]');
    assert((await rows()) === 1, `failed shows ${await rows()}`);
    await page.click('#jpage [data-step-filter="fig"]');
    assert((await rows()) === 1, `figures show ${await rows()}`);
    await page.click('#jpage [data-step-filter="engine:claude"]');
    const badge = await page.$$eval('#jpage .jsteps > details', ds => ds.filter(d => d.getClientRects().length).map(d => d.dataset.engine));
    assert(badge.join(',') === 'claude', badge.join(','));
    await page.click('#jpage [data-step-filter=""]');
    assert((await rows()) === 4, 'filter did not reset');
    await page.click('#jpage [data-step-order]');
    const first = await page.$eval('#jpage .jsteps', s => { const r = [...s.children].map(c => [c.getBoundingClientRect().top, c.querySelector('.jcid').textContent]); r.sort((a, b) => a[0] - b[0]); return r[0][1]; });
    await page.click('#jpage [data-step-order]');
    assert(first === 'c0004', `newest first starts with ${first}`);
  });
  await check('findings and decisions are one line; a finding shows its untraced numbers', async () => {
    const kinds = await page.$$eval('#jpage .jp-notes .jkind', ks => ks.map(k => k.textContent));
    assert(kinds.includes('finding') && kinds.includes('decision') && kinds.includes('note'), kinds.join(','));
    await page.click('#jpage .jp-notes details[data-kind="finding"] > summary');
    const text = await page.textContent('#jpage .jp-notes details[open]');
    assert(text.includes('numbers not found in the cited cells: 2,000'), text.slice(0, 160));
    assert((await page.textContent('#jpage .jp-notes')).includes('please confirm the control label'), 'note for the student missing');
  });
  await check('a filter stays with its project', async () => {
    await page.click('#jpage [data-step-filter="failed"]');
    await page.click('.jitem[data-path="pbmc-exp-00"]');
    await page.waitForFunction(() => document.querySelector('#jpage .jp')?.dataset.project === 'pbmc-exp-00');
    const shown = await visibleRows('#jpage .jsteps > details');
    const active = await page.$eval('#jpage [data-step-filter][aria-pressed="true"]', b => b.dataset.stepFilter);
    assert(shown === 1 && active === '', `${shown} rows, filter '${active}'`);
    await page.click('.jitem[data-path="k562-qc"]');
    await page.waitForFunction(() => document.querySelector('#jpage .jp')?.dataset.project === 'k562-qc');
    assert((await visibleRows('#jpage .jsteps > details')) === 1, 'k562-qc lost its filter');
    await page.click('#jpage [data-step-filter=""]');
  });
  await check('journal hints fit', () => hintsFit('.view[data-view="journal"]'));

  // ---------------------------------------------------------------- runs
  section = 'Runs';
  await go('runs/history', true);
  const runRows = () => page.locator('#run-list tbody tr[data-href]:visible').count();
  const runsAll = await runRows();
  await check('history search and state filter', async () => {
    const word = (await page.textContent('#run-list tbody tr[data-href] td a')).split('/').pop().split('·')[0].trim();
    await page.fill('#run-search', word);
    const n = await runRows();
    await page.fill('#run-search', '');
    await page.selectOption('#run-state', 'COMPLETED');
    const done = await runRows();
    await page.selectOption('#run-state', '');
    assert(n >= 1 && n <= runsAll && done <= runsAll && (await runRows()) === runsAll, `${n}, ${done}, ${runsAll}`);
    return `"${word}" ${n}, completed ${done}, all ${runsAll}`;
  });
  await check('a click on a run row opens it; ← All runs goes back', async () => {
    const href = await page.getAttribute('#run-list tbody tr[data-href] >> nth=0', 'data-href');
    await page.click('#run-list tbody tr[data-href] >> nth=0 >> td:nth-child(2)');
    await page.waitForTimeout(150);
    assert((await hash()) === `#${href}`, await hash());
    await shot('06-run');
    await page.click(`.run-detail[data-run="${href.slice(5)}"] a.back`);
    await page.waitForTimeout(150);
    assert((await hash()) === '#runs/history' && await vis('#run-list'), await hash());
  });
  const firstRun = await page.getAttribute('#run-list tbody tr[data-href] >> nth=0', 'data-href');
  const detail = `.run-detail[data-run="${firstRun.slice(5)}"]`;
  await go(firstRun);
  await check('run: download notebook', () => download(`${detail} .nb-box button.nb-download`));
  await check('run: Open in JupyterLab copies the request', async () => {
    await page.click(`${detail} [data-ask="jupyter"]`);
    const t = await copied();
    assert(t.includes('make_notebook') && t.includes(firstRun.slice(5)), t.slice(0, 80));
  });
  await check('run: a step card opens its details and asks the assistant', async () => {
    const step = page.locator(`${detail} li.step`).first();
    await step.locator('details > summary:has-text("Details")').click();
    assert(await step.locator('details').first().evaluate(d => d.open), 'details closed');
    if (await step.locator('[data-ask="fix"]').count()) {
      await step.locator('[data-ask="fix"]').click();
      assert((await copied()).includes('save_branch'), 'no fix request');
    }
    return `${await page.locator(`${detail} li.step`).count()} steps`;
  });
  await go('runs/queue');
  await check('queue: sc-hub queue, jobs, the why-wait ? and the cluster link', async () => {
    assert(await vis('[data-subview="runs"][data-sub="queue"]'), 'queue hidden');
    await hintsFit('[data-subview="runs"][data-sub="queue"]');
    await page.click('.why-wait a');
    await page.waitForTimeout(100);
    assert((await hash()) === '#cluster', await hash());
  });
  await go('runs/history');
  await check('subtabs change the address', async () => {
    for (const sub of ['queue', 'sessions', 'history']) {
      await page.click(`[data-subtabs="runs"] button[data-sub="${sub}"]`);
      assert((await hash()) === `#runs/${sub}`, await hash());
      assert(await vis(`[data-subview="runs"][data-sub="${sub}"]`), `${sub} hidden`);
    }
  });

  // ---------------------------------------------------------------- library
  section = 'Library';
  await go('library', true);
  await check('subtabs: datasets, models, references, environment', async () => {
    const subs = await page.$$eval('[data-subtabs="library"] button', bs => bs.map(b => b.dataset.sub));
    for (const sub of subs) {
      await page.click(`[data-subtabs="library"] button[data-sub="${sub}"]`);
      assert(await vis(`[data-subview="library"][data-sub="${sub}"]`), `${sub} hidden`);
    }
    await page.click('[data-subtabs="library"] button[data-sub="datasets"]');
    await hintsFit(view('library'));
    return subs.join(', ');
  });
  await shot('07-library');

  // ---------------------------------------------------------------- refresh
  section = 'Auto-refresh';
  const clocked = await context.newPage();
  clocked.on('pageerror', e => errors.push(`refresh: ${e.message}`));
  await clocked.clock.install();
  await clocked.goto(`${BASE}#journal/k562-qc`);
  await clocked.evaluate(() => sessionStorage.clear());
  await clocked.goto(`${BASE}#journal/k562-qc`);
  await clocked.clock.runFor(500);
  await check('after a minute the page reloads and keeps the project on screen', async () => {
    await clocked.evaluate(() => { window.__marker = 1; });
    await clocked.clock.runFor(61000);
    await clocked.waitForLoadState('load');
    await clocked.clock.runFor(1000);
    await clocked.waitForSelector('#jpage .jp');
    const after = await clocked.evaluate(() => ({ m: window.__marker, h: location.hash,
      shown: document.querySelector('#jpage .jp')?.dataset.project }));
    assert(after.m === undefined, 'did not reload');
    assert(after.h === '#journal/k562-qc' && after.shown === 'k562-qc', JSON.stringify(after));
  });
  await check('an opened navigator group survives the reload', async () => {
    await clocked.click('.jgroup[data-group="idle"] > summary');
    await clocked.evaluate(() => { window.__marker = 3; });
    await clocked.clock.runFor(61000);
    await clocked.waitForLoadState('load');
    await clocked.clock.runFor(1000);
    await clocked.waitForSelector('#jpage .jp');
    assert((await clocked.evaluate(() => window.__marker)) === undefined, 'did not reload');
    assert(await clocked.$eval('.jgroup[data-group="idle"]', g => g.open), 'the group closed again');
  });
  await check('an opened step holds the reload; the open project list does not', async () => {
    await clocked.click('#jpage .jsteps details.jstep >> nth=0 >> summary');
    await clocked.evaluate(() => { window.__marker = 2; });
    await clocked.clock.runFor(61000);
    assert((await clocked.evaluate(() => window.__marker)) === 2, 'reloaded while a step was open');
  });
  await clocked.close();

  // ---------------------------------------------------------------- phone
  section = 'Phone (375 px)';
  const phone = await browser.newContext({ viewport: { width: 375, height: 812 }, colorScheme: 'light', isMobile: true, hasTouch: true });
  const mp = await phone.newPage();
  mp.on('pageerror', e => errors.push(`phone: ${e.message}`));
  for (const h of ['journal', 'runs/history', 'library', 'cluster']) {
    await check(`${h}: nothing wider than the screen`, async () => {
      await mp.goto(`${BASE}#${h}`);
      await mp.waitForTimeout(300);
      const w = await mp.evaluate(() => document.documentElement.scrollWidth);
      await mp.screenshot({ path: path.join(OUT, `phone-${h.replace('/', '-')}.png`) });
      assert(w <= 376, `page is ${w}px wide`);
    });
  }
  await check('the project list opens from its button and closes on a pick', async () => {
    await mp.goto(`${BASE}#journal/k562-qc`);
    await mp.waitForSelector('#jpage .jp');
    assert(!(await mp.isVisible('.jnav')), 'the list takes the screen before it is asked for');
    await mp.tap('.jnav-toggle');
    assert(await mp.isVisible('.jnav'), 'the list did not open');
    await mp.screenshot({ path: path.join(OUT, 'phone-journal-list.png') });
    await mp.tap('.jitem[data-path="cell-jepa"]');
    await mp.waitForFunction(() => document.querySelector('#jpage .jp')?.dataset.project === 'cell-jepa');
    assert(!(await mp.isVisible('.jnav')), 'the list stayed open');
    const w = await mp.evaluate(() => document.documentElement.scrollWidth);
    assert(w <= 376, `page is ${w}px wide`);
  });
  await check('tips fit the screen', () => mp.evaluate(() => {
    const out = [];
    for (const h of document.querySelectorAll('.view.active .hint')) {
      h.focus(); const r = h.querySelector('.tip').getBoundingClientRect(); if (r.left < 0 || r.right > innerWidth) out.push(Math.round(r.left) + '..' + Math.round(r.right)); h.blur();
    }
    if (out.length) throw new Error('off screen: ' + out.join(', '));
  }));
  await phone.close();

  // light theme pictures
  const light = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'light' });
  const lp = await light.newPage();
  for (const h of ['journal', 'runs/history', 'library']) {
    await lp.goto(`${BASE}#${h}`);
    await lp.waitForTimeout(400);
    await lp.screenshot({ path: path.join(OUT, `light-${h.split('/')[0]}.png`) });
  }
  await light.close();
  await browser.close();

  section = 'Console';
  results.push({ section, name: 'no page errors or console errors', ok: !errors.length, note: errors.slice(0, 5).join(' | ') });
  report();
}

function report() {
  fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify(results, null, 1));
  for (const r of results) console.log(`${r.ok ? 'PASS' : 'FAIL'}  [${r.section}] ${r.name}${r.note ? '  — ' + r.note : ''}`);
  console.log(`\n${results.filter(r => r.ok).length}/${results.length} passed`);
}
main().catch(e => { results.push({ section, name: 'test run stopped', ok: false, note: String(e.message).split('\n')[0] }); report(); process.exit(1); });
