from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Company profile (the subject of the CI report)                              #
# --------------------------------------------------------------------------- #


class CompanyProfile(BaseModel):
    """Structured understanding of the company being analyzed (from its URL)."""

    name: str = ""
    one_liner: str = ""
    what_they_do: str = ""
    category: str = ""
    subcategory: str = ""
    product_capabilities: List[str] = Field(default_factory=list)
    business_model: str = ""  # B2B SaaS / D2C / marketplace / ...
    pricing_model: str = ""
    value_proposition: str = ""
    differentiation: str = ""
    stage: str = ""  # inferred: pre-seed/seed/Series A/growth
    stage_evidence: str = ""
    geography: str = ""


# --------------------------------------------------------------------------- #
# Competitive analysis                                                        #
# --------------------------------------------------------------------------- #


class Competitor(BaseModel):
    name: str
    url: Optional[str] = None
    positioning: str = ""
    how_they_differ: str = ""
    verified: bool = False  # confirmed via web search
    # relative size vs the analyzed company: "enterprise incumbent" | "funded scale-up"
    #                                         | "peer / similar-stage" | "adjacent"
    tier: str = ""
    size_note: str = ""  # funding/stage/scale if known
    target_customers: str = ""  # who THEY primarily sell to
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)


class CompetitiveAnalysis(BaseModel):
    direct_competitors: List[Competitor] = Field(default_factory=list)
    indirect_alternatives: List[Competitor] = Field(default_factory=list)
    category_landscape: str = ""
    market_crowdedness: str = ""        # narrative on how crowded/saturated the space is
    crowdedness_score: int = 0          # 1 (blue ocean) .. 5 (saturated)
    our_unique_advantage: str = ""      # what the analyzed company does better / differently
    our_moat: str = ""                  # defensible moat — what they can own that others can't copy
    whitespace: str = ""                # underserved segments competitors miss
    competitor_targets_vs_ours: str = ""  # who competitors target vs what this company should target
    pricing_landscape: str = ""         # pricing comparison + this company's pricing advantage
    positioning: str = ""               # where this company lands in the competitive landscape


# --------------------------------------------------------------------------- #
# Deep CI: per-competitor dossiers + strategic frameworks (depth='deep')       #
# --------------------------------------------------------------------------- #


class CompetitorDossier(BaseModel):
    """Deep dive on ONE competitor (parallel fan-out in deep mode)."""

    name: str = ""
    url: Optional[str] = None
    summary: str = ""
    pricing: str = ""            # pricing teardown if inferable
    gtm: str = ""                # go-to-market motion
    target_customers: str = ""
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    recent_news: List[str] = Field(default_factory=list)  # funding/launches/moves
    sentiment_summary: str = ""  # how the market talks about them


class Frameworks(BaseModel):
    """Strategic frameworks over the landscape (deep mode)."""

    five_forces: dict = Field(default_factory=dict)  # rivalry/new_entrants/substitutes/buyer/supplier power
    moats: List[str] = Field(default_factory=list)
    positioning_map: str = ""
    where_to_play: str = ""
    how_to_win: str = ""


# --------------------------------------------------------------------------- #
# ICP + winning category                                                       #
# --------------------------------------------------------------------------- #


class IcpPersona(BaseModel):
    role: str = ""
    seniority: str = ""
    pains: List[str] = Field(default_factory=list)
    triggers: List[str] = Field(default_factory=list)  # buying triggers/signals to watch
    pitch_angle: str = ""        # the message that lands for THIS persona


class IcpSegment(BaseModel):
    name: str = ""
    firmographics: str = ""      # stage/size/industry of the best-fit accounts
    why: str = ""


class IcpAnalysis(BaseModel):
    personas: List[IcpPersona] = Field(default_factory=list)
    segments: List[IcpSegment] = Field(default_factory=list)
    winning_category: str = ""   # the category/segment where this company can WIN (where-to-play)
    how_to_target: str = ""
    summary: str = ""


# --------------------------------------------------------------------------- #
# The CI report (the product output, persisted)                              #
# --------------------------------------------------------------------------- #


class CompetitiveIntelligenceReport(BaseModel):
    """Standalone CI report. depth='quick' = single-pass analysis;
    'deep' adds per-competitor dossiers + frameworks + ICP/winning-category."""

    id: str = Field(default_factory=lambda: new_id("ci"))
    url: str
    depth: str = "quick"  # "quick" | "deep"
    profile: CompanyProfile = Field(default_factory=CompanyProfile)
    competitive: CompetitiveAnalysis = Field(default_factory=CompetitiveAnalysis)
    dossiers: List[CompetitorDossier] = Field(default_factory=list)   # deep mode
    frameworks: Optional[Frameworks] = None                           # deep mode
    icp: Optional[IcpAnalysis] = None                                 # deep mode
    source: str = "heuristic"  # provider name | "heuristic"
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Signals layer: scraped posts, derived signals, events, customer leads        #
# --------------------------------------------------------------------------- #


