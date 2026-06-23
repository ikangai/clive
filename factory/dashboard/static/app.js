"use strict";
const STAGES = ["proposed","evaluating","scored","awaiting_gate","promoted","rejected"];
const pct = x => (x==null ? "—" : (x*100).toFixed(0)+"%");
// Escape all HTML-significant chars incl. quotes — some fields (digests, change
// summaries, judge notes) originate from LLM output and land in attributes.
const esc = s => String(s==null?"":s).replace(/[&<>"']/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function refresh(){
  let st;
  try { st = await (await fetch("/api/state")).json(); }
  catch(e){ document.getElementById("updated").textContent = "fetch error: "+e; return; }
  document.getElementById("phase").textContent = st.phase || "";
  document.getElementById("updated").textContent = "updated " + new Date().toLocaleTimeString();
  renderAlarm(st.divergence);
  renderKanban(st.kanban);
  renderScoreboard(st.scoreboard);
  renderLeakage(st.leakage);
  renderCost(st.cost);
  renderQueue(st.queue);
  renderSafety(st.safety);
}

function renderAlarm(div){
  const el = document.getElementById("alarm");
  const firing = (div||[]).filter(d=>d.alarm);
  if(!firing.length){ el.classList.add("hidden"); el.innerHTML=""; return; }
  el.classList.remove("hidden");
  el.innerHTML = "<h3>⚠ DIVERGENCE ALARM — the proxy may be gamed (Goodhart)</h3>" +
    firing.map(d=>`<div><b>${esc(d.id)}</b>: working Δ ${pct(d.working_delta)}, `+
      `held-out Δ ${pct(d.held_delta)}, panel spread ${pct(d.panel_spread)}`+
      `<ul>${(d.reasons||[]).map(r=>`<li>${esc(r)}</li>`).join("")}</ul></div>`).join("");
}

function renderKanban(k){
  const wrap = document.getElementById("kanban");
  wrap.innerHTML = STAGES.map(s=>{
    const cards = (k[s]||[]).map(c=>{
      const sc = c.scores||{};
      const scline = (sc.working_set!=null)
        ? `ws ${pct(sc.working_set)} · ho ${pct(sc.held_out)}${sc.safety_tripped?" · ⚠safety":""}` : "";
      return `<div class="card s-${s}"><div class="id">${esc(c.id)}</div>`+
        `<div class="chg">${esc(c.change||"")}</div>`+
        (scline?`<div class="sc">${scline}</div>`:"")+`</div>`;
    }).join("") || `<div class="empty">—</div>`;
    return `<div class="col"><h4>${s.replace("_"," ")}<span class="cnt">${(k[s]||[]).length}</span></h4>${cards}</div>`;
  }).join("");
}

function renderScoreboard(sb){
  if(!sb){return;}
  const panel = sb.panel||[];
  const head = `<tr><th>candidate</th><th>working</th><th>held-out</th>`+
    panel.map(p=>`<th>${esc(p)}</th>`).join("")+`<th>spread</th><th>safety</th></tr>`;
  const rows = (sb.rows||[]).map(r=>{
    const cls = r.id==="champion"?"champ":"";
    const cell = v=>`<td class="${v>=0.999?'good':v<0.5?'badc':'warnc'}">${pct(v)}</td>`;
    return `<tr><td class="${cls}">${esc(r.label)}</td>${cell(r.working)}${cell(r.held_out)}`+
      panel.map(p=>cell(r.panel?r.panel[p]:null)).join("")+
      `<td>${pct(r.spread)}</td><td>${r.safety?'<span class="badc">⚠</span>':'<span class="good">ok</span>'}</td></tr>`;
  }).join("");
  document.getElementById("scoreboard").innerHTML = `<table>${head}${rows}</table>`;
}

function renderLeakage(rows){
  const el = document.getElementById("leakage");
  if(!rows||!rows.length){ el.innerHTML='<div class="empty">no held-out scenarios</div>'; return; }
  el.innerHTML = rows.map(r=>{
    const frac = r.threshold? Math.min(1, r.leakage/r.threshold):0;
    const cls = frac>=1?"fill-bad":frac>=0.6?"fill-warn":"fill-ok";
    const retired = r.active?"":" · RETIRED";
    return `<div class="mlabel"><span>${esc(r.id)}${retired}</span><span>${r.leakage}/${r.threshold}</span></div>`+
      `<div class="meter"><span class="${cls}" style="width:${frac*100}%"></span></div>`;
  }).join("");
}

function renderCost(c){
  if(!c){return;}
  const frac = c.round_cap? Math.min(1, c.tokens/c.round_cap):0;
  const cls = frac>=1?"fill-bad":frac>=0.7?"fill-warn":"fill-ok";
  document.getElementById("cost").innerHTML =
    `<div class="mlabel"><span>${c.tokens.toLocaleString()} tokens · $${(c.cost||0).toFixed(4)}</span>`+
    `<span>cap ${c.round_cap.toLocaleString()}</span></div>`+
    `<div class="meter"><span class="${cls}" style="width:${frac*100}%"></span></div>`;
}

function renderQueue(q){
  const el = document.getElementById("queue");
  if(!q||!q.length){ el.innerHTML='<div class="empty">no candidates have cleared the rule — nothing to promote</div>'; return; }
  el.innerHTML = q.map(c=>{
    const p = c.promotion||{};
    const chip=(ok,label)=>`<span class="chip ${ok?'pass':'failc'}">${ok?'✓':'✗'} ${label}</span>`;
    return `<div class="qcard"><div class="qhead"><span class="id">${esc(c.id)}</span>`+
      `<button class="promote" data-id="${esc(c.id)}">Promote to champion</button></div>`+
      `<div>${esc(c.change||"")}</div>`+
      `<div class="checks">${chip(p.beats_working,"beats champion")}${chip(p.held_out_ok,"held-out ok")}`+
      `${chip(p.panel_ok,"panel ok")}${chip(p.safety_ok,"safety ok")}</div>`+
      `<div class="digest">${esc(c.digest||"(no digest)")}</div></div>`;
  }).join("");
  el.querySelectorAll("button.promote").forEach(b=>b.addEventListener("click",()=>promote(b.dataset.id)));
}

function renderSafety(flags){
  const el = document.getElementById("safety");
  if(!flags||!flags.length){ el.innerHTML='<div class="empty">no safety flags</div>'; return; }
  el.innerHTML = flags.map(f=>`<div class="flag sev-${esc(f.severity)}">`+
    `[<b>${esc(f.severity)}</b>] ${esc(f.kind)} <span class="dim">(${esc(f.candidate_id)}/${esc(f.scenario_id)})</span><br>${esc(f.detail)}</div>`).join("");
}

async function promote(id){
  const operator = prompt("Operator name (you are taking responsibility for this promotion):");
  if(!operator) return;
  const rationale = prompt("Rationale (why promote "+id+"?):")||"";
  const res = await fetch("/api/promote",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({candidate_id:id,operator,rationale})});
  const body = await res.json();
  if(res.ok){ alert("Promoted "+id+" to champion."); refresh(); }
  else { alert("Promotion refused: "+(body.error||res.status)); }
}

refresh();
setInterval(refresh, 4000);
