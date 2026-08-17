// pdf.js — renders a print-friendly target sheet for one saved event, then prints.
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const FALLBACK = "https://cdn.lu.ma/avatars-default/avatar_0.png";

document.getElementById("printBtn").addEventListener("click", () => window.print());

(async function () {
  const wantUrl = new URLSearchParams(location.search).get("event");
  const { saved } = await chrome.storage.local.get("saved");
  const events = saved || [];
  const ev = events.find((e) => e.url === wantUrl) || events[0];
  const root = document.getElementById("root");
  if (!ev) { root.textContent = "No saved event found. Run a scan first."; return; }

  const people = (ev.people || []).filter((p) => p.isTarget).sort((a, b) => (b.score || 0) - (a.score || 0));
  const when = ev.startAt ? new Date(ev.startAt).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : (ev.at || "").slice(0, 10);

  root.innerHTML =
    `<h1>${esc(ev.name || "Event")}</h1>` +
    `<div class="meta">${esc(when)}${ev.location ? " · " + esc(ev.location) : ""}</div>` +
    `<div class="count">${people.length} people to target</div>` +
    people.map((p) => {
      const li = p.linkedinUrl ? `<a href="${esc(p.linkedinUrl)}">${esc(p.linkedinUrl.replace(/^https?:\/\/(www\.)?/, ""))}</a>` : "";
      const talk = p.talkingPoint ? `<div class="talk"><b>Talking point:</b> ${esc(p.talkingPoint)}</div>` : "";
      return `<div class="person"><img class="avatar" src="${esc(p.avatarUrl || FALLBACK)}" />` +
        `<div><div class="name">${esc(p.name || "(no name)")}</div>` +
        `<div class="why">${esc((p.reasons || []).join(" · ") || p.headline || p.bio || "")}</div>` +
        `<div>${li}</div>${talk}</div></div>`;
    }).join("");

  root.querySelectorAll("img.avatar").forEach((img) =>
    img.addEventListener("error", () => { if (img.src !== FALLBACK) img.src = FALLBACK; }));
  document.title = (ev.name || "targets") + " — target sheet";
})();