class Post(BaseModel):
    """One real scraped post/comment (any platform). Drives live feeds + lead sourcing."""

    id: str = Field(default_factory=lambda: new_id("post"))
    subject_domain: str = ""     # the analyzed company this run is for ('serro.ai')
    platform: str = ""           # hackernews|reddit|producthunt|news|twitter|linkedin|events
    external_id: str = ""        # platform's own id (dedup key with platform)
    author: str = ""
    author_handle: str = ""
    title: str = ""
    text: str = ""
    url: str = ""
    posted_at: Optional[datetime] = None
    score: int = 0
    num_comments: int = 0
    matched_query: str = ""
    relevance: float = 0.0       # 0..1, filled by the relevance/signal classifier
    sentiment: str = ""          # positive|negative|neutral about the problem
    problem_theme: str = ""      # the pain-point/topic this post is about
    signal_type: str = ""        # seeking_solution|complaining|comparing|asking_advice|hiring|sharing
    competitors_mentioned: List[str] = Field(default_factory=list)
    industry: str = ""           # tag for the shared corpus
    embedding: List[float] = Field(default_factory=list)  # for semantic retrieval (shared corpus)
    recommended_pitch: str = ""  # per-customer reply (display only; NOT saved to the shared corpus)
    tier: str = ""               # 'tier 1'|'tier 2'|'tier 3' (prospect strength; display only)
    raw: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class Signal(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sig"))
    subject_domain: str = ""
    source: str = ""             # platform/source
    type: str = ""               # problem_discussion|competitor_mention|hiring_intent|event_attendance|funding|buying_intent
    strength: float = 0.0
    recency: Optional[datetime] = None
    entity_name: str = ""        # person/company the signal is about
    entity_domain: str = ""
    evidence: str = ""
    url: str = ""
    post_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class Event(BaseModel):
    id: str = Field(default_factory=lambda: new_id("evt"))
    subject_domain: str = ""
    name: str = ""
    url: str = ""
    platform: str = ""           # luma|meetup|eventbrite|conference
    starts_at: Optional[datetime] = None
    city: str = ""
    is_virtual: bool = False
    description: str = ""
    attendees: List[str] = Field(default_factory=list)  # names/handles where public
    raw: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class Contact(BaseModel):
    """A real DECISION-MAKER / champion at a target company, with reach-out details. Enriched by the
    contact waterfall (free LinkedIn discovery + email inference now; paid providers pluggable later)."""

    id: str = Field(default_factory=lambda: new_id("contact"))
    subject_domain: str = ""              # the customer whose list this contact belongs to
    company: str = ""
    company_domain: str = ""              # ties the contact to its Account
    person: str = ""                      # full name
    title: str = ""
    seniority: str = ""                   # 'buyer' (Head/VP/Director) | 'champion' (Manager/IC)
    role_match: str = ""                  # which ICP/trigger role this person matched
    is_decision_maker: bool = False
    linkedin_url: str = ""
    email: str = ""
    email_status: str = "unknown"         # 'verified' | 'guessed' | 'unknown'
    email_confidence: float = 0.0         # 0..1
    phone: str = ""
    location: str = ""
    provider: str = ""                    # which provider produced the record (e.g. 'free_infer','apollo')
    source: str = ""                      # human source label (e.g. 'LinkedIn')
    source_url: str = ""                  # grounding link (the profile)
    created_at: datetime = Field(default_factory=utcnow)


class CustomerLead(BaseModel):
    """THE priority output — a person/company to reach out to, with a pitch."""

    id: str = Field(default_factory=lambda: new_id("lead"))
    subject_domain: str = ""
    person: str = ""
    person_handle: str = ""
    company: str = ""
    company_domain: str = ""
    source: str = ""             # 'post'|'signal'|'event'
    source_url: str = ""
    evidence: str = ""           # the triggering quote/post
    signal_types: List[str] = Field(default_factory=list)
    fit: float = 0.0
    intent: float = 0.0
    tier: str = ""               # 'tier 1'|'tier 2'|'tier 3'
    recommended_pitch: str = ""
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Agent report outputs (social listening, events, customer list)               #
# --------------------------------------------------------------------------- #


class PlatformInsight(BaseModel):
    """Per-platform signals (we process each platform one-by-one)."""

    platform: str = ""
    post_count: int = 0                                       # relevant posts kept
    scanned: int = 0                                          # posts scanned before filtering
    pain_points: List[str] = Field(default_factory=list)      # what people here struggle with
    themes: List[str] = Field(default_factory=list)
    competitor_mentions: dict = Field(default_factory=dict)   # competitor -> count
    competitor_sentiment: dict = Field(default_factory=dict)  # competitor -> how they talk about it
    sentiment: str = ""                                       # problem sentiment on this platform
    summary: str = ""
    posts: List[Post] = Field(default_factory=list)           # the RELEVANT posts only


class SocialInsights(BaseModel):
    """Overall signals about THE PROBLEM (not the URL) across all platforms."""

    problem_sentiment: str = ""                       # how the market feels about the problem
    sentiment_score: float = 0.0                      # -1 .. +1
    major_pain_points: List[str] = Field(default_factory=list)
    trending_themes: List[str] = Field(default_factory=list)
    competitor_sentiment: dict = Field(default_factory=dict)  # competitor -> market sentiment
    summary: str = ""


class SocialListeningReport(BaseModel):
    id: str = Field(default_factory=lambda: new_id("soc"))
    subject_domain: str = ""
    queries: List[str] = Field(default_factory=list)
    platforms: List[PlatformInsight] = Field(default_factory=list)  # per-platform, one by one
    overall: SocialInsights = Field(default_factory=SocialInsights)
    created_at: datetime = Field(default_factory=utcnow)


class EventsReport(BaseModel):
    id: str = Field(default_factory=lambda: new_id("evtr"))
    subject_domain: str = ""
    events: List[Event] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class AccountSignal(BaseModel):
    """One structured signal on an account = one chat 'message'. Carries its own provenance
    (source name + original link + date) so the UI can show and FILTER by source per signal."""

    signal_type: str = ""    # 'fundraising' | 'hiring' | 'team'
    source: str = ""         # human source name, e.g. 'techcrunch.com' | 'Greenhouse' | 'LinkedIn'
    source_url: str = ""     # the original grounding link
    date: str = ""           # signal date ('' = current/ongoing, e.g. a team signal)
    text: str = ""           # the rationale / evidence in words
    role: str = ""           # the trigger role, for hiring/team signals
    relevance: str = ""      # 'High' | 'Medium' | 'Low' (per-signal fit, if known)


class Account(BaseModel):
    """A company surfaced by signals, with SIGNAL STACKING — the #1 converters stack 2-3 signal types
    (fundraising + hiring + team). stack_score = # distinct signal types present."""

    id: str = Field(default_factory=lambda: new_id("acct"))
    subject_domain: str = ""               # which customer's list this account belongs to
    company: str = ""
    company_domain: str = ""
    website: str = ""
    industry: str = ""
    location: str = ""                     # HQ / signal location (filter)
    funding_stage: str = ""                # Seed / Series A / ... parsed from a fundraising signal (filter)
    role_present: bool = False             # a trigger role is hired-for OR on-staff (filter)
    social_count: int = 0
    hiring_count: int = 0
    fundraising_count: int = 0
    team_count: int = 0
    signal_types: List[str] = Field(default_factory=list)
    stack_score: int = 0                   # distinct signal types: 1=low, 2=med, 3=high
    relevance: float = 0.0                 # cosine to the customer's context
    pref_score: float = 0.0                # learned preference (approve/reject feedback); 0 = neutral
    latest_signal_date: str = ""
    signals: List[AccountSignal] = Field(default_factory=list)  # structured feed (the chat messages)
    contacts: List["Contact"] = Field(default_factory=list)     # decision-makers to reach out to
    evidence: List[str] = Field(default_factory=list)   # signal snippets (role/round/rationale) — legacy
    sources: List[str] = Field(default_factory=list)    # grounding source URLs — legacy
    created_at: datetime = Field(default_factory=utcnow)


class CustomerListReport(BaseModel):
    id: str = Field(default_factory=lambda: new_id("clr"))
    subject_domain: str = ""
    accounts: List[Account] = Field(default_factory=list)   # stacked, ranked companies (primary output)
    leads: List[CustomerLead] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Signal-driven COMPANY leads (hiring / fundraising) — the Jesse-format record  #
# --------------------------------------------------------------------------- #


class CompanyLead(BaseModel):
    """A company surfaced by a live signal (hiring or fundraising). Company-level, grounded in a
    real scraped source URL — matches the Jesse sample (no emails; contacts are a later layer)."""

    id: str = Field(default_factory=lambda: new_id("clead"))
    subject_domain: str = ""        # the customer this list is for
    company: str = ""
    website: str = ""
    employees: str = ""             # size band, if known
    location: str = ""
    source: str = ""                # where the signal came from, e.g. "Greenhouse", "TechCrunch"
    source_url: str = ""            # the actual scraped URL (grounding — no hallucinated leads)
    signal_type: str = ""           # 'hiring' | 'fundraising'
    role: str = ""                  # hiring: the role being hired
    rationale: str = ""             # the specific signal in words
    relevance: str = ""             # 'High' | 'Medium' | 'Low'
    score: float = 0.0
    signal_date: str = ""           # date of the signal (kept ≤3 months)
    industry: str = ""              # tag for the shared corpus
    company_domain: str = ""        # for accounts roll-up / signal stacking
    embedding: List[float] = Field(default_factory=list)  # for semantic retrieval (shared corpus)
    created_at: datetime = Field(default_factory=utcnow)


class LeadsReport(BaseModel):
    id: str = Field(default_factory=lambda: new_id("leads"))
    subject_domain: str = ""
    signal_type: str = ""           # 'hiring' | 'fundraising'
    leads: List[CompanyLead] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Eval / preference learning: human approve-reject feedback on leads            #
# --------------------------------------------------------------------------- #


# reject-reason taxonomy — each maps to a canned exclusion rule the agents apply
REJECT_CATEGORIES = {
    "wrong-segment": "the company is not in our target segment",
    "agency-vendor": "the company is an agency, vendor, or consultancy selling this service (not a buyer)",
    "too-enterprise": "the company is too large / enterprise for our ICP",
    "too-early": "the company is too small / too early for our ICP",
    "wrong-geo": "the company is in a geography we do not target",
    "stale": "the signal is old / no longer active",
    "competitor": "the company is a competitor of ours",
    "duplicate": "duplicate of another company already in the list",
    "not-a-buyer": "no real buying intent — not a genuine prospect",
    "low-intent": "intent is too weak to prioritize",
    "other": "",
}


class LeadFeedback(BaseModel):
    """One human judgement on one lead — the training signal for the listing. Keyed by
    subject_domain so every customer's list learns its OWN ICP taste."""

    id: str = Field(default_factory=lambda: new_id("fb"))
    subject_domain: str = ""
    company: str = ""
    company_domain: str = ""
    signal_type: str = ""            # the signal judged, or 'account' for the whole company
    decision: str = ""              # 'approve' | 'reject'
    reason_category: str = ""       # one of REJECT_CATEGORIES (rejections)
    reason_text: str = ""           # the user's detailed analysis (free text)
    lead_text: str = ""             # snapshot of what was judged (rationale/role/company)
    embedding: List[float] = Field(default_factory=list)  # for the preference reranker
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Customer Profile (built once per URL by `company_icp`, stored + reused)       #
# --------------------------------------------------------------------------- #


class SignalContext(BaseModel):
    """Per signal-type context = the QUERY. Inferred from CI/ICP, used to scrape + retrieve."""

    signal_type: str = ""              # 'social' | 'hiring' | 'fundraising'
    context_text: str = ""             # what's relevant for this type (the embed/query text)
    search_params: dict = Field(default_factory=dict)  # queries / roles / segment terms (editable)
    params_locked: bool = False        # refresh won't overwrite if the user customized
    embedding: List[float] = Field(default_factory=list)  # filled by the embeddings step


class Profile(BaseModel):
    """The stored understanding of one company URL — built once, reused by every agent."""

    id: str = Field(default_factory=lambda: new_id("prof"))
    url: str = ""
    domain: str = ""
    relationship_id: Optional[str] = None   # reserved for signup; open for now
    profile: CompanyProfile = Field(default_factory=CompanyProfile)
    competitive: CompetitiveAnalysis = Field(default_factory=CompetitiveAnalysis)
    icp: IcpAnalysis = Field(default_factory=IcpAnalysis)
    industry: str = ""
    segments: List[str] = Field(default_factory=list)
    contexts: List[SignalContext] = Field(default_factory=list)  # social / hiring / fundraising
    source: str = "heuristic"
    created_at: datetime = Field(default_factory=utcnow)
    refreshed_at: datetime = Field(default_factory=utcnow)

    def context(self, signal_type: str) -> Optional[SignalContext]:
        return next((c for c in self.contexts if c.signal_type == signal_type), None)
