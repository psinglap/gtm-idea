// icp.js — default ICP definition + the free full-text heuristic scorer.
// Loaded as a plain script; exposes window.ICP.

(function () {
  // Each signal group is a set of lowercase substrings we look for anywhere
  // in the person's combined text (Luma bio + LinkedIn headline/about/experience).
  const DEFAULT_ICP = {
    label: "",
    // Natural-language ICP — derived LIVE from your website, or written by you.
    // No hardcoded default; the judge reads this + the fetched site summary.
    description: "",
    website: "",       // your site — analyzed live to build the ICP
    siteSummary: "",   // fetched from the website on run / "Suggest"
    // Signal library below powers the free keyword heuristic (fallback engine).
    groups: {
      founder: {
        weight: 3,
        keywords: [
          "founder", "co-founder", "cofounder", "founding", "ceo",
          "building ", "i'm building", "im building", "we're building",
          "started ", "my startup", "my company"
        ]
      },
      growth: {
        weight: 3,
        keywords: [
          "growth", "head of growth", "growth lead", "growth marketer",
          "user acquisition", "demand gen", "demand generation",
          "performance marketing", "gtm", "go-to-market", "go to market"
        ]
      },
      marketing: {
        weight: 2,
        keywords: [
          "marketing", "cmo", "brand", "content marketing", "social media",
          "community", "head of marketing", "marketing lead", "paid social"
        ]
      },
      creatorIntent: {
        weight: 4, // example: the strongest buying signal for a creator-marketing product
        keywords: [
          "creator marketing", "influencer marketing", "influencer",
          "creator economy", "creators", "ugc", "user generated",
          "ambassador", "creator partnerships", "brand partnerships",
          "tiktok", "instagram marketing", "content creator"
        ]
      }
    },
    // These pull a person DOWN (likely not a buyer / too early / wrong persona).
    negatives: {
      weight: -3,
      keywords: [
        "student", "intern", "undergraduate", "undergrad", "bs/ms", "msc student",
        "phd student", "seeking opportunities", "looking for a job", "open to work",
        "recruiter", "talent acquisition", "investor", "venture capital", " vc ",
        "stealth", "day one", "just getting started"
      ]
    },
    // Company-age gate (months). The heuristic can't read a company's founded
    // date from profile text — this is only enforced when Apollo enrichment is
    // available. Kept here so the value is editable and travels with the ICP.
    minCompanyMonths: 10,
    // Minimum positive score to be called a "target". 2 = one clear signal
    // (marketing) or any stronger signal (founder/growth/creator) qualifies.
    threshold: 2
  };

  // rawText: full profile blob (headline + bio + all experience). currentText:
  // just the CURRENT positioning (headline + bio). Positive signals are matched
  // across the full text, but NEGATIVE signals (student/intern/etc.) are matched
  // ONLY against the current role — so a founder with a past internship isn't rejected.
  function score(rawText, icp, currentText) {
    const text = (" " + (rawText || "") + " ").toLowerCase();
    const cur = (" " + (currentText != null ? currentText : rawText || "") + " ").toLowerCase();
    let total = 0;
    const reasons = [];
    const tags = [];

    for (const [name, group] of Object.entries(icp.groups)) {
      const hits = group.keywords.filter((k) => k && text.includes(k.toLowerCase()));
      if (hits.length) {
        total += group.weight;
        tags.push(name);
        reasons.push(`${name}: ${hits.slice(0, 3).join(", ")}`);
      }
    }

    if (icp.negatives) {
      const negHits = icp.negatives.keywords.filter((k) => k && cur.includes(k.toLowerCase()));
      if (negHits.length) {
        total += icp.negatives.weight;
        reasons.push(`⚠ current-role negative: ${negHits.slice(0, 3).join(", ")}`);
      }
    }

    const isTarget = total >= (icp.threshold ?? 3) && tags.length > 0;
    return { score: total, isTarget, tags, reasons };
  }

  window.ICP = { DEFAULT_ICP, score };
})();
