// sidepanel.js — one-click auto-run: scan → match ICP → read top LinkedIn → final list.

const $ = (s) => document.querySelector(s);
let state = { people: [], eventUrl: "", eventName: "", startAt: "", location: "" };
let dashTabId = null;
let stopFlag = false;

// Persistent LinkedIn read-cache, keyed by profile URL, so we never re-read and
// can resume covering everyone across runs/sessions.
async function getLiCache() { return (await chrome.storage.local.get("liCache")).liCache || {}; }
async function setLiCache(c) { await chrome.storage.local.set({ liCache: c }); }
function mergeCacheInto(people, cache) {
  people.forEach((p) => {
    const c = p.linkedinUrl && cache[p.linkedinUrl];
    if (c && !c.gated) { p.headline = c.headline || p.headline; p.profileText = c.profileText || p.profileText; p.liRead = true; }
    else if (c && c.gated) { p.liRead = true; p.liGated = true; }
  });
}

// Learning loop: remember every manual accept/reject as (a) few-shot examples that
// teach the judge, and (b) per-person overrides that persist across events.
const sigText = (p) => [p.headline, p.bio].filter(Boolean).join(" · ") || p.name || "";
async function getFeedback() { return (await chrome.storage.local.get("feedback")).feedback || []; }
async function pushFeedback(text, label, reason) {
  if (!text || text.length < 3) return;
  const f = await getFeedback();
  f.push({ text: text.slice(0, 200), label, reason: (reason || "").slice(0, 120), at: Date.now() });
  await chrome.storage.local.set({ feedback: f.slice(-60) });
}
async function getOverrides() { return (await chrome.storage.local.get("overrides")).overrides || {}; }
async function setOverride(url, label) {
  if (!url) return;
  const o = await getOverrides(); o[url] = label;
  await chrome.storage.local.set({ overrides: o });
}
async function applyOverrides(people) {
  const o = await getOverrides();
  people.forEach((p) => {
    const ov = p.linkedinUrl && o[p.linkedinUrl];
    if (!ov) return;
    p.isTarget = ov === "target"; p.manual = true;
    if (!p.reasons?.length || !p.reasons[0].startsWith("you ")) p.reasons = [ov === "target" ? "you accepted this person before" : "you rejected this person before"];
    if (p.isTarget && !p.talkingPoint) p.talkingPoint = talkingPoint(p);
  });
}

