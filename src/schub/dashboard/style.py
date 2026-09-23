"""Inline CSS: system fonts, no external requests, light and dark themes.

Calm by design: few borders, room between things, one accent. Explanations live in
'?' hints (.hint) and secondary actions in '⋯' menus (.menu-pop), so a page shows
what a researcher needs and nothing else until asked."""

CSS = """
:root{--bg:#f7f6f2;--card:#fff;--text:#1f1e1d;--muted:#72716b;--line:#e8e6df;--soft:#efeee8;--accent:#185fa5;
--ok:#0f6e56;--okbg:#e1f5ee;--run:#185fa5;--runbg:#e6f1fb;--bad:#a32d2d;--badbg:#fcebeb;
--wait:#5f5e5a;--waitbg:#eeece6;--plan:#854f0b;--planbg:#faeeda;--ds:#444441;--dsbg:#e9e7e0;
--shadow:0 1px 2px rgba(0,0,0,.04);--float:0 10px 30px rgba(0,0,0,.14)}
@media (prefers-color-scheme:dark){:root{--bg:#1b1a19;--card:#242321;--text:#ecebe6;--muted:#a3a19b;--line:#34332f;--soft:#2c2b28;
--accent:#85b7eb;--ok:#9fe1cb;--okbg:#10392f;--run:#b5d4f4;--runbg:#0f2f52;--bad:#f7c1c1;--badbg:#4d1c1c;
--wait:#d3d1c7;--waitbg:#33322f;--plan:#fac775;--planbg:#44300a;--ds:#d3d1c7;--dsbg:#302f2c;--shadow:none;--float:0 10px 30px rgba(0,0,0,.5)}}
*{box-sizing:border-box}[hidden]{display:none!important}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1,h2,h3,h4{font-weight:600;margin:0}h2{font-size:16px;margin:22px 0 10px}h3{font-size:15px}
h4{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:500;margin:18px 0 6px}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}.muted{color:var(--muted)}.small{font-size:12px}.right{margin-left:auto}
button{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:4px 10px;cursor:pointer;transition:background .15s,border-color .15s}
button:hover{background:var(--soft)}button.link{border:none;background:none;padding:0;color:var(--accent);font-size:inherit}button.link:hover{text-decoration:underline;background:none}
button.quiet{color:var(--muted)}

/* header: the sc-hub menu, three tabs, one status */
header.top{position:sticky;top:0;z-index:40;display:flex;flex-wrap:wrap;align-items:center;gap:8px 20px;padding:12px 28px;
background:var(--bg);border-bottom:1px solid transparent}
.brand{display:flex;align-items:center;gap:8px;padding:4px 10px 4px 4px;border-radius:10px;cursor:pointer;list-style:none;transition:background .15s}
.brand:hover,.brand.here,details.account[open] .brand{background:var(--soft)}.brand::-webkit-details-marker{display:none}
.logo{width:28px;height:28px;border-radius:8px;display:grid;place-items:center;flex:none;color:#fff;font-size:11.5px;font-weight:600;letter-spacing:.02em;
background:linear-gradient(135deg,var(--ok),var(--run))}.logo.big{width:38px;height:38px;border-radius:10px;font-size:14px}
.caret{width:7px;height:7px;border-right:1.5px solid var(--muted);border-bottom:1.5px solid var(--muted);transform:rotate(45deg) translate(-2px,-2px);margin-left:2px}
.paused{font-size:11.5px;padding:1px 8px;border-radius:999px;background:var(--planbg);color:var(--plan)}
.status{margin-left:auto;display:inline-flex;align-items:center;gap:6px;font-size:12.5px;padding:4px 12px;border-radius:999px;color:var(--muted);background:var(--soft)}
.status:hover{text-decoration:none;color:var(--text)}.status.bad{color:var(--bad);background:var(--badbg)}
.tabs{display:flex;gap:2px;flex-wrap:wrap}.tabs a{padding:6px 14px;border-radius:999px;color:var(--muted);transition:background .15s,color .15s}
.tabs a:hover{text-decoration:none;color:var(--text)}.tabs a.active{background:var(--soft);color:var(--text);font-weight:500}
.count{display:inline-block;min-width:18px;margin-left:4px;padding:0 5px;border-radius:9px;background:var(--soft);color:var(--muted);font-size:11px;text-align:center}
details.account{position:relative;margin:0}
.account .menu{position:absolute;left:0;top:44px;z-index:10;width:300px;max-width:calc(100vw - 32px);background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:8px;box-shadow:var(--float);display:flex;flex-direction:column;gap:2px}
.account .who{display:flex;align-items:center;gap:10px;padding:6px 8px 10px}
.account .menu a{display:flex;flex-direction:column;padding:7px 10px;border-radius:8px;color:var(--text)}.account .menu a b{font-weight:500}
.account .menu a:hover{background:var(--soft);text-decoration:none}.account hr{border:none;border-top:1px solid var(--line);margin:6px 0;width:100%}
.menu-row{display:flex;justify-content:space-between;gap:10px;padding:6px 10px;font-size:12.5px}.menu-row b{font-weight:500}
.switch-row{display:flex;align-items:center;justify-content:space-between;border:none;background:none;padding:7px 10px;border-radius:8px;text-align:left;font-size:12.5px}
.switch{width:30px;height:18px;border-radius:9px;background:var(--line);position:relative;flex:none;transition:background .15s}
.switch::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:#fff;transition:transform .15s;box-shadow:0 1px 2px rgba(0,0,0,.3)}
.switch-row[aria-checked="true"] .switch{background:var(--ok)}.switch-row[aria-checked="true"] .switch::after{transform:translateX(12px)}

main{max-width:1760px;margin:0 auto;padding:12px 28px 56px}
.view{display:none}.view.active{display:block;animation:fade .22s ease}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
h1.view-title{font-size:22px;margin:16px 0 4px}

/* '?' hints and '⋯' menus */
.hint{position:relative;display:inline-flex;vertical-align:middle;margin-left:6px;outline:none}
.hint-mark{width:17px;height:17px;border-radius:50%;border:1px solid var(--line);color:var(--muted);font-size:10.5px;font-weight:600;
display:grid;place-items:center;cursor:help;transition:color .15s,border-color .15s;background:var(--card)}
.hint:hover .hint-mark,.hint:focus .hint-mark{color:var(--text);border-color:var(--muted)}
.tip{display:none;position:absolute;z-index:30;top:calc(100% + 8px);left:-6px;width:max-content;max-width:min(320px,calc(100vw - 40px));
padding:10px 12px;border-radius:12px;background:var(--card);border:1px solid var(--line);box-shadow:var(--float);
font-size:12.5px;line-height:1.5;font-weight:400;color:var(--text);text-transform:none;letter-spacing:0;white-space:normal;text-align:left}
.tip::before{content:"";position:absolute;left:0;right:0;top:-10px;height:10px}
.hint:hover .tip,.hint:focus .tip,.hint:focus-within .tip{display:block;animation:fade .15s ease}
.hint.end .tip{left:auto;right:-6px}.tip .legend{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px}
details.menu-pop{position:relative;display:inline-block;margin:0}
summary.dots{list-style:none;width:30px;height:30px;border-radius:8px;display:grid;place-items:center;color:var(--muted);font-size:18px;line-height:1;cursor:pointer;transition:background .15s,color .15s}
summary.dots::-webkit-details-marker{display:none}summary.dots:hover,details.menu-pop[open] summary.dots{background:var(--soft);color:var(--text)}
.pop{position:absolute;z-index:20;top:calc(100% + 6px);left:0;min-width:230px;max-width:360px;max-height:65vh;overflow:auto;
background:var(--card);border:1px solid var(--line);border-radius:14px;padding:6px;box-shadow:var(--float);text-align:left;cursor:default}
.menu-pop.end .pop{left:auto;right:0}
.pop>a,.pop>button,.pop-section>button{display:block;width:100%;text-align:left;padding:7px 10px;border:none;background:none;border-radius:8px;color:var(--text);font-size:13px;font-weight:400}
.pop>a:hover,.pop>button:hover,.pop-section>button:hover{background:var(--soft);text-decoration:none}
.pop-section{padding:8px 10px 4px;border-top:1px solid var(--line);margin-top:4px;font-size:13px}.pop>.pop-section:first-child{border-top:none;margin-top:0}
.pop-section>button{margin:0 -10px;width:calc(100% + 20px)}
.pop-label{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0 0 4px}
.pop pre.entry{font-size:11.5px;margin:4px 0}.pop ul.history{padding-left:16px;margin:0}

/* status */
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--waitbg);border:1px solid var(--wait);flex:none}
.pill{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;white-space:nowrap;background:var(--waitbg);color:var(--wait)}
.COMPLETED{background:var(--okbg);color:var(--ok);border-color:var(--ok)}.RUNNING,.CONFIGURING{background:var(--runbg);color:var(--run);border-color:var(--run)}
.FAILED,.STOPPED,.MISSING{background:var(--badbg);color:var(--bad);border-color:var(--bad)}.PLANNED{background:var(--planbg);color:var(--plan);border-color:var(--plan)}
.dot.RUNNING{animation:pulse 1.4s ease-in-out infinite}@keyframes pulse{50%{opacity:.35}}
.dot.COMPLETED{background:var(--ok)}.dot.RUNNING{background:var(--run)}.dot.FAILED,.dot.STOPPED,.dot.MISSING{background:var(--bad)}
.dot.PLANNED{background:var(--planbg);border-style:dashed}
.OUTDATED{background:var(--planbg);color:var(--plan);border-color:var(--plan)}.dot.OUTDATED{background:var(--plan)}
.BLOCKED{background:var(--badbg);color:var(--bad);border-color:var(--bad)}.dot.BLOCKED{background:var(--badbg);border-width:2px}
.ACTIVE{background:var(--runbg);color:var(--run);border-color:var(--run)}.dot.ACTIVE{background:var(--run)}
.WAITING{background:var(--runbg);color:var(--run);border-color:var(--run)}.dot.WAITING{background:var(--runbg);border-style:dashed}
.tag{display:inline-block;font-size:11px;padding:0 7px;border-radius:999px;background:var(--soft);color:var(--muted);margin-left:6px;font-weight:400}
.star{color:var(--plan);margin-right:4px}

/* shared blocks */
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:14px}
.metric{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;color:var(--text);box-shadow:var(--shadow)}
.metric span{color:var(--muted);font-size:12px}.metric>span:first-child{display:block;margin-bottom:6px}
.metrics+h3,.metrics+h2{margin-top:26px}.metric b{display:block;font-size:24px;font-weight:600}.metric:hover{text-decoration:none}
.bar{height:6px;border-radius:3px;background:var(--soft);overflow:hidden;margin:4px 0}.bar span{display:block;height:100%;background:var(--run);transition:width .4s}
.bar.warn span{background:var(--plan)}.bar.bad span{background:var(--bad)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;color:var(--text);box-shadow:var(--shadow)}
.run-title{display:flex;align-items:center;gap:10px;justify-content:space-between}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:500;color:var(--muted);font-size:12px}tr:last-child td{border-bottom:none}td.num{text-align:right;font-variant-numeric:tabular-nums}
table.clickable tbody tr{cursor:pointer;transition:background .12s}table.clickable tbody tr:hover{background:var(--soft)}
table.kv th{width:42%;font-weight:400;font-size:12.5px}table.kv td,table.kv th{padding:4px 6px;border-bottom:none}
table.compact td,table.compact th{padding:5px 8px}td.dots{white-space:nowrap}td.dots .dot{margin-right:3px}
.toolbar{display:flex;gap:8px;margin:14px 0}
input[type=search],select{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 10px}
.toolbar input{flex:1;max-width:460px}
.empty{color:var(--muted);padding:24px;text-align:center;border:1px dashed var(--line);border-radius:14px}
.note{padding:8px 12px;border-radius:10px;background:var(--soft);font-size:13px;margin:8px 0}.note.bad{background:var(--badbg);color:var(--bad)}.note.warn{background:var(--planbg);color:var(--plan)}
details{margin-top:8px}details summary{cursor:pointer;color:var(--muted)}pre{white-space:pre-wrap;font:12px/1.45 ui-monospace,Menlo,monospace;margin:6px 0}
pre.log{background:var(--soft);padding:8px 10px;border-radius:8px;max-height:260px;overflow:auto}pre.entry{background:var(--soft);padding:8px 10px;border-radius:8px}
details.quiet-details summary{font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0}.chip{display:inline-block;padding:2px 10px;border:1px solid var(--line);border-radius:999px;font-size:12.5px;background:var(--card)}
.subtabs{display:flex;flex-wrap:wrap;gap:4px;margin:14px 0}.subtabs button{border:none;background:none;border-radius:999px;padding:5px 14px;color:var(--muted)}
.subtabs button:hover{color:var(--text);background:none}.subtabs button.active{background:var(--soft);color:var(--text);font-weight:500}
.listing .list-filter,.tree-filter{width:100%;margin-bottom:8px}button.more{display:block;margin:12px auto 0}
.thumbs{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}img.thumb{max-width:100%;max-height:220px;border:1px solid var(--line);border-radius:10px;background:#fff}
ul.plain{margin:0;padding-left:18px}ul.history>li{margin-bottom:6px}
dl.facts{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-size:12.5px;margin:8px 0}dl.facts dt{color:var(--muted)}dl.facts dd{margin:0}
.headline{font-weight:500;margin:6px 0;font-size:15px}

/* Projects */
.master{display:grid;grid-template-columns:230px minmax(0,1fr);gap:40px;margin-top:18px;align-items:start}
.pipe-list{position:sticky;top:72px;max-height:calc(100vh - 90px);overflow:auto;padding:2px}
.pipe-group h4{margin:14px 10px 4px}.pipe-group:first-child h4{margin-top:0}
.pipe{display:flex;align-items:center;gap:9px;width:100%;border:none;background:none;padding:7px 10px;border-radius:9px;text-align:left;color:var(--muted)}
.pipe:hover{background:var(--soft);color:var(--text)}.pipe.active{background:var(--soft);color:var(--text);font-weight:500}.pipe .small{margin-left:auto}
.detail{max-width:980px}.project-head{display:flex;align-items:center;gap:10px}.project-head h2{margin:0;font-size:22px}
.project-head .menu-pop{margin-left:auto}.question{font-size:17px;line-height:1.5;margin:8px 0 6px}
.meta{color:var(--muted);font-size:13px;margin:0 0 8px}.meta a{color:inherit;border-bottom:1px dotted var(--muted)}.meta a:hover{text-decoration:none;color:var(--text)}
.section-head{display:flex;align-items:center;margin:34px 0 8px}.section-head h3{font-size:15px}
.links{margin-left:auto;display:inline-flex;gap:14px;align-items:center;font-size:13px}
.rows{border-top:1px solid var(--line)}
.row-link{display:flex;align-items:center;gap:16px;padding:13px 12px;border-bottom:1px solid var(--line);cursor:pointer;transition:background .12s;border-radius:10px}
.row-link:hover{background:var(--soft)}.row-main{flex:1;min-width:0}
.row-title{display:flex;align-items:baseline;flex-wrap:wrap;gap:0 4px}
a.row-name{color:var(--text);font-weight:600;font-size:14.5px}.origin{color:var(--muted);font-size:12px;margin-left:8px}
.desc{color:var(--muted);font-size:13px;margin-top:1px}.row-side{display:flex;align-items:center;gap:6px;flex:none}
ul.ideas{list-style:none;padding:0;margin:0;border-top:1px solid var(--line)}
li.idea{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;padding:11px 12px;border-bottom:1px solid var(--line)}

/* Pipelines */
.pipes{display:grid;grid-template-columns:220px minmax(0,1fr) clamp(380px,36vw,760px);gap:24px;margin-top:18px;align-items:start}
.pipes.no-node{grid-template-columns:220px minmax(0,1fr)}.pipes.no-node .panel{display:none}
.panel{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px 20px;position:sticky;top:72px;max-height:calc(100vh - 90px);overflow:auto;min-height:200px;box-shadow:var(--shadow)}
.panel.filled{animation:slide .2s ease}@keyframes slide{from{opacity:0;transform:translateX(8px)}to{opacity:1;transform:none}}
.panel-close{position:absolute;top:12px;right:14px;border:none;background:none;font-size:20px;line-height:1;color:var(--muted);cursor:pointer}
.panel-head{display:flex;align-items:center;gap:10px;padding-right:26px}.panel-head h3{font-size:17px}
.graph-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:2px 0 14px}.graph-head h3{margin:0;font-size:17px}
.graph-actions{margin-left:auto;display:inline-flex;align-items:center;gap:6px;font-size:12.5px}.graph-actions .hint{margin:0 2px}
.graph-actions button.nb-download{font-size:12.5px;padding:3px 12px;border-radius:999px}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:12px}
.legend{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.lineage .edge{fill:none;stroke:var(--muted);stroke-width:1.2;transition:opacity .2s}.lineage .dashed{stroke-dasharray:5 4}
.lineage .t1{font-size:12.5px;font-weight:600}.lineage .t2{font-size:11px}
.node{cursor:pointer;transition:opacity .2s}.node rect{stroke-width:1.2;transition:stroke-width .15s}.node:hover rect,.node:focus rect{stroke-width:2.4}.node:focus{outline:none}
.lineage.focused .node:not(.on-path){opacity:.35}.node.selected rect{stroke-width:3}
.node.ds{cursor:default}.node.ds rect{fill:var(--dsbg);stroke:var(--ds)}.node.ds text{fill:var(--ds)}
.node rect{fill:var(--waitbg);stroke:var(--wait)}.node text{fill:var(--wait)}
.node.st-COMPLETED rect{fill:var(--okbg);stroke:var(--ok)}.node.st-COMPLETED text{fill:var(--ok)}
.node.st-RUNNING rect{fill:var(--runbg);stroke:var(--run)}.node.st-RUNNING text{fill:var(--run)}
.node.st-FAILED rect,.node.st-STOPPED rect,.node.st-MISSING rect{fill:var(--badbg);stroke:var(--bad)}.node.st-FAILED text,.node.st-STOPPED text,.node.st-MISSING text{fill:var(--bad)}
.node.st-PLANNED rect{fill:var(--planbg);stroke:var(--plan);stroke-dasharray:5 4}.node.st-PLANNED text{fill:var(--plan)}
.node.outdated rect{stroke-dasharray:none;stroke-width:1.6}.node.old{opacity:.5}.lineage.focused .node.old:not(.on-path){opacity:.25}
.node .flag{fill:var(--plan);font-size:13px;font-weight:700}.node.flagged rect{stroke-width:1.8}
.node.st-OUTDATED rect{fill:var(--planbg);stroke:var(--plan)}.node.st-BLOCKED rect{fill:var(--badbg);stroke:var(--bad)}
.node.st-WAITING rect{fill:var(--runbg);stroke:var(--run);stroke-dasharray:3 3}
.node.stub rect{fill:var(--card);stroke-dasharray:2 3}.node.stub text{fill:var(--accent)}.lineage .edge.stub{stroke-dasharray:2 4;opacity:.7}
.result{margin:10px 0}.folds{margin-top:18px;border-top:1px solid var(--line)}
.folds>details{margin:0;padding:10px 2px;border-bottom:1px solid var(--line)}
.folds>details>summary{list-style:none;display:flex;align-items:center;gap:8px;color:var(--text);font-weight:500;font-size:13.5px}
.folds>details>summary::-webkit-details-marker{display:none}
.folds>details>summary::after{content:"";margin-left:auto;width:7px;height:7px;border-right:1.5px solid var(--muted);border-bottom:1.5px solid var(--muted);transform:rotate(-45deg);transition:transform .15s}
.folds>details[open]>summary::after{transform:rotate(45deg)}.folds>details>summary .small{font-weight:400;color:var(--muted)}
.ask{margin:18px 0 6px;padding:12px 14px;border-radius:14px;background:var(--soft)}
.ask-head{display:flex;align-items:center;gap:4px}.ask-head b{font-weight:500;font-size:13.5px}.ask-head .hint{margin-left:auto}
.ask-buttons{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.ask-buttons button{font-size:13px;padding:5px 12px;border-radius:999px}
.ask-buttons button.primary{background:var(--text);color:var(--bg);border-color:var(--text)}.ask-buttons button.primary:hover{opacity:.88}
button.done,.ask-buttons button.done{border-color:var(--ok);color:var(--ok);background:var(--okbg)}
.ask-foot{display:flex;align-items:center;gap:10px;margin-top:8px;font-size:12px;color:var(--muted)}
.ask-foot code.ref{background:var(--card);padding:1px 6px;border-radius:6px}.ask-foot select{font-size:12px;padding:2px 6px;max-width:60%}
.ask-foot button.link{font-size:12px;color:var(--muted)}.ask-foot select.need{border-color:var(--plan);box-shadow:0 0 0 2px var(--planbg)}
.ask textarea.manual,.nb-box textarea.manual{width:100%;margin-top:6px;font:12px ui-monospace,Menlo,monospace;background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px}
.pop .ask{margin:4px 0 0;padding:8px 10px 4px;border-radius:0;background:none}
#node-panel .ask{position:sticky;bottom:-18px;z-index:2;box-shadow:0 -10px 24px -12px rgba(0,0,0,.35)}
.cellmap canvas{display:block;width:100%;max-width:560px;aspect-ratio:1;border:1px solid var(--line);border-radius:12px;background:var(--card)}
.cm-bar{display:flex;gap:8px;align-items:center;margin:6px 0}.cm-bar select{max-width:220px;padding:3px 8px}
.cm-legend{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}.cm-item{font-size:11px;padding:1px 7px;border-radius:999px;display:inline-flex;align-items:center;gap:5px}
.cm-item i{width:9px;height:9px;border-radius:50%;display:inline-block}.cm-item.on{border-color:var(--text);background:var(--soft)}
details.cells{margin:8px 0}
details.code summary .small{margin-left:6px}
pre.code{background:var(--soft);padding:10px 12px;border-radius:8px;max-height:520px;overflow:auto;white-space:pre;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;tab-size:4}
.c-com{color:var(--muted);font-style:italic}.c-str{color:var(--ok)}.c-key{color:var(--accent);font-weight:600}.c-num{color:var(--plan)}

/* Compare */
.exp-top{display:flex;align-items:flex-start;gap:8px;margin:18px 0 12px}.exp-bar{flex:1;min-width:0}.exp-top>.hint{margin-top:7px}
.exp-line{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.exp-line input[type=search]{flex:1 1 260px;max-width:460px}
button.toggle{border-radius:999px;padding:5px 14px;color:var(--muted)}button.toggle.on{background:var(--soft);color:var(--text);border-color:var(--muted)}
.exp-filters{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:10px;padding:10px 12px;border-radius:14px;background:var(--soft)}
.exp-filters select{padding:5px 8px}
table.exp-rows td,table.exp-rows th{vertical-align:top}table.exp-rows td.name{min-width:160px}
table.exp-rows th.sortable button{border:none;background:none;font:inherit;font-weight:500;color:inherit;padding:0;cursor:pointer;white-space:nowrap}
table.exp-rows td.sel,table.exp-rows th.sel{width:26px}table.exp-rows tr.member td.name{padding-left:26px}
table.exp-rows tr.sweep{background:var(--soft)}table.exp-rows tr.section td,table.compare tr.section td{font-weight:600;color:var(--muted);background:var(--bg);padding-top:14px}
td.num.stale{color:var(--muted);font-style:italic}.steps .st{white-space:nowrap}table.exp-rows td.steps{min-width:170px}.steps .st.OUTDATED,.steps .st.PLANNED{color:var(--plan)}.steps .st.FAILED{color:var(--bad)}
.steps .flag,.flag{color:var(--plan);font-weight:700}button.fold{padding:0 4px}
.exp-compare{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 16px;margin-bottom:14px}
ul.verdicts{margin:6px 0 10px;padding-left:18px}ul.verdicts li{margin:3px 0}table.compare td.same{background:var(--okbg)}
table.compare img.thumb{max-width:180px;margin:2px}

/* Runs, sessions, notebooks */
.back{display:inline-block;margin:14px 0 6px}.run-detail h2{margin:0}
ol.timeline{list-style:none;padding:0;margin:14px 0;border-left:2px solid var(--line);margin-left:8px}
.step{position:relative;margin:0 0 12px 18px;background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:14px;padding:12px 16px;box-shadow:var(--shadow)}
.step .dot{position:absolute;left:-26px;top:16px;width:12px;height:12px}.step-head{display:flex;align-items:center;gap:8px}
.session{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center}.session code{background:var(--soft);padding:1px 6px;border-radius:6px}
.nb-box{margin:12px 0 4px;padding:12px 14px;border-radius:14px;background:var(--soft)}.nb-box .ask-buttons button{font-size:12.5px}
.why-wait{margin-top:14px}

@media (max-width:1400px){.pipes{grid-template-columns:200px minmax(0,1fr) minmax(360px,42%)}.pipes.no-node{grid-template-columns:200px minmax(0,1fr)}}
@media (max-width:1100px){.pipes,.master,.pipes.no-node{grid-template-columns:minmax(0,1fr);gap:16px}.panel,.pipe-list{max-height:none}
.pipe-list{position:static;display:flex;flex-wrap:wrap;gap:6px;padding:0}.pipe-group{display:contents}.pipe-group h4{display:none}
.pipe{width:auto;border:1px solid var(--line)}.pipe .small{margin-left:6px}.panel{position:relative;top:0}}
@media (max-width:700px){header.top{padding:10px 16px;gap:8px}.status{order:2}.tabs{order:3;width:100%;flex-wrap:nowrap;overflow-x:auto}
.tabs a{white-space:nowrap}main{padding:8px 16px 32px}table{display:block;overflow-x:auto}
.row-link{flex-wrap:wrap;gap:8px}.links{gap:10px}.section-head{flex-wrap:wrap}}
"""

