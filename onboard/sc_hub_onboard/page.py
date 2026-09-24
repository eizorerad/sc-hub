"""The onboarding page: a progress bar, the steps with one line each, and a form when a step needs the
student. Plain HTML, CSS and a little JavaScript, no requests anywhere but this computer."""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Setting up sc-hub</title>
<style>
:root{--bg:#f7f6f2;--card:#fff;--text:#1f1e1d;--muted:#72716b;--line:#e8e6df;--soft:#efeee8;--accent:#185fa5;
--ok:#0f6e56;--bad:#a32d2d;--warn:#854f0b;--warnbg:#faeeda}
@media (prefers-color-scheme:dark){:root{--bg:#1b1a19;--card:#242321;--text:#ecebe6;--muted:#a3a19b;--line:#34332f;
--soft:#2c2b28;--accent:#85b7eb;--ok:#9fe1cb;--bad:#f7c1c1;--warn:#fac775;--warnbg:#44300a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:760px;margin:0 auto;padding:28px 16px 60px}h1{font-size:22px;font-weight:600;margin:0 0 2px}
.sub{color:var(--muted);margin:0 0 18px}.bar{height:8px;background:var(--soft);border-radius:99px;overflow:hidden;margin:0 0 6px}
.bar>i{display:block;height:100%;background:var(--accent);width:0;transition:width .4s}.pct{color:var(--muted);font-size:13px;margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:0 0 18px}
.ask{border-color:var(--accent)}.ask h2{font-size:17px;margin:0 0 6px}.ask p{margin:6px 0}
label{display:block;margin:10px 0 4px;font-size:13px;color:var(--muted)}input[type=text],input[type=password]{width:100%;padding:9px 11px;
border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font-size:15px}
.check{display:flex;gap:8px;align-items:flex-start;margin:10px 0;color:var(--text);font-size:14px}.check input{margin-top:4px}
button{font:inherit;padding:8px 16px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}.row{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
.code{font:600 22px ui-monospace,Menlo,monospace;letter-spacing:.08em;padding:8px 12px;background:var(--soft);border-radius:8px;display:inline-block}
a{color:var(--accent)}.steps{list-style:none;margin:0;padding:0}.steps li{border-bottom:1px solid var(--line);padding:10px 2px}
.head{display:flex;gap:10px;align-items:baseline}.icon{width:18px;flex:none;text-align:center}.done .icon{color:var(--ok)}
.failed .icon{color:var(--bad)}.skipped .icon,.waiting{color:var(--muted)}.title{font-weight:500}.detail{color:var(--muted);font-size:13.5px;margin:2px 0 0 28px}
.hint{margin:6px 0 0 28px;padding:8px 10px;background:var(--warnbg);color:var(--warn);border-radius:8px;font-size:13.5px}
details{margin:4px 0 0 28px}summary{cursor:pointer;color:var(--muted);font-size:12.5px}pre{white-space:pre-wrap;font:12px ui-monospace,Menlo,monospace;
background:var(--soft);padding:8px 10px;border-radius:8px;max-height:260px;overflow:auto}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:s 1s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}.err{color:var(--bad);font-size:13px}
</style></head><body><main>
<h1>Setting up sc-hub</h1><p class="sub" id="sub">Your cluster, your assistants and a first run, in one go. Keep this page open.</p>
<div class="bar"><i id="bar"></i></div><p class="pct" id="pct">Starting…</p>
<div id="ask"></div><div id="done"></div><ul class="steps" id="steps"></ul>
</main><script>
const TOKEN = "__TOKEN__";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const call = (path, body) => fetch(path, {method: body === undefined ? "GET" : "POST", headers: {"X-Onboard-Token": TOKEN,
  "Content-Type": "application/json"}, body: body === undefined ? undefined : JSON.stringify(body)}).then(r => r.json());
const ICON = {waiting: "○", running: '<span class="spin"></span>', asking: "?", done: "✓", skipped: "–", failed: "✗"};
let shownAsk = "", openLogs = new Set();
const WORKING = "working";  // an answer was sent: the next state clears this note or shows the next form

function renderAsk(step) {
  const form = step && step.ask;
  const key = form ? step.id + JSON.stringify(form) : "";
  if (key === shownAsk) return;
  shownAsk = key;
  if (!form) { $("#ask").innerHTML = ""; return; }
  const fields = (form.fields || []).map(f => f.type === "checkbox"
    ? `<label class="check"><input type="checkbox" name="${esc(f.name)}"${f.value ? " checked" : ""}> <span>${esc(f.label)}</span></label>`
    : `<label for="f-${esc(f.name)}">${esc(f.label)}</label><input id="f-${esc(f.name)}" name="${esc(f.name)}" type="${f.type === "password" ? "password" : "text"}"
        value="${esc(f.value || "")}" placeholder="${esc(f.placeholder || "")}" autocomplete="${f.type === "password" ? "current-password" : "off"}">`).join("");
  const text = (form.text || []).map(p => `<p>${esc(p)}</p>`).join("");
  const links = (form.links || []).filter(l => /^https:\/\//.test(l.url)).map(l => `<p><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.label)}</a>
    <button type="button" data-copy="${esc(l.url)}">Copy link</button></p>`).join("");
  const code = form.code ? `<p>Code: <span class="code">${esc(form.code)}</span>
    <button type="button" data-copy="${esc(form.code)}">Copy code</button></p>` : "";
  const waiting = form.wait ? `<p><span class="spin"></span> ${esc(form.wait_text || "Waiting for you to finish in the browser…")}</p>` : "";
  const buttons = [form.wait ? "" : `<button class="primary" type="submit">${esc(form.submit || "Continue")}</button>`,
    ...(form.choices || []).map(c => `<button type="button" data-choice="${esc(c.name)}">${esc(c.label)}</button>`),
    form.cancel === false ? "" : '<button type="button" id="cancel">Stop here</button>'].join("");
  $("#ask").innerHTML = `<form class="card ask" id="form"><h2>${esc(form.title || step.title)}</h2>${text}${links}${code}${fields}
    <p class="err" id="err"></p>${waiting}<div class="row">${buttons}</div></form>`;
  const first = $("#form input:not([type=checkbox])"); if (first) first.focus();
  $("#form").addEventListener("submit", async e => {
    e.preventDefault();
    const answer = {};
    for (const input of $("#form").querySelectorAll("input")) answer[input.name] = input.type === "checkbox" ? input.checked : input.value;
    const missing = (form.fields || []).filter(f => f.required && !answer[f.name]);
    if (missing.length) { $("#err").textContent = `Fill in: ${missing.map(f => f.label).join(", ")}`; return; }
    for (const input of $("#form").querySelectorAll("input[type=password]")) input.value = "";
    shownAsk = WORKING; $("#ask").innerHTML = '<p class="card">Working on it…</p>';
    await call("/api/answer", {...answer, form_id: form.id});
  });
  const cancel = $("#cancel"); if (cancel) cancel.onclick = () => { shownAsk = WORKING; call("/api/answer", {cancel: true, form_id: form.id}); };
  for (const button of $("#form").querySelectorAll("[data-choice]")) button.onclick = () => {
    shownAsk = WORKING; $("#ask").innerHTML = '<p class="card">Working on it…</p>'; call("/api/answer", {choice: button.dataset.choice, form_id: form.id});
  };
}

function render(state) {
  $("#bar").style.width = state.progress + "%";
  $("#pct").textContent = state.finished ? "Done" : `${state.progress}%`;
  renderAsk(state.steps.find(s => s.status === "asking"));
  $("#steps").innerHTML = state.steps.map(s => `<li class="${s.status}"><div class="head"><span class="icon">${ICON[s.status] || ""}</span>
    <span class="title">${esc(s.title)}</span></div>${s.detail ? `<div class="detail">${esc(s.detail)}</div>` : ""}
    ${s.status === "failed" ? `<div class="hint">${esc(s.hint || "Something went wrong.")} <button type="button" data-retry="${esc(s.id)}">Retry</button></div>` : ""}
    ${s.log.length ? `<details data-log="${esc(s.id)}"${openLogs.has(s.id) ? " open" : ""}><summary>Details</summary><pre>${esc(s.log.join("\n"))}</pre></details>` : ""}</li>`).join("");
  const summary = state.summary || {};
  $("#done").innerHTML = state.finished ? `<div class="card"><h2 style="font-size:17px;margin:0 0 6px">Ready</h2>
    ${(summary.lines || []).map(l => `<p>${esc(l)}</p>`).join("")}</div>` : "";
}

document.addEventListener("click", e => {
  const retry = e.target.closest("[data-retry]"); if (retry) { call("/api/retry", {step: retry.dataset.retry}); return; }
  const copy = e.target.closest("[data-copy]");
  if (copy) navigator.clipboard?.writeText(copy.dataset.copy).then(() => { copy.textContent = "Copied"; });
});
document.addEventListener("toggle", e => { const d = e.target; if (d.dataset && d.dataset.log) (d.open ? openLogs.add(d.dataset.log) : openLogs.delete(d.dataset.log)); }, true);
async function poll() { try { render(await call("/api/state")); } catch (e) { $("#pct").textContent = "The helper stopped: run it again to continue."; } setTimeout(poll, 1000); }
call("/api/start", {}).then(poll);
</script></body></html>
"""
