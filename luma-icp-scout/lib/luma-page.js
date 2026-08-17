/* Everything that touches a Luma page, in one place.
 *
 * Injected into a luma.com tab and called from there. Two runtimes use it and must never diverge:
 *
 *   · tools/luma-runner  — Playwright, headed Chrome, run by cron. This is what actually runs.
 *   · luma-icp-scout     — the Chrome extension, via chrome.scripting.executeScript({files:[...]}).
 *
 * A plain script on purpose: no imports, no exports, no build step. It defines globalThis.LumaPage
 * and nothing else, so `addScriptTag` and `executeScript({files})` can both just drop it in.
 *
 * Why this file exists at all: the browser side once carried its own simplified copy of the
 * server's question-matching, including a normaliser missing two passes. The two disagreed
 * silently — every stored answer missed, and forms were reported unanswerable while the answer
 * bank held the answer. One implementation, injected by both runtimes, is the fix.
 *
 * ---------------------------------------------------------------------------
 * The DOM facts below were all read off live Luma pages, never guessed. Each one cost a failed
 * registration to find:
 *
 *   · The registration form opens in an overlay that is NOT [role=dialog] and has no aria-modal.
 *     The stable anchor is `form.registration-form-container`.
 *   · The page keeps its own "Request to Join" button (type=button) while the overlay is open,
 *     and renders its header TWICE. A document-wide search for the submit button finds the
 *     page's and REOPENS the form instead of submitting. The form's own [type=submit] is unique.
 *   · An INVITED event says "Accept Invite" (beside a "Decline"), not "Register".
 *   · Choice questions come in two shapes and only one is a DIV:
 *         "Select one or more" -> div.lux-input   with span.placeholder
 *         "Select an option"   -> input.lux-input.has-indicator   (a real, NON-readonly input)
 *     Typing into the second looks like it worked and submits nothing. Both carry a `.chevron`.
 *   · Option menus are portalled outside the form and are LEFT IN THE DOM when closed, still
 *     reporting themselves visible. Identifying the right one by "newest" or "last" picked
 *     another question's menu.
 *   · "By registering, I agree to the event terms. *" is required, but the asterisk lives only in
 *     the label — the checkbox's DOM `.required` is false.
 *   · Ticking that checkbox can raise an "Accept Terms" dialog that wants a TYPED SIGNATURE.
 *   · The feed's `registration_availability` can say "open" for a page saying "Registration
 *     Closed".
 *   · Page text is never evidence of success. See checkRegistration.
 */
