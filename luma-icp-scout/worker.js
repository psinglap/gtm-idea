// worker.js — the resumable daily loop.
//
// The extension owns everything that needs a logged-in browser: enumerating and registering for
// Luma events, pulling guest lists, and reading LinkedIn profiles. The server owns all durable
// state, so this file holds NO work-in-progress of its own: every tick re-reads what to do from
// the server and checkpoints each item the moment it completes.
//
// That is what makes closing the laptop a normal event rather than a failure. A profile read
// mid-flight is leased on the server; the lease expires and the row returns to the queue. At
// most one profile is ever redone.
//
// Order each tick: heartbeat -> register (A) -> scan (B) -> read LinkedIn (C). Each stage stops
// cleanly when its daily budget is spent; the next tick resumes from server state.

const DEFAULTS = {
  serverUrl: "",           // e.g. https://warmgraph-api.onrender.com
  workspaceToken: "",      // pairs this install with one workspace. No login, no account.
  linkedinDailyCap: 200,   // start conservative; raise once gated reads stay at zero
  registerDailyCap: 25,
  horizonDays: 14,       // matches the server's WG_EVENT_HORIZON_DAYS
  // How often the REAL work runs. The alarm below fires far more often than this on purpose:
  // Chrome may only be open for a few minutes a day, so it wakes frequently and asks "is it
  // due yet?" — but a full pass re-syncs every Luma event twice over, and doing that every five
  // minutes is ~288 syncs a day for work that genuinely needs doing twice. Frequent wake-up,
  // infrequent work.
  runEveryHours: 12,
  readMinDelayMs: 15000,   // human-paced, randomised inside the window
  readMaxDelayMs: 20000,
  enabled: false,          // master switch, off until the user connects
};

// LinkedIn's first signal that it has noticed the pattern. Pushing through it is how accounts
// get restricted, so the worker stops for the day and shows red instead.
const GATED_STREAK_LIMIT = 5;

// The alarm is a WAKE-UP, not a schedule. It fires often so that a browser opened briefly still
// catches up; `runEveryHours` decides whether there is anything to do.
const TICK_MINUTES = 5;

// Login probes open a real background tab on luma.com and linkedin.com. They belong to a run,
// and to nothing else.
//
// This used to be on a schedule of its own — every 5-minute tick, plus a separate 15-minute
// alarm — so roughly 24 tabs an hour opened and closed behind whatever the user was doing. It
// bought nothing: a login session is stable for days, and knowing at 11:05 that Luma is signed
// in changes no decision when the next run is at 18:00.
//
// So there is no probe schedule at all. A quiet tick opens ZERO tabs — it posts a liveness ping,
// which is all the UI needs, and goes back to sleep. Sessions are checked once, at the top of a
// real pass, where a wrong answer would actually cost something.

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const todayKey = () => new Date().toISOString().slice(0, 10);

// --------------------------------------------------------------------------- //
// state (budgets + streaks only — never work-in-progress)                       //
// --------------------------------------------------------------------------- //
async function getConfig() {
  const s = await chrome.storage.local.get("workerConfig");
  return { ...DEFAULTS, ...(s.workerConfig || {}) };
}

async function setConfig(patch) {
  const cfg = await getConfig();
  const next = { ...cfg, ...patch };
  await chrome.storage.local.set({ workerConfig: next });
  return next;
}

async function getDaily() {
  const s = await chrome.storage.local.get("workerDaily");
  const d = s.workerDaily || {};
  if (d.day !== todayKey()) {
    // New day: budgets and the gated-pause both reset.
    return { day: todayKey(), reads: 0, registrations: 0, gatedStreak: 0, pausedUntilTomorrow: false };
  }
  return d;
}

async function setDaily(patch) {
  const d = await getDaily();
  const next = { ...d, ...patch };
  await chrome.storage.local.set({ workerDaily: next });
  return next;
}