// ---------- storage ----------
const DEFAULTS = { engine: "auto", apiKey: "", liMax: 40, liDelay: 20, autoLinkedin: true, syncUrl: "", syncToken: "" };
async function loadStore() {
  const s = await chrome.storage.local.get(["icp", "settings"]);
  return { icp: s.icp || structuredClone(window.ICP.DEFAULT_ICP), settings: { ...DEFAULTS, ...(s.settings || {}) } };
}
const saveIcp = (icp) => chrome.storage.local.set({ icp });
const saveSettings = (settings) => chrome.storage.local.set({ settings });
const esc = (s) => (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const targets = () => state.people.filter((p) => p.isTarget);

// ---------- ICP + settings editors ----------
function fillIcp(icp) {
  $("#icpDescription").value = icp.description || "";
  $("#icpWebsite").value = icp.website || "";
  $("#siteHint").textContent = icp.siteSummary ? "Site context saved ✓" : "";
  document.querySelectorAll("textarea[data-group]").forEach((ta) => {
    const g = ta.dataset.group;
    ta.value = ((g === "negatives" ? icp.negatives?.keywords : icp.groups[g]?.keywords) || []).join(", ");
  });
  $("#minMonths").value = icp.minCompanyMonths ?? 10;
  $("#threshold").value = icp.threshold ?? 2;
}
function readIcp(base) {
  const icp = structuredClone(base);
  icp.description = $("#icpDescription").value.trim();
  icp.website = $("#icpWebsite").value.trim();
  document.querySelectorAll("textarea[data-group]").forEach((ta) => {
    const kws = ta.value.split(",").map((x) => x.trim()).filter(Boolean);
    if (ta.dataset.group === "negatives") icp.negatives.keywords = kws;
    else icp.groups[ta.dataset.group].keywords = kws;
  });
  icp.minCompanyMonths = +$("#minMonths").value || 0;
  icp.threshold = +$("#threshold").value || 0;
  return icp;
}
function fillSettings(s) {
  $("#engineSelect").value = s.engine; $("#apiKey").value = s.apiKey || "";
  $("#liMax").value = s.liMax; $("#liDelay").value = s.liDelay;
  $("#autoLinkedin").checked = s.autoLinkedin;
  $("#syncUrl").value = s.syncUrl || ""; $("#syncToken").value = s.syncToken || "";
}
function readSettings() {
  return {
    engine: $("#engineSelect").value, apiKey: $("#apiKey").value.trim(),
    liMax: Math.max(0, +$("#liMax").value || 0), liDelay: Math.max(6, +$("#liDelay").value || 15),
    autoLinkedin: $("#autoLinkedin").checked,
    syncUrl: $("#syncUrl").value.trim(), syncToken: $("#syncToken").value.trim()
  };
}

/* Pairing this browser with the deployed backend.
 *
 * Everything the server cannot do itself — reading Luma, accepting invitations, pulling guest
 * lists, reading LinkedIn — needs a logged-in browser, and this extension is that browser. It
 * had no way to be told WHERE the server is: background.js could pair, but nothing called it,
 * so the only route was the service-worker console. A feature reachable only from DevTools is
 * not a feature.
 */
async function refreshWorkerPairing() {
  const cfg = await chrome.runtime.sendMessage({ type: "WORKER_CONFIG" }).catch(() => null);
  const el = $("#workerPairStatus");
  if (!el) return;
  if (cfg && cfg.ok && cfg.data && cfg.data.serverUrl && cfg.data.workspaceToken) {
    $("#workerServerUrl").value = cfg.data.serverUrl;
    el.textContent = `Connected to ${cfg.data.serverUrl} — runs every 5 minutes while Chrome is open.`;
    el.style.color = "#2f9e44";
  } else {
    el.textContent = "Not connected. Until this is set, nothing runs on its own.";
    el.style.color = "";
  }
}

/* Run the same pass the alarm would, now.
 *
 * `tick()` only applies the twice-a-day rate limit to alarm-triggered runs, so a manual trigger
 * takes the identical path with nothing skipped — this is not a special "test mode" that might
 * behave differently from the real thing. Watching this IS watching the scheduled run.
 */
async function runWorkerNow() {
  const el = $("#workerPairStatus");
  el.textContent = "Starting a full pass — sync, register, guest lists…"; el.style.color = "";
  const res = await chrome.runtime.sendMessage({ type: "WORKER_TICK" })
    .catch((e) => ({ ok: false, error: String(e) }));
  if (res && res.ok) {
    el.textContent = "Running. Watch progress on the dashboard under \"This browser\".";
    el.style.color = "#2f9e44";
    setTimeout(refreshWorkerPairing, 30000);
  } else {
    el.textContent = `Could not start: ${(res && res.error) || "not connected yet"}`;
    el.style.color = "#c92a2a";
  }
}

async function pairWorker() {
  const el = $("#workerPairStatus");
  const serverUrl = $("#workerServerUrl").value.trim();
  const companyUrl = ($("#icpWebsite").value || "").trim() || "";
  if (!serverUrl) { el.textContent = "Enter the server URL first."; el.style.color = "#c92a2a"; return; }
  el.textContent = "Connecting…"; el.style.color = "";
  const res = await chrome.runtime.sendMessage({
    type: "WORKER_PAIR", serverUrl, url: companyUrl,
  }).catch((e) => ({ ok: false, error: String(e) }));
  if (res && res.ok) {
    el.textContent = `Connected to ${serverUrl} — runs every 5 minutes while Chrome is open.`;
    el.style.color = "#2f9e44";
  } else {
    el.textContent = `Could not connect: ${(res && res.error) || "unknown error"}`;
    el.style.color = "#c92a2a";
  }
}

async function refreshEngine() {
  const { settings } = await loadStore();
  const eng = await window.Judge.availableEngines(settings.apiKey);
  let active = settings.engine;
  if (active === "auto") active = settings.apiKey ? "claude" : eng.local ? "local" : "heuristic";
  $("#enginePill").textContent = { claude: "Claude", local: "Local AI", heuristic: "Keyword" }[active] || active;
  $("#engineHint").innerHTML = `Local on-device: <b>${eng.local ? "yes" : "no"}</b> · Claude key: <b>${eng.claude ? "set" : "none"}</b>` +
    (!eng.local && !eng.claude ? "<br>Using keyword scoring. For sharper judging add a Claude key (pennies/event)." : "");
}

// ---------- steps UI ----------
const STEPS = [["scan", "Scan everyone"], ["linkedin", "Read LinkedIn → match ICP"], ["done", "Final list"]];
function initSteps() {
  $("#steps").innerHTML = STEPS.map(([id, label]) =>
    `<div class="step wait" id="st-${id}"><span class="ic"></span><span class="tx">${label}</span></div>`).join("");
}
function setStep(id, cls, extra) {
  const el = $("#st-" + id); if (!el) return;
  el.className = "step " + cls;
  el.querySelector(".tx").textContent = STEPS.find((s) => s[0] === id)[1] + (extra ? " — " + extra : "");
}

// ---------- rendering ----------
const FALLBACK_AVATAR = "https://cdn.lu.ma/avatars-default/avatar_0.png";
function avatar(p) {
  return `<img class="avatar" src="${esc(p.avatarUrl || FALLBACK_AVATAR)}" alt="" />`;
}
const hasData = (p) => !!(p.headline || p.bio || p.profileText);
function render() {
  const box = $("#results"); box.innerHTML = "";
  const filter = $("#viewFilter").value;
  const t = targets().length;
  const rejected = state.people.filter((p) => !p.isTarget && hasData(p));
  const nodata = state.people.filter((p) => !p.isTarget && !hasData(p));
  $("#summaryLine").textContent = state.people.length
    ? `${t} targets · ${rejected.length} rejected · ${nodata.length} thin · ${state.people.length} total`
    : "";
  $("#resultbar").hidden = !state.people.length;
  $("#footerbar").hidden = !state.people.length;

  let people = state.people;
  if (filter === "targets") people = state.people.filter((p) => p.isTarget);
  else if (filter === "rejected") people = rejected;
  people = [...people].sort((a, b) => (b.isTarget - a.isTarget) || (b.score || 0) - (a.score || 0));
  if (!people.length) { box.innerHTML = `<div class="hint" style="padding:10px">Nothing to show for this view yet.</div>`; }
  for (const p of people) {
    const card = document.createElement("div");
    card.className = "card" + (p.isTarget ? " target" : "");
    const li = p.linkedinUrl ? `<a href="${esc(p.linkedinUrl)}" target="_blank">LinkedIn ↗</a>` : "";
    card.innerHTML =
      `<div class="row2">${avatar(p)}<div class="body">` +
      `<div class="top"><span class="name">${esc(p.name || "(no name)")}</span>` +
      `<span class="badge ${p.isTarget ? "yes" : "no"}">${p.isTarget ? "TARGET" : "skip"}${p.score ? " · " + p.score : ""}</span></div>` +
      `<div class="why">${esc((p.reasons || []).join(" · ") || p.headline || p.bio || "")}</div>` +
      `<div>${li}` +
      (p.invited ? ` <span style="color:var(--good);font-size:11px">✓ invited</span>` : "") +
      (p.inviteError ? ` <span style="color:var(--warn);font-size:11px">${esc(p.inviteError)}</span>` : "") +
      ` <button class="ovr ghost small" data-id="${esc(pid(p))}">${p.isTarget ? "✕ Reject" : "✓ Accept"}</button>` +
      (p.manual ? ` <span style="color:var(--muted);font-size:10px">manual</span>` : "") +
      `</div></div></div>`;
    box.appendChild(card);
  }
  box.querySelectorAll("img.avatar").forEach((img) =>
    img.addEventListener("error", () => { if (img.src !== FALLBACK_AVATAR) img.src = FALLBACK_AVATAR; }));
  box.querySelectorAll("button.ovr").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const p = state.people.find((x) => pid(x) === btn.dataset.id);
      if (!p) return;
      const willTarget = !p.isTarget;
      const reason = (window.prompt(
        willTarget ? `Why is ${p.name || "this person"} a GOOD fit? (optional — helps it learn)`
                   : `Why reject ${p.name || "this person"}? (optional — helps it learn)`
      ) || "").trim();
      p.isTarget = willTarget;
      p.manual = true;                                  // manual overrides survive re-judging
      if (p.isTarget && !p.talkingPoint) p.talkingPoint = talkingPoint(p);
      const label = p.isTarget ? "target" : "reject";
      p.reasons = [(p.isTarget ? "manually accepted" : "manually rejected") + (reason ? ": " + reason : "")];
      await pushFeedback(sigText(p), label, reason);    // teach the judge (with your rationale)
      await setOverride(p.linkedinUrl, label);          // remember this exact person
      render();
      await saveCurrentEvent({});
    }));
}

