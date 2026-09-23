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
  await go('projects', true);
  await shot('01-projects');
  await check('four tabs, Projects active', async () => {
    const tabs = await page.$$eval('[data-tab]', ts => ts.map(t => `${t.textContent}${t.classList.contains('active') ? '*' : ''}`));
    assert(tabs.join() === 'Journal,Projects*,Pipelines,Compare', tabs.join());
  });
  await check('the page opens on the Journal', async () => {
    await go('', true);
    assert(await vis(view('journal')), 'journal hidden');
    await go('projects');
  });
  for (const [label, key] of [['Journal', 'journal'], ['Pipelines', 'pipelines'], ['Compare', 'experiments'], ['Projects', 'projects']]) {
    await check(`tab ${label} opens its view`, async () => {
      await page.click(`[data-tab="${key}"]`);
      await page.waitForTimeout(150);
      assert(await vis(view(key)), 'view hidden');
      assert((await hash()).startsWith(`#${key}`), await hash());
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

  // ---------------------------------------------------------------- projects
  section = 'Projects';
  await go('projects', true);
  const projects = await page.$$eval('.project-tree [data-project-link]', bs => bs.map(b => b.dataset.projectLink));
  for (const p of projects) {
    await check(`tree → ${p}`, async () => {
      await page.click(`.project-tree [data-project-link="${p}"]`);
      await page.waitForTimeout(120);
      const shown = await page.$$eval('.project[data-project]', cs => cs.filter(c => !c.hidden).map(c => c.dataset.project));
      assert(shown.join() === p, `shown ${shown.join()}`);
      assert(await page.$eval(`.project-tree [data-project-link="${p}"]`, b => b.classList.contains('active')), 'not highlighted');
      await hintsFit(`.project[data-project="${p}"]`);
      const rows = await page.$$eval(`.project[data-project="${p}"] .row-link`, rs => rs.length);
      return `${rows} branch rows`;
    });
  }
  await check('names and descriptions show as text, never as markup', async () => {
    const found = await page.$$eval('main .desc *, main .row-name *, main .question *', els => els.map(e => e.tagName));
    assert(!found.length, `markup inside text: ${found.join(', ')}`);
  });
  const P = projects[0];
  const card = `.project[data-project="${P}"]`;
  await go(`projects/${P}`);
  await check('a click on a branch row (its description) opens its pipeline', async () => {
    const href = await page.getAttribute(`${card} .row-link`, 'data-href');
    await page.click(`${card} .row-link .row-main`, { position: { x: 400, y: 30 } });
    await page.waitForTimeout(400);
    assert((await hash()) === `#${href}`, `${await hash()} vs #${href}`);
    assert(await page.locator(`#pipe-slot [data-pipe-view="${href.split('/')[1]}"]`).count() === 1, 'graph not loaded');
  });
  await go(`projects/${P}`);
  await check('the branch name link opens its pipeline', async () => {
    const href = await page.getAttribute(`${card} .row-link a.row-name`, 'href');
    await page.click(`${card} .row-link a.row-name`);
    await page.waitForTimeout(300);
    assert((await hash()) === href, `${await hash()} vs ${href}`);
  });
  await go(`projects/${P}`);
  await check('branch ⋯ opens; opening another closes the first; the row does not navigate', async () => {
    // the last row's menu first: an open menu covers the rows below it, as dropdowns do
    const menus = page.locator(`${card} .row-link details.menu-pop`);
    const last = (await menus.count()) - 1;
    await menus.nth(last).locator('summary').click();
    assert(await menus.nth(last).evaluate(d => d.open), 'did not open');
    assert((await hash()) === `#projects/${P}`, `navigated to ${await hash()}`);
    await menus.nth(0).locator('summary').click();
    assert(!(await menus.nth(last).evaluate(d => d.open)) && await menus.nth(0).evaluate(d => d.open), 'two menus open');
    await page.keyboard.press('Escape');
    assert(!(await menus.nth(0).evaluate(d => d.open)), 'Escape did not close it');
  });
  await check('branch ⋯ → Open the pipeline', async () => {
    const m = page.locator(`${card} .row-link details.menu-pop`).first();
    await m.locator('summary').click();
    await m.locator('.pop a:has-text("Open the pipeline")').click();
    await page.waitForTimeout(300);
    assert((await hash()).startsWith('#pipelines/v-'), await hash());
  });
  await go(`projects/${P}`);
  await check('branch ⋯ → Latest run opens the run', async () => {
    const m = page.locator(`${card} .row-link details.menu-pop`).first();
    await m.locator('summary').click();
    await m.locator('.pop a:has-text("Latest run")').click();
    await page.waitForTimeout(200);
    const h = await hash();
    assert(h.startsWith('#runs/2'), h);
    assert(await page.locator(`.run-detail[data-run="${h.slice(6)}"]`).isVisible(), 'run detail hidden');
  });
  await go(`projects/${P}`);
  await check('branch ⋯ → Download notebook gives a notebook', async () => {
    const m = page.locator(`${card} .row-link details.menu-pop`).first();
    await m.locator('summary').click();
    return download(`${card} .row-link details.menu-pop[open] button.nb-download`);
  });
  await check('a revised branch lists its revisions in its ⋯', async () => {
    const found = await page.$$eval(`${card} .row-link`, rs => rs.filter(r => r.querySelector('.pop-label')?.textContent.includes('revisions')).map(r => r.querySelector('.row-name').textContent));
    return found.length ? `revisions under: ${found.join(', ')}` : 'no revised branch in this project';
  });
  await check('project ⋯ shows software and logbook', async () => {
    await page.keyboard.press('Escape');
    await page.click(`${card} .project-head summary.dots`);
    const text = await page.textContent(`${card} .project-head .pop`);
    assert(text.includes('Software'), text.slice(0, 80));
    await page.keyboard.press('Escape');
    return text.includes('Logbook') ? 'software + logbook' : 'software only (empty logbook)';
  });
  await check('"Compare all N" opens Compare filtered to the project', async () => {
    await page.click(`${card} .links a:has-text("Compare all")`);
    await page.waitForTimeout(250);
    assert((await hash()) === `#experiments/project=${P}`, await hash());
    const projectsShown = await page.$$eval('#exp-table tbody tr[data-id]', rs => [...new Set(rs.map(r => r.dataset.id.split('/').slice(0, -1).join('/')))]);
    assert(projectsShown.join() === P, `rows of ${projectsShown.join()}`);
    const btn = await page.textContent('#exp-bar button.toggle >> nth=0');
    assert(btn.includes('· 1'), `Filters button says "${btn}"`);
    return `${projectsShown.length ? 'only ' + P : ''}; button "${btn}"`;
  });
  await go(`projects/${P}`);
  await check('"Map" opens the project map', async () => {
    await page.click(`${card} .links a:has-text("Map")`);
    await page.waitForTimeout(300);
    assert((await hash()) === `#pipelines/p-${P.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}`, await hash());
    assert(await page.locator('#pipe-slot [data-pipe-view^="p-"]').count() === 1, 'no project graph');
  });
  await go(`projects/${P}`);
  await check('datasets in the project line open the Library', async () => {
    await page.click(`${card} .meta a`);
    await page.waitForTimeout(150);
    assert((await hash()) === '#library', await hash());
  });
  await go(`projects/${P}`);
  await check('ideas: done ones fold and open', async () => {
    const det = page.locator(`${card} details.quiet-details`);
    if (!(await det.count())) return 'no folded ideas';
    await det.locator('summary').click();
    assert(await det.evaluate(d => d.open), 'did not open');
    return await det.locator('summary').textContent();
  });

  // ---------------------------------------------------------------- pipelines
  section = 'Pipelines';
  await go('pipelines', true);
  const pipes = await page.$$eval('[data-pipe]', bs => bs.map(b => b.dataset.pipe));
  let nodesClicked = 0, panelProblems = [];
  for (const id of pipes) {
    await check(`graph ${id}: loads, every step opens its panel, × closes it`, async () => {
      await page.click(`[data-pipe="${id}"]`);
      await page.waitForTimeout(350);
      assert(await page.locator(`#pipe-slot [data-pipe-view="${id}"]`).count() === 1, 'graph not loaded');
      await hintsFit(view('pipelines'));
      const keys = await page.$$eval('#pipe-slot [data-history="0"] .node[data-key]:not(.ds):not(.stub)', ns => ns.map(n => n.dataset.key));
      for (const key of keys) {
        await page.click(`#pipe-slot [data-history="0"] .node[data-key="${key}"]`);
        await page.waitForTimeout(60);
        const state = await page.evaluate(k => ({
          open: !document.querySelector('.pipes').classList.contains('no-node'),
          title: document.querySelector('#node-panel .panel-head h3')?.textContent || '',
          selected: document.querySelector('.node.selected')?.dataset.key,
          asks: document.querySelectorAll('#node-panel [data-ask="fix"]').length,
          folds: document.querySelectorAll('#node-panel .folds > details').length,
        }), key);
        nodesClicked++;
        if (!state.open || !state.title || state.selected !== key || state.folds < 2 || state.asks > 1) panelProblems.push(`${id}/${state.title || key}: ${JSON.stringify(state)}`);
      }
      if (keys.length) {
        await page.click('#node-panel .panel-close');
        assert(await page.evaluate(() => document.querySelector('.pipes').classList.contains('no-node')), '× did not close');
      }
      return `${keys.length} steps`;
    });
  }
  await check('every step panel: title, selection, ≤1 request box, folds', async () => {
    assert(!panelProblems.length, panelProblems.slice(0, 3).join(' | '));
    return `${nodesClicked} panels opened`;
  });
  await shot('03-pipeline');

  const branchViews = fs.readdirSync(path.join(path.dirname(fileURLToPath(BASE)), 'br')).filter(f => f.startsWith('v-')).map(f => f.slice(0, -3));
  await check('a → box (another branch) opens that branch', async () => {
    for (const v of branchViews) {
      await go(`pipelines/${v}`);
      const stub = page.locator('#pipe-slot [data-history="0"] .node.stub[data-href]').first();
      if (!(await stub.count())) continue;
      const href = await stub.getAttribute('data-href');
      await stub.click();
      await page.waitForTimeout(300);
      assert((await hash()) === href, `${await hash()} vs ${href}`);
      return `${v} → ${href}`;
    }
    return 'no → boxes in this data';
  });
  const B = branchViews.find(v => v.includes('main')) || branchViews[0];
  await go(`pipelines/${B}`);
  await check('graph head: Notebook downloads a notebook', async () => {
    if (!(await page.locator('.graph-head button.nb-download').count())) return 'no run yet';
    return download('.graph-head button.nb-download');
  });
  const menuItem = async text => {
    await page.click('.graph-head summary.dots');
    await page.click(`.graph-head .pop >> text=${text}`);
    await page.waitForTimeout(250);
  };
  await check('graph ⋯ → Latest run', async () => { await menuItem('Latest run'); assert((await hash()).startsWith('#runs/2'), await hash()); });
  await go(`pipelines/${B}`);
  await check('graph ⋯ → Compare in a table', async () => { await menuItem('Compare in a table'); assert((await hash()).startsWith('#experiments/project='), await hash()); });
  await go(`pipelines/${B}`);
  await check('graph ⋯ → Open the project map', async () => { await menuItem('Open the project map'); assert((await hash()).startsWith('#pipelines/p-'), await hash()); });
  await go(`pipelines/${B}`);
  for (const [text, must] of [['Sweep a parameter', 'sweep_branch'], ['Pin this branch', 'pinned=true'], ['Archive this branch', 'archived=true']]) {
    await check(`graph ⋯ → ${text} copies the request`, async () => {
      await page.click('.graph-head summary.dots');
      await page.click(`.graph-head .pop button:has-text("${text}")`);
      await page.waitForTimeout(100);
      const t = await copied();
      assert(t.includes(must), `copied: ${t.slice(0, 80)}`);
      assert(await page.locator('.graph-head .pop .copied').isVisible(), 'no "Copied" note');
      await page.keyboard.press('Escape');
    });
  }
  await check('older steps: shown and hidden again from ⋯', async () => {
    for (const v of [...pipes, ...branchViews]) {
      await go(`pipelines/${v}`);
      if (!(await page.locator('.graph-head [data-history-toggle]').count())) continue;
      await page.click('.graph-head summary.dots');
      await page.click('.graph-head [data-history-toggle]');
      await page.waitForTimeout(100);
      assert(await page.locator('#pipe-slot [data-history="1"]').isVisible(), 'history graph hidden');
      const label = await page.textContent('.graph-head [data-history-toggle]');
      assert(label.startsWith('Hide'), label);
      await page.click('.graph-head [data-history-toggle]');
      assert(await page.locator('#pipe-slot [data-history="0"]').isVisible(), 'current graph hidden');
      await page.keyboard.press('Escape');
      return v;
    }
    return 'no older steps in this data';
  });

  // step panel of a step several branches share
  const shared = await (async () => {
    for (const v of pipes.filter(p => p.startsWith('p-'))) {
      await go(`pipelines/${v}`);
      const keys = await page.$$eval('#pipe-slot [data-history="0"] .node[data-key]:not(.ds):not(.stub)', ns => ns.map(n => n.dataset.key));
      for (const k of keys) {
        await page.click(`#pipe-slot [data-history="0"] .node[data-key="${k}"]`);
        if (await page.locator('#node-panel select.ask-ref').count()) return { v, k };
      }
    }
    return null;
  })();
  await check('shared step: the branch picker decides what Fix / Try an alternative / copy reference say', async () => {
    assert(shared, 'no shared step found');
    const options = await page.$$eval('#node-panel select.ask-ref option', os => os.map(o => o.value).filter(Boolean));
    // on a project map nothing is picked: the request waits for the branch
    const before = await page.evaluate(() => window.__copied.length);
    assert((await page.inputValue('#node-panel select.ask-ref')) === '', 'a branch was picked for the student');
    await page.click('#node-panel [data-ask="fix"]');
    assert((await page.evaluate(() => window.__copied.length)) === before, 'copied without a branch');
    assert(await page.evaluate(() => document.activeElement.matches('select.ask-ref.need')), 'picker not pointed out');
    await page.selectOption('#node-panel select.ask-ref', options[0]);
    await page.click('#node-panel [data-ask="fix"]');
    const first = await copied();
    const picked = await page.inputValue('#node-panel select.ask-ref');
    assert(first.includes(`fix step ${picked}`) && first.includes('revise_branch'), first.slice(0, 90));
    const other = options.find(o => o !== picked);
    await page.selectOption('#node-panel select.ask-ref', other);
    await page.click('#node-panel [data-ask="fork"]');
    const fork = await copied();
    const branch = other.split('#')[0].split('/').pop();
    assert(fork.includes(`from step ${other}`) && fork.includes(`keep ${branch} as it is`) && fork.includes('fork_branch'), fork.slice(0, 120));
    await page.click('#node-panel [data-ask="ref"]');
    assert((await copied()) === other, `ref ${await copied()}`);
    assert(await page.locator('#node-panel .ask .copied').isVisible(), 'no "Copied" note');
    return `${options.length} branches; picked ${other}`;
  });
  await check("on a branch's own graph the request is already about that branch", async () => {
    for (const v of branchViews) {
      await go(`pipelines/${v}`, true);
      const label = await page.getAttribute('#pipe-slot .graph', 'data-label');
      for (const k of await page.$$eval('#pipe-slot [data-history="0"] .node[data-key]:not(.ds):not(.stub)', ns => ns.map(n => n.dataset.key))) {
        await page.click(`#pipe-slot [data-history="0"] .node[data-key="${k}"]`);
        if (!(await page.locator('#node-panel select.ask-ref').count())) continue;
        const value = await page.inputValue('#node-panel select.ask-ref');
        assert(value.startsWith(`${label}#`), `${v}: picker says ${value || 'nothing'}`);
        return `${v}: ${value}`;
      }
    }
    return 'no shared step on a branch graph';
  });
  await go(`pipelines/${shared.v}`);
  await page.click(`#pipe-slot [data-history="0"] .node[data-key="${shared.k}"]`);
  await page.selectOption('#node-panel select.ask-ref', { index: 1 });
  await check('when copying is refused, the request appears to copy by hand', async () => {
    await page.evaluate(() => { window.__refuse = true; });
    await page.click('#node-panel [data-ask="fix"]');
    await page.waitForTimeout(100);
    const manual = page.locator('#node-panel .ask textarea.manual');
    assert(await manual.isVisible(), 'no textarea');
    assert((await manual.inputValue()).includes('inspect_step'), 'empty textarea');
    await page.evaluate(() => { window.__refuse = false; });
  });
  await check('panel folds: Parameters, Code (filled), Log, Details open', async () => {
    const titles = await page.$$eval('#node-panel .folds > details > summary', ss => ss.map(s => s.textContent.trim().split(' ')[0]));
    for (let i = 0; i < titles.length; i++) {
      await page.click(`#node-panel .folds > details:nth-child(${i + 1}) > summary`);
    }
    const open = await page.$$eval('#node-panel .folds > details', ds => ds.filter(d => d.open).length);
    assert(open === titles.length, `${open}/${titles.length} open`);
    const code = await page.$eval('#node-panel pre.code', p => p.textContent.length).catch(() => 0);
    assert(code > 100, `code ${code} chars`);
    return `${titles.join(', ')}; code ${code} chars`;
  });
  await check('Details → Open the run', async () => {
    const link = page.locator('#node-panel .folds a:has-text("Open the run")');
    if (!(await link.count())) return 'step never ran';
    await link.click();
    await page.waitForTimeout(150);
    assert((await hash()).startsWith('#runs/2'), await hash());
  });
  await check('cell map: drawn, colour-by switch, legend focus', async () => {
    await go(`pipelines/${shared.v}`);
    await page.waitForTimeout(200);
    for (const k of await page.$$eval('#pipe-slot [data-history="0"] .node[data-key]:not(.ds):not(.stub)', ns => ns.map(n => n.dataset.key))) {
      await page.click(`#pipe-slot [data-history="0"] .node[data-key="${k}"]`);
      if (!(await page.locator('#node-panel .cellmap').count())) continue;
      await page.waitForTimeout(500);
      const legend = await page.locator('#node-panel .cm-legend button').count();
      assert(legend > 0, 'no legend');
      const options = await page.$$eval('#node-panel .cm-bar select option', os => os.map(o => o.value));
      if (options.length > 1) await page.selectOption('#node-panel .cm-bar select', options[1]);
      await page.click('#node-panel .cm-legend button >> nth=0');
      assert(await page.locator('#node-panel .cm-legend button.on').count() === 1, 'legend did not focus');
      return `${legend} groups, ${options.length} colourings`;
    }
    return 'no cell map on this graph';
  });
  await check('figures link to files that exist', async () => {
    const hrefs = await page.$$eval('#node-templates template', ts => ts.flatMap(t => [...t.content.querySelectorAll('.thumbs a')].map(a => a.getAttribute('href'))));
    const dir = path.dirname(fileURLToPath(BASE));
    const missing = hrefs.filter(h => !fs.existsSync(path.join(dir, h)));
    assert(!missing.length, `missing ${missing.slice(0, 3).join(', ')}`);
    return `${hrefs.length} figures`;
  });
  await check('"Show that result" opens the earlier result of a step to re-run', async () => {
    for (const v of pipes) {
      await go(`pipelines/${v}`);
      for (const k of await page.$$eval('#pipe-slot [data-history="0"] .node[data-key]:not(.ds):not(.stub)', ns => ns.map(n => n.dataset.key))) {
        await page.click(`#pipe-slot [data-history="0"] .node[data-key="${k}"]`);
        const btn = page.locator('#node-panel [data-select-node]').first();
        if (!(await btn.count())) continue;
        const old = await btn.getAttribute('data-select-node');
        await btn.click();
        await page.waitForTimeout(150);
        const sel = await page.evaluate(() => document.querySelector('[data-history="1"]:not([hidden]) .node.selected')?.dataset.key);
        assert(sel === old, `selected ${sel} vs ${old}`);
        return `${v}: ${k} → ${old}`;
      }
    }
    return 'no step waiting for a re-run in this data';
  });
  await check('keyboard: Enter on a step opens its panel', async () => {
    await go(`pipelines/${B}`);
    await page.click('#node-panel .panel-close').catch(() => {});
    const node = page.locator('#pipe-slot [data-history="0"] .node[data-key]:not(.ds):not(.stub)').first();
    await node.focus();
    await page.keyboard.press('Enter');
    await page.waitForTimeout(100);
    assert(await page.evaluate(() => !document.querySelector('.pipes').classList.contains('no-node')), 'panel closed');
  });

  // ---------------------------------------------------------------- compare
  section = 'Compare';
  await go('experiments', true);
  await shot('04-compare');
  const rows = () => page.locator('#exp-table tbody tr[data-id]').count();
  const all = await rows();
  await check('search filters branches', async () => {
    const name = await page.locator('#exp-table tbody tr[data-id] td.name a').last().textContent();  // main would match its forks too
    await page.fill('#exp-bar input[type=search]', name);
    const n = await rows();
    assert(n >= 1 && n < all, `${n} of ${all}`);
    await page.fill('#exp-bar input[type=search]', '');
    assert((await rows()) === all, 'not restored');
    return `"${name}": ${n} of ${all}`;
  });
  await check('Filters opens the panel; every filter keeps focus and filters', async () => {
    await page.click('#exp-bar button.toggle >> nth=0');
    assert(await vis('.exp-filters'), 'panel hidden');
    const notes = [];
    const n = await page.locator('.exp-filters select').count();
    for (let i = 0; i < n; i++) {
      const sel = page.locator('.exp-filters select').nth(i);
      const label = await sel.getAttribute('aria-label');
      const values = await sel.locator('option').evaluateAll(os => os.map(o => o.value).filter(Boolean));
      if (!values.length) continue;
      await sel.focus();
      await sel.selectOption(values[0]);
      const focus = await sel.evaluate(s => document.activeElement === s);
      const btn = await page.textContent('#exp-bar button.toggle >> nth=0');
      const k = await rows();
      notes.push(`${label}=${values[0]}→${k}`);
      assert(focus, `${label}: focus lost`);
      assert(btn.includes('· 1'), `${label}: button "${btn}"`);
      await sel.selectOption('');
      assert((await rows()) === all || label === 'No grouping', `${label}: not restored`);
    }
    return notes.join(', ');
  });
  await check('fold sweeps / show archived keep focus; Reset restores', async () => {
    for (const text of ['fold sweeps', 'show archived']) {
      const box = page.locator(`.exp-filters label:has-text("${text}") input`);
      await box.click();
      assert(await box.evaluate(b => document.activeElement === b), `${text}: focus lost`);
    }
    await page.click('.exp-filters button:has-text("Reset")');
    assert((await rows()) === all, 'reset did not restore');
    assert(await vis('.exp-filters'), 'reset closed the panel');
    const btn = await page.textContent('#exp-bar button.toggle >> nth=0');
    assert(btn === 'Filters', btn);
  });
  await check('Steps & parameters adds and removes columns', async () => {
    const before = await page.locator('#exp-table thead th').count();
    await page.click('#exp-bar button.toggle >> nth=1');
    const after = await page.locator('#exp-table thead th').count();
    await page.click('#exp-bar button.toggle >> nth=1');
    const back = await page.locator('#exp-table thead th').count();
    assert(after > before && back === before, `${before} → ${after} → ${back}`);
    return `${before} → ${after} columns`;
  });
  await check('sorting by Branch, both ways', async () => {
    const unpinned = () => page.$$eval('#exp-table tbody tr[data-id]', rs => rs.filter(r => !r.querySelector('.star')).map(r => r.dataset.id));
    await page.click('#exp-table th.sortable button:has-text("Branch")');
    const asc = await unpinned();
    await page.click('#exp-table th.sortable button:has-text("Branch")');
    const desc = await unpinned();
    if (asc.length < 2) return 'fewer than 2 unpinned rows';
    const mark = await page.textContent('#exp-table th.sortable button:has-text("Branch")');
    assert(asc.join() !== desc.join() && mark.includes('↓'), `${asc[0]} / ${desc[0]} ${mark}`);
    return `${asc[0]} … / ${desc[0]} …`;
  });
  await check('ticking 2 rows compares them; Clear ends it', async () => {
    await page.click('#exp-table tbody tr[data-id] >> nth=0 >> input[type=checkbox]');
    await page.click('#exp-table tbody tr[data-id] >> nth=1 >> input[type=checkbox]');
    assert(await vis('#exp-compare'), 'no comparison');
    const title = await page.textContent('#exp-compare h3');
    await shot('05-compare-two');
    await page.click('#exp-compare button:has-text("Clear")');
    assert(!(await vis('#exp-compare')), 'still comparing');
    assert(!(await page.locator('#exp-table tbody input:checked').count()), 'boxes still ticked');
    return title;
  });
  await check('a branch name opens its pipeline', async () => {
    const href = await page.getAttribute('#exp-table tbody tr[data-id] td.name a', 'href');
    await page.click('#exp-table tbody tr[data-id] td.name a');
    await page.waitForTimeout(250);
    assert((await hash()) === href, `${await hash()} vs ${href}`);
  });
  await go('experiments');
  await check('the help ? stays on screen', () => hintsFit(view('experiments')));
  await check('a sweep folds into one row, opens, and ticks all its branches', async () => {
    const sweep = page.locator('#exp-table tr.sweep').first();
    if (!(await sweep.count())) return 'no sweeps in this data';
    const before = await page.locator('#exp-table tr.member').count();
    await sweep.locator('button.fold').click();
    const members = await page.locator('#exp-table tr.member').count();
    assert(before === 0 && members > 1, `members ${before} → ${members}`);
    await page.locator('#exp-table tr.sweep').first().locator('input[type=checkbox]').click();
    assert(await vis('#exp-compare'), 'no comparison');
    const title = await page.textContent('#exp-compare h3');
    await page.click('#exp-compare button:has-text("Clear")');
    await page.locator('#exp-table tr.sweep').first().locator('button.fold').click();
    assert((await page.locator('#exp-table tr.member').count()) === 0, 'did not fold back');
    return `${members} branches; ${title}`;
  });

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
      assert((await copied()).includes('revise_branch'), 'no fix request');
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
  await clocked.goto(`${BASE}#pipelines/${shared.v}`);
  await clocked.evaluate(() => sessionStorage.clear());
  await clocked.goto(`${BASE}#pipelines/${shared.v}`);
  await clocked.clock.runFor(500);
  await clocked.click(`#pipe-slot [data-history="0"] .node[data-key="${shared.k}"]`);
  await check('after a minute the page reloads and keeps the graph and the open step', async () => {
    await clocked.evaluate(() => { window.__marker = 1; });
    await clocked.clock.runFor(61000);
    await clocked.waitForLoadState('load');
    await clocked.clock.runFor(1000);
    await clocked.waitForTimeout(800);  // the graph file loads in real time
    const after = await clocked.evaluate(() => ({ m: window.__marker, h: location.hash, sel: document.querySelector('.node.selected')?.dataset.key }));
    assert(after.m === undefined, 'did not reload');
    assert(after.h === `#pipelines/${shared.v}` && after.sel === shared.k, JSON.stringify(after));
  });
  await check('an open ⋯ menu holds the reload', async () => {
    await clocked.click('.graph-head summary.dots');
    await clocked.evaluate(() => { window.__marker = 2; });
    await clocked.clock.runFor(61000);
    assert((await clocked.evaluate(() => window.__marker)) === 2, 'reloaded while the menu was open');
    await clocked.keyboard.press('Escape');
  });
  await check('the branch picked in the request box survives the reload', async () => {
    const options = await clocked.$$eval('#node-panel select.ask-ref option', os => os.map(o => o.value));
    const pick = options[options.length - 1];
    await clocked.selectOption('#node-panel select.ask-ref', pick);
    await clocked.evaluate(() => { window.__marker = 3; });
    await clocked.clock.runFor(61000 + 16000);  // typing holds it 15 s
    await clocked.waitForLoadState('load');
    await clocked.clock.runFor(1000);
    await clocked.waitForTimeout(800);
    const after = await clocked.evaluate(() => ({ m: window.__marker, v: document.querySelector('#node-panel select.ask-ref')?.value }));
    assert(after.m === undefined, 'did not reload');
    assert(after.v === pick, `${after.v} vs ${pick}`);
  });
  await clocked.close();

  // ---------------------------------------------------------------- phone
  section = 'Phone (375 px)';
  const phone = await browser.newContext({ viewport: { width: 375, height: 812 }, colorScheme: 'light', isMobile: true, hasTouch: true });
  const mp = await phone.newPage();
  mp.on('pageerror', e => errors.push(`phone: ${e.message}`));
  for (const h of ['journal', 'projects', 'pipelines', 'experiments', 'runs/history', 'library', 'cluster']) {
    await check(`${h}: nothing wider than the screen`, async () => {
      await mp.goto(`${BASE}#${h}`);
      await mp.waitForTimeout(300);
      const w = await mp.evaluate(() => document.documentElement.scrollWidth);
      await mp.screenshot({ path: path.join(OUT, `phone-${h.replace('/', '-')}.png`) });
      assert(w <= 376, `page is ${w}px wide`);
    });
  }
  await check('branch ⋯ menu fits the screen', async () => {
    await mp.goto(`${BASE}#projects`);
    await mp.waitForTimeout(200);
    await mp.tap('.project:not([hidden]) .row-link summary.dots >> nth=0');
    const r = await mp.$eval('.project:not([hidden]) .row-link details[open] .pop', p => { const b = p.getBoundingClientRect(); return [b.left, b.right]; });
    assert(r[0] >= 0 && r[1] <= 375, `menu at ${r.map(Math.round)}`);
  });
  await check('tips fit the screen', () => mp.evaluate(() => {
    const out = [];
    for (const h of document.querySelectorAll('.project:not([hidden]) .hint')) {
      h.focus(); const r = h.querySelector('.tip').getBoundingClientRect(); if (r.left < 0 || r.right > innerWidth) out.push(Math.round(r.left) + '..' + Math.round(r.right)); h.blur();
    }
    if (out.length) throw new Error('off screen: ' + out.join(', '));
  }));
  await phone.close();

  // light theme pictures
  const light = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'light' });
  const lp = await light.newPage();
  for (const h of ['projects', `pipelines/${shared.v}`, 'experiments']) {
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