// Status goes to the SERVER as well as local storage. It used to live only in
// chrome.storage.local, so the deployed UI had no way to say whether the browser half was
// running — the honest answer to "is it working?" was "open the extension and look", which is
// no answer at all when the point is to watch it from a live URL. Best-effort: a failed status
// post must never interrupt a run.
async function setStatus(patch) {
  const s = await chrome.storage.local.get("workerStatus");
  const next = { ...(s.workerStatus || {}), ...patch, updatedAt: new Date().toISOString() };
  await chrome.storage.local.set({ workerStatus: next });
  try {
    const cfg = await getConfig();
    if (cfg.serverUrl && cfg.workspaceToken) {
      const keepCounts = next.heartbeatOnly === true;
      await api("/outreach/worker-status", { cfg, method: "POST", body: {
        running: !!next.running,
        stage: next.stage || "",
        reason: next.reason || "",
        last_run_at: next.lastRunAt || "",
        last_error: next.lastError || "",
        next_due_in_min: next.nextDueInMin || 0,
        // `registered` alone cannot distinguish "nothing to do" from "tried five and every one
        // failed" — both read as 0. The skips and their reasons are the useful half.
        ...(keepCounts ? {} : { counts: {
          registered: (next.register || {}).registered || 0,
          skipped: (next.register || {}).skipped || 0,
          scanned: (next.scan || {}).scanned || 0,
          guests: (next.scan || {}).guests || 0,
          profiles: (next.linkedin || {}).read || 0,
        } }),
        ...(keepCounts ? {} : {
          failures: ((next.register || {}).failures || []).slice(0, 8)
            .map((f) => `${(f.url || "").split("/").pop()}: ${f.reason || ""}`.slice(0, 120)),
        }),
      }});
    }
  } catch (_) { /* never let reporting break the run */ }
  return next;
}

// --------------------------------------------------------------------------- //
// server                                                                        //
// --------------------------------------------------------------------------- //
// COLD START. The API is hosted on a free tier that sleeps after ~15 minutes idle and takes
// 30-60s to wake. This worker's traffic is exactly the sparse kind that keeps it asleep, so a
// wake-up is the NORMAL case, not an exception — and a run that aborts on the first timeout
// would fail every morning and look like a broken server.
//
// So: retry through the wake-up. The window is deliberately longer than a cold start, the waits
// grow, and only genuinely retryable failures qualify — a network error (a sleeping instance
// refuses the connection) or a 502/503/504 from the platform's own proxy while it boots. A 401
// or a 422 is a real answer and fails immediately; retrying it would only delay the truth.
const COLD_START_RETRIES = [2000, 5000, 10000, 20000, 30000];   // ~67s of patience

async function api(path, { method = "GET", body, cfg } = {}) {
  const c = cfg || (await getConfig());
  if (!c.serverUrl || !c.workspaceToken) throw new Error("Worker is not paired with a server yet");
  const sep = path.includes("?") ? "&" : "?";
  const url = `${c.serverUrl.replace(/\/$/, "")}${path}${sep}token=${encodeURIComponent(c.workspaceToken)}`;
  const opts = {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  };

  let lastError = "";
  for (let attempt = 0; attempt <= COLD_START_RETRIES.length; attempt++) {
    let res;
    try {
      res = await fetch(url, opts);
    } catch (e) {                       // connection refused / DNS / abort — instance asleep
      lastError = String((e && e.message) || e);
      res = null;
    }
    if (res && res.ok) return res.json();
    if (res && ![502, 503, 504, 408, 429].includes(res.status)) {
      // A real answer from a running server. Never retry it.
      throw new Error(`${method} ${path} -> ${res.status} ${(await res.text()).slice(0, 160)}`);
    }
    if (res) lastError = `${res.status}`;
    if (attempt < COLD_START_RETRIES.length) await sleep(COLD_START_RETRIES[attempt]);
  }
  throw new Error(`${method} ${path} -> server did not wake (${lastError})`);
}

// --------------------------------------------------------------------------- //
// Luma reads (run inside a luma.com tab so they use the user's own session)     //
// --------------------------------------------------------------------------- //
async function withLumaTab(fn) {
  const tab = await chrome.tabs.create({ url: "https://luma.com/home", active: false });
  try {
    await waitForComplete(tab.id, 20000).catch(() => {});
    await sleep(1200);
    return await fn(tab);
  } finally {
    chrome.tabs.remove(tab.id).catch(() => {});
  }
}