// ---------- judging + talking points ----------
const pid = (p) => p.lumaUserId || p.linkedinUrl || p.name;
function applyJudged(judged) {
  const byId = new Map(state.people.map((p) => [pid(p), p]));
  judged.forEach((j) => {
    const p = byId.get(pid(j));
    if (!p || p.manual) return;                         // never overwrite a manual accept/reject
    Object.assign(p, { isTarget: j.isTarget, score: j.score, reasons: j.reasons, judgedBy: j.judgedBy });
    if (p.isTarget && !p.talkingPoint) p.talkingPoint = talkingPoint(p);
  });
}
// Judge a subset (already carrying whatever info we have) against the ICP.
async function judgeSome(list) {
  if (!list.length) return;
  const { icp, settings } = await loadStore();
  const feedback = await getFeedback();       // teach the judge from past accept/reject
  const judged = await window.Judge.judge(list, icp, { apiKey: settings.apiKey, mode: settings.engine, feedback });
  applyJudged(judged);
  await applyOverrides(state.people);          // your manual calls always win
}
function talkingPoint(p) {
  const t = ((p.name || "") + " " + (p.bio || "") + " " + (p.headline || "") + " " + (p.about || "")).toLowerCase();
  const co = (p.bio || p.headline || "").replace(/^(founder|co-?founder|building|ceo)\s*(of|@|at)?\s*/i, "").trim();
  if (/creator|influencer|ugc|ambassador/.test(t)) return `Already in the creator space${co ? " (" + co + ")" : ""} — ask what's working and where you could plug in.`;
  if (/founder|co-?founder|building|ceo/.test(t)) return `Ask what they're building${co ? " at " + co : ""} and how they think about distribution — creators could be a channel.`;
  if (/growth|acquisition|demand/.test(t)) return `Ask how they're driving growth now and whether they've tested creator/influencer campaigns.`;
  if (/market|brand|content|social/.test(t)) return `Ask about their current marketing mix and appetite for creator-led campaigns.`;
  return `Open with what they're working on, then how they think about creators/influencers for reach.`;
}

