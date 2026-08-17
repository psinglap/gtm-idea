const BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export interface Competitor {
  name: string;
  url?: string | null;
  positioning: string;
  how_they_differ: string;
  verified: boolean;
  tier: string;
  size_note: string;
  target_customers: string;
  strengths: string[];
  weaknesses: string[];
}

export interface CIReport {
  id: string;
  url: string;
  depth: string;
  source: string;
  profile: {
    name: string; one_liner: string; what_they_do: string; category: string;
    subcategory: string; business_model: string; pricing_model: string;
    value_proposition: string; stage: string;
  };
  competitive: {
    direct_competitors: Competitor[];
    indirect_alternatives: Competitor[];
    category_landscape: string; market_crowdedness: string; crowdedness_score: number;
    our_unique_advantage: string; our_moat: string; whitespace: string;
    competitor_targets_vs_ours: string; pricing_landscape: string; positioning: string;
  };
}

export interface Post {
  id: string; platform: string; author: string; author_handle: string;
  title: string; text: string; url: string; posted_at: string | null;
  score: number; num_comments: number; matched_query: string;
  sentiment: string; problem_theme: string; signal_type: string;
  recommended_pitch?: string; tier?: string;
}

export interface PlatformInsight {
  platform: string; post_count: number; scanned: number;
  pain_points: string[]; themes: string[];
  competitor_mentions: Record<string, number>;
  competitor_sentiment: Record<string, string>;
  sentiment: string; summary: string; posts: Post[];
}

export interface SocialReport {
  subject_domain: string;
  queries: string[];
  platforms: PlatformInsight[];
  overall: {
    problem_sentiment: string; sentiment_score: number;
    major_pain_points: string[]; trending_themes: string[];
    competitor_sentiment: Record<string, string>; summary: string;
  };
}

export interface Lead {
  person: string; person_handle: string; company: string; source: string;
  source_url: string; evidence: string; tier: string; intent: number;
  recommended_pitch: string;
}
export interface AccountSignal {
  signal_type: string;   // 'fundraising' | 'hiring' | 'team'
  source: string;        // 'techcrunch.com' | 'Greenhouse' | 'LinkedIn'
  source_url: string;
  date: string;          // '' = current/ongoing
  text: string;
  role: string;
  relevance: string;
}
export interface Account {
  company: string; company_domain: string; website: string; industry: string;
  location: string; funding_stage: string; role_present: boolean;
  social_count: number; hiring_count: number; fundraising_count: number; team_count: number;
  signal_types: string[]; stack_score: number; relevance: number;
  latest_signal_date: string; signals: AccountSignal[];
  evidence: string[]; sources: string[];
}
export interface CustomerReport { subject_domain: string; accounts: Account[]; leads?: Lead[] }

export interface IcpReport {
  personas: { role: string; seniority: string; pains: string[]; triggers: string[]; pitch_angle: string }[];
  segments: { name: string; firmographics: string; why: string }[];
  winning_category: string; how_to_target: string; summary: string;
}

export interface EventItem {
  name: string; url: string; platform: string; city: string;
  is_virtual: boolean; description: string; starts_at: string | null;
}
export interface EventsReport { events: EventItem[] }

export interface CompanyLead {
  company: string; website: string; employees: string; location: string;
  source: string; source_url: string; signal_type: string; role: string;
  rationale: string; relevance: string; signal_date: string;
}
export interface LeadsReport { subject_domain: string; signal_type: string; leads: CompanyLead[] }

export interface FeedbackItem {
  company: string; company_domain?: string; signal_type?: string;
  decision: "approve" | "reject"; reason_category?: string; reason_text?: string;
}
export interface FeedbackResult {
  subject_domain: string; saved: number; approved: number; rejected: number;
  exclusion_rules: string[]; suppressed: string[];
}