// Runs IN the page. Guest list for one event. Needs BOTH approval_status === "approved" and
// show_guest_list === true upstream — anything else 403s.
async function lumaGuestList(eventApiId, ticketKey) {
  const guests = [];
  let cursor = "", pages = 0;
  do {
    const u = `https://api.luma.com/event/get-guest-list?event_api_id=${eventApiId}`
      + `&pagination_limit=100`
      + (ticketKey ? `&ticket_key=${encodeURIComponent(ticketKey)}` : "")
      + (cursor ? `&pagination_cursor=${encodeURIComponent(cursor)}` : "");
    let j;
    try {
      const r = await fetch(u, { credentials: "include" });
      if (!r.ok) return { error: `guest list ${r.status}`, guests };
      j = await r.json();
    } catch (e) { return { error: String(e), guests }; }
    (j.entries || []).forEach((e) => guests.push({ user: e.user || {} }));
    cursor = j.has_more ? j.next_cursor : "";
    pages++;
  } while (cursor && pages < 40);
  return { guests };
}

function waitForComplete(tabId, timeout) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => { chrome.tabs.onUpdated.removeListener(fn); reject(new Error("load timeout")); }, timeout);
    const fn = (id, info) => {
      if (id === tabId && info.status === "complete") {
        clearTimeout(t); chrome.tabs.onUpdated.removeListener(fn); resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(fn);
  });
}

async function inTab(tabId, func, args = []) {
  const [{ result }] = await chrome.scripting.executeScript({ target: { tabId }, func, args });
  return result;
}

/* Inject the shared page module, then call one of its functions in the tab.
 *
 * lib/luma-page.js is the ONLY place that knows Luma's DOM, and it is shared with
 * tools/luma-runner so the two can never drift. The browser side once carried its own simplified
 * copy of the server's question-matching — including a normaliser missing two passes — and the
 * two disagreed silently: every stored answer missed, and forms were reported unanswerable while
 * the answer bank held the answer. One implementation, injected by both runtimes.
 */
async function withPage(tabId, method, args = []) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ["lib/luma-page.js"] });
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (m, a) => globalThis.LumaPage[m](...a),
    args: [method, args],
  });
  return result;
}

// --------------------------------------------------------------------------- //
// Stage A — register                                                            //
// --------------------------------------------------------------------------- //
// Everything that touches the page now lives in lib/luma-page.js, shared with tools/luma-runner.
// This stage only orders the steps and decides what counts as success.
//
// The one rule it enforces: success is decided by Luma's API, never by page text. A run once
// reported two registrations and delivered zero, because a success regex matched incidental copy
// after a click that reopened the form instead of submitting it. Nothing is written to the server
// until checkRegistration has seen the event in the user's own list.
const ACCEPT_TERMS_KEY = "accept event terms";
// Holds the NAME to sign, not a yes/no: there is no signing without an explicit string to sign,
// so a truthy flag can never stand in for one.
const SIGN_TERMS_KEY = "sign event terms as";

