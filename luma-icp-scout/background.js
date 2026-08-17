// background.js — service worker. Orchestrates scraping using the user's own
// logged-in sessions. Injected page functions run in page context and cannot
// reference outer scope, so each is self-contained.

// Pipeline (validated on a live 169-person event):
//   1) SCAN_LUMA        — pull the FULL guest list from api.luma.com/event/get-guest-list
//                         (paginated, auth via ticket_key). Name, avatar, LinkedIn, bio.
//   2) (judge in panel)
//   3) ENRICH_LINKEDIN  — throttled deep-read of LinkedIn headlines for the target shortlist.
//   4) FETCH_SITE       — read the user's website to auto-suggest an ICP.

// The automated daily loop (register -> scan -> read LinkedIn) lives in worker.js and shares
// this global scope, so it reuses readOneLinkedIn/linkedinExtractor below rather than
// duplicating the extraction logic that is already proven against live profiles.
importScripts("worker.js");

chrome.action.onClicked.addListener((tab) => chrome.sidePanel.open({ windowId: tab.windowId }));
chrome.runtime.onInstalled.addListener(() =>
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {}));

chrome.runtime.onMessage.addListener((msg, _s, sendResponse) => {
  (async () => {
    try {
      if (msg.type === "SCAN_LUMA") sendResponse({ ok: true, data: await scanLuma(msg.url) });
      else if (msg.type === "READ_LINKEDIN_ONE") sendResponse({ ok: true, data: await readOneLinkedIn(msg.url) });
      else if (msg.type === "ENRICH_LINKEDIN") sendResponse({ ok: true, data: await enrichLinkedIn(msg.people, msg.opts || {}) });
      else if (msg.type === "FETCH_SITE") sendResponse({ ok: true, data: await fetchSite(msg.url) });
      // --- automated worker control (side panel talks to it through these) ---
      else if (msg.type === "WORKER_GET") {
        const [cfg, status, daily] = await Promise.all([getConfig(), getWorkerStatus(), getDaily()]);
        sendResponse({ ok: true, data: { cfg, status, daily } });
      }
      else if (msg.type === "WORKER_SET") sendResponse({ ok: true, data: await setConfig(msg.patch || {}) });
      else if (msg.type === "WORKER_TICK") { tick("manual"); sendResponse({ ok: true, data: { started: true } }); }
      else if (msg.type === "WORKER_PAIR") sendResponse({ ok: true, data: await pairWorkspace(msg.serverUrl, msg.url) });
      // So the side panel can show whether this browser is paired, rather than the user
      // having to open the service-worker console to find out.
      else if (msg.type === "WORKER_CONFIG") sendResponse({ ok: true, data: await getConfig() });
      else sendResponse({ ok: false, error: "unknown message " + msg.type });
    } catch (e) { sendResponse({ ok: false, error: e.message }); }
  })();
  return true;
});

async function getWorkerStatus() {
  return (await chrome.storage.local.get("workerStatus")).workerStatus || {};
}

// One-time pairing: ask the server for this company's workspace token and store it locally.
// This is the whole "account system" — no sign-in, no password, just a token per install.
async function pairWorkspace(serverUrl, companyUrl) {
  const base = (serverUrl || "").replace(/\/$/, "");
  const res = await fetch(`${base}/workspace`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: companyUrl }),
  });
  if (!res.ok) throw new Error(`pairing failed: ${res.status} ${(await res.text()).slice(0, 140)}`);
  const d = await res.json();
  await setConfig({ serverUrl: base, workspaceToken: d.token, enabled: true });
  return d;
}

async function activeLumaTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab || !/luma\.com|lu\.ma/.test(tab.url || "")) throw new Error("Open a Luma event page in the active tab first (or paste the event URL).");
  return tab;
}

