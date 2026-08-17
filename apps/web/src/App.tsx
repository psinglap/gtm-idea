import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api, outreach, workspaceToken, Account, AccountSignal, CIReport, ConnectionRow,
  CustomerReport, EventsReport, FeedbackItem, IcpReport, LeadsReport, MappedAnswer, WorkerStatus,
  WorkerLogLine, OutreachSettings, ScheduleStatus, EventsStatus, RunRow, FunnelStage,
  OpenQuestion, OutreachEvent, OutreachSummary, Readiness, SocialReport,
} from "./api";

type TabKey = "ci" | "icp" | "social" | "hiring" | "fundraising" | "events" | "customer" | "outreach";
const TABS: { key: TabKey; label: string }[] = [
  { key: "ci", label: "Competitive Intelligence" },
  { key: "icp", label: "ICP & Winning Category" },
  { key: "social", label: "Social Signals" },
  { key: "hiring", label: "Hiring Signals" },
  { key: "fundraising", label: "Fundraising Signals" },
  { key: "events", label: "Events" },
  { key: "customer", label: "★ Customer List" },
  { key: "outreach", label: "✉ Event Outreach" },
];

interface TabState { loading: boolean; error: string | null; data: any }
const empty: TabState = { loading: false, error: null, data: null };

export function App() {
  const [url, setUrl] = useState("https://serro.ai");
  const [active, setActive] = useState<TabKey>("ci");
  const [tabs, setTabs] = useState<Record<TabKey, TabState>>({
    ci: { ...empty }, icp: { ...empty }, social: { ...empty }, hiring: { ...empty },
    fundraising: { ...empty }, events: { ...empty }, customer: { ...empty },
    outreach: { ...empty },
  });

  function set(k: TabKey, patch: Partial<TabState>) {
    setTabs((t) => ({ ...t, [k]: { ...t[k], ...patch } }));
  }

  async function load(k: TabKey, force = false) {
    setActive(k);
    if (tabs[k].data && !force) return;
    set(k, { loading: true, error: null });
    try {
      let data: any;
      if (k === "ci") data = await api.competitiveIntelligence(url);
      else if (k === "icp") data = await api.icp(url);
      else if (k === "social") data = await api.social(url);
      else if (k === "hiring") data = await api.hiringLeads(url);
      else if (k === "fundraising") data = await api.fundraisingLeads(url);
      else if (k === "events") data = await api.events(url);
      else if (k === "outreach") data = await outreach.summary();
      else data = await api.customerList(url, force);   // force = rebuild; else serve normalized
      set(k, { loading: false, data });
    } catch (e: any) {
      set(k, { loading: false, error: String(e.message ?? e) });
    }
  }

  const cur = tabs[active];

  return (
    <div style={s.page}>
      <h1 style={{ marginBottom: 2 }}>Pleniq · Signal-Driven GTM</h1>
      <p style={s.sub}>Company URL → competitors, ICP, what the market is saying, and a live customer list.</p>

      <Connections url={url} />

      <div style={s.row}>
        <input style={s.input} value={url} onChange={(e) => setUrl(e.target.value)}
               placeholder="https://yourcompany.com" />
        <button style={s.button} onClick={() => load(active, true)} disabled={cur.loading}>
          {cur.loading ? "Working…" : "Run"}
        </button>
      </div>

      <div style={s.tabs}>
        {TABS.map((t) => (
          <button key={t.key} onClick={() => load(t.key)}
                  style={{ ...s.tab, ...(active === t.key ? s.tabActive : {}) }}>
            {t.label}
          </button>
        ))}
      </div>

      {cur.loading && <div style={s.note}>Running the {active} agent… (scraping + analysis can take ~30–90s)</div>}
      {cur.error && <div style={s.error}>Error: {cur.error}</div>}

      {!cur.loading && !cur.data && !cur.error && (
        <div style={s.note}>Click a tab to run that agent for <b>{url}</b>.</div>
      )}

      {active === "ci" && cur.data && <CI r={cur.data as CIReport} />}
      {active === "icp" && cur.data && <Icp r={cur.data as IcpReport} />}
      {active === "social" && cur.data && <Social r={cur.data as SocialReport} />}
      {active === "hiring" && cur.data && <Leads r={cur.data as LeadsReport} kind="Hiring" />}
      {active === "fundraising" && cur.data && <Leads r={cur.data as LeadsReport} kind="Fundraising" />}
      {active === "events" && cur.data && <Events r={cur.data as EventsReport} />}
      {active === "customer" && cur.data && (
        <Customers r={cur.data as CustomerReport} url={url} onRefresh={() => load("customer", true)} />
      )}
      {active === "outreach" && cur.data && (
        <Outreach r={cur.data as OutreachSummary} url={url} onRefresh={() => load("outreach", true)} />
      )}
    </div>
  );
}

/* ============================ Connections panel ============================ */
/* Four accounts, always all four, so the row is stable. Luma and LinkedIn store NO credential:
   they are green only while the extension reports a live browser session. */
const PROVIDER_LABEL: Record<string, string> = {
  luma: "Luma", linkedin: "LinkedIn", apollo: "Apollo",
  gmail: "Gmail (sends)", gmail_history: "Gmail (history only)",
};
const DOT: Record<string, string> = {
  connected: "#2f9e44", stale: "#f59f00", error: "#e03131",
  expired: "#e03131", disconnected: "#adb5bd", needs_reconnect: "#f59f00",
};

/* Luma and LinkedIn have no OAuth and no API key — they are read through your own logged-in
   browser. So the only useful action is "open the site and log in", which is what these do. */
const SESSION_SITE: Record<string, string> = {
  luma: "https://luma.com/home",
  linkedin: "https://www.linkedin.com/feed/",
};

function statusLabel(c: ConnectionRow): string {
  if (c.status === "connected") {
    const who = c.account_label || "connected";
    return c.via_claude ? `${who} · via Claude` : who;
  }
  if (c.status === "needs_reconnect") {
    const what = (c.missing_scopes ?? []).map((s) => s.split("/").pop()).join(", ");
    return `reconnect to grant ${what || "missing access"}`;
  }
  if (c.status === "error") return "needs attention";
  if (c.kind === "session") {
    return c.status === "stale" ? "log in again" : "log in required";
  }
  return "not connected";
}