// ---------- read LinkedIn FIRST, then match ICP on the full profile ----------
// For each guest: read their LinkedIn (slowly, cached, resumable), THEN judge them
// against the ICP using the complete profile. People already read (cached) are judged
// immediately without re-reading. Covers everyone over successive runs.
async function readAndMatch(settings) {
  const { icp } = await loadStore();
  const cache = await getLiCache();
  const total = state.people.filter((p) => p.linkedinUrl).length;

  // 1) Judge everyone we ALREADY have full info for (cached reads) + people with no LinkedIn.
  const ready = state.people.filter((p) => !p.linkedinUrl || (cache[p.linkedinUrl] && !p._judgedFull));
  await judgeSome(ready);
  ready.forEach((p) => { if (p.linkedinUrl && cache[p.linkedinUrl]) p._judgedFull = true; });
  render();

  // 2) Queue the not-yet-read profiles, prioritised by a quick bio guess so likely
  //    targets get read first — but the REAL verdict is set after reading LinkedIn.
  const bioScore = (p) => window.ICP.score((p.bio || "") + " " + (p.name || ""), icp).score;
  let queue = state.people.filter((p) => p.linkedinUrl && !cache[p.linkedinUrl]).sort((a, b) => bioScore(b) - bioScore(a));
  const perRun = settings.liMax > 0 ? Math.min(settings.liMax, queue.length) : queue.length;
  queue = queue.slice(0, perRun);
  const alreadyRead = total - state.people.filter((p) => p.linkedinUrl && !cache[p.linkedinUrl]).length;

  if (!queue.length) {
    setStep("linkedin", "done", alreadyRead >= total ? `all ${total} read & matched` : `nothing new to read`);
    return;
  }
  $("#stopBtn").hidden = false;
  stopFlag = false;

  for (let i = 0; i < queue.length && !stopFlag; i++) {
    const p = queue[i];
    setStep("linkedin", "active", `read+match ${alreadyRead + i + 1}/${total} · ${p.name || ""}`);
    const res = await chrome.runtime.sendMessage({ type: "READ_LINKEDIN_ONE", url: p.linkedinUrl });
    const d = res?.ok && res.data;
    if (d && d.ok && !d.gated && (d.headline || d.profileText)) {
      cache[p.linkedinUrl] = { headline: d.headline, profileText: d.profileText, at: Date.now() };
    } else {
      cache[p.linkedinUrl] = { gated: true, at: Date.now() }; // couldn't read (gated); don't re-hammer
    }
    await setLiCache(cache);
    mergeCacheInto([p], cache);
    await judgeSome([p]);      // <-- match ICP using the FULL profile we just read
    p._judgedFull = true;
    render();
    if (i % 6 === 5) await saveCurrentEvent({});
    if (i < queue.length - 1 && !stopFlag) {
      const base = settings.liDelay * 1000;
      await new Promise((r) => setTimeout(r, base + Math.floor(Math.random() * base * 0.5)));
    }
  }
  $("#stopBtn").hidden = true;
  await saveCurrentEvent({});
  const readNow = state.people.filter((p) => p.linkedinUrl && cache[p.linkedinUrl]).length;
  const remaining = total - readNow;
  setStep("linkedin", "done", remaining ? `${readNow}/${total} read — run again for ${remaining} more` : `all ${total} read & matched`);
}
$("#stopBtn")?.addEventListener("click", () => { stopFlag = true; $("#stopBtn").hidden = true; });
document.querySelectorAll(".seg-btn").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".seg-btn").forEach((x) => x.classList.toggle("active", x === b));
    $("#viewFilter").value = b.dataset.f;
    render();
  }));