// --- Event outreach (Luma -> LinkedIn -> Apollo -> Gmail) --------------------
export interface ConnectionRow {
  provider: "luma" | "linkedin" | "apollo" | "gmail" | "gmail_history";
  status: "connected" | "disconnected" | "stale" | "expired" | "error" | "needs_reconnect";
  account_label: string;
  has_secret: boolean;
  via_claude?: boolean;
  needs_reconnect?: boolean;
  missing_scopes?: string[];
  kind: "session" | "credential";
  hint: string;
  last_error?: string;
}
export interface Readiness {
  can_scan_events: boolean; can_read_linkedin: boolean; can_find_emails: boolean;
  can_deliver: boolean; secrets_configured: boolean; google_configured: boolean;
  unattended_blocked_by?: string[];
  history_mailboxes?: string[];
}
export interface OutreachEvent {
  event_id: string; title: string; url: string; short_name: string;
  short_name_source?: string; starts_at: string;
  approval_status: string; guest_count: number; scanned: boolean; blocked_reason: string;
}
export interface OutreachMessageRow {
  id: string; email: string; subject: string; status: string; skip_reason: string;
  gmail_draft_id: string; created_at: string; event_id: string;
}
export interface OpenQuestion { key: string; label: string; type: string; options: string[] }
/** `options` is non-empty for Luma's click-only dropdowns, where nothing off the list is a
 *  valid answer — the UI renders a picker rather than a text box for those. */
export interface MappedAnswer { key: string; label: string; answer: string; options?: string[] }

export interface OutreachSummary {
  queue: Record<string, number>;
  events: OutreachEvent[];
  messages: OutreachMessageRow[];
  connections: ConnectionRow[];
}

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

/* COLD START.
 *
 * The API sleeps after ~15 minutes idle on a free host and takes 30-60s to wake. Opening this
 * app is exactly the moment that happens, so a wake-up is the normal case and must not surface
 * as "Error: Failed to fetch" — which reads as a broken deployment rather than a pause.
 *
 * Retries only what is genuinely retryable: a network error (a sleeping instance refuses the
 * connection) or a 502/503/504 from the host's proxy while it boots. A 401 or a 404 is a real
 * answer from a running server and fails immediately; retrying it would only delay the truth.
 */
const COLD_START_WAITS = [2000, 5000, 10000, 20000, 30000];   // ~67s of patience
const RETRYABLE = new Set([408, 429, 502, 503, 504]);

async function fetchWaking(input: string, init?: RequestInit): Promise<Response> {
  let last = "";
  for (let attempt = 0; attempt <= COLD_START_WAITS.length; attempt++) {
    try {
      const res = await fetch(input, init);
      if (res.ok || !RETRYABLE.has(res.status)) return res;
      last = String(res.status);
    } catch (e) {
      last = e instanceof Error ? e.message : String(e);
    }
    if (attempt < COLD_START_WAITS.length) {
      await new Promise((r) => setTimeout(r, COLD_START_WAITS[attempt]));
    }
  }
  throw new Error(`the server did not wake up (${last}) — it may be starting, try again shortly`);
}