function Connections({ url }: { url: string }) {
  const [rows, setRows] = useState<ConnectionRow[] | null>(null);
  const [ready, setReady] = useState<Readiness | null>(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  const refresh = useCallback(async () => {
    if (!workspaceToken.get()) return;
    try {
      const d = await outreach.connections();
      setRows(d.connections);
      setReady(d.readiness);
      setErr("");
    } catch (e: any) { setErr(String(e.message ?? e)); }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  async function pair() {
    setBusy("pair");
    try {
      const d = await outreach.pair(url);
      workspaceToken.set(d.token);
      await refresh();
    } catch (e: any) { setErr(String(e.message ?? e)); } finally { setBusy(""); }
  }

  async function connect(provider: string) {
    setBusy(provider);
    setErr("");
    try {
      if (provider === "gmail" || provider === "gmail_history") {
        const role = provider === "gmail" ? "send" : "history";
        let account = "";
        if (role === "history") {
          account = window.prompt(
            "Which mailbox should be searched for prior conversations?\n\n" +
            "This account never sends — it only stops you cold-emailing someone you already " +
            "have a thread with. Use the address where your existing conversations live.") ?? "";
          if (!account.trim()) return;
        }
        const { auth_url } = await outreach.googleAuthUrl(role, account.trim());
        window.open(auth_url, "_blank", "width=520,height=640");
      } else if (provider === "apollo") {
        const key = window.prompt(
          "Apollo API key — only needed for unattended daily runs on the server.\n\n" +
          "Leave this blank and click OK if you'd rather use the Apollo account already " +
          "connected in Claude.");
        if (key === null) return;                    // cancelled
        if (key.trim()) await outreach.connectApollo(key.trim());
        else await outreach.linkViaClaude("apollo");
        await refresh();
      }
    } catch (e: any) { setErr(String(e.message ?? e)); } finally { setBusy(""); }
  }

  if (!workspaceToken.get()) {
    return (
      <div style={s.note}>
        <b>Event outreach is not set up yet.</b> Pairing creates a workspace for <b>{url}</b>.
        There is no sign-in: a token identifies this workspace, and the extension holds the same one.
        <div style={{ marginTop: 8 }}>
          <button style={s.button} onClick={pair} disabled={busy === "pair"}>
            {busy === "pair" ? "Pairing…" : "Set up event outreach"}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div style={os.connBar}>
      {(rows ?? []).map((c) => (
        <div key={c.provider} style={os.chip} title={c.last_error || c.hint}>
          <span style={{ ...os.dot, background: DOT[c.status] ?? "#adb5bd" }} />
          <b>{PROVIDER_LABEL[c.provider]}</b>
          <span style={os.chipMeta}>{statusLabel(c)}</span>
          {c.kind === "credential" && (c.status !== "connected" || c.needs_reconnect) && (
            <button style={os.chipBtn} onClick={() => connect(c.provider)} disabled={busy === c.provider}>
              {c.needs_reconnect ? "Reconnect" : "Connect"}
            </button>
          )}
          {c.kind === "session" && c.status !== "connected" && (
            <a style={os.chipBtn} href={SESSION_SITE[c.provider]} target="_blank" rel="noreferrer">
              Open {PROVIDER_LABEL[c.provider]} ↗
            </a>
          )}
          {c.status === "connected" && (
            <button style={os.chipBtn} onClick={refresh} title="Re-check this connection now">
              Re-check
            </button>
          )}
        </div>
      ))}
      <div style={os.explain}>
        <b>Luma</b> and <b>LinkedIn</b> are read from your own logged-in browser — there is no
        OAuth for either, so no password or token is ever stored. Open each one, stay signed in,
        and runs will work. If a session has dropped, the run says so instead of failing quietly.
      </div>
      {ready && (ready.history_mailboxes?.length ?? 0) > 0 && (
        <div style={os.explain}>
          Checking <b>{ready.history_mailboxes!.join(" and ")}</b> for prior conversations before
          emailing anyone. If you send from one address but your threads live in another, connect
          the second as <i>Gmail (history only)</i> — otherwise people you already know look new.
        </div>
      )}
      {ready && (ready.unattended_blocked_by?.length ?? 0) > 0 && (
        <div style={os.explain}>
          <b>{ready.unattended_blocked_by!.join(" and ")}</b>{" "}
          {ready.unattended_blocked_by!.length > 1 ? "are" : "is"} connected through Claude, which
          works whenever you run this with me. A daily run on the server can't reach a Claude
          connector, so add an API key when you want it to go fully unattended.
        </div>
      )}
      {ready && !ready.secrets_configured && (
        <span style={os.warn}>Set WG_SECRET_KEY on the API before connecting Gmail or Apollo.</span>
      )}
      {ready && !ready.google_configured && (
        <span style={os.warn}>Set WG_GOOGLE_CLIENT_ID / SECRET to enable Gmail.</span>
      )}
      {err && <span style={{ ...os.warn, color: "#b00020" }}>{err}</span>}
    </div>
  );
}

/* ============================ Event Outreach tab =========================== */
const FUNNEL: { key: string; label: string }[] = [
  { key: "queued", label: "Waiting for Apollo" },
  { key: "reading", label: "Being read" },
  { key: "profiled", label: "Profile found, awaiting judgment" },
  { key: "judged", label: "ICP match" },
  { key: "rejected", label: "Not a fit" },
  { key: "enriched", label: "Email found" },
  { key: "sent", label: "Sent" },
  { key: "drafted", label: "Drafted" },
  { key: "skipped", label: "Skipped" },
  { key: "bounced", label: "Bounced (retired)" },
  { key: "meeting_scheduled", label: "Already have a meeting" },
  { key: "no_linkedin", label: "No LinkedIn handle (cannot judge)" },
  { key: "unreadable", label: "LinkedIn unreadable" },
];

function Outreach({ r, url, onRefresh }: { r: OutreachSummary; url: string; onRefresh: () => void }) {
  const [previews, setPreviews] = useState<{ to: string; subject: string; body: string }[] | null>(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  const skipped = useMemo(
    () => r.messages.filter((m) => m.status === "skipped"), [r.messages]);
  const delivered = useMemo(
    () => r.messages.filter((m) => m.status === "sent" || m.status === "drafted"), [r.messages]);

  async function preview() {
    setBusy("preview"); setErr("");
    try { setPreviews((await outreach.preview(url)).previews ?? []); }
    catch (e: any) { setErr(String(e.message ?? e)); } finally { setBusy(""); }
  }

  async function run(mode: "draft" | "send") {
    if (mode === "send" && !window.confirm(
      "This SENDS real emails from your Gmail. Drafts are the safer first run. Continue?")) return;
    setBusy(mode); setErr("");
    try { await outreach.runNow(url, mode); onRefresh(); }
    catch (e: any) { setErr(String(e.message ?? e)); } finally { setBusy(""); }
  }

  return (
    <>
      <Section title="The loop">
        <LoopControl />
      </Section>

      <EventsFunnels />
      <Funnel title="Everyone from every guest list" fetcher={outreach.funnel}
              note={"Each stage is what survived the one above it. Grey lines are people who stop "
                    + "there for good; the purple cards are work still to do."} />

      <Section title="Actions">
        <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
          <button style={s.button} onClick={preview} disabled={!!busy}>
            {busy === "preview" ? "Rendering…" : "Preview emails (sends nothing)"}
          </button>
          <button style={s.button} onClick={() => run("draft")} disabled={!!busy}>
            {busy === "draft" ? "Working…" : "Server half only → Gmail drafts"}
          </button>
          <button style={{ ...s.button, background: "#c92a2a" }} onClick={() => run("send")} disabled={!!busy}>
            {busy === "send" ? "Sending…" : "Server half only → send"}
          </button>
        </div>
        {err && <div style={s.error}>{err}</div>}
      </Section>


      <OpenQuestions />

      <EmailTemplate sampleEvent={r.events.find((e) => e.short_name)?.short_name || "your event"} />

      {previews && (
        <Section title={`Preview (${previews.length}) — nothing was sent`}>
          {previews.length === 0 && <div style={s.dim}>Nobody is ready to email yet.</div>}
          {previews.map((p, i) => (
            <div key={i} style={s.lead}>
              <div style={s.kv}><b>To:</b> {p.to}</div>
              <div style={s.kv}><b>Subject:</b> {p.subject}</div>
              <pre style={os.body}>{p.body}</pre>
            </div>
          ))}
        </Section>
      )}

      <Section title={`Events (${r.events.length})`}>
        <div style={s.dim}>
          The name below is what the email calls the event — chosen to be what a person actually
          remembers, not the formal title. Edit any that read oddly; the change sticks.
        </div>
        {r.events.map((e) => (
          <EventRow key={e.event_id} e={e} onSaved={onRefresh} />
        ))}
        {r.events.length === 0 && <div style={s.dim}>No Luma events synced yet.</div>}
      </Section>

      <Section title={`Sent / drafted (${delivered.length})`}>
        {delivered.map((m) => (
          <div key={m.id} style={s.card}>
            <b>{m.email}</b> · {m.subject}{" "}
            <span style={s.badge}>{m.status}</span>
            {m.gmail_draft_id && (
              <> · <a href={`https://mail.google.com/mail/u/0/#drafts`} target="_blank" rel="noreferrer">open in Gmail</a></>
            )}
          </div>
        ))}
        {delivered.length === 0 && <div style={s.dim}>Nothing delivered yet.</div>}
      </Section>

      <Section title={`Skipped (${skipped.length}) — and why`}>
        {skipped.map((m) => (
          <div key={m.id} style={s.card}>
            {m.email || "(no email)"} · <span style={s.badge}>{m.skip_reason}</span>
          </div>
        ))}
        {skipped.length === 0 && <div style={s.dim}>Nobody skipped yet.</div>}
      </Section>
    </>
  );
}

/* [anchor](url) -> a real link, mirroring warmgraph/outreach/template.py:to_html so the preview
   is the same transformation the send path performs, not a lookalike. */
function renderPreview(text: string): string {
  const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const out: string[] = [];
  const re = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
  let last = 0, m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    out.push(esc(text.slice(last, m.index)));
    out.push(`<a href="${esc(m[2])}" target="_blank" rel="noreferrer">${esc(m[1])}</a>`);
    last = m.index + m[0].length;
  }
  out.push(esc(text.slice(last)));
  return out.join("").replace(/\n/g, "<br>");
}

/* The preview must show the SAME string the send path would use — the event's stored
   short_name (venue, else the Luma community, else the title), not the formal title. Feeding it
   a hardcoded sample made the preview disagree with reality. */
/* Registration forms ask things we cannot answer from a profile. Rather than skipping those
   events for good, the questions surface here. You answer in one box however you like and an
   LLM maps your words onto the questions — but it shows the mapping BEFORE saving, because a
   wrong answer attached to the wrong question gets submitted to a real host and is otherwise
   invisible. */
/* The browser half, reported live.
 *
 * Steps 1, 2 and 4 — syncing Luma, accepting invitations, pulling guest lists — run in the
 * user's own Chrome because they need her logged-in session. That made them invisible here: the
 * page could show the server's queue but never say whether anything was filling it, so "is it
 * running?" could only be answered by opening the extension. This panel is the answer.
 */
// The whole loop, in one control: what it is doing now, when it last finished, when it runs
// next, and a button that starts both halves.
//
// Deliberately separate from the Pipeline buttons below, which only run the server half. Those
// were the only controls for a while, and "Run now" that quietly meant "run three of the seven
// steps" is how a loop can look started while the half that fills the queue never moves.
// How this page refreshes, and why it is not a timer.
//
// The funnels change when a RUN happens — four times a day — and when the browser half adds
// people. Polling them once a minute is 1,440 requests a day to observe four events, which is the
// same mistake as before in a smaller size: an earlier version refetched every 30 seconds in every
// open tab and exhausted the database's monthly transfer quota in a day.
//
// So the expensive panels load when you open the page, and again when you come back to the tab
// after being away — the two moments you are actually looking. Nothing polls them.
function useLoadOnFocus(load: () => void, staleAfterMs = 120000) {
  useEffect(() => {
    let lastLoad = 0;
    const run = () => { lastLoad = Date.now(); load(); };
    run();
    const onVisible = () => {
      if (!document.hidden && Date.now() - lastLoad > staleAfterMs) run();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
    };
  }, [load, staleAfterMs]);
}

// The live log is the exception: during a pass it is the only thing moving, and watching it is the
// point. So it polls quickly WHILE a run is in flight and slowly when nothing is happening — and
// never while the tab is hidden, because a tab nobody is looking at learns nothing.
function usePoll(load: () => void, everyMs: number) {
  useEffect(() => {
    let timer: number | undefined;
    const tick = () => { if (!document.hidden) load(); };
    const start = () => { stop(); tick(); timer = window.setInterval(tick, everyMs); };
    const stop = () => { if (timer) window.clearInterval(timer); timer = undefined; };
    const onVisibility = () => (document.hidden ? stop() : start());
    start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => { stop(); document.removeEventListener("visibilitychange", onVisibility); };
  }, [load, everyMs]);
}

function LoopControl() {
  const [w, setW] = useState<WorkerStatus | null>(null);
  const [log, setLog] = useState<WorkerLogLine[]>([]);
  const [kick, setKick] = useState("");
  const [cfg, setCfg] = useState<OutreachSettings | null>(null);
  const [sched, setSched] = useState<ScheduleStatus | null>(null);

  const load = useCallback(async () => {
    if (!workspaceToken.get()) return;
    try { setW(await outreach.workerStatus()); } catch { /* leave the last good state */ }
    try { setLog((await outreach.workerLog()).lines || []); } catch { /* log is optional */ }
    try { setCfg(await outreach.settings()); } catch { /* older server */ }
    try { setSched(await outreach.schedule()); } catch { /* older server */ }
  }, []);

  usePoll(load, w?.running ? 10000 : 120000);

  const runNow = useCallback(async () => {
    setKick("Starting…");
    try {
      await outreach.runEverything();
      setKick("Started. The server half is running now; Chrome joins within 5 minutes.");
    } catch (e) {
      setKick(`Could not start: ${(e as Error).message}`);
    }
    load();
  }, [load]);

  const seen = w?.received_at || w?.last_run_at || "";
  const minsAgo = seen ? Math.round((Date.now() - new Date(seen).getTime()) / 60000) : null;
  const stale = minsAgo === null || minsAgo > 15;
  const running = !!w?.running;
  const dot = w?.last_error ? "#c92a2a" : stale ? "#adb5bd" : running ? "#f59f00" : "#2f9e44";
  const when = (iso: string) =>
    iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric",
                                             hour: "2-digit", minute: "2-digit" }) : "never";

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <button style={{ ...s.button, fontWeight: 600 }} onClick={runNow} disabled={running}>
          {running ? "Running…" : "Run the whole loop now"}
        </button>
        <span style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 14 }}>
          <span style={{ width: 9, height: 9, borderRadius: 9, background: dot }} />
          <strong>
            {w?.last_error ? "Error"
              : stale ? "Chrome not connected"
              : running ? "Running now"
              : "Idle"}
          </strong>
        </span>
        <span style={s.dim}>last finished {when(w?.last_run_at || "")}</span>
        {!stale && !running && !!w?.next_due_in_min && (
          <span style={s.dim}>
            next automatic run in {w.next_due_in_min < 60
              ? `${w.next_due_in_min} min`
              : `${Math.round(w.next_due_in_min / 60)} h`}
          </span>
        )}
      </div>
      {cfg && (
        <div style={{ marginTop: 10, display: "flex", gap: 14, alignItems: "center",
                      flexWrap: "wrap", fontSize: 13 }}>
          <span style={{
            padding: "3px 10px", borderRadius: 999, fontWeight: 600,
            background: cfg.mode === "send" ? "#c92a2a" : "#e9ecef",
            color: cfg.mode === "send" ? "#fff" : "#495057",
          }}>
            {cfg.mode === "send" ? "SENDING real emails" : "Drafts only"}
          </span>
          <span style={s.dim}>register {cfg.event_horizon_days}d ahead</span>
          <span style={s.dim}>events up to {cfg.event_max_age_days}d old</span>
          <span style={s.dim}>{cfg.hourly_cap}/hour, {cfg.daily_cap}/day</span>
          {cfg.send_to_catchall && <span style={s.dim}>catch-all domains INCLUDED</span>}
        </div>
      )}
      {(() => {
        // Chrome's liveness, beside the scheduler's. It used to be its own panel titled "This
        // browser", carrying the previous pass's raw counters — "registered 0, skipped 5" — which
        // read as totals and were wrong as totals. The Events panel above answers that properly;
        // what is left worth saying is whether Chrome is connected at all.
        const seen = w?.received_at || w?.last_run_at || "";
        const mins = seen ? Math.round((Date.now() - new Date(seen).getTime()) / 60000) : null;
        const live = mins !== null && mins <= 15;
        return (
          <div style={{ marginTop: 8, fontSize: 13 }}>
            <span style={{
              padding: "2px 8px", borderRadius: 999, marginRight: 8, fontWeight: 600,
              background: live ? "#2f9e44" : "#adb5bd", color: "#fff",
            }}>
              {live ? "Chrome connected" : "Chrome not connected"}
            </span>
            <span style={s.dim}>
              {live
                ? "steps 1 to 4 — Luma sync, registering, guest lists — can run"
                : "steps 1 to 4 pause until Chrome is open; sending carries on regardless"}
            </span>
          </div>
        );
      })()}

      {sched?.enabled && (() => {
        // A heartbeat, not just "it will fire eventually". Without it the only proof the schedule
        // works was to wait for the next slot — the same silence that hid a dead cron earlier.
        const beat = sched.alive?.at ? (Date.now() - new Date(sched.alive.at).getTime()) / 60000 : null;
        const ok = beat !== null && beat < 12;
        return (
        <div style={{ marginTop: 8, fontSize: 13 }}>
          <span style={{
            padding: "2px 8px", borderRadius: 999, marginRight: 8, fontWeight: 600,
            background: ok ? "#2f9e44" : "#c92a2a", color: "#fff",
          }}>
            {ok ? "Scheduler alive" : "SCHEDULER NOT RESPONDING"}
          </span>
          {ok && sched.alive?.next_slot && (
            <span style={s.dim}>next run {sched.alive.next_slot} UTC · </span>
          )}
          <span style={s.dim}>Runs at {sched.runs_at_utc.join(", ")} UTC. </span>
          {sched.last?.slot ? (
            <span style={{ color: sched.last.state === "failed" ? "#c92a2a" : "inherit" }}>
              Last automatic run {sched.last.slot}:{" "}
              {sched.last.state === "failed"
                ? `FAILED — ${sched.last.error}`
                : sched.last.state === "running"
                  ? "in progress"
                  : `sent ${sched.last.delivered ?? 0}`}
            </span>
          ) : <span style={s.dim}>No automatic run yet.</span>}
        </div>
        );
      })()}
      {!!kick && <div style={{ ...s.dim, marginTop: 6 }}>{kick}</div>}
      {w?.last_error && <div style={{ ...s.error, marginTop: 6 }}>{w.last_error}</div>}
      {stale && !w?.last_error && (
        <div style={{ ...s.dim, marginTop: 8 }}>
          Steps 1 to 4 run in your own Chrome, so they only happen while it is open with the
          extension connected. The server half — Apollo, judging, drafting, sending — keeps
          running without it.
        </div>
      )}

      {!!sched?.history?.length && (
        <div style={{ marginTop: 14 }}>
          <div style={{ ...s.dim, marginBottom: 6 }}>
            Every run, newest first — Chrome registers and reads, the server does the rest.
          </div>
          {/* Fixed height and scrolling: the newest run stays at the top where it is looked for,
              and older ones fall away below instead of pushing the page down four times a day. */}
          <div style={{ maxHeight: 420, overflowY: "auto", paddingRight: 4 }}>
            {sched.history.map((r) => <RunFunnel key={r.slot + (r.at || "")} r={r} />)}
          </div>
        </div>
      )}

      {!!log.length && (
        <div style={{ marginTop: 14 }}>
          <div style={{ ...s.dim, marginBottom: 4 }}>Live activity</div>
          <div style={{
            maxHeight: 240, overflowY: "auto", background: "#121016", color: "#d8d4de",
            borderRadius: 8, padding: "8px 10px", fontSize: 12.5,
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", lineHeight: 1.65,
          }}>
            {[...log].reverse().map((l) => (
              <div key={l.seq} style={{ display: "flex", gap: 10 }}>
                <span style={{ color: "#8a8496", flexShrink: 0 }}>
                  {l.at ? new Date(l.at).toLocaleTimeString([], {
                    hour: "2-digit", minute: "2-digit", second: "2-digit" }) : ""}
                </span>
                <span>{l.text}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  );
}

// Events, counted the way a person asks about them. The Pipeline panel counts CONTACTS, and the
// browser panel showed one pass's raw counters — "registered 0" stayed on screen for hours after
// a pass that registered nothing, on a day when ten events had in fact been registered.
// One run, as the funnel it actually is. A row of eight numbers reads as eight unrelated facts;
// the point is that each stage takes the previous one's output and loses most of it, and the
// losses are where the work is. Registration and guest lists are included because a run report
// covering only email describes half the loop — and a week where Chrome never opened looks
// identical in the email numbers right up until the queue runs dry.
// One run per line, newest at the top, the whole loop left to right in the order it happens.
//
// The previous version gave each run four labelled columns and half a screen. Four of those a day
// meant scrolling past yesterday to see this morning, and the shape of "is this working" was
// buried in labels. A run is a sequence, so it reads as one: registered, read, looked up, found,
// judged, fit, SENT — and the eye can run down the SENT column across days.
// One run, drawn as the same funnel as the panels above — because it is the same funnel, over
// one pass instead of all time. Same bars, same "what fell out and why" lines, so a run can be
// compared to the overall shape at a glance rather than translated.
//
// Events come first because they are first: registering and reading guest lists is what produces
// the people the rest of the run works through.
function RunFunnel({ r }: { r: RunRow }) {
  if (r.state === "failed") {
    return (
      <div style={{ border: "1px solid #ffc9c9", background: "#fff5f5", borderRadius: 8,
                    padding: "10px 14px", marginBottom: 10, fontSize: 13, color: "#c92a2a" }}>
        <strong>{r.slot} UTC</strong> — FAILED: {r.error}
      </div>
    );
  }

  const apollo = r.apollo ?? 0;
  const found = r.emails_found ?? 0;
  const judged = r.judged ?? 0;
  const fit = r.targets ?? 0;
  const sent = r.sent ?? 0;
  const skipped = r.skipped ?? 0;
  const held = Object.entries(r.skip_reasons || {}).sort((a, b) => b[1] - a[1]);
  const top = Math.max(apollo, judged, 1);

  const stage = (n: number, label: string, note?: string, strong = false) => (
    <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "3px 0" }}>
      <span style={{ minWidth: 56, textAlign: "right", fontWeight: 700,
                     fontSize: strong ? 17 : 14, fontVariantNumeric: "tabular-nums" }}>{n}</span>
      <span style={{ width: 150, flexShrink: 0, height: strong ? 11 : 9, borderRadius: 6,
                     background: "#f1f3f5", overflow: "hidden" }}>
        <span style={{ display: "block", height: "100%", borderRadius: 6,
                       width: `${Math.max(1.5, (n / top) * 100)}%`,
                       background: strong ? "#CFD11A" : "#220925" }} />
      </span>
      <span style={{ fontSize: 13, fontWeight: strong ? 700 : 400 }}>{label}</span>
      {!!note && <span style={{ ...s.dim, fontSize: 11.5 }}>{note}</span>}
    </div>
  );
  const lost = (n: number, why: string) => n > 0 && (
    <div key={why} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11.5,
                            color: "#9a97a3", padding: "1px 0" }}>
      <span style={{ minWidth: 56, textAlign: "right" }}>−{n}</span>
      <span style={{ width: 12, borderBottom: "1px solid #dee2e6" }} />
      <span>{why}</span>
    </div>
  );

  return (
    <div style={{ border: "1px solid #e9ecef", borderRadius: 8, padding: "10px 14px",
                  marginBottom: 10 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 6,
                    flexWrap: "wrap" }}>
        <span style={{ fontWeight: 700, fontSize: 13 }}>{r.slot} UTC</span>
        <span style={{ ...s.dim, fontSize: 12 }}>
          {sent} sent · {fit} of {judged} judged were a fit
          {r.registered ? ` · ${r.registered} registered` : ""}
          {r.guests_added ? ` · ${r.guests_added.toLocaleString()} new people` : ""}
        </span>
      </div>

      {/* Always shown, including at zero. Hiding it when nothing happened made a run where
          Chrome never opened look identical to one where it did — and Chrome not running is the
          failure that takes longest to notice, because sending carries on for weeks off the
          existing queue while nothing new arrives. */}
      <div style={{ marginBottom: 6 }}>
        <div style={{ ...s.dim, fontSize: 11.5 }}>Events, in Chrome</div>
        {stage(r.registered ?? 0, "events registered")}
        {stage(r.guest_lists ?? 0, "guest lists read",
               r.guests_added ? `${r.guests_added.toLocaleString()} people added` : "")}
        {(r.registered ?? 0) === 0 && (r.guest_lists ?? 0) === 0 && (
          <div style={{ fontSize: 11.5, color: "#9a97a3", paddingLeft: 68 }}>
            nothing to do, or Chrome was not open
          </div>
        )}
      </div>

      <div style={{ ...s.dim, fontSize: 11.5, marginTop: 8 }}>People, on the server</div>
      {stage(apollo, "looked up in Apollo",
             r.reused ? `+${r.reused} already known, free` : "")}
      {lost(apollo - found, "no address, or one Apollo would not vouch for")}
      {stage(found, "verified email found")}
      {/* An ADDITION, so it goes above the line it adds to. Below it, it read as a deduction
          from 409 — which is why the column stopped adding up. */}
      {judged > found && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11.5,
                      color: "#6741d9", padding: "1px 0" }}>
          <span style={{ minWidth: 56, textAlign: "right" }}>+{judged - found}</span>
          <span style={{ width: 12, borderBottom: "1px solid #d0bfff" }} />
          <span>carried over — enriched on earlier runs, judged now</span>
        </div>
      )}
      {stage(judged, "judged against your ICP")}
      {lost(judged - fit, "wrong role for your ICP")}
      {stage(fit, "a fit", "join the ready pool")}

      {/* Sending is a SEPARATE funnel and has to be drawn as one. It draws from everyone ready,
          not from this run's fits, so subtracting the held-back from "a fit" and expecting SENT
          never worked: 158 − 53 is 105, and 50 went out. The two populations overlap and are not
          the same, and pretending otherwise is what made the column look broken. */}
      <div style={{ ...s.dim, fontSize: 11.5, marginTop: 8 }}>
        Sending — from everyone ready, not just this run
      </div>
      {stage(sent + skipped, "considered",
             fit > sent + skipped ? "it stops at the cap, so the rest are not looked at" : "")}
      {held.map(([k, v]) => lost(v, `held back — ${k.replace(/_/g, " ")}`))}
      {stage(sent, "SENT", sent >= 50 ? "the per-run cap stopped it here" : "", true)}
      {lost(r.bounced ?? 0, "bounced later, address retired")}
      {/* Delivery stops the instant it hits the cap, so everyone past that point is simply not
          looked at — they are still a fit, still reachable, still queued. Without this line the
          difference between "a fit" and "considered" reads as people who went missing. */}
      {(r.ready_after ?? 0) > 0 && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11.5,
                      color: "#6741d9", padding: "2px 0" }}>
          <span style={{ minWidth: 56, textAlign: "right" }}>{r.ready_after}</span>
          <span style={{ width: 12, borderBottom: "1px solid #d0bfff" }} />
          <span>still ready when the run stopped — first in line next time</span>
        </div>
      )}
    </div>
  );
}

