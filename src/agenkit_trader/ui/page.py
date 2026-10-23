"""The single HTML page: vanilla JS, no external resources. Every dynamic value
goes through esc() before it is placed in markup."""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgenKit Trader (read-only)</title>
<style>
:root{--bg:#fff;--fg:#1b1f24;--mut:#5b6570;--line:#d8dde3;--warn:#a15c00;--bad:#b42318;--ok:#067647}
@media (prefers-color-scheme:dark){:root{--bg:#14171b;--fg:#e6e9ed;--mut:#9aa4af;--line:#2d343c;--warn:#f0b429;--bad:#ff7b72;--ok:#56d364}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
header{padding:12px 16px;border-bottom:1px solid var(--line)}
h1{font-size:16px;margin:0}.mut{color:var(--mut)}
nav{display:flex;gap:4px;flex-wrap:wrap;padding:8px 16px;border-bottom:1px solid var(--line)}
nav button{background:none;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 10px;cursor:pointer;font:inherit}
nav button[aria-selected=true]{border-color:var(--fg);font-weight:600}
main{padding:16px;max-width:1200px}
table{border-collapse:collapse;width:100%;margin:8px 0}
th,td{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
.scroll{overflow-x:auto}.warn{color:var(--warn);font-weight:600}.bad{color:var(--bad);font-weight:600}.ok{color:var(--ok);font-weight:600}
h2{font-size:15px;margin:20px 0 6px}input,select{font:inherit;padding:4px;background:var(--bg);color:var(--fg);border:1px solid var(--line)}
pre{white-space:pre-wrap}svg{width:100%;max-width:720px;height:auto;border:1px solid var(--line)}
</style></head><body>
<header><h1>AgenKit Trader</h1>
<div class="mut">Read-only local view. Shows only what is in the files; it can change nothing.</div></header>
<nav role="tablist" id="tabs"></nav>
<main id="view" aria-live="polite">Loading...</main>
<script>
"use strict";
function esc(v){return String(v==null?"":v).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");}
var TABS=[["status","Status"],["decisions","Decisions"],["research","Research"],["risk","Risk"],["failure","How could this fail"]];
var cur="status", timer=null, decisions=null;
function pct(x){return typeof x==="number"?(x*100).toFixed(1)+"%":"n/a";}
function fx(x,d){return typeof x==="number"?x.toFixed(d===undefined?2:d):"n/a";}
function note(m){return m?'<p class="warn">'+esc(m)+"</p>":"";}
function table(head,rows){
  return '<div class="scroll"><table><thead><tr>'+head.map(function(h){return "<th>"+esc(h)+"</th>";}).join("")+
  "</tr></thead><tbody>"+rows.map(function(r){return "<tr>"+r.map(function(c){return "<td>"+esc(c)+"</td>";}).join("")+"</tr>";}).join("")+"</tbody></table></div>";
}
function chart(eq){
  var W=700,H=160,P=8,n=eq.length,lo=Infinity,hi=-Infinity,i;
  for(i=0;i<n;i++){lo=Math.min(lo,eq[i].equity);hi=Math.max(hi,eq[i].equity);}
  if(hi===lo){hi=lo+1;}
  function x(k){return P+k*(W-2*P)/(n-1);}
  var line=eq.map(function(e,k){return x(k).toFixed(1)+","+(H-P-(e.equity-lo)/(hi-lo)*(H-2*P)).toFixed(1);}).join(" ");
  var md=0;eq.forEach(function(e){md=Math.max(md,e.drawdown);});if(md===0){md=1;}
  var dd=eq.map(function(e,k){return x(k).toFixed(1)+","+(P+e.drawdown/md*(H-2*P)).toFixed(1);}).join(" ");
  return "<h2>Equity (as logged with each order)</h2>"+
  '<svg viewBox="0 0 '+W+" "+H+'" role="img" aria-label="equity"><polyline fill="none" stroke="currentColor" stroke-width="2" points="'+line+'"/></svg>'+
  '<div class="mut">min '+esc(fx(lo))+", max "+esc(fx(Math.max.apply(null,eq.map(function(e){return e.equity;}))))+", "+n+" points</div>"+
  "<h2>Drawdown from peak</h2>"+
  '<svg viewBox="0 0 '+W+" "+H+'" role="img" aria-label="drawdown"><polyline fill="none" stroke="currentColor" stroke-width="2" points="'+dd+'"/></svg>'+
  '<div class="mut">worst '+esc(pct(Math.max.apply(null,eq.map(function(e){return e.drawdown;}))))+" (bottom of chart = worst)</div>";
}
function viewStatus(s){
  var h="<h2>Mode</h2><p><b>"+esc(s.mode)+"</b>";
  if(s.halt_reason){h+='<br><span class="bad">halted: '+esc(s.halt_reason)+"</span>";}
  h+="</p>"+note(s.log_notice)+note(s.log_message)+note(s.state_error);
  if(s.log_lines_skipped){h+=note(s.log_lines_skipped+" unreadable log line(s) skipped");}
  h+="<h2>Last decision</h2><p>"+(s.last_decision_ts?esc(s.last_decision_ts):"none recorded")+"</p>";
  if(s.stale===true){h+='<p class="warn">stale: no log record for over '+esc(s.stale_after_hours)+" hours; the runtime may not be running.</p>";}
  h+="<h2>Open positions and stops</h2>";
  if(!s.positions.length){h+="<p>"+(s.positions_note?esc(s.positions_note):"none in the decision log")+(s.no_trades?" (no paper trades yet)":"")+"</p>";}
  else{h+=table(["symbol","net qty","stop price","source"],s.positions.map(function(p){return [p.symbol,p.net_qty,p.stop_price===null?"no stop logged":p.stop_price,p.source];}));}
  if(s.state){h+="<h2>State file</h2>"+table(["field","value"],[["baseline equity",s.state.baseline_equity],["peak equity",s.state.peak_equity],["day",s.state.day],["day start equity",s.state.day_start_equity],["halted",s.state.halted||"no"]].map(function(r){return [r[0],r[1]===null||r[1]===undefined?"not recorded":r[1]];}));
    var lb=Object.keys(s.state.last_bar||{});if(lb.length){h+=table(["deployment","last bar"],lb.map(function(k){return [k,s.state.last_bar[k]];}));}}
  var g=s.live_gate;
  h+='<h2>Live gate</h2><p class="mut">"locked" or "thresholds met". Live order placement is not implemented in this build.</p><p><b>'+esc(g.state)+"</b></p>"+note(g.error)+"<p>enabled in config: "+esc(g.enabled)+"<br>paper days: "+esc(g.paper_days===null?(g.paper_note||"unknown"):fx(g.paper_days))+" of "+esc(g.min_paper_days===null?"unknown":g.min_paper_days)+" needed<br>paper filled orders: "+esc(g.paper_trades===null?(g.paper_note||"unknown"):g.paper_trades)+" of "+esc(g.min_paper_trades===null?"unknown":g.min_paper_trades)+' needed</p><p class="mut">'+esc(g.note)+"</p>";
  if(s.no_trades){h+="<h2>Performance</h2><p>no paper trades yet</p>";}
  else if(s.equity.length>=2){h+=chart(s.equity);}
  else{h+="<h2>Equity</h2><p>fewer than 2 data points; no chart.</p>";}
  return h;
}
function viewDecisions(d){
  var h=note(d.log_notice)+note(d.message);
  if(d.skipped){h+=note(d.skipped+" unreadable log line(s) skipped");}
  if(!d.rows.length){return h+"<p>no decisions logged</p>";}
  var kinds={};d.rows.forEach(function(r){kinds[r.kind]=1;});
  h+='<p>Filter: <input id="q" placeholder="text" aria-label="text filter"> <select id="k" aria-label="kind filter"><option value="">all kinds</option>'+Object.keys(kinds).sort().map(function(k){return '<option value="'+esc(k)+'">'+esc(k)+"</option>";}).join("")+"</select> <span id=cnt class=mut></span></p>";
  h+='<div id="tbl"></div><p class="mut">Fee is shown as logged ("fee unknown" means the venue did not report one). '+esc(d.funding_note)+".</p>";
  if(d.truncated){h+='<p class="mut">showing the newest '+d.rows.length+" of "+d.total+" records</p>";}
  return h;
}
function drawDecisions(){
  var d=decisions,q=document.getElementById("q").value.toLowerCase(),k=document.getElementById("k").value;
  var rows=d.rows.filter(function(r){return (!k||r.kind===k)&&(!q||JSON.stringify(r).toLowerCase().indexOf(q)>=0);}).reverse();
  document.getElementById("cnt").textContent=rows.length+" shown";
  document.getElementById("tbl").innerHTML=table(["time","kind","symbol","signal","action","gate verdict / reason","client id","fee"],rows.map(function(r){return [r.ts,r.kind,r.symbol,r.signal,r.action,r.gate,r.client_id,r.fee];}));
}
function metrics(m){return "Sharpe "+fx(m.sharpe)+", return "+pct(m.total_return)+", max DD "+pct(m.max_drawdown)+", PF "+fx(m.profit_factor)+", win "+pct(m.win_rate)+", trades "+(m.n_trades===undefined?"n/a":m.n_trades);}
function viewResearch(r){
  var h="<p class='mut'>"+esc(r.metric_note)+"</p>"+note(r.report_error)+note(r.trials_error);
  h+="<p>Trials counted in total: <b>"+esc(r.trials_total===null?"unknown":r.trials_total)+"</b></p>";
  if(r.candidates.length){h+="<h2>Candidates (hold-out metrics)</h2>"+table(["strategy","market","policy","verdict","hold-out metrics","trials (this candidate)","p-value","reasons"],r.candidates.map(function(c){return [c.name,c.symbol+" "+c.interval,c.policy,c.passed?"PASS":"REJECT",metrics(c.holdout),c.n_trials===null||c.n_trials===undefined?"n/a":c.n_trials,c.pvalue===null?"n/a":fx(c.pvalue,4),c.reasons.join("; ")];}));}
  h+="<h2>Deployed</h2>"+note(r.deployed_error);
  if(r.deployed_message){h+="<p>"+esc(r.deployed_message)+"</p>";}
  else if(r.deployed.length){h+=table(["strategy","market","policy"],r.deployed.map(function(c){return [c.name,c.symbol+" "+c.interval,c.policy];}));}
  return h;
}
function kv(obj){return obj?table(["limit","value"],Object.keys(obj).map(function(k){return [k,obj[k]];})):"";}
function viewRisk(r){
  var h="";Object.keys(r.errors).forEach(function(k){h+=note(r.errors[k]);});
  [["risk","Risk limits (config/risk.yaml)"],["runtime","Runtime (config/runtime.yaml)"],["gate","Validation gate (config/gate.yaml)"],["live","Live gate (live config)"]].forEach(function(p){if(r[p[0]]){h+="<h2>"+esc(p[1])+"</h2>"+kv(r[p[0]]);}});
  if(r.symbol_rules){h+="<h2>Symbol rules (static snapshot)</h2>"+table(["symbol","tick","lot","min notional"],Object.keys(r.symbol_rules).map(function(s){var x=r.symbol_rules[s];return [s,x.tick,x.lot,x.min_notional];}));}
  return h;
}
function viewFailure(f){
  if(f.error){return note(f.error);}
  return f.text.split("\n").map(function(l){
    var t=esc(l).replace(/\*\*(.+?)\*\*/g,"<b>$1</b>").replace(/`([^`]+)`/g,"<code>$1</code>");
    if(/^## /.test(l)){return "<h2>"+t.slice(3)+"</h2>";}
    if(/^\s*- /.test(l)){return '<div style="margin-left:'+(l.search(/\S/)*8+12)+'px">&bull; '+t.replace(/^\s*- /,"")+"</div>";}
    return l.trim()?"<p>"+t+"</p>":"";
  }).join("");
}
var VIEWS={status:viewStatus,decisions:viewDecisions,research:viewResearch,risk:viewRisk,failure:viewFailure};
function load(){
  var name=cur;
  fetch("/api/"+name,{cache:"no-store"}).then(function(r){return r.json();}).then(function(d){
    if(name!==cur){return;}
    var el=document.getElementById("view");
    try{el.innerHTML=(d.error&&Object.keys(d).length===1)?note(d.error):VIEWS[name](d);}
    catch(e){el.textContent="This view could not be drawn.";return;}
    if(name==="decisions"&&d.rows&&d.rows.length){decisions=d;drawDecisions();document.getElementById("q").oninput=drawDecisions;document.getElementById("k").onchange=drawDecisions;}
  }).catch(function(){document.getElementById("view").textContent="Could not load data from the local server.";});
}
function show(name){
  cur=name;
  if(timer!==null){clearInterval(timer);timer=null;}
  Array.prototype.forEach.call(document.querySelectorAll("#tabs button"),function(b){b.setAttribute("aria-selected",b.dataset.t===name?"true":"false");});
  document.getElementById("view").textContent="Loading...";
  load();
  if(name==="status"){timer=setInterval(load,30000);}
}
TABS.forEach(function(t){var b=document.createElement("button");b.textContent=t[1];b.dataset.t=t[0];b.setAttribute("role","tab");b.onclick=function(){show(t[0]);};document.getElementById("tabs").appendChild(b);});
show("status");
</script></body></html>
"""