async function stageRegister(cfg) {
  const daily = await getDaily();
  const budget = Math.max(0, cfg.registerDailyCap - daily.registrations);
  if (budget <= 0) return { registered: 0, skipped: 0, questions: [] };

  const { events } = await api(`/outreach/register-queue?limit=${budget}`, { cfg });
  await logLine(`step 2 — ${(events || []).length} event(s) to register for`);
  // Only the exact-text half of the bank. Anything needing interpretation — which field a
  // question is asking for, whether a model may write it — is resolved by /outreach/plan-answers
  // so there is exactly one implementation of it.
  const answers = (await api("/outreach/questions", { cfg })).answers || {};

  let registered = 0, skipped = 0;
  const collected = [];            // every question that blocked an event, asked once at the end
  const failures = [];             // reported, never swallowed — a silent skip reads as success

  const consented = String(answers[ACCEPT_TERMS_KEY] || "").toLowerCase() === "yes";
  const signer = answers[SIGN_TERMS_KEY] || "";

  for (const ev of events || []) {
    if (registered >= budget) break;
    const slug = (ev.url || "").split("/").filter(Boolean).pop();
    await logLine(`opening ${ev.title || slug}`);
    let tab;
    try {
      tab = await chrome.tabs.create({ url: ev.url, active: false });
      await waitForComplete(tab.id, 20000).catch(() => {});
      await sleep(1500);

      const opened = await withPage(tab.id, "openForm");
      await logLine(`  form: ${opened.ok ? "opened" : opened.already ? "says already registered"
        : `not opened (${opened.reason})`}`);

      // "form did not open" is NOT the same as "did not register". On a one-click event the
      // button IS the registration: Luma signs the user up and shows a confirmation, and no form
      // ever appears. We clicked it, then treated the missing form as a failure and moved on
      // without ever asking Luma whether we were now registered — so a successful one-click
      // registration was recorded as a skip.
      //
      // Only "no register button" means nothing happened, because nothing was clicked. Everything
      // else falls through to the check below, which asks Luma directly.
      if (opened.reason === "no register button") {
        skipped++;
        await logLine(`skipped ${ev.title || slug}: no register button on the page`);
        failures.push({ event_id: ev.event_id, url: ev.url, reason: opened.reason });
        continue;
      }

      if (opened.ok) {          // a real form: fill it in and submit it
        // Step-by-step, because four events reached "form: opened" and then produced no line at
        // all — not a question, not a submit, not the catch. Every exit below is supposed to log,
        // so the assumption that one of them runs is wrong somewhere, and narrowing it by reading
        // the code has failed twice. These lines cost nothing and end the guessing.
        const questions = await withPage(tab.id, "readQuestions");
        await logLine(`  read ${(questions || []).length} question(s)`);
        // The SERVER decides what every question means and what may be answered from company
        // context. The browser never interprets a question — that is how the two implementations
        // drifted apart last time.
        const plan = await api("/outreach/plan-answers", {
          method: "POST", cfg, body: { event: ev.title, questions },
        });
        await logLine(`  plan: ${Object.keys(plan.filled || {}).length} answered, `
          + `${(plan.open || []).length} open`);
        if ((plan.open || []).length) {
          skipped++;
          await logLine(`  needs an answer: ${plan.open.map((q) => q.label).join("; ").slice(0, 120)}`);
          // Reported immediately rather than batched to the end of the pass. A pass that is
          // interrupted — a closed laptop, or an extension reload — used to lose every question
          // it had collected, so the events stayed blocked and nothing ever said why.
          collected.push(...plan.open);
          await api("/outreach/registration-questions", {
            method: "POST", cfg, body: { questions: collected },
          }).catch(() => {});
          continue;
        }
        const sent = await withPage(tab.id, "fillAndSubmit", [plan.filled, { consented, signer }]);
        await logLine(`  submit: ${sent.ok ? "clicked" : `refused (${sent.reason})`}`);
        if (!sent.ok) {
          skipped++;
          if (sent.waiver) {
            // Never silently skipped: the user has to see that this event wanted a waiver.
            collected.push({ label: "This event's terms are a LIABILITY WAIVER / MEDIA RELEASE, "
                                    + "not ordinary event terms. Accept it yourself if you want "
                                    + "to attend: " + ev.url,
                             required: true, type: "waiver", options: [] });
          } else if (sent.consent) {
            collected.push({ label: sent.consent, required: true, type: "consent",
                             options: ["Yes", "No"] });
          } else {
            failures.push({ event_id: ev.event_id, url: ev.url, reason: sent.reason });
          }
          continue;
        }
      }

      // Luma's OWN id when we have it, falling back to the url slug. checkRegistration matched
      // `ev.api_id === x || ev.url === x`, and we were only ever passing the slug taken off the
      // end of our stored url — so any event whose feed `url` differs from its public path could
      // never be confirmed, and reported as "submitted but not in the list" no matter what it did.
      const ident = ev.luma_event_id || slug;
      let check = await withPage(tab.id, "checkRegistration", [ident]);
      if (!check || !check.found) {          // the list can lag a beat behind the write
        await sleep(2500);
        check = await withPage(tab.id, "checkRegistration", [ident]);
      }
      if (!check || !check.found) {          // and it can lag more than a beat
        await sleep(6000);
        check = await withPage(tab.id, "checkRegistration", [ident]);
      }
      // What Luma's own list says, verbatim. "submit not confirmed" was reported for three very
      // different situations — never submitted, submitted and rejected, and submitted fine but
      // not found in the list — and they need completely different fixes.
      await logLine(`  luma says: ${check && check.found
        ? `found, status "${check.status || "(empty)"}"`
        : `NOT in my events list${check && check.error ? ` (${check.error})` : ""}`}`);
      if (check && check.found && check.status && check.status !== "invited") {
        await api("/outreach/registered", {
          method: "POST", cfg,
          body: { event_id: ev.event_id, approval_status: check.status },
        });
        registered++;
        await logLine(`registered — ${ev.title || slug} (${check.status})`);
      } else {
        skipped++;
        // Name the actual situation. `already` means the page showed no registration button at
        // all, so nothing was ever submitted — reporting that as a failed submit sent me looking
        // at the form code for something that had never run.
        const why = opened.already
          ? "page said already registered, but Luma's list does not have this event"
          : !opened.ok
            ? `clicked the button but no form appeared, and Luma does not show the event (${opened.reason})`
          : !check || !check.found
            ? "submitted, but Luma's list still does not show this event"
            : `submitted, but Luma still says "${check.status || "(empty)"}"`;
        await logLine(`could not register ${ev.title || slug}: ${why}`);
        failures.push({ event_id: ev.event_id, url: ev.url, reason: why });
      }
    } catch (e) {
      skipped++;
      // Logged, not merely collected. Three events showed "form: opened" and then nothing at all
      // in the activity log — they were throwing here, and the reason went into `failures`, which
      // the log never printed. A silent skip is indistinguishable from a stage that did not run.
      let why = "unknown";
      try { why = String((e && e.stack) || (e && e.message) || e); } catch (_) { /* keep going */ }
      await logLine(`  FAILED on ${ev.title || slug}: ${why.slice(0, 200)}`);
      failures.push({ event_id: ev.event_id, url: ev.url, reason: why });
    } finally {
      if (tab) chrome.tabs.remove(tab.id).catch(() => {});
    }
    await sleep(3000 + Math.random() * 2000);
  }

  // One report at the end of the run. The server dedupes, so the same question asked by
  // twenty events is one thing for the user to answer.
  if (collected.length) {
    await api("/outreach/registration-questions", {
      method: "POST", cfg, body: { questions: collected },
    }).catch(() => {});
  }

  await setDaily({ registrations: (await getDaily()).registrations + registered });
  // `registered` is now a count of Luma-confirmed registrations, and `failures` is carried out
  // rather than discarded, so a run that achieves nothing cannot report a clean summary.
  return { registered, skipped, questions: collected.length, failures };
}

