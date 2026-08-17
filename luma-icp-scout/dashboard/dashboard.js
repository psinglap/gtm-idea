const esc = s => (s||"").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const $ = s => document.querySelector(s);
const FALLBACK_AVATAR = "https://cdn.lu.ma/avatars-default/avatar_0.png";
let STORE = { mode:"embed", events:[], api:"", token:"" };

// ---- data source: extension storage, hosted API, or embedded ----
async function load(){
  if (typeof chrome!=="undefined" && chrome.storage?.local){
    const {saved} = await chrome.storage.local.get("saved");
    return { mode:"local", events: saved||[] };
  }
  const p = new URLSearchParams(location.search);
  // When served by the backend, a bare ?token= is enough — use same origin.
  const api = p.get("api") || (p.get("token") ? location.origin : "");
  if (api){
    const r = await fetch(api.replace(/\/$/,"")+"/api/events?token="+encodeURIComponent(p.get("token")||""));
    return { mode:"api", api, token:p.get("token")||"", events: r.ok ? await r.json() : [] };
  }
  return { mode:"embed", events: window.__DATA__||[] };
}

async function setConnected(evIdx, pIdx, val){
  const ev = STORE.events[evIdx]; const person = ev.people[pIdx];
  person.connected = val;
  if (STORE.mode==="local"){
    await chrome.storage.local.set({ saved: STORE.events });
  } else if (STORE.mode==="api"){
    fetch(STORE.api.replace(/\/$/,"")+"/api/person", {
      method:"PATCH", headers:{"content-type":"application/json"},
      body: JSON.stringify({ token:STORE.token, event:ev.url, linkedin:person.linkedinUrl, connected:val })
    }).catch(()=>{});
  }
}

function personMatches(p, q){
  if(!q) return true;
  return ((p.name||"")+" "+(p.reasons||[]).join(" ")+" "+(p.headline||"")+" "+(p.bio||"")).toLowerCase().includes(q);
}

function render(){
  const q = $("#q").value.trim().toLowerCase();
  const onlyOpen = $("#onlyOpen").checked, onlyTargets = $("#onlyTargets").checked;
  const root = $("#root"); root.innerHTML = "";
  const events = [...STORE.events].sort((a,b)=> (b.startAt||b.at||"").localeCompare(a.startAt||a.at||""));
  let shown = 0;
  events.forEach((ev)=>{
    const evIdx = STORE.events.indexOf(ev);
    let people = (ev.people||[]).map((p,i)=>({p,i}));
    if (onlyTargets) people = people.filter(x=>x.p.isTarget);
    if (onlyOpen) people = people.filter(x=>!x.p.connected);
    people = people.filter(x=>personMatches(x.p, q) || (ev.name||"").toLowerCase().includes(q));
    if (!people.length) return;
    shown++;
    const targets = (ev.people||[]).filter(p=>p.isTarget).length;
    const conn = (ev.people||[]).filter(p=>p.isTarget && p.connected).length;
    const when = ev.startAt ? new Date(ev.startAt).toLocaleString([], {month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}) : (ev.at||"").slice(0,10);
    const det = document.createElement("details"); det.className="event"; det.open = shown<=2;
    det.innerHTML =
      `<summary><div class="ev-title"><a href="${esc(ev.url)}" target="_blank">${esc(ev.name||ev.url||"Event")}</a></div>`+
      `<div class="ev-meta">${esc(when)}${ev.location?" · "+esc(ev.location):""}</div>`+
      `<div class="ev-counts"><span class="pill">${targets} targets</span><span class="pill good">${conn} connected</span></div></summary>`+
      `<div class="people"></div>`;
    const box = det.querySelector(".people");
    people.forEach(({p,i})=>{
      const row = document.createElement("div");
      row.className = "person"+(p.connected?" connected":"");
      const li = p.linkedinUrl ? `<a class="li" href="${esc(p.linkedinUrl)}" target="_blank">LinkedIn ↗</a>` : "";
      row.innerHTML =
        `<input type="checkbox" class="chk" ${p.connected?"checked":""} title="Connected" />`+
        `<img class="avatar" src="${esc(p.avatarUrl||FALLBACK_AVATAR)}" alt="" />`+
        `<div class="body"><div class="name">${esc(p.name||"(no name)")}</div>`+
        `<div class="why">${esc((p.reasons||[]).join(" · ")||p.headline||p.bio||"")}</div></div>`+
        `<div>${li}</div>`;
      row.querySelector(".chk").addEventListener("change", async e=>{
        await setConnected(evIdx, i, e.target.checked); render();
      });
      box.appendChild(row);
    });
    box.querySelectorAll("img.avatar").forEach((img)=>
      img.addEventListener("error", ()=>{ if(img.src!==FALLBACK_AVATAR) img.src=FALLBACK_AVATAR; }));
    root.appendChild(det);
  });
  if (!shown) root.innerHTML = `<div class="empty">No events yet. Scan one in the extension, hit “Save event”, then refresh.</div>`;
  $("#mode").textContent = STORE.mode==="local" ? "Reading from your browser (this device)."
    : STORE.mode==="api" ? "Synced dashboard — open this link anywhere." : "Preview mode.";
}

["#q","#onlyOpen","#onlyTargets"].forEach(s=>$(s).addEventListener("input", render));

(async function(){ STORE = await load(); render(); })();

// Live-refresh when the extension saves a new event (local mode).
if (typeof chrome!=="undefined" && chrome.storage?.onChanged){
  chrome.storage.onChanged.addListener((changes, area)=>{
    if (area==="local" && changes.saved){ STORE.events = changes.saved.newValue||[]; render(); }
  });
}
