"""Inline CSS: system fonts, no external requests, light and dark themes."""

CSS = """
:root{--bg:#f6f5f1;--card:#fff;--text:#1f1e1d;--muted:#6b6a66;--line:#e5e3dc;--soft:#efede7;--accent:#185fa5;
--ok:#0f6e56;--okbg:#e1f5ee;--run:#185fa5;--runbg:#e6f1fb;--bad:#a32d2d;--badbg:#fcebeb;
--wait:#5f5e5a;--waitbg:#eeece6;--plan:#854f0b;--planbg:#faeeda;--ds:#444441;--dsbg:#e9e7e0;--shadow:0 1px 2px rgba(0,0,0,.04)}
@media (prefers-color-scheme:dark){:root{--bg:#1b1a19;--card:#242321;--text:#ecebe6;--muted:#a3a19b;--line:#383734;--soft:#2c2b28;
--accent:#85b7eb;--ok:#9fe1cb;--okbg:#10392f;--run:#b5d4f4;--runbg:#0f2f52;--bad:#f7c1c1;--badbg:#4d1c1c;
--wait:#d3d1c7;--waitbg:#33322f;--plan:#fac775;--planbg:#44300a;--ds:#d3d1c7;--dsbg:#302f2c;--shadow:none}}
*{box-sizing:border-box}[hidden]{display:none!important}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1,h2,h3,h4{font-weight:600;margin:0}h2{font-size:16px;margin:22px 0 10px}h3{font-size:15px}h4{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:14px 0 6px}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}.muted{color:var(--muted)}.small{font-size:12px}.right{margin-left:auto}
header.top{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px;padding:10px 20px;
background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(6px);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:8px;font-weight:600}.logo{width:18px;height:18px;border-radius:5px;background:linear-gradient(135deg,var(--ok),var(--run))}
.tabs{display:flex;gap:4px;flex-wrap:wrap}.tabs a{padding:6px 12px;border-radius:999px;color:var(--muted);transition:background .15s,color .15s}
.tabs a:hover{background:var(--soft);text-decoration:none;color:var(--text)}.tabs a.active{background:var(--text);color:var(--bg)}
.count{display:inline-block;min-width:18px;margin-left:4px;padding:0 5px;border-radius:9px;background:var(--soft);color:var(--text);font-size:11px;text-align:center}
.tabs a.active .count{background:var(--bg);color:var(--text)}
.meta{margin-left:auto;display:flex;gap:10px;align-items:center;color:var(--muted);font-size:12px}
button{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:4px 10px;cursor:pointer;transition:background .15s}
button:hover{background:var(--soft)}
main{max-width:1760px;margin:0 auto;padding:8px 20px 40px}
.view{display:none}.view.active{display:block;animation:fade .22s ease}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:14px}
.metric{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;color:var(--text);box-shadow:var(--shadow);transition:transform .15s,border-color .15s}
.metric:hover{transform:translateY(-2px);border-color:var(--muted);text-decoration:none}.metric span{color:var(--muted);font-size:12px}.metric b{display:block;font-size:26px;font-weight:600}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;color:var(--text);box-shadow:var(--shadow)}
a.card{transition:transform .15s,border-color .15s}a.card:hover{transform:translateY(-2px);border-color:var(--muted);text-decoration:none}
.run-title{display:flex;align-items:center;gap:8px;justify-content:space-between}.dots{display:flex;gap:4px;margin:8px 0}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--waitbg);border:1px solid var(--wait)}
.pill{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;white-space:nowrap;background:var(--waitbg);color:var(--wait)}
.COMPLETED{background:var(--okbg);color:var(--ok);border-color:var(--ok)}.RUNNING,.CONFIGURING{background:var(--runbg);color:var(--run);border-color:var(--run)}
.FAILED,.STOPPED,.MISSING{background:var(--badbg);color:var(--bad);border-color:var(--bad)}.PLANNED{background:var(--planbg);color:var(--plan);border-color:var(--plan)}
.dot.RUNNING{animation:pulse 1.4s ease-in-out infinite}@keyframes pulse{50%{opacity:.35}}
table{width:100%;border-collapse:collapse;font-size:13px;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{font-weight:500;color:var(--muted);background:var(--soft)}
tr:last-child td{border-bottom:none}td.num{text-align:right;font-variant-numeric:tabular-nums}
table.clickable tbody tr{cursor:pointer;transition:background .12s}table.clickable tbody tr:hover{background:var(--soft)}
table.kv{border:none;background:none}table.kv th{background:none;width:40%;font-weight:400}table.kv td,table.kv th{padding:3px 6px}
table.compact td,table.compact th{padding:4px 8px}
.toolbar{display:flex;gap:8px;margin:14px 0}.toolbar input,.toolbar select{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
.toolbar input{flex:1}.empty{color:var(--muted);padding:18px;text-align:center;border:1px dashed var(--line);border-radius:12px}
.note{padding:8px 12px;border-radius:8px;background:var(--soft);font-size:13px}.note.bad{background:var(--badbg);color:var(--bad)}.note.warn{background:var(--planbg);color:var(--plan)}
details{margin-top:8px}details summary{cursor:pointer;color:var(--muted)}pre{white-space:pre-wrap;font:12px/1.45 ui-monospace,Menlo,monospace;margin:6px 0}
pre.log{background:var(--soft);padding:8px 10px;border-radius:8px;max-height:260px;overflow:auto}pre.entry{background:var(--soft);padding:8px 10px;border-radius:8px}
.bar{height:6px;border-radius:3px;background:var(--soft);overflow:hidden;margin:4px 0}.bar span{display:block;height:100%;background:var(--run);transition:width .4s}
.pipes{display:grid;grid-template-columns:220px minmax(0,1fr) clamp(380px,36vw,760px);gap:16px;margin-top:14px;align-items:start}
.pipe-list,.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px;position:sticky;top:64px;max-height:calc(100vh - 80px);overflow:auto}
.panel{padding:14px 16px}
.pipe-group h4{margin:8px 6px 4px}.pipe{display:flex;align-items:center;gap:8px;width:100%;border:none;background:none;padding:6px 8px;border-radius:8px;text-align:left}
.pipe:hover{background:var(--soft)}.pipe.active{background:var(--text);color:var(--bg)}.pipe .small{margin-left:auto}
.legend{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:10px}.graph h3{margin:4px 0 10px;font-weight:500}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:8px}
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
.panel{min-height:200px}.panel.filled{animation:slide .2s ease}@keyframes slide{from{opacity:0;transform:translateX(8px)}to{opacity:1;transform:none}}
.dot.COMPLETED{background:var(--ok)}.dot.RUNNING{background:var(--run)}.dot.FAILED,.dot.STOPPED,.dot.MISSING{background:var(--bad)}
.dot.PLANNED{background:var(--planbg);border-style:dashed}
@media (max-width:1400px){.pipes{grid-template-columns:200px minmax(0,1fr) minmax(360px,42%)}}
@media (max-width:1100px){.pipes,.master{grid-template-columns:minmax(0,1fr)}.panel,.project-tree{max-height:none}.pipe-list{position:static;display:flex;flex-wrap:wrap;gap:6px;padding:8px}
.pipe-group{display:contents}.pipe-group h4{display:none}.pipe{width:auto;border:1px solid var(--line)}.pipe .small{margin-left:6px}.panel{position:static}}
.panel-head{display:flex;align-items:center;justify-content:space-between}.headline{font-weight:500;margin:6px 0}
dl.facts{display:grid;grid-template-columns:auto 1fr;gap:3px 10px;font-size:12.5px;margin:8px 0}dl.facts dt{color:var(--muted)}dl.facts dd{margin:0}
ul.plain{margin:0;padding-left:18px}.button{display:inline-block;margin-top:10px;padding:6px 12px;border:1px solid var(--line);border-radius:8px}
.thumbs{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}img.thumb{max-width:100%;max-height:220px;border:1px solid var(--line);border-radius:8px;background:#fff}
.back{display:inline-block;margin:14px 0 6px}.run-detail h2{margin:0}
ol.timeline{list-style:none;padding:0;margin:14px 0;border-left:2px solid var(--line);margin-left:8px}
.step{position:relative;margin:0 0 12px 18px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px;box-shadow:var(--shadow)}
.step .dot{position:absolute;left:-26px;top:14px;width:12px;height:12px}.step-head{display:flex;align-items:center;gap:8px}
.board{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}.column{background:var(--soft);border-radius:10px;padding:8px}
.idea{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 8px;margin-top:6px}
.project{margin-top:14px}.project h2{margin:0}.question{font-size:15px;margin:6px 0 12px}
.cellmap canvas{display:block;width:100%;max-width:560px;aspect-ratio:1;border:1px solid var(--line);border-radius:10px;background:var(--card)}
.cm-bar{display:flex;gap:8px;align-items:center;margin:6px 0}.cm-bar select{max-width:220px;font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:3px 8px}
.cm-legend{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}.cm-item{font-size:11px;padding:1px 7px;border-radius:999px;display:inline-flex;align-items:center;gap:5px}
.cm-item i{width:9px;height:9px;border-radius:50%;display:inline-block}.cm-item.on{border-color:var(--text);background:var(--soft)}
details.cells{margin:8px 0}.tag{display:inline-block;font-size:11px;padding:0 6px;border-radius:999px;background:var(--soft);color:var(--muted);margin-left:6px}
.session{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center}.session code{background:var(--soft);padding:1px 6px;border-radius:6px}
.subtabs{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0}.subtabs button{border-radius:999px;padding:5px 12px}
.subtabs button.active{background:var(--text);color:var(--bg);border-color:var(--text)}.subtabs button.active .count{background:var(--bg);color:var(--text)}
.listing .list-filter,.tree-filter{width:100%;font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px;margin-bottom:8px}
button.more{display:block;margin:10px auto 0}
.ask{margin:10px 0;padding:8px 10px;border:1px dashed var(--line);border-radius:10px;background:var(--soft)}
.ask-head{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.ask code.ref{background:var(--card);padding:1px 6px;border-radius:6px}
.ask-buttons{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.ask-buttons button{font-size:12.5px}
.ask-buttons button.quiet{color:var(--muted)}button.done{border-color:var(--ok);color:var(--ok)}
.ask textarea.manual{width:100%;margin-top:6px;font:12px ui-monospace,Menlo,monospace;background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px}
.chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0}.chip{display:inline-block;padding:2px 10px;border:1px solid var(--line);border-radius:999px;font-size:12.5px;background:var(--card)}
.master{display:grid;grid-template-columns:260px minmax(0,1fr);gap:16px;margin-top:14px;align-items:start}
.project-tree .pipe.active{background:var(--text);color:var(--bg)}ul.history>li{margin-bottom:6px}
details.account{position:relative}summary.avatar{list-style:none;width:30px;height:30px;border-radius:50%;display:grid;place-items:center;
background:linear-gradient(135deg,var(--ok),var(--run));color:#fff;font-weight:600;font-size:12px;cursor:pointer}
summary.avatar::-webkit-details-marker{display:none}
.account .menu{position:absolute;right:0;top:38px;z-index:10;min-width:260px;background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:10px;box-shadow:0 8px 24px rgba(0,0,0,.18);display:flex;flex-direction:column;gap:6px}.account .menu a{padding:6px 8px;border-radius:8px}
.account .menu a:hover{background:var(--soft);text-decoration:none}
.metric.static{cursor:default}.metric.static:hover{transform:none}.bar.warn span{background:var(--plan)}.bar.bad span{background:var(--bad)}
"""