// --------------------------------------------------------------------------- //
// Stage B — scan guest lists (no cap: one API call per page)                     //
// --------------------------------------------------------------------------- //
async function stageScan(cfg) {
  const q = await api("/outreach/scan-queue", { cfg });
  const events = q.events || [];
  // "0 past events with a readable guest list" reads as a failure, and an empty queue is in fact
  // the normal steady state — every list is already read until another event finishes. Say which.
  if (!events.length) {
    await logLine(`step 4 — no new guest lists: all ${q.already_read || 0} read`
      + (q.guest_list_hidden ? `, ${q.guest_list_hidden} hidden by the host` : ""));
    return { scanned: 0, guests: 0 };
  }
  await logLine(`step 4 — ${events.length} guest list(s) to read`
    + (q.already_read ? ` (${q.already_read} already done)` : ""));

  let scanned = 0, guests = 0;
  await withLumaTab(async (tab) => {
    for (const ev of events) {
      try {
        const res = await inTab(tab.id, lumaGuestList, [ev.luma_event_id, ev.ticket_key]);
        if (!res || res.error) continue;
        const out = await api("/outreach/guests", {
          method: "POST", cfg,
          body: { event_id: ev.event_id, guests: res.guests || [] },
        });
        scanned++;
        guests += out.queued || 0;
        await logLine(`read ${(res.guests || []).length} guests from ${ev.title || ev.event_id} — ${out.queued || 0} new`);
      } catch (_) { /* next event */ }
      await sleep(1500);
    }
  });
  return { scanned, guests };
}