function Funnel({ title, fetcher, note }: {
  title: string;
  fetcher: () => Promise<{ stages: FunnelStage[] }>;
  note: string;
}) {
  const [stages, setStages] = useState<FunnelStage[] | null>(null);

  const load = useCallback(async () => {
    if (!workspaceToken.get()) return;
    try { setStages((await fetcher()).stages); } catch { /* older server */ }
  }, [fetcher]);
  useLoadOnFocus(load);

  if (!stages?.length) return null;
  return <Section title={title}>{FunnelBody(stages, note)}</Section>;
}


// Two funnels in one panel: what is coming up, and what the events already held produced. They
// were a single column, which made "past events you attended" read as 386% of the approved line
// above it — a ratio between two numbers with nothing to do with each other.
function EventsFunnels() {
  const [d, setD] = useState<{ upcoming: FunnelStage[]; past: FunnelStage[] } | null>(null);

  const load = useCallback(async () => {
    if (!workspaceToken.get()) return;
    try { setD(await outreach.eventsFunnel()); } catch { /* older server */ }
  }, []);
  useLoadOnFocus(load);

  if (!d) return null;
  return (
    <Section title="Events">
      <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>Coming up</div>
      {FunnelBody(d.upcoming, "What we can still register for, and what you are already in.")}
      <div style={{ fontWeight: 600, fontSize: 13, margin: "18px 0 6px" }}>Already held</div>
      {FunnelBody(d.past,
        "What the events you attended actually produced. The last line is where the people " +
        "funnel below starts.")}
    </Section>
  );
}

