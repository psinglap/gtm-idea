#!/usr/bin/env node
/* The thing that actually runs the Luma loop, unattended.
 *
 *   node run.js login       once, to sign into Luma in the profile this uses
 *   node run.js sync        step 1  — read both feeds, post them to the API
 *   node run.js register    step 2  — register for whatever the queue returns
 *   node run.js daily       sync then register (what cron calls)
 *
 * Why this exists: luma-icp-scout/worker.js holds the same loop but is built on chrome.tabs,
 * chrome.alarms and chrome.storage, so it only runs inside a Chrome extension — and we are not
 * shipping one. The logic was correct and had nothing to execute it.
 *
 * All the page-side logic lives in luma-icp-scout/lib/luma-page.js and is injected here, so this
 * file contains no selectors and no knowledge of Luma's DOM. That separation is deliberate: a
 * second copy of that logic is exactly what caused the worst bug in this system's history, where
 * the browser's simplified question-matcher silently disagreed with the server's.
 *
 * Every decision about what a question MEANS is made by the API (/outreach/plan-answers), never
 * here. This file drives a browser and reports what happened.
 */
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const API = process.env.WG_API || "http://localhost:8000";
const TOKEN = process.env.WG_TOKEN || "";
const PROFILE = process.env.WG_PROFILE || path.join(__dirname, ".chrome-profile");
const PAGE_JS = path.join(__dirname, "..", "..", "luma-icp-scout", "lib", "luma-page.js");
const SF_PLACE = process.env.WG_LUMA_DISCOVER_PLACE || "discplace-BDj7GNbGlsF7Cka";
const HEADLESS = process.env.WG_HEADLESS === "1";
// Attach to an already-running Chrome instead of launching a profile. Set to its
// --remote-debugging-port endpoint, e.g. http://127.0.0.1:9222.
const CDP = process.env.WG_CDP || "";

// BOTH periods, always. `past` is what step 4 selects from — guest lists are only readable once
// an event has ended — so syncing only `future` leaves the scan queue permanently empty while
// every other stage looks healthy. ("upcoming" is not a valid period; it returns HTTP 400.)
const HOME_FEED_FUTURE = "https://api.luma.com/home/get-events?period=future&pagination_limit=50";
const HOME_FEED_PAST = "https://api.luma.com/home/get-events?period=past&pagination_limit=50";
const DISCOVER_FEED =
  `https://api.luma.com/discover/get-paginated-events?discover_place_api_id=${SF_PLACE}&pagination_limit=50`;

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

// COLD START. A free-tier API sleeps after ~15 minutes idle and takes 30-60s to wake, and a
// cron's traffic is exactly the sparse kind that keeps it asleep — so a wake-up is the normal
// case. Retry through it rather than failing the run: a network error or a 502/503/504 from the
// host's proxy means "still booting", while a 401 is a real answer and fails immediately.
const COLD_START_WAITS = [2000, 5000, 10000, 20000, 30000];
const RETRYABLE = new Set([408, 429, 502, 503, 504]);

async function api(pathname, { method = "GET", body } = {}) {
  if (!TOKEN) throw new Error("WG_TOKEN is not set — get it from POST /workspace");
  const sep = pathname.includes("?") ? "&" : "?";
  const url = `${API}${pathname}${sep}token=${TOKEN}`;
  const opts = {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  };
  let last = "";
  for (let attempt = 0; attempt <= COLD_START_WAITS.length; attempt++) {
    let r = null;
    try {
      r = await fetch(url, opts);
    } catch (e) { last = String((e && e.message) || e); }
    if (r && r.ok) return r.json();
    if (r && !RETRYABLE.has(r.status)) {
      throw new Error(`${method} ${pathname} -> ${r.status} ${await r.text()}`);
    }
    if (r) last = String(r.status);
    if (attempt < COLD_START_WAITS.length) {
      if (attempt === 0) log(`api asleep (${last}) — waiting for it to wake`);
      await new Promise((res) => setTimeout(res, COLD_START_WAITS[attempt]));
    }
  }
  throw new Error(`${method} ${pathname} -> server did not wake (${last})`);
}

/* Two ways to get a browser that is signed in to Luma.
 *
 * WG_CDP — attach to the Chrome you are already using. No new profile, no second sign-in, your
 * existing session. Chrome has to have been started with --remote-debugging-port for this to be
 * possible at all (see `npm run chrome`). This is the low-effort path.
 *
 * Otherwise — a dedicated profile under .chrome-profile, signed into once via `run.js login`.
 * Slower to set up, but it never fights with your normal browsing and works from cron on a
 * machine where you are not sitting.
 *
 * No password is handled on either path. */