// ---------- live dashboard link (view the list anytime, any device) --------
$("#linkBtn").addEventListener("click", async () => {
  const { settings } = await loadStore();
  if (!settings.syncUrl || !settings.syncToken) {
    $("#summaryLine").textContent = "Set a Sync URL in Settings to get a live link (deploy the backend first).";
    $("#settingsPanel").open = true; return;
  }
  const link = settings.syncUrl.replace(/\/$/, "") + "/board?token=" + encodeURIComponent(settings.syncToken);
  try { await navigator.clipboard.writeText(link); } catch (_) {}
  chrome.tabs.create({ url: link });
  $("#summaryLine").textContent = "Live link opened & copied — open it on any device.";
});

// ---------- the auto-run ----------
// Build the ICP LIVE from the website each run (no hardcoded default). If a Claude
// key is set and there's no written description, draft one from the site too.
async function ensureIcpReady() {
  const { icp, settings } = await loadStore();
  const url = ($("#icpWebsite").value.trim() || icp.website || "").trim();
  // Only fetch the site during a run if we ALREADY have access — never prompt mid-run.
  const origin = url ? siteOrigin(url) : null;
  const allowed = origin ? await chrome.permissions.contains({ origins: [origin] }).catch(() => false) : false;
  if (url && allowed) {
    $("#siteHint").textContent = "Reading your site to build the ICP…";
    try {
      const res = await chrome.runtime.sendMessage({ type: "FETCH_SITE", url });
      if (res?.ok) {
        icp.website = url;
        icp.siteSummary = [res.data.title, res.data.description, res.data.sample].filter(Boolean).join(" — ").slice(0, 600);
        if (!icp.description?.trim()) {
          icp.description = await draftAnyIcp(icp.siteSummary, res.data, settings.apiKey);
        }
        await saveIcp(icp); fillIcp(icp);
        $("#siteHint").textContent = "ICP built from " + url + " ✓";
      }
    } catch (_) { $("#siteHint").textContent = "Couldn't read the site; using what you've written."; }
  }
  return icp;
}