function runAgent<T>(name: string, payload: Record<string, unknown>): Promise<T> {
  return fetchWaking(`${BASE}/agents/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-API-Key": import.meta.env.VITE_API_KEY ?? "" },
    body: JSON.stringify(payload),
  }).then(j<T>);
}

// The workspace token is this app's only credential — there is no sign-in. It is minted once
// per company URL and kept in localStorage; the extension holds the same token.
const TOKEN_KEY = "wg_workspace_token";
export const workspaceToken = {
  get: () => localStorage.getItem(TOKEN_KEY) ?? "",
  set: (t: string) => localStorage.setItem(TOKEN_KEY, t),
};

function outreachUrl(path: string, params: Record<string, string> = {}) {
  const q = new URLSearchParams({ token: workspaceToken.get(), ...params });
  return `${BASE}${path}?${q}`;
}

function outreachPost<T>(path: string, body: Record<string, unknown>): Promise<T> {
  return fetchWaking(outreachUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(j<T>);
}

/** What the browser half is doing. It runs in the user's own Chrome, so without this the UI
 *  could show the server's queue but never say whether anything was filling it. */
export interface WorkerStatus {
  running: boolean; stage: string; reason: string;
  last_run_at: string; last_error: string; received_at?: string;
  next_due_in_min?: number;
  counts: { registered?: number; skipped?: number; scanned?: number;
            guests?: number; profiles?: number };
  failures?: string[];
}

export interface EventsStatus {
  upcoming: Record<string, number>; upcoming_total: number;
  registered_by_day: { date: string; count: number }[];
  scanned_by_day: { date: string; count: number; guests: number }[];
  registered_all_time: number;
  next_up: { title: string; url: string; starts_at: string; guests: number }[];
}

export interface FunnelStage {
  label: string; on: number;
  /** What this stage counts. Bars are scaled within a unit, never across two. */
  unit?: string;
  /** What happens to these next — for stages that feed something rather than ending. */
  note?: string;
  /** People who stop here permanently. */
  out: { n: number; why: string; examples?: string[] }[];
  /** People who have not got here YET — a backlog, not a loss. */
  queue?: { n: number; why: string; next: string }[];
}

export interface RunRow {
  slot: string; state: string; error?: string; at?: string;
  registered?: number; guest_lists?: number; guests_added?: number;
  events_approved?: number; events_pending?: number;
  apollo?: number; reused?: number; no_match?: number; unverified?: number;
  emails_found?: number; judged?: number; targets?: number;
  sent?: number; skipped?: number; bounced?: number; ready_after?: number;
  skip_reasons?: Record<string, number>;
}

export interface ScheduleStatus {
  enabled: boolean; runs_at_utc: string[];
  alive: { at?: string; next_slot?: string };
  last: { slot?: string; state?: string; delivered?: number; error?: string; at?: string };
  history: RunRow[];
}

export interface OutreachSettings {
  mode: "draft" | "send";
  event_horizon_days: number; event_max_age_days: number; scan_lookback_days: number;
  daily_cap: number; hourly_cap: number; send_to_catchall: boolean;
}

/** One line per thing the browser worker did, oldest first. */
export interface WorkerLogLine { seq: number; at: string; text: string }

export const outreach = {
  workerStatus: () => fetch(outreachUrl("/outreach/worker-status")).then(j<WorkerStatus>),

  funnel: () => fetch(outreachUrl("/outreach/funnel")).then(j<{ stages: FunnelStage[] }>),

  eventsFunnel: () =>
    fetch(outreachUrl("/outreach/events-funnel"))
      .then(j<{ upcoming: FunnelStage[]; past: FunnelStage[] }>),

  eventsStatus: () => fetch(outreachUrl("/outreach/events-status")).then(j<EventsStatus>),

  /** When the loop last ran by itself, and whether it worked. */
  schedule: () => fetch(outreachUrl("/outreach/schedule")).then(j<ScheduleStatus>),

  /** What this deployment is configured to do, read from the running process. */
  settings: () => fetch(outreachUrl("/outreach/settings")).then(j<OutreachSettings>),

  workerLog: () =>
    fetch(outreachUrl("/outreach/worker-log")).then(j<{ lines: WorkerLogLine[] }>),

  /** Kicks the server half immediately and queues the browser half for its next tick.
   *  Distinct from `runNow`, which runs only the server agent for one workspace. */
  runEverything: () =>
    fetch(outreachUrl("/outreach/run-now"), { method: "POST" })
      .then(j<{ ok: boolean; server_half: boolean }>),

  /** Idempotent: the same company URL always returns the same token. */
  pair: (url: string) =>
    fetch(`${BASE}/workspace`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    }).then(j<{ company_id: string; domain: string; token: string }>),

  connections: () =>
    fetch(outreachUrl("/connections")).then(j<{ connections: ConnectionRow[]; readiness: Readiness }>),

  /** role=send is the account that sends; role=history is an extra mailbox searched only for
   *  prior conversations, for when your history lives somewhere other than where you send. */
  googleAuthUrl: (role: "send" | "history" = "send", account = "") =>
    fetch(outreachUrl("/connect/google", { role, account }))
      .then(j<{ auth_url: string; account: string; role: string }>),

  connectApollo: (api_key: string) => outreachPost<ConnectionRow>("/connect/apollo", { api_key }),

  /** Use the Apollo/Gmail connector already authenticated in the user's Claude session. */
  linkViaClaude: (provider: string, account_label = "") =>
    outreachPost<ConnectionRow>("/connect/via-claude", { provider, account_label }),

  disconnect: (provider: string) =>
    fetch(outreachUrl(`/connections/${provider}`), { method: "DELETE" }).then(j<{ removed: boolean }>),

  summary: () => fetch(outreachUrl("/outreach/summary")).then(j<OutreachSummary>),

  /** Registration questions the last run could not answer. */
  openQuestions: () =>
    fetch(outreachUrl("/outreach/questions")).then(
      j<{ open_questions: OpenQuestion[]; answers: Record<string, string> }>),

  /** Map one free-text reply onto the questions. Writes nothing — you confirm first. */
  parseAnswers: (reply: string) =>
    outreachPost<{ mapped: MappedAnswer[]; matched: number; unmatched: string[] }>(
      "/outreach/parse-answers", { reply }),

  /** Save the confirmed mapping. Reused for every future event asking the same thing. */
  answer: (answers: Record<string, string>) =>
    outreachPost<{ saved: number; still_open: number }>("/outreach/answers", { answers }),

  /** The email you write once. {first_name} {event_name} {event_short} {when} get filled in. */
  getTemplate: () =>
    fetch(outreachUrl("/outreach/template")).then(
      j<{ subject: string; body: string; fields: string[]; unknown_fields: string[] }>),

  saveTemplate: (subject: string, body: string) =>
    outreachPost<{ subject: string; body: string }>("/outreach/template", { subject, body }),

  setShortName: (event_id: string, short_name: string) =>
    outreachPost<{ short_name: string }>("/outreach/event-short-name", { event_id, short_name }),

  addDoNotContact: (values: string[], reason = "") =>
    outreachPost<{ added: number }>("/outreach/do-not-contact", { values, reason }),

  /** dry_run renders every message and touches nothing — the safe way to read the copy. */
  preview: (url: string) =>
    runAgent<{ previews: { to: string; subject: string; body: string }[]; skip_reasons: Record<string, number> }>(
      "outreach_send", { url, dry_run: true, limit: 25 }),

  runNow: (url: string, mode: "draft" | "send") =>
    runAgent<Record<string, unknown>>("outreach_daily", { url, mode }),
};

export const api = {
  competitiveIntelligence: (url: string, depth = "deep") =>
    runAgent<CIReport>("competitive_intelligence", { url, depth }),
  icp: (url: string) => runAgent<IcpReport>("icp_winning_category", { url }),
  social: (url: string) => runAgent<SocialReport>("social_listening", { url, since_days: 90, limit: 50 }),
  events: (url: string) => runAgent<EventsReport>("events", { url }),
  // default = serve the ranked list fast from the normalized tables; rebuild = recompute from the
  // signal corpus (applies everything learned so far) and refresh the stored list.
  customerList: (url: string, rebuild = false) =>
    runAgent<CustomerReport>("customer_list", { url, limit: 60, rebuild }),
  hiringLeads: (url: string) => runAgent<LeadsReport>("hiring_leads", { url, limit: 25 }),
  fundraisingLeads: (url: string) => runAgent<LeadsReport>("fundraising_leads", { url, limit: 25 }),
  leadFeedback: (url: string, items: FeedbackItem[]) =>
    runAgent<FeedbackResult>("lead_feedback", { url, items }),
};