async function browser() {
  if (CDP) {
    const b = await chromium.connectOverCDP(CDP);
    const ctx = b.contexts()[0];
    if (!ctx) throw new Error(`connected to ${CDP} but it has no browser context`);
    ctx.__cdp = b;                       // so close() can detach without killing your Chrome
    return ctx;
  }
  return chromium.launchPersistentContext(PROFILE, {
    headless: HEADLESS,
    channel: "chrome",
    viewport: { width: 1440, height: 900 },
    args: ["--disable-blink-features=AutomationControlled"],
  });
}

/* Detach, never quit. Closing a CDP-attached context would shut down the user's own browser
 * with every tab in it. */
async function shutdown(ctx) {
  if (ctx.__cdp) return ctx.__cdp.close();
  return ctx.close();
}

async function lumaPage(ctx, url) {
  const page = await ctx.newPage();
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.addScriptTag({ path: PAGE_JS });
  await page.waitForTimeout(1200);
  return page;
}

async function assertLoggedIn(page) {
  const n = await page.evaluate(() => LumaPage.listEvents(
    "https://api.luma.com/home/get-events?period=future&pagination_limit=1", 1).then((e) => e.length));
  if (!n) throw new Error("Not signed in to Luma in this profile — run `node run.js login` first");
}

// --------------------------------------------------------------------------- //
// login                                                                         //
// --------------------------------------------------------------------------- //
/* Waits for the session to APPEAR rather than for a keypress.
 *
 * The first version blocked on stdin ("press Enter when done"), which is unusable the moment the
 * command is backgrounded or run from cron — the window opens and nothing can ever release it.
 * Polling Luma's own API is also a better check than a keypress: it confirms the session really
 * works, instead of trusting that someone finished. */
async function login() {
  const ctx = await chromium.launchPersistentContext(PROFILE, {
    headless: false, channel: "chrome", viewport: { width: 1280, height: 900 },
  });
  const page = await ctx.newPage();
  await page.goto("https://luma.com/signin");
  console.log("\n  Sign in to Luma in the window that opened.");
  console.log("  This will notice by itself and close — nothing to press.\n");

  const deadline = Date.now() + 10 * 60 * 1000;
  while (Date.now() < deadline) {
    await page.waitForTimeout(4000);
    let ok = false;
    try {
      ok = await page.evaluate(async () => {
        const r = await fetch("https://api.luma.com/home/get-events?period=future&pagination_limit=1",
                              { credentials: "include" });
        if (!r.ok) return false;
        const j = await r.json();
        return Array.isArray(j.entries);
      });
    } catch (_) { /* mid-navigation; try again */ }
    if (ok) {
      log("signed in — session saved to", PROFILE);
      await ctx.close();
      return;
    }
  }
  await ctx.close();
  throw new Error("timed out after 10 minutes without a Luma session");
}

// --------------------------------------------------------------------------- //
// step 1 — sync                                                                 //
// --------------------------------------------------------------------------- //
async function sync(ctx) {
  const page = await lumaPage(ctx, "https://luma.com/home");
  await assertLoggedIn(page);
  const [future, past, disc] = await Promise.all([
    page.evaluate((u) => LumaPage.listEvents(u, 10), HOME_FEED_FUTURE),
    page.evaluate((u) => LumaPage.listEvents(u, 10), HOME_FEED_PAST),
    page.evaluate((u) => LumaPage.listEvents(u, 6), DISCOVER_FEED),
  ]);
  const home = [...future, ...past];
  log(`feeds: ${future.length} upcoming + ${past.length} past + ${disc.length} discovered`);
  // One POST of both feeds. The server dedupes by url and owns the horizon, the free/sold-out
  // rules and the leisure filter — none of that is decided here.
  const res = await api("/outreach/events", { method: "POST", body: { entries: [...home, ...disc] } });
  log("ingested:", JSON.stringify(res));
  await page.close();
  return res;
}

