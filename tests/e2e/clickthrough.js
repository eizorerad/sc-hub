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
  await shot('00-journal');
  const shownJournal = () => page.$eval('.journal:not([hidden])', s => s.dataset.journal);
  await check('the first project is shown, its cards newest first', async () => {
    const first = await shownJournal();
    const whys = await page.$$eval(`.journal[data-journal="${first}"] .jcard .why`, ws => ws.map(w => w.textContent));
    assert(whys[0] === 'a failing cell' && whys[whys.length - 1] === 'download K562 essential', whys.join(' | '));
    return first;
  });
  await check('the project picker switches project and hash', async () => {
    await page.click('[data-journal-link="k562-qc"]');
    await page.waitForTimeout(150);
    assert((await hash()) === '#journal/k562-qc', await hash());
    assert((await shownJournal()) === 'k562-qc', await shownJournal());
  });
  await check('a figure is shown', async () => {
    const ok = await page.$eval('.journal:not([hidden]) img.jfig', i => i.complete && i.naturalWidth > 0);
    assert(ok, 'image did not load');
  });
  await check('code folds open', async () => {
    await page.click('.journal:not([hidden]) .jcard details.fold > summary >> nth=0');
    assert(await vis('.journal:not([hidden]) .jcard details.fold[open] pre.code'), 'code hidden');
  });
  await check('long output folds', async () => {
    const more = page.locator('.journal:not([hidden]) details.more-out').first();
    await more.locator('summary').click();
    assert(await more.evaluate(d => d.open), 'did not open');
  });
  await check('⋯ on a cell copies its reference', async () => {
    await page.click('.journal:not([hidden]) .jcard .jhead summary.dots >> nth=0');
    await page.click('.journal:not([hidden]) .jcard details[open] [data-copy]');
    assert((await copied()).startsWith('k562-qc#c0'), await copied());
  });
  await check('⋯ of the project shows decisions and mistakes', async () => {
    await page.click('.journal:not([hidden]) .jtitle summary.dots');
    const text = await page.textContent('.journal:not([hidden]) .jtitle details[open] .pop');
    assert(text.includes('genome-wide fits in 90 GB') && text.includes('a failing cell'), text.slice(0, 200));
  });
  await check('the notebook downloads', () => download('.journal:not([hidden]) .jtitle details[open] [data-jnb]'));
  await check('the engine filter shows only the lab agent\'s entries, then all', async () => {
    const cards = () => page.$$eval('.journal:not([hidden]) .jcard', cs => cs.filter(c => !c.hidden).length);
    const all = await cards();
    if (!(await vis('.journal:not([hidden]) .jtitle details[open] .pop'))) await page.click('.journal:not([hidden]) .jtitle summary.dots');
    await page.click('.journal:not([hidden]) [data-engine-filter="claude"]');
    const only = await cards();
    const badge = await page.textContent('.journal:not([hidden]) .jcard:not([hidden]) .jfoot .engine');
    assert(only === 1 && badge.trim() === 'claude', `${only} cards, badge ${badge}`);
    await page.click('.journal:not([hidden]) [data-engine-filter=""]');
    assert((await cards()) === all, 'filter did not reset');
  });
  await check('notes for the student and untraced numbers are shown', async () => {
    const text = await page.textContent('.journal:not([hidden])');
    assert(text.includes('please confirm the control label') && text.includes('numbers not found in the cited cells: 2,000'), 'missing');
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
    const after = await clocked.evaluate(() => ({ m: window.__marker, h: location.hash,
      shown: document.querySelector('.journal:not([hidden])')?.dataset.journal }));
    assert(after.m === undefined, 'did not reload');
    assert(after.h === '#journal/k562-qc' && after.shown === 'k562-qc', JSON.stringify(after));
  });
  await check('an open ⋯ menu holds the reload', async () => {
    await clocked.click('.journal:not([hidden]) .jtitle summary.dots');
    await clocked.evaluate(() => { window.__marker = 2; });
    await clocked.clock.runFor(61000);
    assert((await clocked.evaluate(() => window.__marker)) === 2, 'reloaded while the menu was open');
    await clocked.keyboard.press('Escape');
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
  await check('project ⋯ menu fits the screen', async () => {
    await mp.goto(`${BASE}#journal/k562-qc`);
    await mp.waitForTimeout(200);
    await mp.tap('.journal:not([hidden]) .jtitle summary.dots');
    const r = await mp.$eval('.journal:not([hidden]) .jtitle details[open] .pop', p => { const b = p.getBoundingClientRect(); return [b.left, b.right]; });
    assert(r[0] >= 0 && r[1] <= 375, `menu at ${r.map(Math.round)}`);
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