function FunnelBody(stages: FunnelStage[], note: string) {
  const TRACK = 200;
  const last = stages.length - 1;
  // Scaled WITHIN a unit. The last two stages of the events funnel count people while everything
  // above counts events, and on one scale 11,553 against a funnel starting at 300 drew a bar four
  // screens wide: the number right, the picture meaningless.
  const topFor = (u?: string) =>
    Math.max(1, ...stages.filter((x) => (x.unit || "") === (u || "")).map((x) => x.on));

  return (
    <>
      <div style={{ fontSize: 12.5, color: "#6b6875", marginBottom: 12 }}>{note}</div>

      {stages.map((st, i) => {
        const top = topFor(st.unit);
        const prev = stages[i - 1];
        // A percentage only means something between two stages counting the same thing.
        const kept = i === 0 || (prev.unit || "") !== (st.unit || "")
          ? null : st.on / Math.max(prev.on, 1);
        const queue = (st.queue || []).filter((q) => q.n > 0);
        return (
          <div key={st.label}>
            {/* Subtractions sit ABOVE the stage: they are what was lost on the way into it. */}
            {i > 0 && st.out.filter((o) => o.n > 0).map((o) => (
              <div key={o.why}>
                <div style={{ display: "flex", alignItems: "center", gap: 8,
                              fontSize: 12, color: "#9a97a3", padding: "1px 0" }}>
                  <span style={{ minWidth: 66, textAlign: "right" }}>−{o.n}</span>
                  <span style={{ width: 14, borderBottom: "1px solid #dee2e6", height: 1 }} />
                  <span>{o.why}</span>
                </div>
                {/* "Wrong role" says nothing on its own. Which roles is the part worth arguing
                    with, and the part that tells you whether the ICP itself is wrong. */}
                {!!o.examples?.length && (
                  <div style={{ fontSize: 11.5, color: "#adb5bd", paddingLeft: 88 }}>
                    mostly {o.examples.join("  ·  ")}
                  </div>
                )}
              </div>
            ))}

            <div style={{ display: "flex", alignItems: "stretch", gap: 0 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "6px 0" }}>
                <span style={{ minWidth: 66, textAlign: "right", fontWeight: 700,
                               fontSize: i === last ? 21 : 17 }}>
                  {st.on.toLocaleString()}
                </span>
                <span style={{ width: TRACK, flexShrink: 0, height: i === last ? 14 : 12,
                               borderRadius: 7, background: "#f1f3f5", overflow: "hidden" }}>
                  <span style={{ display: "block", height: "100%", borderRadius: 7,
                                 width: `${Math.max(1.2, (st.on / top) * 100)}%`,
                                 background: i === last ? "#CFD11A" : "#220925" }} />
                </span>
                <span style={{ fontSize: 14, fontWeight: i === last ? 700 : 500 }}>{st.label}</span>
                {kept !== null && (
                  <span style={{ fontSize: 12, color: kept < 0.5 ? "#c92a2a" : "#868e96" }}>
                    {(kept * 100).toFixed(0)}% of the step above
                  </span>
                )}
              </div>
              {/* A stage that feeds something else says so, rather than looking like an end. */}
              {!!st.note && (
                <div style={{ fontSize: 11.5, color: "#6741d9", paddingLeft: 78 }}>
                  {st.note}
                </div>
              )}

              {/* Attached to the stage it belongs to, not floated off on its own. */}
              {!!queue.length && (
                <div style={{ display: "flex", alignItems: "center", marginLeft: 14 }}>
                  <span style={{ width: 22, borderTop: "2px dashed #b197fc" }} />
                  {queue.map((q) => (
                    <div key={q.why} style={{ border: "1px solid #b197fc", background: "#f8f5ff",
                                              borderRadius: 8, padding: "6px 12px" }}>
                      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                        <span style={{ fontWeight: 700, fontSize: 17 }}>
                          {q.n.toLocaleString()}
                        </span>
                        <span style={{ fontSize: 12.5 }}>{q.why}</span>
                      </div>
                      <div style={{ fontSize: 11.5, color: "#6741d9" }}>{q.next}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        );
      })}
    </>
  );
}

function OpenQuestions() {
  const [qs, setQs] = useState<OpenQuestion[] | null>(null);
  const [reply, setReply] = useState("");
  const [mapped, setMapped] = useState<MappedAnswer[] | null>(null);
  const [busy, setBusy] = useState<"" | "parse" | "save">("");
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    if (!workspaceToken.get()) return;
    try { setQs((await outreach.openQuestions()).open_questions); } catch { /* stay hidden */ }
  }, []);
  // Polled, not loaded once. Questions appear when a REGISTRATION RUN hits a form it cannot
  // answer, which happens minutes or hours after the page was opened — so a panel that fetched
  // once on mount showed an empty box while seven questions sat behind it blocking events. Every
  // other panel here polls; this one silently did not.
  useEffect(() => {
  }, [load]);
  useLoadOnFocus(load);

  async function parse() {
    if (!reply.trim()) return;
    setBusy("parse"); setNote("");
    try { setMapped((await outreach.parseAnswers(reply)).mapped); }
    catch (e: any) { setNote(String(e.message ?? e)); }
    finally { setBusy(""); }
  }

  async function save() {
    const answers = Object.fromEntries((mapped ?? [])
      .filter((m) => m.answer.trim()).map((m) => [m.key, m.answer.trim()]));
    if (!Object.keys(answers).length) return;
    setBusy("save");
    try {
      const r = await outreach.answer(answers);
      setNote(`Saved ${r.saved}. ${r.still_open} still open.`);
      setMapped(null); setReply(""); await load();
    } finally { setBusy(""); }
  }

  // Rendering nothing when the list is empty means "no questions" and "this panel is broken"
  // look identical, which is exactly how seven blocking questions went unnoticed.
  if (!qs) return null;
  if (qs.length === 0) {
    return (
      <Section title="Questions I couldn't answer">
        <div style={s.dim}>
          None right now. When a registration form asks something I should not guess at — a
          private fact, a consent, a choice that is yours — it appears here and blocks that event
          until you answer. Answers are reused for every future event that asks the same thing.
        </div>
      </Section>
    );
  }

  return (
    <Section title={`Questions I couldn't answer (${qs.length})`}>
      <div style={s.dim}>
        Registration forms asked these and I don't guess — a wrong answer goes to a real host.
        Answer in one box, in any order, however you like. Answers are reused for every future
        event that asks the same thing.
      </div>
      <ul style={{ margin: "10px 0", paddingLeft: 20, fontSize: 14, lineHeight: 1.7 }}>
        {qs.map((q) => (
          <li key={q.key}>
            {q.label}
            {/* Click-only dropdowns: showing the options is the only way to give a usable
                answer, since anything not on the list cannot be entered at all. */}
            {!!q.options?.length && (
              <span style={{ ...s.dim, display: "block", marginTop: 2 }}>
                pick one: {q.options.join("  ·  ")}
              </span>
            )}
          </li>
        ))}
      </ul>

      <textarea style={os.textarea} rows={4} value={reply} placeholder='e.g. "we are pre-seed, 3 people, and yes I am technical"'
                onChange={(e) => { setReply(e.target.value); setMapped(null); }} />
      <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 8 }}>
        <button style={s.button} onClick={parse} disabled={busy === "parse" || !reply.trim()}>
          {busy === "parse" ? "Reading…" : "Read my answers"}
        </button>
        {note && <span style={{ fontSize: 13, color: "#2f9e44" }}>{note}</span>}
      </div>

      {mapped && (
        <div style={{ marginTop: 14 }}>
          <div style={s.dim}>Check this before it saves. Edit anything that's wrong.</div>
          {mapped.map((m, i) => (
            <div key={m.key} style={{ display: "flex", gap: 10, alignItems: "center", margin: "8px 0" }}>
              <span style={{ flex: 1, fontSize: 13.5 }}>{m.label}</span>
              {m.options?.length ? (
                <select style={{ ...s.input, flex: 1, padding: "6px 10px", fontSize: 13 }}
                        value={m.answer}
                        onChange={(e) => setMapped((prev) => prev!.map((x, j) =>
                          j === i ? { ...x, answer: e.target.value } : x))}>
                  <option value="">not answered</option>
                  {m.options.map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              ) : (
                <input style={{ ...s.input, flex: 1, padding: "6px 10px", fontSize: 13 }}
                       value={m.answer} placeholder="not answered"
                       onChange={(e) => setMapped((prev) => prev!.map((x, j) =>
                         j === i ? { ...x, answer: e.target.value } : x))} />
              )}
            </div>
          ))}
          <button style={{ ...s.button, marginTop: 6 }} onClick={save} disabled={busy === "save"}>
            {busy === "save" ? "Saving…" : "Save answers"}
          </button>
        </div>
      )}
    </Section>
  );
}

function EmailTemplate({ sampleEvent }: { sampleEvent: string }) {
  const SAMPLE = { first_name: "Jane", name: "Jane Doe", event_name: sampleEvent };
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [fields, setFields] = useState<string[]>([]);
  const [state, setState] = useState<"loading" | "idle" | "saving" | "saved">("loading");
  const [err, setErr] = useState("");
  const [showPreview, setShowPreview] = useState(true);

  const filled = (s: string) =>
    s.replace(/\{(\w+)\}/g, (whole, f) => (SAMPLE as Record<string, string>)[f] ?? whole);

  useEffect(() => {
    outreach.getTemplate()
      .then((t) => { setSubject(t.subject); setBody(t.body); setFields(t.fields); setState("idle"); })
      .catch((e) => { setErr(String(e.message ?? e)); setState("idle"); });
  }, []);

  async function save() {
    setState("saving"); setErr("");
    try { await outreach.saveTemplate(subject, body); setState("saved"); }
    catch (e: any) { setErr(String(e.message ?? e)); setState("idle"); }
  }

  return (
    <Section title="The email">
      <div style={s.dim}>
        Write it once, calendar link and all. These get filled in per person:{" "}
        {fields.map((f) => <code key={f} style={os.field}>{`{${f}}`}</code>)}
        <br />
        Wrap a link as <code style={os.field}>[Book a Slot](https://…)</code> and the reader sees
        clickable <b>Book a Slot</b>. The plain-text copy keeps the URL, so nothing is lost.
      </div>
      <input style={{ ...s.input, width: "100%", margin: "10px 0 8px" }} value={subject}
             onChange={(e) => { setSubject(e.target.value); setState("idle"); }}
             placeholder="Subject" />
      <textarea style={os.textarea} value={body} rows={12}
                onChange={(e) => { setBody(e.target.value); setState("idle"); }} />
      <div style={{ display: "flex", alignItems: "center", gap: 8, margin: "10px 0 4px" }}>
        <b style={{ fontSize: 13 }}>Preview</b>
        <span style={s.dim}>exactly what lands in their inbox</span>
        <button style={os.chipBtn} onClick={() => setShowPreview((v) => !v)}>
          {showPreview ? "hide" : "show"}
        </button>
      </div>
      {showPreview && (
        <div style={os.preview}>
          <div style={os.previewSubj}>{filled(subject).replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")}</div>
          <div dangerouslySetInnerHTML={{ __html: renderPreview(filled(body)) }} />
        </div>
      )}
      <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 8 }}>
        <button style={s.button} onClick={save} disabled={state === "saving"}>
          {state === "saving" ? "Saving…" : "Save template"}
        </button>
        {state === "saved" && <span style={{ color: "#2f9e44", fontSize: 13 }}>Saved</span>}
        {err && <span style={{ color: "#b00020", fontSize: 13 }}>{err}</span>}
      </div>
    </Section>
  );
}

const SOURCE_LABEL: Record<string, string> = {
  venue: "from the venue", calendar: "from the Luma calendar", title: "from the title",
};

function EventRow({ e, onSaved }: { e: OutreachEvent; onSaved: () => void }) {
  const [name, setName] = useState(e.short_name);
  const [saving, setSaving] = useState(false);
  const dirty = name.trim() !== e.short_name;

  async function save() {
    setSaving(true);
    try { await outreach.setShortName(e.event_id, name.trim()); onSaved(); }
    finally { setSaving(false); }
  }

  return (
    <div style={s.lead}>
      <div style={s.kv}>
        <b>{e.title || "(untitled)"}</b>{" "}
        <span style={s.badge}>{e.approval_status || "not registered"}</span>{" "}
        {e.scanned ? <span style={s.badge}>scanned</span>
          : e.blocked_reason
            ? <span style={{ ...s.badge, background: "#ffe3e3", color: "#c92a2a" }}>{e.blocked_reason}</span>
            : <span style={s.badge}>pending scan</span>}
      </div>
      <div style={s.dim}>{e.starts_at?.slice(0, 10)} · {e.guest_count} guests</div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 6, flexWrap: "wrap" }}>
        <span style={s.dim}>Email will say:</span>
        <input style={os.nameInput} value={name} onChange={(ev) => setName(ev.target.value)} />
        {e.short_name_source && !dirty && (
          <span style={s.badge}>{SOURCE_LABEL[e.short_name_source] ?? e.short_name_source}</span>
        )}
        {dirty && (
          <button style={os.chipBtn} onClick={save} disabled={saving}>
            {saving ? "saving…" : "save"}
          </button>
        )}
      </div>
      <div style={os.sentence}>“I was at <b>{name || "…"}</b> and was hoping to say hello in person.”</div>
    </div>
  );
}

