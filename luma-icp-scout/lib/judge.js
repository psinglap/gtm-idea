// judge.js — decides whether a person matches the ICP. Three tiers, best-available:
//   1) Claude (Haiku) — only if the user pasted their own API key in settings.
//   2) Free LOCAL model — Chrome's built-in on-device AI (Gemini Nano / Prompt API).
//      No key, no signup, no network, private, free. Runs on the user's machine.
//   3) Free heuristic (lib/icp.js) — keyword scan; always works, zero deps.
// Loaded as a plain script in the side panel; exposes window.Judge.

(function () {
  const CLAUDE_MODEL = "claude-haiku-4-5-20251001";

  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  // ---- combined text blob per person, labelling CURRENT vs PAST for the model --
  function personText(p) {
    const parts = [];
    if (p.name) parts.push("Name: " + clean(p.name));
    if (p.headline) parts.push("Current headline: " + clean(p.headline));
    if (p.bio) parts.push("Bio: " + clean(p.bio));
    const prof = clean(p.profileText || [p.about, p.experience].filter(Boolean).join(" "));
    if (prof) parts.push("Full profile (may include PAST/junior roles — do not penalize those): " + prof.slice(0, 1500));
    return parts.join("\n");
  }
  // Just the current positioning — used so negatives only apply to the current role.
  function currentText(p) { return [clean(p.headline), clean(p.bio)].filter(Boolean).join("  ·  "); }
  // True only when we actually have descriptive text (not just a name) to judge on.
  function hasRealData(p) {
    return !!(clean(p.headline) || clean(p.bio) || clean(p.profileText) || clean(p.about) || clean(p.experience));
  }

  // Natural-language ICP statement for the LLM judges.
  function icpStatement(icp) {
    return [
      icp.description || "",
      icp.siteSummary ? `\nContext about our product (from ${icp.website || "our site"}): ${icp.siteSummary}` : "",
      icp.minCompanyMonths ? `\nPrefer companies at least ${icp.minCompanyMonths} months old.` : ""
    ].join("").trim();
  }
  // Few-shot examples from the user's own accept/reject history — this is how the
  // judge LEARNS the user's ICP over time.
  function fewShot(feedback) {
    if (!feedback || !feedback.length) return "";
    const fmt = (tag) => (f) => `  ${tag}${f.reason ? ` (user's reason: ${f.reason})` : ""}: ${f.text}`;
    const g = feedback.filter((f) => f.label === "target").slice(-8).map(fmt("GOOD FIT"));
    const b = feedback.filter((f) => f.label === "reject").slice(-8).map(fmt("NOT A FIT"));
    if (!g.length && !b.length) return "";
    return "\n\nThe user manually labeled these before, with their reasons — learn the pattern and apply the SAME judgment to similar profiles:\n" + [...g, ...b].join("\n");
  }
  // Compact signal hints for the keyword heuristic / short prompts.
  function icpSummary(icp) {
    return Object.entries(icp.groups)
      .map(([k, g]) => `${k}: ${g.keywords.slice(0, 8).join(", ")}`)
      .join(" | ");
  }

  // ---- Tier 2: Chrome built-in on-device model ---------------------------
  // Detects the Prompt API across the shapes Chrome has shipped it under.
  async function getLocalModel() {
    try {
      if (typeof LanguageModel !== "undefined" && LanguageModel.create) {
        const a = await LanguageModel.availability?.();
        if (a && a !== "unavailable") return { api: LanguageModel };
      }
      const ai = self.ai || window.ai;
      if (ai?.languageModel?.create) {
        const a = await ai.languageModel.availability?.().catch(() => "readily");
        if (a !== "unavailable" && a !== "no") return { api: ai.languageModel };
      }
    } catch (_) {}
    return null;
  }

  async function judgeWithLocal(people, icp, local, onProgress, feedback) {
    const sys =
      "You qualify event attendees against the user's ideal-customer profile (ICP). " +
      "You are given the ICP and one attendee's profile text. Decide if they're a good fit.\n" +
      "STRICT RULES: Judge ONLY on their CURRENT / most-recent role (the headline and latest " +
      "position) — do NOT reject someone for PAST internships or junior roles if they are now a " +
      "founder/growth/marketing person. Use ONLY facts stated in the profile; NEVER invent titles, " +
      "companies, seniority, or company size. Someone whose CURRENT role is student/intern/junior IC " +
      "engineer with no founder/growth/marketing signal is NOT a target. If too thin to tell, answer " +
      "NO. Answer ONE line: YES or NO, dash, then a short reason quoting current-role evidence.\n\nICP:\n" + icpStatement(icp) + fewShot(feedback);
    // Chrome's Prompt API wants an output language declared. Different Chrome builds
    // accept it in different shapes, so try several, then fall back plainly.
    let session;
    const sysPrompt = { initialPrompts: [{ role: "system", content: sys }] };
    const variants = [
      { ...sysPrompt, outputLanguage: "en", expectedInputs: [{ type: "text", languages: ["en"] }], expectedOutputs: [{ type: "text", languages: ["en"] }] },
      { ...sysPrompt, outputLanguage: "en" },
      { ...sysPrompt, expectedOutputs: [{ type: "text", languages: ["en"] }] },
      sysPrompt
    ];
    for (const v of variants) {
      try { session = await local.api.create(v); break; } catch (_) {}
    }
    if (!session) return judgeWithHeuristic(people, icp);
    // Prompt with an explicit output language when supported.
    const ask = async (text) => {
      try { return await session.prompt(text, { outputLanguage: "en" }); }
      catch (_) { return await session.prompt(text); }
    };
    const out = [];
    for (let i = 0; i < people.length; i++) {
      const p = people[i];
      let isTarget = false, reason = "no verdict";
      try {
        const ans = await ask(
          `Attendee profile: ${personText(p)}\n\nFit the ICP? YES or NO — reason?`
        );
        const line = (ans || "").trim();
        isTarget = /^\s*(yes|y|true)\b/i.test(line);
        reason = line.replace(/^\s*(yes|no|y|n|true|false)\b[\s\-:—]*/i, "").slice(0, 160) || line.slice(0, 160);
      } catch (e) {
        reason = "local model error: " + e.message;
      }
      out.push({ ...p, isTarget, score: isTarget ? 6 : 0, reasons: [reason], judgedBy: "local" });
      onProgress?.(i + 1, people.length);
    }
    session.destroy?.();
    return out;
  }

  // ---- Tier 1: Claude (optional key) -------------------------------------
  async function judgeWithClaude(people, icp, apiKey, feedback) {
    const sys =
      "You qualify event attendees against the user's ICP. Judge each attendee ONLY on their " +
      "CURRENT / most-recent role (headline + latest position) — do NOT penalize PAST internships " +
      "or junior roles if they are now a founder/growth/marketing person. Use ONLY facts stated in " +
      "the profile; NEVER invent job titles, companies, seniority, or company size. Someone whose " +
      "CURRENT role is student/intern/junior IC engineer with no founder/growth/marketing signal is " +
      "NOT a target. If too thin to tell, isTarget=false. Only mark isTarget true when the current " +
      "role clearly matches; the reason must quote real current-role evidence. Return STRICT JSON only." + fewShot(feedback);
    const list = people.map((p, i) => `#${i}: ${personText(p)}`).join("\n");
    const userMsg =
      `ICP:\n${icpStatement(icp)}\n\nAttendees:\n${list}\n\n` +
      `Return {"results":[{"i":<index>,"isTarget":<bool>,"score":<0-10>,` +
      `"reason":"<short why, grounded in their profile>"}]} — one per attendee.`;

    const res = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-api-key": apiKey,
        "anthropic-version": "2023-06-01",
        "anthropic-dangerous-direct-browser-access": "true"
      },
      body: JSON.stringify({
        model: CLAUDE_MODEL, max_tokens: 2048, system: sys,
        messages: [{ role: "user", content: userMsg }]
      })
    });
    if (!res.ok) throw new Error(`Claude API ${res.status}: ${(await res.text()).slice(0, 160)}`);
    const data = await res.json();
    const txt = (data.content || []).map((c) => c.text || "").join("");
    const json = JSON.parse(txt.slice(txt.indexOf("{"), txt.lastIndexOf("}") + 1));
    return people.map((p, i) => {
      const r = (json.results || []).find((x) => x.i === i) || {};
      return {
        ...p, isTarget: !!r.isTarget,
        score: typeof r.score === "number" ? r.score : 0,
        reasons: r.reason ? [r.reason] : ["no verdict"], judgedBy: "claude"
      };
    });
  }

  // ---- Tier 3: heuristic -------------------------------------------------
  function judgeWithHeuristic(people, icp) {
    // Score keywords ONLY against the person's own current role (headline + bio),
    // NOT the full page scrape — the full page contains other people's headlines,
    // "People also viewed", and activity text that produced false positives.
    return people.map((p) => {
      const cur = currentText(p);
      return { ...p, ...window.ICP.score(cur, icp, cur), judgedBy: "heuristic" };
    });
  }

  // Public. mode: "auto" | "local" | "claude" | "heuristic".
  // Strategy: ALWAYS run the reliable keyword baseline, then OVERLAY a smart engine
  // (Claude/local) only where it actually produced a usable verdict. This keeps obvious
  // fits (e.g. "Founder of X") from being dropped when the local model returns junk.
  async function judge(people, icp, opts = {}) {
    const { apiKey, mode = "auto", onProgress, feedback } = opts;
    // Grounding gate: with no real profile text, a person CANNOT be a target — this
    // prevents any engine from fabricating a verdict from just a name.
    const gate = (arr) => arr.map((r) =>
      hasRealData(r) ? r : { ...r, isTarget: false, score: 0, reasons: ["not enough profile info — LinkedIn not read or private"] });

    const base = judgeWithHeuristic(people, icp); // reliable, always
    if (mode === "heuristic") return gate(base);

    let smart = null;
    try {
      if ((mode === "claude" || mode === "auto") && apiKey) {
        smart = await judgeWithClaude(people, icp, apiKey, feedback);
      } else if (mode === "local" || mode === "auto") {
        const local = await getLocalModel();
        if (local) smart = await judgeWithLocal(people, icp, local, onProgress, feedback);
        else if (mode === "local") console.warn("[ICP Scout] Local model unavailable; using keyword scorer.");
      }
    } catch (e) {
      console.warn("[ICP Scout] Smart judge failed; keeping keyword scorer:", e.message);
    }
    if (!smart) return gate(base);

    // Count how often the smart engine gave a real answer; if it mostly returned
    // empty (a flaky local model), trust the baseline entirely.
    const useful = smart.filter((s) => s && (s.reasons?.[0] || "").trim().length > 3);
    if (useful.length < Math.ceil(smart.length * 0.5)) {
      console.warn(`[ICP Scout] Smart engine gave few usable verdicts (${useful.length}/${smart.length}); using keyword scorer.`);
      return gate(base);
    }

    // Merge — a person is a target if EITHER engine says so (then grounding-gated).
    return gate(people.map((p, i) => {
      const b = base[i], s = smart[i];
      const sUseful = s && (s.reasons?.[0] || "").trim().length > 3;
      const reasons = [];
      if (sUseful && s.isTarget) reasons.push(...s.reasons);
      if (b.reasons?.length) reasons.push(...b.reasons);
      if (sUseful && !s.isTarget && !b.isTarget) reasons.push(...s.reasons);
      return {
        ...p,
        isTarget: b.isTarget || (sUseful && s.isTarget),
        score: Math.max(b.score || 0, (sUseful && s.score) || 0),
        reasons: reasons.length ? reasons : b.reasons,
        judgedBy: sUseful ? (s.judgedBy + "+heuristic") : "heuristic"
      };
    }));
  }

  async function availableEngines(apiKey) {
    return {
      claude: !!apiKey,
      local: !!(await getLocalModel()),
      heuristic: true
    };
  }

  window.Judge = { judge, personText, availableEngines };
})();