(function () {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const text = (el) => ((el && el.innerText) || "").replace(/\s+/g, " ").trim();

  const REG_FORM = "form.registration-form-container";
  // Luma lets hosts relabel this, so the list grows as new wordings turn up in the wild:
  // "Accept Invite" (invited events, beside a "Decline"), "One-Click Apply", "One-Click Register".
  // Anchored, so "Decline" can never match.
  // "Join Waitlist" is included: on a full event it is the only registration available, it is
  // free and reversible, and a waitlist place can convert. It yields approval_status "waitlist",
  // which checkRegistration reports as-is rather than as approved — a waitlist is not a seat and
  // does not grant guest-list access.
  // Buttons that mean "put me on this event". Hosts relabel this freely, so the old exact-match
  // list missed anything phrased differently — "One-Click RSVP" failed because the list happened
  // to contain "one-click register" and "one-click apply" but not that. Whole categories of event
  // were silently unregisterable because of a missing row in an alternation.
  //
  // Now: strip decoration, drop a "one-click" prefix and a "now/here/today" suffix, then match a
  // verb list. Anchored still, so it cannot fire on a sentence, but tolerant of the wording.
  const NEVER_BTN =
    /\b(decline|cancel|unregister|withdraw|remove|leave|not going|can'?t make it|maybe)\b/i;

  const OPEN_BTN = new RegExp(
    "^(accept( invite| invitation)?|request to join|register( for .*)?|rsvp|join( event| waitlist)?"
    + "|apply|get ticket(s)?|claim( your)?( spot| ticket| place)?|sign ?up|attend"
    + "|i( ?am|.?m) going|going|confirm( attendance)?|reserve( my)?( spot| seat| place)?)$", "i");

  // "One-Click RSVP", "Register Now", "Get Tickets →" all reduce to the verb this matches on.
  const btnText = (el) => text(el)
    .replace(/[→›»\u2192\u00bb]/g, " ")
    .replace(/^one[-\s]?click\s+/i, "")
    .replace(/\s+(now|here|today|free)$/i, "")
    .replace(/\s+/g, " ")
    .trim();

  const isOpenButton = (el) => {
    const t = btnText(el);
    return !!t && !NEVER_BTN.test(t) && OPEN_BTN.test(t);
  };
  const CLOSED = /registration closed|not currently taking registrations/i;
  // \b after "in" is load-bearing: without it "you're in" matches inside "You're invited", which
  // is what an INVITED event's page says. Every such event was read as already-registered, so the
  // form never opened, nothing was ever submitted, and the run then reported "submit not
  // confirmed by Luma" for a submit that had not happened. That was the whole invited backlog —
  // 157 events — failing on a missing word boundary.
  const ALREADY = /you'?re in\b|you are in\b|cancel registration|thanks for registering/i;
  const TERMS = /\b(agree|accept|consent|terms|conditions|policy|waiver|liability|release)\b/i;
  const SIGN_BTN = /^sign\s*(&|and)\s*accept$/i;

  // Character-for-character identical to registration.normalise() in Python, which keys the
  // answer bank. Same steps, same order — a test asserts they agree.
  const STOP = /\b(please|kindly|your|the|a|an|is|are|do|you|we|us)\b/g;
  const norm = (s) => (s || "").trim().toLowerCase()
    .replace(/\(.*?\)/g, " ")
    .replace(/[^a-z0-9\s]/g, " ")
    .replace(STOP, " ")
    .replace(/\s+/g, " ").trim();

  /** The one open menu's items. See the header note on stale menus. */
  async function openMenu(trigger, want) {
    const before = new Set(document.querySelectorAll(".lux-menu-content"));
    trigger.click();
    await sleep(800);
    const open = [...document.querySelectorAll(".lux-menu-content")]
      .filter((m) => m.offsetParent !== null);
    const menu = open.find((m) => !before.has(m)) || open[open.length - 1];
    const items = menu ? [...menu.querySelectorAll(".lux-menu-item")] : [];
    if (!want) return items;
    if (items.some((i) => norm(text(i)) === norm(want))) return items;
    // The menu could not be paired to its trigger. The wanted answer can only have come from
    // THIS question's own option list, so an unambiguous exact match identifies the right row.
    const all = open.flatMap((m) => [...m.querySelectorAll(".lux-menu-item")]);
    const exact = all.filter((i) => norm(text(i)) === norm(want));
    return exact.length === 1 ? exact : items;
  }

  function closeMenu() {
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    return sleep(300);
  }

  function setValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);   // React needs this
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  /** Open the registration form. {ok} | {ok:false, reason} | {already:true} */
  async function openForm() {
    const body = document.body.innerText;
    if (ALREADY.test(body)) return { ok: false, already: true };
    if (CLOSED.test(body)) return { ok: false, reason: "registration closed (feed said open)" };
    const btn = [...document.querySelectorAll("button, a[role='button'], a.lux-button")]
      .find(isOpenButton);
    if (!btn) return { ok: false, reason: "no register button" };
    btn.click();
    await sleep(3200);
    return document.querySelector(REG_FORM)
      ? { ok: true } : { ok: false, reason: "form did not open" };
  }

  /** Every unfilled question on the open form, for the server to resolve. */
  async function readQuestions() {
    const form = document.querySelector(REG_FORM);
    if (!form) return [];
    const out = [];
    for (const wrap of form.querySelectorAll(".lux-input-wrapper")) {
      const raw = text(wrap.querySelector(".lux-input-label, label"));
      if (!raw) continue;
      const el = wrap.querySelector("input.lux-input, textarea.lux-input, div.lux-input");
      if (!el) continue;
      const choice = el.tagName === "DIV" || !!wrap.querySelector(".chevron");
      const empty = el.tagName === "DIV" ? !!el.querySelector(".placeholder") : !el.value;
      if (!empty) continue;                 // Luma pre-filled it from a past registration
      let options = [];
      if (choice) {
        options = (await openMenu(el)).map(text).filter(Boolean);
        await closeMenu();
      }
      const label = raw.replace(/\s*\*\s*$/, "").trim();
      out.push({ id: label, label, required: /\*\s*$/.test(raw),
                 type: choice ? "choice" : "text", options });
    }
    return out;
  }

  /* "By registering, I agree to the event terms" does not tell you what the terms ARE.
   *
   * On a live event that label sat in front of a LIABILITY WAIVER AND MEDIA RELEASE — "release,
   * discharge, and hold harmless ... from any and all liability, claims, demands ... arising out
   * of any loss, damage, or injury, including death." Permission to tick an event-terms box is
   * not permission to waive liability or release media rights, and the label gave no sign.
   *
   * So the DIALOG TEXT is checked, not just the label. Ordinary terms proceed under the user's
   * standing permission; a waiver stops and is reported, whatever the permission says. */
  const HEAVY_TERMS = new RegExp(
    "\\b(liabilit\\w+|waiver|hold harmless|indemnif\\w+|media release|assume all risks?"
    + "|injury|death|arbitration|class action)\\b", "i");

  function heavyTermsDialog() {
    const btn = [...document.querySelectorAll("button")]
      .find((b) => /^accept terms$/i.test(text(b)));
    if (!btn) return null;
    const box = btn.closest("div");
    const body = text(box || document.body);
    return HEAVY_TERMS.test(body) ? body.slice(0, 400) : null;
  }

  /** Type the signature if one is asked for and we are permitted. */
  async function signIfAsked(signer) {
    const btn = [...document.querySelectorAll("button")].find((b) => SIGN_BTN.test(text(b)));
    if (!btn) return { ok: true };
    if (!signer) return { ok: false, reason: "requires a typed signature to accept terms" };
    // Walk OUTWARD from the button until a visible empty text field turns up.
    //
    // This used to look only inside `btn.closest("div")`, the button's immediate parent, which on
    // some events is a footer containing the button and nothing else — so the pad sat one level
    // up, was never found, and the event was skipped with "signature field not found" despite
    // having permission to sign. Climbing stops at the dialog rather than the document, so it can
    // never reach past it and type a name into the registration form behind.
    const looksLikeAPad = (i) =>
      !i.value && i.offsetParent !== null && !["checkbox", "radio", "hidden"].includes(i.type);

    let scope = btn.parentElement, pad = null;
    for (let up = 0; up < 6 && scope && !pad; up++) {
      pad = [...scope.querySelectorAll('input, textarea')].find(looksLikeAPad) || null;
      if (scope.getAttribute("role") === "dialog" || scope.classList.contains("lux-modal")) break;
      scope = scope.parentElement;
    }
    if (!pad) return { ok: false, reason: "signature field not found" };
    setValue(pad, signer);
    await sleep(500);
    btn.click();
    await sleep(1800);
    return { ok: true };
  }

  /** Fill the resolved answers, handle consent, submit. Never a verdict — see checkRegistration. */
  async function fillAndSubmit(filled, opts) {
    const { consented = false, signer = "" } = opts || {};
    const form = document.querySelector(REG_FORM);
    if (!form) return { ok: false, reason: "form not open" };

    for (const wrap of form.querySelectorAll(".lux-input-wrapper")) {
      const raw = text(wrap.querySelector(".lux-input-label, label"));
      const label = raw.replace(/\s*\*\s*$/, "").trim();
      const value = filled[label];
      if (!value) continue;
      const el = wrap.querySelector("input.lux-input, textarea.lux-input, div.lux-input");
      if (!el) continue;
      if (el.tagName === "DIV" || wrap.querySelector(".chevron")) {
        const hit = (await openMenu(el, value)).find((i) => norm(text(i)) === norm(value));
        if (hit) { hit.click(); await sleep(450); } else { await closeMenu(); }
      } else if (!el.value) {
        setValue(el, value);
      }
    }

    const sign1 = await signIfAsked(signer);          // some events raise it as the form opens
    if (!sign1.ok) return sign1;

    for (const box of form.querySelectorAll('input[type="checkbox"]')) {
      if (box.checked) continue;
      const label = text(box.closest("label") || box.parentElement);
      if (!(box.required || /\*/.test(label))) continue;    // genuinely optional, leave it alone
      if (TERMS.test(label) && !consented) {
        return { ok: false, reason: "needs consent to the event terms",
                 consent: label.replace(/\s*\*\s*$/, "").trim() };
      }
      box.click();
      await sleep(900);

      // The tick may raise an "Accept Terms" dialog. If what it actually contains is a waiver,
      // stop — regardless of the standing permission, which was given for event terms.
      const heavy = heavyTermsDialog();
      if (heavy) {
        return { ok: false, reason: "terms are a liability waiver / media release",
                 waiver: heavy };
      }

      const sign2 = await signIfAsked(signer);        // and others raise it from the tick itself
      if (!sign2.ok) return sign2;
    }

    const submit = form.querySelector('button[type="submit"]');
    if (!submit) return { ok: false, reason: "no submit button in form" };
    submit.click();
    await sleep(4000);
    return { ok: true, submitted: true };
  }

  /* The ONLY trustworthy answer to "am I registered?".
   *
   * Page text is not evidence. A run once reported two events registered and approved; Luma's
   * API showed one had never been registered and the other was still `invited`. The success
   * regex had matched incidental copy after a click that reopened the form rather than
   * submitting it. Nothing is recorded until this has seen the event in the user's own list. */
  async function checkRegistration(slugOrApiId) {
    let cursor = "", pages = 0;
    do {
      const u = "https://api.luma.com/home/get-events?period=future&pagination_limit=50"
        + (cursor ? `&pagination_cursor=${encodeURIComponent(cursor)}` : "");
      let j;
      try {
        const r = await fetch(u, { credentials: "include" });
        if (!r.ok) return { found: false, error: `http ${r.status}` };
        j = await r.json();
      } catch (e) { return { found: false, error: String(e) }; }
      for (const e of j.entries || []) {
        const ev = e.event || {};
        if (ev.api_id === slugOrApiId || ev.url === slugOrApiId || e.api_id === slugOrApiId) {
          // `guest_info`, not `guest` — a wrong path here reads as "registered, status unknown"
          // and would reject every genuine registration.
          return { found: true, status: (e.guest_info || {}).approval_status || "" };
        }
      }
      cursor = j.has_more ? j.next_cursor : "";
      pages++;
    } while (cursor && pages < 6);
    return { found: false };
  }

  /** Both Luma feeds, in the shape ingest.event_from_luma expects. */
  async function listEvents(url, maxPages) {
    const out = [];
    let cursor = "", pages = 0;
    do {
      const u = url + (cursor ? `&pagination_cursor=${encodeURIComponent(cursor)}` : "");
      let j;
      try {
        const r = await fetch(u, { credentials: "include" });
        if (!r.ok) break;
        j = await r.json();
      } catch (_) { break; }
      (j.entries || []).forEach((e) => out.push(e));
      cursor = j.has_more ? j.next_cursor : "";
      pages++;
    } while (cursor && pages < (maxPages || 10));
    return out;
  }

  globalThis.LumaPage = {
    norm, openForm, readQuestions, fillAndSubmit, signIfAsked, heavyTermsDialog,
    checkRegistration, listEvents,
  };
})();