const os: Record<string, React.CSSProperties> = {
  nameInput: { padding: "4px 9px", fontSize: 13, border: "1px solid #ced4da", borderRadius: 7, minWidth: 220 },
  sentence: { fontSize: 12.5, color: "#868e96", marginTop: 5, fontStyle: "italic" },
  field: { background: "#f1f3f5", borderRadius: 5, padding: "1px 5px", marginRight: 5, fontSize: 12 },
  preview: { border: "1px solid #e9ecef", borderRadius: 10, padding: "14px 16px", background: "#fff", fontSize: 14, lineHeight: 1.55, color: "#111" },
  previewSubj: { fontWeight: 700, fontSize: 14.5, marginBottom: 10, paddingBottom: 8, borderBottom: "1px solid #f1f3f5" },
  textarea: { width: "100%", minHeight: 220, resize: "vertical", padding: "10px 12px", fontSize: 13.5, lineHeight: 1.5, border: "1px solid #ced4da", borderRadius: 8, fontFamily: "inherit" },
  connBar: { display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", margin: "10px 0 4px" },
  chip: { display: "flex", alignItems: "center", gap: 7, fontSize: 12.5, border: "1px solid #dee2e6", borderRadius: 999, padding: "4px 12px", background: "#fff" },
  dot: { width: 8, height: 8, borderRadius: "50%", flexShrink: 0 },
  chipMeta: { color: "#868e96" },
  chipBtn: { fontSize: 11.5, border: "none", background: "none", color: "#4c6ef5", cursor: "pointer", textDecoration: "underline", padding: 0 },
  warn: { fontSize: 12, color: "#e8590c" },
  explain: { flexBasis: "100%", fontSize: 12.5, color: "#868e96", lineHeight: 1.5, marginTop: 2 },
  funnel: { display: "flex", gap: 10, flexWrap: "wrap" },
  stat: { border: "1px solid #eee", borderRadius: 10, padding: "8px 14px", minWidth: 96 },
  statN: { fontSize: 20, fontWeight: 700 },
  statL: { fontSize: 11.5, color: "#868e96" },
  body: { background: "#f8f9fa", borderRadius: 8, padding: "10px 12px", fontSize: 13, whiteSpace: "pre-wrap", fontFamily: "inherit", margin: "6px 0 0" },
};

function CI({ r }: { r: CIReport }) {
  const { profile: p, competitive: c } = r;
  return (
    <>
      <Section title="Company">
        <div style={s.kv}><b>{p.name}</b> — {p.one_liner || p.what_they_do}</div>
        <div style={s.kv}>{p.category} / {p.subcategory} · {p.business_model} · {p.stage}</div>
      </Section>
      <Section title="Competitive landscape">
        <div style={s.kv}><b>Crowdedness:</b> {c.crowdedness_score}/5 — {c.market_crowdedness}</div>
        <div style={s.kv}><b>Unique advantage:</b> {c.our_unique_advantage}</div>
        <div style={s.kv}><b>Moat:</b> {c.our_moat}</div>
        <div style={s.kv}><b>Whitespace:</b> {c.whitespace}</div>
        <div style={s.kv}><b>Positioning:</b> {c.positioning}</div>
      </Section>
      <Section title={`Competitors (${c.direct_competitors.length})`}>
        {c.direct_competitors.map((x, i) => (
          <div key={i} style={s.card}>
            <b>{x.name}</b> {x.tier && <span style={s.badge}>{x.tier}</span>}
            {x.url && <> · <a href={x.url} target="_blank" rel="noreferrer">site</a></>}
            <div style={s.dim}>{x.positioning}</div>
            {x.strengths?.length > 0 && <div style={s.dim}>+ {x.strengths.join("; ")}</div>}
            {x.weaknesses?.length > 0 && <div style={s.dim}>− {x.weaknesses.join("; ")}</div>}
          </div>
        ))}
      </Section>
    </>
  );
}

function Icp({ r }: { r: IcpReport }) {
  return (
    <>
      <Section title="Winning category (where to play)">
        <div style={s.kv}>{r.winning_category || "—"}</div>
        {r.how_to_target && <div style={s.kv}><b>How to target:</b> {r.how_to_target}</div>}
      </Section>
      <Section title={`Personas (${r.personas.length})`}>
        {r.personas.map((x, i) => (
          <div key={i} style={s.card}>
            <b>{x.role}</b> {x.seniority && <span style={s.badge}>{x.seniority}</span>}
            {x.pains?.length > 0 && <div style={s.dim}>Pains: {x.pains.join("; ")}</div>}
            {x.pitch_angle && <div style={s.kv}>Pitch: {x.pitch_angle}</div>}
          </div>
        ))}
      </Section>
      <Section title={`Segments (${r.segments.length})`}>
        {r.segments.map((x, i) => (
          <div key={i} style={s.card}><b>{x.name}</b> — {x.firmographics}<div style={s.dim}>{x.why}</div></div>
        ))}
      </Section>
    </>
  );
}

function Social({ r }: { r: SocialReport }) {
  const o = r.overall;
  const withSignal = r.platforms.filter((p) => p.post_count > 0);
  return (
    <>
      <Section title="The problem — market signal (not the company)">
        <div style={s.kv}><b>Sentiment:</b> {o.problem_sentiment} ({o.sentiment_score >= 0 ? "+" : ""}{o.sentiment_score})</div>
        {o.major_pain_points?.length > 0 && (
          <div style={s.kv}><b>Major pain-points:</b>
            <ul style={{ margin: "4px 0" }}>{o.major_pain_points.map((x, i) => <li key={i} style={s.dim}>{x}</li>)}</ul>
          </div>)}
        {o.trending_themes?.length > 0 && <div style={s.kv}><b>Trending themes:</b> {o.trending_themes.join(" · ")}</div>}
        {Object.keys(o.competitor_sentiment || {}).length > 0 && (
          <div style={s.kv}><b>Competitor sentiment:</b> {Object.entries(o.competitor_sentiment).map(([k, v]) => `${k}: ${v}`).join(" · ")}</div>)}
      </Section>

      {withSignal.length === 0 && (
        <div style={s.note}>No relevant posts this run — the filter is strict and free scrapers can hit rate limits. Try again in a minute.</div>)}

      {withSignal.map((pi, idx) => (
        <Section key={idx} title={`${pi.platform} — ${pi.post_count} relevant of ${pi.scanned} scanned · ${pi.sentiment}`}>
          {pi.pain_points?.length > 0 && <div style={s.kv}><b>Pains:</b> {pi.pain_points.join(" · ")}</div>}
          {Object.keys(pi.competitor_mentions || {}).length > 0 && (
            <div style={s.dim}>competitors mentioned: {Object.entries(pi.competitor_mentions).map(([k, v]) => `${k} (${v})`).join(", ")}</div>)}
          {pi.posts.map((p, i) => (
            <div key={i} style={s.card}>
              <b>@{p.author || "?"}</b>{" "}
              {p.score > 0 && <span style={s.badge}>▲ {p.score}</span>}{" "}
              {p.num_comments > 0 && <span style={s.badge}>{p.num_comments} comments</span>}{" "}
              {p.tier && <span style={tierStyle(p.tier)}>{p.tier}</span>}{" "}
              {p.signal_type && <span style={s.badge}>{p.signal_type}</span>}{" "}
              {p.sentiment && <span style={s.badge}>{p.sentiment}</span>}
              {p.url && <> · <a href={p.url} target="_blank" rel="noreferrer">open</a></>}
              {p.problem_theme && <div style={s.dim}>pain: {p.problem_theme}</div>}
              <div style={s.dim}>{(p.title || p.text || "").slice(0, 200)}</div>
              {p.recommended_pitch && <div style={s.pitch}>→ {p.recommended_pitch}</div>}
            </div>
          ))}
        </Section>
      ))}
    </>
  );
}

function Events({ r }: { r: EventsReport }) {
  if (r.events.length === 0) return <div style={s.note}>No events found (needs a Tavily key + a recognizable category).</div>;
  return (
    <Section title={`Events (${r.events.length})`}>
      {r.events.map((e, i) => (
        <div key={i} style={s.card}>
          <b>{e.name}</b> {e.platform && <span style={s.badge}>{e.platform}</span>} {e.is_virtual && <span style={s.badge}>virtual</span>}
          {e.url && <> · <a href={e.url} target="_blank" rel="noreferrer">link</a></>}
          <div style={s.dim}>{[e.city, e.starts_at].filter(Boolean).join(" · ")} {e.description}</div>
        </div>
      ))}
    </Section>
  );
}

function Leads({ r, kind }: { r: LeadsReport; kind: string }) {
  if (!r.leads || r.leads.length === 0)
    return <div style={s.note}>No {kind.toLowerCase()} signals found in the last ~3 months for this ICP. Try again, or it may be a thin segment.</div>;
  return (
    <Section title={`${kind} signals — ${r.leads.length} companies (last ~3 months)`}>
      {r.leads.map((L, i) => {
        const site = L.website ? (L.website.startsWith("http") ? L.website : `https://${L.website}`) : "";
        return (
          <div key={i} style={s.lead}>
            <div>
              <b>{L.company}</b>{" "}
              {site && <>· <a href={site} target="_blank" rel="noreferrer">{L.website.replace(/^https?:\/\//, "")}</a></>}
              {L.relevance && <span style={tierStyle(L.relevance)}> {L.relevance}</span>}
            </div>
            <div style={s.dim}>
              {[L.employees, L.location, L.role].filter(Boolean).join(" · ")}
              {(L.employees || L.location || L.role) ? " · " : ""}
              {L.source && <a href={L.source_url} target="_blank" rel="noreferrer">{L.source}</a>}
              {L.signal_date ? ` · ${L.signal_date}` : ""}
            </div>
            <div style={s.kv}>{L.rationale}</div>
          </div>
        );
      })}
    </Section>
  );
}

// ------- Customer list: WhatsApp-style chat threads + a source/facet filter bar ------- //

const SIGNAL_META: Record<string, { label: string; dot: string; bubble: string }> = {
  fundraising: { label: "Funded", dot: "#12b886", bubble: "#e6fcf3" },
  hiring: { label: "Hiring", dot: "#4c6ef5", bubble: "#e7ecff" },
  team: { label: "In-house team", dot: "#ae3ec9", bubble: "#f8ecfd" },
  social: { label: "Social intent", dot: "#f76707", bubble: "#fff1e6" },
};

const RECENCY = [
  { key: "any", label: "Any time", days: 0 },
  { key: "30", label: "Last 30 days", days: 30 },
  { key: "90", label: "Last 90 days", days: 90 },
];

function daysAgo(dateStr: string): number | null {
  if (!dateStr) return null;                 // undated (e.g. team) = current/ongoing
  const t = Date.parse(dateStr);
  if (Number.isNaN(t)) return null;
  return Math.floor((Date.now() - t) / 86400000);
}

function uniq(vals: string[]): string[] {
  return Array.from(new Set(vals.filter(Boolean))).sort();
}

interface Filters {
  sources: Set<string>; industry: string; location: string;
  stage: string; roleOnly: boolean; recency: string;
}

function signalPassesRecency(sig: AccountSignal, recencyKey: string): boolean {
  const r = RECENCY.find((x) => x.key === recencyKey);
  if (!r || r.days === 0) return true;
  const d = daysAgo(sig.date);
  return d === null || d <= r.days;          // undated signals are "current" → always pass
}

function accountMatches(a: Account, f: Filters): boolean {
  if (f.industry && a.industry !== f.industry) return false;
  if (f.location && a.location !== f.location) return false;
  if (f.stage && a.funding_stage !== f.stage) return false;
  if (f.roleOnly && !a.role_present) return false;
  const sigs = a.signals || [];
  // company must have ≥1 signal that satisfies BOTH the source filter and the recency filter
  return sigs.some(
    (sig) =>
      (f.sources.size === 0 || f.sources.has(sig.source)) &&
      signalPassesRecency(sig, f.recency),
  );
}

// reject-reason taxonomy — each becomes an exclusion rule the agents obey next run
const REASON_CATS: [string, string][] = [
  ["agency-vendor", "Agency / vendor (sells the service)"],
  ["too-enterprise", "Too enterprise / large"],
  ["too-early", "Too small / too early"],
  ["wrong-segment", "Wrong segment"],
  ["wrong-geo", "Wrong geography"],
  ["stale", "Stale signal"],
  ["competitor", "Competitor of ours"],
  ["duplicate", "Duplicate"],
  ["not-a-buyer", "Not a real buyer"],
  ["low-intent", "Intent too weak"],
  ["other", "Other (see note)"],
];

interface Label { decision: "approve" | "reject"; category: string; note: string; saved?: boolean; submitting?: boolean }
const acctKey = (a: Account) => (a.company_domain || a.company || "").toLowerCase();
const REASON_LABEL: Record<string, string> = Object.fromEntries(REASON_CATS);

function Customers({ r, url, onRefresh }: { r: CustomerReport; url: string; onRefresh: () => Promise<void> }) {
  const accts = r.accounts || [];
  const [f, setF] = useState<Filters>({
    sources: new Set(), industry: "", location: "", stage: "", roleOnly: false, recency: "any",
  });
  const [labels, setLabels] = useState<Record<string, Label>>({});
  const [rebuilding, setRebuilding] = useState(false);
  const [result, setResult] = useState<null | { exclusion_rules: string[]; suppressed: string[] }>(null);

  const opts = useMemo(() => {
    const sigs = accts.flatMap((a) => a.signals || []);
    return {
      sources: uniq(sigs.map((x) => x.source)),
      industries: uniq(accts.map((a) => a.industry)),
      locations: uniq(accts.map((a) => a.location)),
      stages: uniq(accts.map((a) => a.funding_stage)),
    };
  }, [accts]);

  const shown = useMemo(() => accts.filter((a) => accountMatches(a, f)), [accts, f]);

  if (accts.length === 0)
    return <div style={s.note}>No companies yet — run Hiring, Fundraising &amp; Team signals first (the customer list stacks those into ranked companies).</div>;

  function setLabel(key: string, patch: Label | null) {
    setLabels((prev) => {
      const next = { ...prev };
      if (patch === null) delete next[key];
      else next[key] = patch;
      return next;
    });
  }

  // per-entry submit: POST just this one company, mark it saved. Does NOT re-fetch/reshuffle.
  async function submitLabel(key: string) {
    const l = labels[key];
    const a = accts.find((x) => acctKey(x) === key);
    if (!l || !a) return;
    setLabel(key, { ...l, submitting: true });
    try {
      const item: FeedbackItem = {
        company: a.company, company_domain: a.company_domain, signal_type: "account",
        decision: l.decision, reason_category: l.decision === "reject" ? l.category : "",
        reason_text: l.note,
      };
      const res = await api.leadFeedback(url, [item]);
      setResult({ exclusion_rules: res.exclusion_rules, suppressed: res.suppressed });
      setLabel(key, { ...l, saved: true, submitting: false });
    } catch (e: any) {
      setResult({ exclusion_rules: [`Error: ${e.message ?? e}`], suppressed: [] });
      setLabel(key, { ...l, submitting: false });
    }
  }

  // optional: rebuild the list applying everything learned so far (approved pinned, rejects gone)
  async function rebuild() {
    setRebuilding(true);
    try { await onRefresh(); } finally { setRebuilding(false); }
  }

  function toggleSource(src: string) {
    setF((prev) => {
      const next = new Set(prev.sources);
      next.has(src) ? next.delete(src) : next.add(src);
      return { ...prev, sources: next };
    });
  }
  const drop = (val: string, set: (v: string) => void, all: string[], label: string) => (
    <select value={val} onChange={(e) => set(e.target.value)} style={cs.select}>
      <option value="">{label}</option>
      {all.map((o) => <option key={o} value={o}>{o}</option>)}
    </select>
  );

  const savedN = Object.values(labels).filter((l) => l.saved).length;
  const pendingN = Object.values(labels).filter((l) => !l.saved).length;

  return (
    <Section title={`Customer list — ${shown.length}${shown.length !== accts.length ? ` of ${accts.length}` : ""} companies · label to train the list`}>
      {/* filter bar */}
      <div style={cs.filterBar}>
        {drop(f.industry, (v) => setF({ ...f, industry: v }), opts.industries, "All industries")}
        {drop(f.location, (v) => setF({ ...f, location: v }), opts.locations, "All locations")}
        {drop(f.stage, (v) => setF({ ...f, stage: v }), opts.stages, "Any funding stage")}
        <select value={f.recency} onChange={(e) => setF({ ...f, recency: e.target.value })} style={cs.select}>
          {RECENCY.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
        </select>
        <label style={cs.toggle}>
          <input type="checkbox" checked={f.roleOnly} onChange={(e) => setF({ ...f, roleOnly: e.target.checked })} />
          Role in-house / hiring
        </label>
      </div>
      {/* source chips (the primary filter) */}
      {opts.sources.length > 0 && (
        <div style={cs.sourceRow}>
          <span style={cs.sourceLabel}>Source:</span>
          {opts.sources.map((src) => {
            const on = f.sources.has(src);
            return (
              <button key={src} onClick={() => toggleSource(src)}
                      style={{ ...cs.chip, ...(on ? cs.chipOn : {}) }}>
                {src}
              </button>
            );
          })}
          {f.sources.size > 0 && (
            <button onClick={() => setF({ ...f, sources: new Set() })} style={cs.chipClear}>clear</button>
          )}
        </div>
      )}

      {result && (
        <div style={cs.learned}>
          <b>Saved — learning banked for the next build.</b> Approved companies are pinned; rejected are suppressed.
          {result.exclusion_rules.length > 0 && (
            <div style={cs.learnedRules}>Agents will now exclude: {result.exclusion_rules.map((x, i) => <span key={i} style={cs.ruleChip}>{x}</span>)}</div>
          )}
          {result.suppressed.length > 0 && <div style={s.dim}>Suppressed: {result.suppressed.join(", ")}</div>}
        </div>
      )}

      {shown.length === 0 && <div style={s.note}>No companies match these filters.</div>}

      {/* one chat thread per company */}
      {shown.map((a) => (
        <ChatThread key={acctKey(a)} a={a} f={f}
                    label={labels[acctKey(a)]}
                    onLabel={(p) => setLabel(acctKey(a), p)}
                    onSubmit={() => submitLabel(acctKey(a))} />
      ))}

      {/* fixed session bar — labels save per-entry; rebuild is optional */}
      {(savedN > 0 || pendingN > 0 || rebuilding) && (
        <div style={cs.saveBar}>
          <span>{savedN} saved ✓{pendingN > 0 ? ` · ${pendingN} unsaved` : ""} · learning applies on the next build</span>
          <button onClick={rebuild} disabled={rebuilding || savedN === 0} style={cs.saveBtn}>
            {rebuilding ? "Rebuilding…" : "Rebuild now (optional)"}
          </button>
        </div>
      )}
    </Section>
  );
}

function ChatThread({ a, f, label, onLabel, onSubmit }: {
  a: Account; f: Filters; label?: Label; onLabel: (patch: Label | null) => void; onSubmit: () => void;
}) {
  const initials = (a.company || "?").replace(/[^A-Za-z0-9 ]/g, "").split(/\s+/)
    .slice(0, 2).map((w) => w[0]?.toUpperCase()).join("") || "?";
  const decided = label?.decision;
  const saved = label?.saved;

  // a SAVED rejection collapses in place (dimmed one-liner, still editable) — the list stays stable;
  // it's actually removed only on the next rebuild.
  if (decided === "reject" && saved) {
    return (
      <div style={{ ...cs.thread, ...cs.threadRejected }}>
        <div style={cs.collapsed}>
          <span style={cs.collapsedX}>✕</span>
          <b style={cs.collapsedName}>{a.company || "(company)"}</b>
          <span style={cs.collapsedWhy}>
            rejected{label?.category ? ` · ${REASON_LABEL[label.category] ?? label.category}` : ""}{label?.note ? ` — ${label.note}` : ""}
          </span>
          <button onClick={() => onLabel({ ...label!, saved: false })} style={cs.linkBtn}>Edit</button>
        </div>
      </div>
    );
  }

  // show messages honoring the active source + recency filters, newest first (already sorted)
  const msgs = (a.signals || []).filter(
    (sig) => (f.sources.size === 0 || f.sources.has(sig.source)) && signalPassesRecency(sig, f.recency),
  );
  const threadStyle: React.CSSProperties = {
    ...cs.thread,
    ...(decided === "approve" ? { outline: "2px solid #2f9e44" } : {}),
    ...(decided === "reject" ? { outline: "2px solid #e03131" } : {}),
  };
  return (
    <div style={threadStyle}>
      <div style={cs.header}>
        <div style={cs.avatar}>{initials}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={cs.name}>
            {a.company || "(company)"}
            {a.website && <a href={a.website} target="_blank" rel="noreferrer" style={cs.domain}>{a.company_domain || "site"} ↗</a>}
          </div>
          <div style={cs.facets}>
            {a.industry && <span style={cs.facet}>{a.industry}</span>}
            {a.location && <span style={cs.facet}>📍 {a.location}</span>}
            {a.funding_stage && <span style={cs.facet}>💰 {a.funding_stage}</span>}
            {a.role_present && <span style={cs.facet}>🎯 role in-house/hiring</span>}
          </div>
        </div>
        <span style={stackBadge(a.stack_score)}>
          {a.stack_score >= 2 ? `${a.stack_score} signals stacked` : "1 signal"}
        </span>
      </div>

      <div style={cs.chatBody}>
        {msgs.map((sig, j) => {
          const m = SIGNAL_META[sig.signal_type] || { label: sig.signal_type, dot: "#868e96", bubble: "#f1f3f5" };
          return (
            <div key={j} style={{ ...cs.bubble, background: m.bubble }}>
              <div style={cs.bubbleHead}>
                <span style={{ ...cs.dot, background: m.dot }} />
                <span style={cs.sigLabel}>{m.label}</span>
                {sig.role && <span style={cs.role}>· {sig.role}</span>}
                {sig.relevance && <span style={relevancePill(sig.relevance)}>{sig.relevance}</span>}
              </div>
              <div style={cs.bubbleText}>{sig.text}</div>
              <div style={cs.bubbleFoot}>
                {sig.source_url
                  ? <a href={sig.source_url} target="_blank" rel="noreferrer" style={cs.srcLink}>{sig.source} ↗</a>
                  : <span style={cs.srcLink}>{sig.source}</span>}
                <span style={cs.time}>{sig.date || "current"}</span>
              </div>
            </div>
          );
        })}
      </div>

      {/* label controls — per-entry Save (the eval loop) */}
      <div style={cs.actions}>
        <button disabled={saved} onClick={() => onLabel(decided === "approve" ? null : { decision: "approve", category: "", note: label?.note ?? "" })}
                style={{ ...cs.actBtn, ...(decided === "approve" ? cs.actApproveOn : {}), ...(saved ? cs.actDisabled : {}) }}>
          ✓ Approve
        </button>
        <button disabled={saved} onClick={() => onLabel(decided === "reject" ? null : { decision: "reject", category: label?.category ?? "agency-vendor", note: label?.note ?? "" })}
                style={{ ...cs.actBtn, ...(decided === "reject" ? cs.actRejectOn : {}), ...(saved ? cs.actDisabled : {}) }}>
          ✕ Reject
        </button>
        {decided && !saved && (
          <button onClick={onSubmit} disabled={label?.submitting} style={cs.saveEntryBtn}>
            {label?.submitting ? "Saving…" : "Save"}
          </button>
        )}
        {saved && <span style={cs.savedBadge}>✓ Saved</span>}
        {saved && <button onClick={() => onLabel({ ...label!, saved: false })} style={cs.linkBtn}>Edit</button>}
        {decided && !saved && <span style={cs.sourceLabel}>{decided === "approve" ? "add a note (optional), then Save" : "pick a reason + explain, then Save"}</span>}
      </div>
      {/* reveal panel (opens on click, hidden once saved): reason dropdown for rejects + free-text box */}
      {decided && !saved && (
        <div style={cs.reasonPanel}>
          {decided === "reject" && (
            <div style={cs.reasonRow}>
              <span style={cs.reasonLbl}>Reason</span>
              <select value={label?.category ?? ""} onChange={(e) => onLabel({ decision: "reject", category: e.target.value, note: label?.note ?? "" })} style={cs.select}>
                {REASON_CATS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </div>
          )}
          <textarea
            placeholder={decided === "reject"
              ? "Why did you reject this? The more detail, the better the agent learns (e.g. 'agency that resells influencer services', 'enterprise — 5000+ employees', 'B2B not consumer')."
              : "Optional: what makes this a great fit? (e.g. 'exactly our ICP — funded DTC beauty brand') — trains the agent on what you like."}
            value={label?.note ?? ""}
            onChange={(e) => onLabel({ decision: decided, category: label?.category ?? "", note: e.target.value })}
            style={cs.noteArea} />
        </div>
      )}
    </div>
  );
}

function stackBadge(score: number): React.CSSProperties {
  if (score >= 3) return { ...cs.stack, background: "#ffe3e3", color: "#c92a2a" };
  if (score === 2) return { ...cs.stack, background: "#fff3bf", color: "#a07e00" };
  return { ...cs.stack, background: "#e9ecef", color: "#495057" };
}
function relevancePill(rel: string): React.CSSProperties {
  const t = rel.toLowerCase();
  const base = { ...cs.relPill } as React.CSSProperties;
  if (t.includes("high")) return { ...base, background: "#d3f9d8", color: "#2b8a3e" };
  if (t.includes("med")) return { ...base, background: "#fff3bf", color: "#a07e00" };
  return { ...base, background: "#f1f3f5", color: "#868e96" };
}

function tierStyle(tier: string): React.CSSProperties {
  const t = tier.toLowerCase();
  if (t.includes("1") || t.includes("high")) return { ...s.badge, background: "#ffe3e3", color: "#c92a2a" };
  if (t.includes("2") || t.includes("medium")) return { ...s.badge, background: "#fff3bf", color: "#a07e00" };
  return s.badge;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return <div style={s.section}><h2 style={s.h2}>{title}</h2>{children}</div>;
}

const s: Record<string, React.CSSProperties> = {
  page: { fontFamily: "system-ui, sans-serif", maxWidth: 920, margin: "40px auto", padding: 16, color: "#111" },
  sub: { color: "#555", marginTop: 0 },
  row: { display: "flex", gap: 8, margin: "16px 0" },
  input: { flex: 1, padding: "10px 12px", fontSize: 16, border: "1px solid #ccc", borderRadius: 8 },
  button: { padding: "10px 18px", fontSize: 16, border: "none", borderRadius: 8, background: "#111", color: "#fff", cursor: "pointer" },
  tabs: { display: "flex", gap: 6, flexWrap: "wrap", margin: "8px 0 4px" },
  tab: { padding: "7px 12px", fontSize: 13, border: "1px solid #ddd", borderRadius: 8, background: "#fafafa", color: "#333", cursor: "pointer" },
  tabActive: { background: "#111", color: "#fff", borderColor: "#111" },
  note: { background: "#eef6ff", borderRadius: 8, padding: "8px 12px", fontSize: 13, margin: "8px 0" },
  error: { color: "#b00020", margin: "8px 0", whiteSpace: "pre-wrap" },
  section: { border: "1px solid #eee", borderRadius: 12, padding: "12px 16px", margin: "14px 0" },
  h2: { fontSize: 16, margin: "0 0 8px" },
  card: { borderLeft: "3px solid #ddd", padding: "4px 10px", margin: "8px 0", fontSize: 14 },
  lead: { border: "1px solid #eee", borderRadius: 10, padding: "10px 12px", margin: "10px 0", fontSize: 14 },
  pitch: { background: "#f6f8fa", borderRadius: 8, padding: "8px 10px", marginTop: 6, fontSize: 13.5, color: "#222" },
  kv: { fontSize: 14, margin: "4px 0" },
  dim: { color: "#777", fontSize: 13, margin: "2px 0" },
  badge: { fontSize: 11, background: "#eee", color: "#555", padding: "1px 6px", borderRadius: 6 },
};

// Customer-list chat styles (WhatsApp-like)
const cs: Record<string, React.CSSProperties> = {
  filterBar: { display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", margin: "0 0 8px" },
  select: { padding: "6px 8px", fontSize: 13, border: "1px solid #ced4da", borderRadius: 8, background: "#fff", color: "#333" },
  toggle: { display: "flex", alignItems: "center", gap: 5, fontSize: 13, color: "#495057" },
  sourceRow: { display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center", margin: "0 0 14px" },
  sourceLabel: { fontSize: 12, color: "#868e96", marginRight: 2 },
  chip: { fontSize: 12, padding: "3px 10px", borderRadius: 999, border: "1px solid #ced4da", background: "#fff", color: "#495057", cursor: "pointer" },
  chipOn: { background: "#111", color: "#fff", borderColor: "#111" },
  chipClear: { fontSize: 12, padding: "3px 8px", border: "none", background: "none", color: "#c92a2a", cursor: "pointer", textDecoration: "underline" },

  thread: { border: "1px solid #e9ecef", borderRadius: 14, overflow: "hidden", margin: "12px 0", boxShadow: "0 1px 3px rgba(0,0,0,0.04)" },
  header: { display: "flex", alignItems: "center", gap: 10, padding: "10px 12px", background: "#075e54", color: "#fff" },
  avatar: { width: 38, height: 38, borderRadius: "50%", background: "#128c7e", color: "#fff", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14, fontWeight: 700, flexShrink: 0 },
  name: { fontSize: 15, fontWeight: 700, display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" },
  domain: { fontSize: 12, fontWeight: 400, color: "#b2dfdb", textDecoration: "none" },
  facets: { display: "flex", gap: 6, flexWrap: "wrap", marginTop: 3 },
  facet: { fontSize: 11, color: "#d7f0ec" },
  stack: { fontSize: 11, fontWeight: 700, padding: "3px 8px", borderRadius: 999, whiteSpace: "nowrap", flexShrink: 0 },

  chatBody: { padding: "12px 12px 14px", background: "#ece5dd", display: "flex", flexDirection: "column", gap: 8 },
  bubble: { alignSelf: "flex-start", maxWidth: "85%", borderRadius: "0 10px 10px 10px", padding: "7px 10px 5px", boxShadow: "0 1px 1px rgba(0,0,0,0.08)" },
  bubbleHead: { display: "flex", alignItems: "center", gap: 6, marginBottom: 3 },
  dot: { width: 8, height: 8, borderRadius: "50%", flexShrink: 0 },
  sigLabel: { fontSize: 11.5, fontWeight: 700, color: "#333" },
  role: { fontSize: 11.5, color: "#555" },
  relPill: { fontSize: 10, fontWeight: 700, padding: "0 6px", borderRadius: 999, marginLeft: "auto" },
  bubbleText: { fontSize: 13.5, color: "#111", lineHeight: 1.35 },
  bubbleFoot: { display: "flex", alignItems: "center", gap: 8, marginTop: 4 },
  srcLink: { fontSize: 11.5, color: "#0a7", textDecoration: "none", fontWeight: 600 },
  time: { fontSize: 10.5, color: "#667", marginLeft: "auto" },

  // label controls (the eval loop)
  actions: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", padding: "8px 12px", background: "#f8f9fa", borderTop: "1px solid #eee" },
  actBtn: { fontSize: 12.5, padding: "4px 12px", borderRadius: 8, border: "1px solid #ced4da", background: "#fff", color: "#495057", cursor: "pointer", fontWeight: 600 },
  actApproveOn: { background: "#2f9e44", borderColor: "#2f9e44", color: "#fff" },
  actRejectOn: { background: "#e03131", borderColor: "#e03131", color: "#fff" },
  actDisabled: { opacity: 0.55, cursor: "default" },
  saveEntryBtn: { fontSize: 12.5, fontWeight: 700, padding: "5px 16px", border: "none", borderRadius: 8, background: "#12b886", color: "#fff", cursor: "pointer" },
  savedBadge: { fontSize: 12, fontWeight: 700, color: "#2f9e44" },
  linkBtn: { fontSize: 12, border: "none", background: "none", color: "#4c6ef5", cursor: "pointer", textDecoration: "underline", padding: 0, marginLeft: "auto" },
  threadRejected: { opacity: 0.6 },
  collapsed: { display: "flex", alignItems: "center", gap: 9, padding: "9px 13px", flexWrap: "wrap" },
  collapsedX: { color: "#e03131", fontWeight: 700 },
  collapsedName: { textDecoration: "line-through", color: "#495057" },
  collapsedWhy: { fontSize: 12.5, color: "#868e96" },
  reasonPanel: { display: "flex", flexDirection: "column", gap: 8, padding: "11px 12px 13px", background: "#f8f9fa", borderTop: "1px dashed #e9ecef" },
  reasonRow: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" },
  reasonLbl: { fontSize: 10.5, letterSpacing: "0.07em", textTransform: "uppercase", color: "#868e96", fontWeight: 700 },
  noteArea: { width: "100%", minHeight: 52, resize: "vertical", padding: "8px 10px", fontSize: 12.5, lineHeight: 1.4, border: "1px solid #ced4da", borderRadius: 8, fontFamily: "inherit" },
  saveBar: { position: "sticky", bottom: 0, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, marginTop: 12, padding: "10px 14px", background: "#111", color: "#fff", borderRadius: 10, boxShadow: "0 4px 14px rgba(0,0,0,0.25)", fontSize: 13.5 },
  saveBtn: { fontSize: 13.5, fontWeight: 700, padding: "7px 16px", border: "none", borderRadius: 8, background: "#12b886", color: "#fff", cursor: "pointer" },
  learned: { background: "#ebfbee", border: "1px solid #b2f2bb", borderRadius: 10, padding: "10px 12px", margin: "6px 0 12px", fontSize: 13 },
  learnedRules: { marginTop: 6, display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" },
  ruleChip: { fontSize: 11.5, background: "#fff", border: "1px solid #ced4da", borderRadius: 999, padding: "2px 9px", color: "#495057" },
};