JOURNAL_CSS = """
.jbar{display:flex;align-items:center;gap:8px;margin-bottom:10px}.jbar>.hint{margin-left:auto}.journal-tree{display:flex;flex-wrap:wrap;gap:4px}
.journal-tree button{padding:5px 12px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--text)}
.journal-tree button.active{background:var(--text);color:var(--card);border-color:var(--text)}
.jtitle{display:flex;align-items:center;gap:8px}.jtitle h2{margin:6px 0}.question{margin:0 0 6px;color:var(--muted)}
.jstate{display:flex;gap:10px;align-items:center;margin:4px 0 12px;font-size:13px}
.jcards{display:flex;flex-direction:column;gap:10px}
.jcard{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 14px;box-shadow:var(--shadow)}
.jhead{display:flex;align-items:center;gap:8px}.jhead .why{font-weight:500}.expect{margin:2px 0 6px}
.badges{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0}
pre.out,pre.code{background:var(--soft);border-radius:8px;padding:8px 10px;margin:6px 0;white-space:pre-wrap;word-break:break-word;font:12px ui-monospace,SFMono-Regular,Menlo,monospace;max-height:420px;overflow:auto}
pre.out.err{color:var(--bad,#b3261e)}
img.jfig{max-width:100%;max-height:360px;border:1px solid var(--line);border-radius:8px;margin:6px 0;display:block;background:#fff}
details.fold>summary{cursor:pointer;color:var(--muted);font-size:12px;margin-top:4px}
details.more-out>summary{list-style:none;cursor:pointer}.jfoot{margin-top:6px}
.note-card .note-text{margin:6px 0;white-space:pre-wrap}.tag.kind{font-weight:500}
"""