async function runAll() {
  const { settings } = await loadStore();
  const icp = await ensureIcpReady();
  if (!icp.description?.trim() && !icp.siteSummary) {
    $("#siteHint").textContent = "Add your website and click “Build from site” (or write your ICP) first.";
    $("#icpCard").open = true; $("#icpWebsite").focus(); return;
  }
  $("#runBtn").disabled = true; $("#footerbar").hidden = true; $("#results").innerHTML = "";
  initSteps();
  try {
    setStep("scan", "active");
    const url = $("#eventUrl").value.trim();
    const res = await chrome.runtime.sendMessage({ type: "SCAN_LUMA", url: url || undefined });
    if (!res?.ok) throw new Error(res?.error || "scan failed");
    Object.assign(state, { eventUrl: res.data.url, eventName: res.data.eventName, startAt: res.data.startAt, location: res.data.location, people: res.data.guests });
    setStep("scan", "done", `${res.data.counts.total} guests, ${res.data.counts.withLinkedin} with LinkedIn`);
    mergeCacheInto(state.people, await getLiCache()); // reuse anything read before
    await applyOverrides(state.people);                // respect prior manual accept/reject
    render();
    openDashboard();

    // Read LinkedIn first, then match ICP on the full profile (per person, live).
    if (settings.autoLinkedin && state.people.some((p) => p.linkedinUrl)) {
      setStep("linkedin", "active");
      await readAndMatch(settings);
    } else {
      // No LinkedIn reading — match on the Luma bio we have.
      setStep("linkedin", "wait", "skipped — matching on Luma bio only");
      await judgeSome(state.people);
    }
    render();
    setStep("done", "done", `${targets().length} targets`);
    await saveCurrentEvent({ announce: false });
  } catch (e) {
    const active = STEPS.find((s) => $("#st-" + s[0])?.classList.contains("active"));
    if (active) setStep(active[0], "wait", "error");
    $("#summaryLine").textContent = "Error: " + e.message;
  } finally {
    $("#runBtn").disabled = false;
  }
}
$("#runBtn").addEventListener("click", runAll);

// ---------- save + sync ----------
async function saveCurrentEvent({ announce = false } = {}) {
  if (!state.people.length || !state.eventUrl) return;
  const s = await chrome.storage.local.get("saved");
  const saved = s.saved || [];
  const record = {
    at: new Date().toISOString(), url: state.eventUrl, name: state.eventName,
    startAt: state.startAt || "", location: state.location || "",
    people: state.people.map((p) => ({
      name: p.name, linkedinUrl: p.linkedinUrl, avatarUrl: p.avatarUrl || "", isTarget: !!p.isTarget,
      score: p.score || 0, reasons: p.reasons || [], headline: p.headline || "", bio: p.bio || "",
      talkingPoint: p.talkingPoint || "", connected: p.connected || false, invited: p.invited || false, manual: p.manual || false
    }))
  };
  const idx = saved.findIndex((e) => e.url === record.url);
  if (idx >= 0) saved[idx] = record; else saved.unshift(record);
  await chrome.storage.local.set({ saved: saved.slice(0, 100) });
  renderHistory();
  const { settings } = await loadStore();
  if (settings.syncUrl && settings.syncToken) {
    try {
      await fetch(settings.syncUrl.replace(/\/$/, "") + "/api/events", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ token: settings.syncToken, event: record })
      });
    } catch (_) {}
  }
}

// ---------- past events picker ----------
async function renderHistory() {
  const { saved } = await chrome.storage.local.get("saved");
  const list = [...(saved || [])].sort((a, b) => (b.at || "").localeCompare(a.at || ""));
  const sel = $("#historySelect");
  $("#historyCard").hidden = !list.length;
  if (!list.length) return;
  sel.innerHTML = list.map((ev) => {
    const t = (ev.people || []).filter((p) => p.isTarget).length;
    const when = (ev.startAt || ev.at || "").slice(0, 10);
    return `<option value="${esc(ev.url)}">${esc(ev.name || ev.url)} · ${t} targets · ${when}</option>`;
  }).join("");
  if (state.eventUrl) sel.value = state.eventUrl;
}
$("#historySelect").addEventListener("change", async () => {
  const { saved } = await chrome.storage.local.get("saved");
  const ev = (saved || []).find((e) => e.url === $("#historySelect").value);
  if (!ev) return;
  Object.assign(state, { eventUrl: ev.url, eventName: ev.name, startAt: ev.startAt || "", location: ev.location || "", people: ev.people || [] });
  render();
  $("#summaryLine").textContent = `${ev.name || "event"} · ${targets().length} targets`;
});