// --------------------------------------------------------------------------- //
// Stage C — read LinkedIn (the budgeted, risky one)                             //
// --------------------------------------------------------------------------- //
async function stageLinkedin(cfg) {
  let daily = await getDaily();
  if (daily.pausedUntilTomorrow) return { read: 0, paused: true };

  const budget = Math.max(0, cfg.linkedinDailyCap - daily.reads);
  if (budget <= 0) return { read: 0, budgetSpent: true };

  const batch = Math.min(25, budget);
  const { contacts } = await api(`/outreach/linkedin-queue?limit=${batch}&browser_id=${encodeURIComponent(browserId())}`, { cfg });
  if (!contacts || !contacts.length) return { read: 0 };

  let read = 0;
  for (let i = 0; i < contacts.length; i++) {
    daily = await getDaily();
    if (daily.pausedUntilTomorrow || daily.reads >= cfg.linkedinDailyCap) break;
    if (await isIdle()) break;   // user stepped away; resume on the next tick

    const c = contacts[i];
    const result = await readOneLinkedIn(c.linkedin_url);
    const gated = !result.ok || result.gated || !(result.headline || result.profileText);

    // Checkpoint immediately: this is what makes a laptop closing mid-run cost one profile.
    try {
      await api("/outreach/linkedin-result", {
        method: "POST", cfg,
        body: {
          contact_id: c.contact_id,
          headline: result.headline || "",
          profile_text: result.profileText || "",
          gated,
        },
      });
    } catch (_) { /* lease will expire and the row returns to the queue */ }

    read++;
    const streak = gated ? daily.gatedStreak + 1 : 0;
    await setDaily({ reads: daily.reads + 1, gatedStreak: streak });

    if (streak >= GATED_STREAK_LIMIT) {
      await setDaily({ pausedUntilTomorrow: true });
      await setStatus({ lastError: `Paused: ${GATED_STREAK_LIMIT} consecutive gated LinkedIn reads` });
      break;
    }

    if (i < contacts.length - 1) {
      const jitter = cfg.readMinDelayMs + Math.random() * (cfg.readMaxDelayMs - cfg.readMinDelayMs);
      await sleep(jitter);
    }
  }
  return { read };
}

function browserId() {
  return "chrome-" + (chrome.runtime.id || "local").slice(0, 12);
}

async function isIdle() {
  try {
    const state = await chrome.idle.queryState(300);
    return state !== "active";
  } catch (_) { return false; }
}

// --------------------------------------------------------------------------- //
// activity log                                                                  //
// --------------------------------------------------------------------------- //
// One line per thing the worker does, streamed to the server as it happens.
//
// Counts answer "what did it achieve" but never "what is it doing right now", and when the
// answer is 0 they cannot tell an idle worker from a stuck one. Each line carries a sequence
// number so the server can append without duplicating on retry, and so ordering survives two
// lines landing in the same millisecond.
const LOG_KEEP = 60;

async function logLine(text) {
  try {
    const st = await chrome.storage.local.get(["workerLog", "workerLogSeq"]);
    const seq = (st.workerLogSeq || 0) + 1;
    const line = { seq, at: new Date().toISOString(), text: String(text).slice(0, 200) };
    const lines = [...(st.workerLog || []), line].slice(-LOG_KEEP);
    await chrome.storage.local.set({ workerLog: lines, workerLogSeq: seq });
    const cfg = await getConfig();
    if (cfg.serverUrl && cfg.workspaceToken) {
      // Fire and forget. A log line must never be able to fail the work it is describing.
      api("/outreach/worker-log", { method: "POST", cfg, body: { lines: [line] } }).catch(() => {});
    }
  } catch (_) { /* logging is never load-bearing */ }
}