// --------------------------------------------------------------------------- //
// step 2 — register                                                             //
// --------------------------------------------------------------------------- //
async function register(ctx, limit = 50) {
  const { events } = await api(`/outreach/register-queue?limit=${limit}`);
  log(`queue: ${events.length} events`);
  const answers = (await api("/outreach/questions")).answers || {};
  const consented = String(answers["accept event terms"] || "").toLowerCase() === "yes";
  const signer = answers["sign event terms as"] || "";

  const done = [], blocked = [], failed = [], questions = [];

  for (const ev of events) {
    const slug = (ev.url || "").split("/").filter(Boolean).pop();
    let page;
    try {
      page = await lumaPage(ctx, ev.url);

      const opened = await page.evaluate(() => LumaPage.openForm());
      if (!opened.ok && !opened.already) {
        failed.push({ title: ev.title, reason: opened.reason });
        log(`  skip  ${ev.title.slice(0, 45)} — ${opened.reason}`);
        continue;
      }

      if (opened.ok) {
        const qs = await page.evaluate(() => LumaPage.readQuestions());
        // The server decides what every question means and what may be answered from context.
        const plan = await api("/outreach/plan-answers", {
          method: "POST", body: { event: ev.title, questions: qs },
        });
        if ((plan.open || []).length) {
          blocked.push({ title: ev.title, open: plan.open.map((q) => q.label) });
          questions.push(...plan.open);
          log(`  ask   ${ev.title.slice(0, 45)} — ${plan.open.length} question(s) for a human`);
          continue;
        }
        const sent = await page.evaluate(
          ([f, o]) => LumaPage.fillAndSubmit(f, o), [plan.filled, { consented, signer }]);
        if (!sent.ok) {
          blocked.push({ title: ev.title, open: [sent.consent || sent.reason] });
          log(`  ask   ${ev.title.slice(0, 45)} — ${sent.reason}`);
          continue;
        }
      }

      // Ask Luma, never the page. A submit that silently failed looks identical to one that
      // worked if all you have is innerText.
      let check = await page.evaluate((s) => LumaPage.checkRegistration(s), slug);
      if (!check.found) {
        await page.waitForTimeout(2500);
        check = await page.evaluate((s) => LumaPage.checkRegistration(s), slug);
      }
      if (check.found && check.status && check.status !== "invited") {
        await api("/outreach/registered", {
          method: "POST", body: { event_id: ev.event_id, approval_status: check.status },
        });
        done.push({ title: ev.title, status: check.status });
        log(`  OK    ${ev.title.slice(0, 45)} — ${check.status}`);
      } else {
        failed.push({ title: ev.title, reason: "submit not confirmed by Luma" });
        log(`  FAIL  ${ev.title.slice(0, 45)} — not confirmed by Luma`);
      }
    } catch (e) {
      failed.push({ title: ev.title, reason: String((e && e.message) || e) });
      log(`  ERR   ${ev.title.slice(0, 45)} — ${e.message}`);
    } finally {
      if (page) await page.close().catch(() => {});
      await new Promise((r) => setTimeout(r, 3000 + Math.random() * 2000));
    }
  }

  // Asked once, at the end, in one batch — never mid-run, and the same question at ten events
  // is one thing to answer.
  if (questions.length) {
    await api("/outreach/registration-questions", { method: "POST", body: { questions } })
      .catch((e) => log("could not post questions:", e.message));
  }
  return { done, blocked, failed };
}

// --------------------------------------------------------------------------- //
async function main() {
  const cmd = process.argv[2] || "daily";
  if (cmd === "login") return login();
  if (!CDP && !fs.existsSync(PROFILE)) {
    throw new Error(`no profile at ${PROFILE}\n`
      + "  either: npm run login          (one sign-in, dedicated profile)\n"
      + "  or:     npm run chrome         (restart Chrome with a debug port)\n"
      + "          WG_CDP=http://127.0.0.1:9222 node run.js daily   (uses your session)");
  }

  const ctx = await browser();
  try {
    if (cmd === "sync" || cmd === "daily") await sync(ctx);
    if (cmd === "register" || cmd === "daily") {
      const r = await register(ctx);
      console.log("\n" + "-".repeat(60));
      console.log(`registered ${r.done.length}   blocked ${r.blocked.length}   failed ${r.failed.length}`);
      r.done.forEach((d) => console.log(`  OK    ${d.status.padEnd(17)} ${d.title}`));
      r.blocked.forEach((b) => console.log(`  ASK   ${b.title}\n          ${b.open.join("\n          ")}`));
      r.failed.forEach((f) => console.log(`  FAIL  ${f.reason.padEnd(17)} ${f.title}`));
    }
    if (!["sync", "register", "daily"].includes(cmd)) {
      console.log("usage: node run.js [login|sync|register|daily]");
      process.exitCode = 1;
    }
  } finally {
    await shutdown(ctx);
  }
}

main().catch((e) => { console.error("\n" + e.message); process.exitCode = 1; });