// ---------- dashboard / exports ----------
async function openDashboard() {
  const dashUrl = chrome.runtime.getURL("dashboard/index.html");
  if (dashTabId != null) {
    try { await chrome.tabs.get(dashTabId); chrome.tabs.update(dashTabId, { active: true }); chrome.tabs.reload(dashTabId); return; } catch (_) { dashTabId = null; }
  }
  const tab = await chrome.tabs.create({ url: dashUrl });
  dashTabId = tab.id;
}
$("#dashBtn").addEventListener("click", openDashboard);
$("#pdfBtn").addEventListener("click", () =>
  chrome.tabs.create({ url: chrome.runtime.getURL("pdf.html") + "?event=" + encodeURIComponent(state.eventUrl) }));
$("#csvBtn").addEventListener("click", () => {
  const rows = [["name", "isTarget", "score", "linkedin", "why", "talking_point"]];
  state.people.forEach((p) => rows.push([p.name || "", p.isTarget ? "yes" : "no", p.score || 0, p.linkedinUrl || "", (p.reasons || []).join("; "), p.talkingPoint || ""]));
  const csv = rows.map((r) => r.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(",")).join("\n");
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); a.download = "icp-targets.csv"; a.click();
});
$("#copyBtn").addEventListener("click", async () => {
  const txt = targets().map((p) => `${p.name} — ${p.linkedinUrl || ""} — ${p.talkingPoint || (p.reasons || []).join("; ")}`).join("\n");
  await navigator.clipboard.writeText(txt);
  $("#summaryLine").textContent = "Copied " + targets().length + " targets ✓";
});

// ---------- site suggest ----------
// The extension no longer holds broad host access; it asks per-site, on demand.
function siteOrigin(url) {
  let u = (url || "").trim();
  if (!/^https?:\/\//.test(u)) u = "https://" + u;
  try { return new URL(u).origin + "/*"; } catch (_) { return null; }
}
$("#analyzeSiteBtn").addEventListener("click", async () => {
  const url = $("#icpWebsite").value.trim();
  if (!url) { $("#siteHint").textContent = "Enter a website first."; return; }
  const origin = siteOrigin(url);
  if (!origin) { $("#siteHint").textContent = "Enter a valid website."; return; }
  // request access to just this site (instant if already granted)
  const granted = await chrome.permissions.request({ origins: [origin] });
  if (!granted) { $("#siteHint").textContent = "Permission denied — can't read the site to build the ICP."; return; }
  $("#siteHint").textContent = "Reading your site…";
  try {
    const res = await chrome.runtime.sendMessage({ type: "FETCH_SITE", url });
    if (!res?.ok) throw new Error(res?.error || "fetch failed");
    const summary = [res.data.title, res.data.description, res.data.sample].filter(Boolean).join(" — ").slice(0, 600);
    const { icp, settings } = await loadStore();
    icp.website = url; icp.siteSummary = summary;
    if (!$("#icpDescription").value.trim()) {
      $("#siteHint").textContent = "Drafting an ICP from your site…";
      icp.description = await draftAnyIcp(summary, res.data, settings.apiKey);
    } else {
      icp.description = $("#icpDescription").value.trim();
    }
    await saveIcp(icp); fillIcp(icp);
    $("#siteHint").textContent = "ICP drafted from your site ✓ — edit above if needed.";
  } catch (e) { $("#siteHint").textContent = "Couldn't read site: " + e.message; }
});
// Draft an ICP from the site: Claude (if key) → free local model → template fallback.
async function draftAnyIcp(summary, siteData, apiKey) {
  if (apiKey) { const c = await draftIcp(summary, apiKey).catch(() => ""); if (c) return c; }
  const l = await draftIcpLocal(summary).catch(() => ""); if (l) return l;
  return draftIcpFallback(siteData);
}
async function draftIcpLocal(summary) {
  const api = (typeof LanguageModel !== "undefined" && LanguageModel.create) ? LanguageModel : (self.ai && self.ai.languageModel);
  if (!api) return "";
  let session;
  const sys = { initialPrompts: [{ role: "system", content: "You write concise B2B ideal-customer profiles." }] };
  const variants = [{ ...sys, outputLanguage: "en", expectedOutputs: [{ type: "text", languages: ["en"] }] }, { ...sys, outputLanguage: "en" }, sys];
  for (const v of variants) { try { session = await api.create(v); break; } catch (_) {} }
  if (!session) return "";
  const prompt = `From this website text, write a 2-3 sentence ICP: which people/roles to target as customers (titles, seniority, company stage, intent). Plain text only.\n\n${summary}`;
  let ans = "";
  try { ans = await session.prompt(prompt, { outputLanguage: "en" }); } catch (_) { try { ans = await session.prompt(prompt); } catch (_) {} }
  session.destroy?.();
  return (ans || "").trim();
}
function draftIcpFallback(d) {
  const what = [d?.title, d?.description].filter(Boolean).join(" — ").slice(0, 180);
  return `Target customers for ${what || "this product"}. Likely buyers: founders (past the earliest stage), heads of growth, growth/performance marketers, and marketing/brand/community leaders who'd use this. Not a fit: students, interns, job-seekers, and junior individual-contributor engineers.`;
}
async function draftIcp(summary, apiKey) {
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST", headers: { "content-type": "application/json", "x-api-key": apiKey, "anthropic-version": "2023-06-01", "anthropic-dangerous-direct-browser-access": "true" },
    body: JSON.stringify({ model: "claude-haiku-4-5-20251001", max_tokens: 300, messages: [{ role: "user", content: `From this site text, write a 2-3 sentence ICP of which people/roles to target as customers (titles, seniority, company stage, intent). Plain text.\n\n${summary}` }] })
  });
  const d = await r.json();
  return (d.content || []).map((c) => c.text || "").join("").trim();
}