// --------------------------------------------------------------------------- //
// heartbeat                                                                     //
// --------------------------------------------------------------------------- //
// "Connected" for Luma/LinkedIn means a live logged-in session, nothing stored. We check by
// asking each site who we are, which is cheap and honest.
async function checkSession(url, probe) {
  let tab;
  try {
    tab = await chrome.tabs.create({ url, active: false });
    await waitForComplete(tab.id, 15000).catch(() => {});
    await sleep(800);
    return await inTab(tab.id, probe);
  } catch (_) {
    return false;
  } finally {
    if (tab) chrome.tabs.remove(tab.id).catch(() => {});
  }
}

function lumaLoggedIn() {
  return !/sign in|log in/i.test((document.querySelector("nav")?.innerText) || "")
    && !location.pathname.startsWith("/signin");
}

function linkedinLoggedIn() {
  return !/authwall|\/login/.test(location.href) && !!document.querySelector("img.global-nav__me-photo, .global-nav__me");
}

const PROBES = {
  luma:     { url: "https://luma.com/home",            probe: lumaLoggedIn },
  linkedin: { url: "https://www.linkedin.com/feed/",   probe: linkedinLoggedIn },
};

// Opens tabs. Only ever called at the top of a real pass.
async function stageHeartbeat(cfg) {
  const cached = (await chrome.storage.local.get("sessionCache")).sessionCache || {};
  const out = {};

  for (const [name, { url, probe }] of Object.entries(PROBES)) {
    const ok = await checkSession(url, probe);
    if (ok !== (cached[name] || {}).ok) await logLine(`${name}: ${ok ? "signed in" : "SIGNED OUT"}`);
    cached[name] = { ok, at: Date.now() };
    out[name] = ok;
    if (ok) await api("/outreach/heartbeat", { method: "POST", cfg, body: { provider: name } }).catch(() => {});
  }

  await chrome.storage.local.set({ sessionCache: cached });
  return out;
}

// No tabs. This is the every-few-minutes path: say the browser is awake, repeat what the last
// real pass learned about the sessions, and stop.
async function quietBeat(cfg) {
  const cached = (await chrome.storage.local.get("sessionCache")).sessionCache || {};
  const out = {};
  for (const name of Object.keys(PROBES)) {
    out[name] = !!(cached[name] || {}).ok;
    if (out[name]) await api("/outreach/heartbeat", { method: "POST", cfg, body: { provider: name } }).catch(() => {});
  }
  return out;
}

// --------------------------------------------------------------------------- //
// the tick                                                                      //
// --------------------------------------------------------------------------- //
// A service worker is torn down and restarted freely by Chrome — on idle, on update, and on
// every extension reload. An in-memory flag therefore guarantees nothing: reloading the extension
// mid-pass started a second pass that opened the same events again, in parallel, against the same
// queue. The lease is in storage, with an expiry so a genuinely dead pass cannot block forever.
const LEASE_MINUTES = 20;

async function claimRun() {
  const held = (await chrome.storage.local.get("runLease")).runLease || 0;
  if (held && Date.now() - held < LEASE_MINUTES * 60000) return false;
  await chrome.storage.local.set({ runLease: Date.now() });
  return true;
}

const releaseRun = () => chrome.storage.local.set({ runLease: 0 });