// ---------- 1) scan the event via Luma's own guest-list API ----------------
// If a url is passed, open it in a background tab, scan, and close it.
async function scanLuma(url) {
  let tab, temp = false;
  if (url) {
    tab = await chrome.tabs.create({ url, active: false });
    temp = true;
    try { await waitForComplete(tab.id, 20000); } catch (_) {}
    await new Promise((r) => setTimeout(r, 1500));
  } else {
    tab = await activeLumaTab();
  }
  try {
    const [{ result }] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: lumaApiScraper });
    if (!result) throw new Error("The scanner returned nothing — is this a Luma event page you're registered for?");
    if (result.error) throw new Error(result.error);
    if (!result.counts) throw new Error("Scan produced no guest list. Open the event page and try again.");
    return { url: tab.url || url, ...result };
  } finally {
    if (temp && tab) chrome.tabs.remove(tab.id).catch(() => {});
  }
}

// runs IN the luma event page: reads event id + ticket_key, then pulls the
// FULL guest list (paginated) from api.luma.com — name, avatar, LinkedIn, bio.
async function lumaApiScraper() {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const find = (o, k, d) => { if (d > 6 || !o || typeof o !== "object") return undefined; if (k in o && o[k]) return o[k]; for (const kk in o) { const r = find(o[kk], k, d + 1); if (r) return r; } };

  let d = {};
  try { d = JSON.parse(document.getElementById("__NEXT_DATA__").textContent).props.pageProps.initialData.data || {}; } catch (_) {}
  const eventId = d.api_id || find(d, "api_id", 0);
  const tk = new URL(location.href).searchParams.get("tk") || d.ticket_key || (d.guest_data && d.guest_data.ticket_key) || "";
  if (!eventId) return { error: "Could not find the event id on this page." };

  const base = `https://api.luma.com/event/get-guest-list?event_api_id=${eventId}&pagination_limit=100` + (tk ? `&ticket_key=${encodeURIComponent(tk)}` : "");
  const guests = [];
  let cursor = "", pages = 0;
  do {
    const u = base + (cursor ? `&pagination_cursor=${encodeURIComponent(cursor)}` : "");
    let j;
    try { const r = await fetch(u); if (!r.ok) { if (pages === 0) return { error: "Guest list API returned " + r.status + " (are you registered / is this your ticket link?)" }; break; } j = await r.json(); }
    catch (e) { break; }
    (j.entries || []).forEach((e) => {
      const g = e.user || {};
      guests.push({
        lumaUserId: g.api_id,
        name: clean(g.name || [g.first_name, g.last_name].filter(Boolean).join(" ")),
        avatarUrl: g.avatar_url || "",
        bio: clean(g.bio_short),
        linkedinUrl: g.linkedin_handle ? "https://www.linkedin.com/in/" + String(g.linkedin_handle).replace(/^https?:\/\/[^/]*/, "").replace(/^\/?in\//, "").replace(/^\//, "") : "",
        twitter: g.twitter_handle || "", website: g.website || ""
      });
    });
    cursor = j.has_more ? j.next_cursor : "";
    pages++;
  } while (cursor && pages < 15);

  // Exclude the logged-in user (self) — their id shows up in Luma's localStorage.
  let selfId = "";
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const m = (localStorage.key(i) || "").match(/luma\.(?:presence\.last-ping-at-ms|user-storage)\.(usr-[A-Za-z0-9]+)/);
      if (m) { selfId = m[1]; break; }
    }
  } catch (_) {}
  const filtered = selfId ? guests.filter((g) => g.lumaUserId !== selfId) : guests;

  const eventName = clean(d.event?.name || d.name || (document.querySelector("h1")?.innerText) || document.title.replace(/ · Luma$/, ""));
  const startAt = d.start_at || find(d, "start_at", 0) || "";
  const loc = find(d, "full_address", 0) || find(d, "city", 0) || (d.featured_city && (d.featured_city.name || d.featured_city)) || "";
  return { guests: filtered, eventName, startAt, location: loc, counts: { total: filtered.length, withLinkedin: filtered.filter((g) => g.linkedinUrl).length } };
}