// ---------- persistence wires ----------
$("#workerPairBtn")?.addEventListener("click", pairWorker);
$("#workerRunBtn")?.addEventListener("click", runWorkerNow);
refreshWorkerPairing();

["engineSelect", "apiKey", "liMax", "liDelay", "autoLinkedin", "syncUrl", "syncToken"].forEach((id) =>
  $("#" + id).addEventListener("change", async () => { await saveSettings(readSettings()); refreshEngine(); }));
["icpDescription", "icpWebsite", "minMonths", "threshold"].forEach((id) =>
  $("#" + id).addEventListener("change", async () => { const { icp } = await loadStore(); await saveIcp(readIcp(icp)); }));
document.querySelectorAll("textarea[data-group]").forEach((ta) =>
  ta.addEventListener("change", async () => { const { icp } = await loadStore(); await saveIcp(readIcp(icp)); }));
$("#resetIcpBtn").addEventListener("click", async () => { await saveIcp(structuredClone(window.ICP.DEFAULT_ICP)); fillIcp(window.ICP.DEFAULT_ICP); });
$("#clearCacheBtn").addEventListener("click", async () => {
  await chrome.storage.local.set({ liCache: {} });
  $("#clearCacheBtn").textContent = "Cleared ✓ — next run re-reads LinkedIn";
  setTimeout(() => { $("#clearCacheBtn").textContent = "Clear LinkedIn read cache"; }, 2500);
});

// ---------- init ----------
const CACHE_VERSION = 4; // bump to auto-invalidate stale LinkedIn reads
(async function init() {
  // Auto-clear the LinkedIn cache when the extractor/version changes, so old
  // (broken-extractor) reads are discarded without the user doing anything.
  const { liCacheVersion } = await chrome.storage.local.get("liCacheVersion");
  if (liCacheVersion !== CACHE_VERSION) {
    await chrome.storage.local.set({ liCache: {}, liCacheVersion: CACHE_VERSION });
  }
  const { icp, settings } = await loadStore();
  // Give every user a private dashboard token automatically (no config needed).
  if (!settings.syncToken) { settings.syncToken = (crypto.randomUUID?.() || String(Date.now()) + Math.random()).replace(/-/g, ""); await saveSettings(settings); }
  // Migration: drop the old hardcoded default so the ICP derives from the site instead.
  if (typeof icp.description === "string" && icp.description.startsWith("We sell ")) {
    icp.description = "";
    await saveIcp(icp);
  }
  fillIcp(icp); fillSettings(settings);
  $("#icpCard").open = !(icp.description && icp.description.trim()); // collapse once ICP is set
  await refreshEngine();

  // Restore the most recently saved event so a refresh doesn't blank the list.
  const { saved } = await chrome.storage.local.get("saved");
  if (saved && saved.length) {
    const ev = [...saved].sort((a, b) => (b.at || "").localeCompare(a.at || ""))[0];
    Object.assign(state, { eventUrl: ev.url, eventName: ev.name, startAt: ev.startAt || "", location: ev.location || "", people: ev.people || [] });
    render();
    $("#summaryLine").textContent = `Restored: ${ev.name || "last event"} · ${targets().length} targets`;
  }
  await renderHistory();
})();
