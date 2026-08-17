/* signal landing page behavior (theme toggle + URL capture) */
(function () {
  "use strict";

  var root = document.documentElement;

  /* ----- theme: default light, remember choice, honor OS on first visit ----- */
  var STORE = "signal-theme";
  function apply(theme) {
    root.setAttribute("data-theme", theme);
    var btn = document.getElementById("mode");
    if (btn) btn.textContent = theme === "dark" ? "☀️" : "🌙";
  }
  var saved = null;
  try { saved = localStorage.getItem(STORE); } catch (e) {}
  if (saved === "dark" || saved === "light") {
    apply(saved);
  } else {
    // default LIGHT (per brand decision); still respect an explicit OS dark pref
    var prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    apply(prefersDark ? "dark" : "light");
  }

  var modeBtn = document.getElementById("mode");
  if (modeBtn) {
    modeBtn.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      apply(next);
      try { localStorage.setItem(STORE, next); } catch (e) {}
    });
  }

  /* ----- URL capture: hand off to the app with the entered domain ----- */
  // TODO: point this at the real signup/app URL when ready.
  var APP_URL = "https://web-app-chi-neon.vercel.app";

  function normalize(v) {
    v = (v || "").trim().replace(/^https?:\/\//i, "").replace(/\/+$/, "");
    return v;
  }

  Array.prototype.forEach.call(document.querySelectorAll("form[data-cta]"), function (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var input = form.querySelector("input");
      var domain = normalize(input && input.value);
      if (!domain) { if (input) input.focus(); return; }
      var dest = APP_URL + "/?url=" + encodeURIComponent("https://" + domain);
      window.location.href = dest;
    });
  });

  /* ----- how-it-works pipeline: signal -> person -> introduction ----- */
  var pipe = document.getElementById("pipe");
  if (!pipe) return;

  var COLOR = { fund: "var(--d-fund)", hire: "var(--d-hire)", team: "var(--d-team)", social: "var(--d-social)", event: "var(--d-event)" };
  var DATA = [
    { sig: "Series A · $12M", src: "Northwind Labs · via TechCrunch", type: "fund", init: "PS", name: "Priya Shah", role: "COO · Northwind Labs", status: "Introduced" },
    { sig: "Hiring a Head of Ops", src: "Acme Studio · via Greenhouse", type: "hire", init: "DR", name: "Dana Reyes", role: "Founder · Acme Studio", status: "Reply received" },
    { sig: "“Asana is a mess”", src: "Bright Collective · via Reddit", type: "social", init: "SC", name: "Sam Cole", role: "Ops Lead · Bright Collective", status: "Introduced" },
    { sig: "Seed round · $3M", src: "Orbit HQ · via TechCrunch", type: "fund", init: "LP", name: "Leo Park", role: "CEO · Orbit HQ", status: "Reply received" },
    { sig: "Speaking at SaaStr", src: "Loop Studio · via Luma", type: "event", init: "AG", name: "Ari Gold", role: "Founder · Loop Studio", status: "Introduced" },
    { sig: "Switched off ClickUp", src: "Meridian · via LinkedIn", type: "social", init: "TV", name: "Tara Vaz", role: "COO · Meridian", status: "Introduced" }
  ];

  function $(id) { return document.getElementById(id); }
  function setRecord(r) {
    $("s-dot").style.background = COLOR[r.type] || "var(--d-fund)";
    $("s-title").textContent = r.sig;
    $("s-src").textContent = r.src;
    $("p-init").textContent = r.init;
    $("p-name").textContent = r.name;
    $("p-role").textContent = r.role;
    $("c-init").textContent = r.init;
    var st = $("c-status");
    st.textContent = r.status;
    st.className = "cstatus " + (r.status.indexOf("Reply") === 0 ? "reply" : "intro");
  }

  var idx = 0;
  setRecord(DATA[0]);

  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) return;

  var cards = [$("st1"), $("st2"), $("st3")];
  setInterval(function () {
    idx = (idx + 1) % DATA.length;
    for (var i = 0; i < cards.length; i++) if (cards[i]) cards[i].classList.add("swapping");
    setTimeout(function () {
      setRecord(DATA[idx]);
      for (var j = 0; j < cards.length; j++) if (cards[j]) cards[j].classList.remove("swapping");
    }, 380);
  }, 3800);
})();