// ---------- website fetch (to auto-suggest an ICP) ------------------------
async function fetchSite(url) {
  if (!/^https?:\/\//.test(url)) url = "https://" + url;
  const r = await fetch(url, { credentials: "omit" });
  const html = await r.text();
  const meta = (re) => (html.match(re) || [])[1] || "";
  const title = meta(/<title[^>]*>([\s\S]*?)<\/title>/i);
  const desc =
    meta(/<meta[^>]+name=["']description["'][^>]+content=["']([^"']+)["']/i) ||
    meta(/<meta[^>]+property=["']og:description["'][^>]+content=["']([^"']+)["']/i);
  // strip tags for a rough body sample
  const body = html.replace(/<script[\s\S]*?<\/script>/gi, "").replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 1500);
  return { title: title.trim(), description: desc.trim(), sample: body };
}

// ---------- read ONE LinkedIn profile (panel paces + caches these) --------
async function readOneLinkedIn(url) {
  let tab;
  try {
    tab = await chrome.tabs.create({ url, active: false });
    await waitForComplete(tab.id, 15000);
    await new Promise((r) => setTimeout(r, 3500)); // let lazy content render
    const [{ result }] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: linkedinExtractor });
    return { ok: true, ...result };
  } catch (e) {
    return { ok: false, error: e.message };
  } finally {
    if (tab) chrome.tabs.remove(tab.id).catch(() => {});
  }
}

// ---------- 4) batch throttled LinkedIn deep-read (legacy helper) ----------
async function enrichLinkedIn(people, opts) {
  const targets = people.filter((p) => p.linkedinUrl).slice(0, opts.max || 20);
  const minDelay = opts.minDelayMs ?? 12000, maxDelay = opts.maxDelayMs ?? 28000;
  const results = [];
  for (let i = 0; i < targets.length; i++) {
    const p = targets[i];
    chrome.runtime.sendMessage({ type: "ENRICH_PROGRESS", stage: "linkedin", i: i + 1, total: targets.length, name: p.name }).catch(() => {});
    let tab;
    try {
      tab = await chrome.tabs.create({ url: p.linkedinUrl, active: false });
      await waitForComplete(tab.id, 15000);
      await new Promise((r) => setTimeout(r, 3500));
      const [{ result }] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: linkedinExtractor });
      results.push({ ...p, ...result, enriched: true });
    } catch (e) {
      results.push({ ...p, enriched: false, enrichError: e.message });
    } finally { if (tab) chrome.tabs.remove(tab.id).catch(() => {}); }
    if (i < targets.length - 1) await new Promise((r) => setTimeout(r, minDelay + Math.floor(Math.random() * (maxDelay - minDelay))));
  }
  return { results };
}

function waitForComplete(tabId, timeout) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => { chrome.tabs.onUpdated.removeListener(fn); reject(new Error("load timeout")); }, timeout);
    const fn = (id, info) => { if (id === tabId && info.status === "complete") { clearTimeout(t); chrome.tabs.onUpdated.removeListener(fn); resolve(); } };
    chrome.tabs.onUpdated.addListener(fn);
  });
}

function linkedinExtractor() {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const meta = (sel) => document.querySelector(sel)?.getAttribute("content") || "";
  const name = clean(document.querySelector("h1")?.innerText || meta('meta[property="og:title"]').split(/[|·]/)[0]);
  // headline: the top-card subtitle, with meta-tag fallbacks
  let headline = clean(
    document.querySelector(".text-body-medium.break-words")?.innerText ||
    document.querySelector("main .text-body-medium")?.innerText || "");
  if (!headline) { const og = meta('meta[property="og:description"]'); headline = clean(og).slice(0, 180); }
  // Grab the main profile column, then cut off recommendation/suggestion noise
  // ("People also viewed", etc.) so it doesn't leak OTHER people's roles.
  const main = document.querySelector("main");
  let profileText = clean(main ? main.innerText : document.body.innerText);
  const cut = profileText.search(/People also viewed|People you may know|More profiles|Promoted|You might like|Similar profiles|Explore Premium/i);
  if (cut > 120) profileText = profileText.slice(0, cut);
  profileText = profileText.slice(0, 2500);
  const gated = (!name || /sign in|join now|to view .*profile|authwall/i.test(document.body.innerText)) && profileText.length < 200;
  return { name: name || undefined, headline, profileText, gated };
}