async function tick(reason = "alarm") {
  const cfg = await getConfig();
  if (!cfg.enabled || !cfg.serverUrl || !cfg.workspaceToken) return;

  // Decide whether this is a real pass BEFORE touching any tab. The old order probed both sites
  // first and only then discovered it had nothing to do, which is how a worker that works twice
  // a day ended up opening tabs around the clock.
  const everyMs = Math.max(1, cfg.runEveryHours || 12) * 3600 * 1000;
  const last = (await chrome.storage.local.get("workerStatus")).workerStatus || {};
  const sinceLast = last.lastRunAt ? Date.now() - new Date(last.lastRunAt).getTime() : Infinity;
  // A "Run now" from the web UI. Chrome cannot be called from a server, so the request is a
  // counter we collect here — one cheap GET, no tab — and a number higher than the one we last
  // handled means run. That is what makes the button work from a live URL rather than only from
  // the side panel sitting on this machine.
  const handled = (await chrome.storage.local.get("runSeqHandled")).runSeqHandled || 0;
  let asked = 0;
  try { asked = (await api("/outreach/run-now", { cfg })).seq || 0; } catch (_) { /* offline */ }
  const requested = asked > handled;
  if (requested) await chrome.storage.local.set({ runSeqHandled: asked });

  const due = requested || reason !== "alarm" || sinceLast >= everyMs;

  if (!due) {
    const beat = await quietBeat(cfg).catch(() => ({}));   // no tabs
    const mins = Math.round((everyMs - sinceLast) / 60000);
    // Only the liveness fields. Passing counts here would post zeroes — `next` has no
    // register/scan sub-objects on a heartbeat tick — and wipe what the last real pass reported,
    // so a working browser would read "registered 0, guests found 0" between passes.
    await setStatus({ running: false, session: beat, nextDueInMin: mins, heartbeatOnly: true });
    return;                                  // not due — the browser stays quiet
  }

  if (!await claimRun()) return;             // another pass is already in flight
  await logLine(`pass started (${requested ? "Run now" : reason})`);
  const beat = await stageHeartbeat(cfg).catch(() => ({}));   // the only place tabs are probed
  await setStatus({ running: true, reason, lastError: "", nextDueInMin: 0 });
  try {
    const status = { reason };
    status.session = beat;

    if (beat.luma) {
      // Re-sync the event list first so register/scan queues are current.
      await withLumaTab(async (tab) => {
        // BOTH periods. `past` is what step 4 selects from — guest lists are only readable once
        // an event has ended — so syncing only `future` leaves the scan queue permanently empty
        // while every other stage looks healthy.
        const entries = [];
        for (const period of ["future", "past"]) {
          entries.push(...await withPage(tab.id, "listEvents",
            [`https://api.luma.com/home/get-events?period=${period}&pagination_limit=50`, 10]));
        }
        if (entries && entries.length) {
          status.events = await api("/outreach/events", { method: "POST", cfg, body: { entries } });
          await logLine(`step 1 — synced ${entries.length} events from Luma`);
        }
      });
      status.register = await stageRegister(cfg);
      status.scan = await stageScan(cfg);
    }
    if (beat.linkedin) {
      status.linkedin = await stageLinkedin(cfg);
    }

    const daily = await getDaily();
    const r = status.register || {}, sc = status.scan || {};
    await logLine(`pass finished — registered ${r.registered || 0}, guest lists read ${sc.scanned || 0}, `
      + `new people ${sc.guests || 0}, profiles ${(status.linkedin || {}).read || 0}`);
    await setStatus({ running: false, lastRunAt: new Date().toISOString(), ...status, daily });
  } catch (e) {
    await logLine(`pass failed: ${String(e.message || e)}`);
    await setStatus({ running: false, lastError: String(e.message || e) });
  } finally {
    await releaseRun();
  }
}

function installTriggers() {
  chrome.alarms.create("worker-tick", { periodInMinutes: TICK_MINUTES });
  // A second "worker-heartbeat" alarm used to live here doing exactly what the tick's own
  // heartbeat does. Two schedules meant two sets of probe tabs. Clear it on upgrade.
  chrome.alarms.clear("worker-heartbeat");
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "worker-tick") tick("alarm");
});

// Resume the moment Chrome comes back, rather than waiting out the first alarm interval.
// A pass only exists inside a running service worker, so if this handler is firing, any lease
// left in storage belongs to a worker that no longer exists and can be cleared immediately.
// Waiting out the expiry instead would leave the extension refusing to work for 20 minutes after
// every reload — exactly when someone has just fixed something and wants to see it run.
const freshStart = (reason) => () => {
  installTriggers();
  releaseRun().then(() => tick(reason));
};

chrome.runtime.onStartup.addListener(freshStart("startup"));
chrome.runtime.onInstalled.addListener(freshStart("installed"));

// Come back to work when the user does.
chrome.idle.onStateChanged.addListener((state) => { if (state === "active") tick("active"); });
