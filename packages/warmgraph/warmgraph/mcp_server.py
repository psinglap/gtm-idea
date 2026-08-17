"""Shared MCP definition (the tools), used by both the stdio entrypoint (mcp/server.py)
and the remote HTTP runner (warmgraph/mcp_http.py).

NOTE: imports `mcp`, which requires Python >= 3.10 — so this module is only imported by
the 3.11 container / 3.12 mcp-venv, never by the 3.9 local API/tests.
"""
from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from warmgraph.service import WarmgraphService


def build_mcp(service: Optional[WarmgraphService] = None) -> FastMCP:
    svc = service or WarmgraphService()
    # Disable the local-only DNS-rebinding host check (we run behind Cloud Run's proxy and
    # protect access with our own API key). Without this, hosted requests get HTTP 421.
    mcp = FastMCP(
        "warmgraph",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool()
    def competitive_intelligence(url: str, depth: str = "quick") -> dict:
        """Competitive intelligence for a company URL → profile + competitive landscape
        (competitors tiered by size, crowdedness, moat, whitespace, pricing, positioning).
        depth: 'quick' | 'deep'. Returns a report with an id."""
        return svc.competitive_intelligence(url, depth).model_dump(mode="json")

    @mcp.tool()
    def get_competitive_intelligence(report_id: str) -> dict:
        """Fetch a previously generated CI report by its id."""
        r = svc.get_ci_report(report_id)
        return r.model_dump(mode="json") if r else {"error": "not found"}

    @mcp.tool()
    def list_agents() -> list:
        """List all available agents (actions + scrapers) with their input schemas."""
        return svc.list_agents()

    @mcp.tool()
    def run_agent(name: str, payload: dict = {}) -> dict:
        """Run any agent by name with a JSON payload. Agents include: competitive_intelligence,
        icp_winning_category, social_listening, events, customer_list, event_icp_judge,
        outreach_enrich, outreach_send, outreach_daily, and scrape_* per platform.
        Use list_agents to see input schemas."""
        try:
            return svc.run_agent(name, payload)
        except KeyError:
            return {"error": f"unknown agent: {name}"}

    # --- Event outreach: manage and iterate on the Luma -> Gmail pipeline from chat ---
    def _cid(url: str) -> str:
        from warmgraph.storage import mirror
        return mirror.client_id_for(svc.store, svc.get_or_build_profile(url).domain)

    @mcp.tool()
    def outreach_status(url: str, limit: int = 25) -> dict:
        """Event-outreach state for a company URL: queue counts by stage, the Luma events synced
        (with why any are blocked), what was sent/drafted/skipped, and which accounts are
        connected. Start here to see where the pipeline is stuck."""
        from warmgraph.outreach import ingest
        import warmgraph.connections as connections

        cid = _cid(url)
        regs = {r.event_id: r for r in svc.store.get_event_registrations(cid, limit=500)}
        events = [e for e in svc.store.get_raw_events(since_days=90, limit=500)
                  if e.source == ingest.LUMA and e.id in regs]
        return {
            "queue": svc.store.count_event_contacts(cid),
            "connections": connections.connection_status(svc.store, cid),
            "readiness": connections.readiness(svc.store, cid),
            "events": [{"title": e.title, "url": e.url,
                        "approval_status": regs[e.id].approval_status,
                        "guests": regs[e.id].guest_count,
                        "scanned": bool(regs[e.id].scanned_at),
                        "blocked_reason": regs[e.id].blocked_reason} for e in events[:limit]],
            "messages": [m.model_dump(mode="json")
                         for m in svc.store.get_outreach_messages(cid, limit=limit)],
        }

    @mcp.tool()
    def outreach_leads(url: str, verdict: str = "", limit: int = 50) -> dict:
        """The event lead list: people found at events, with the ICP verdict, the score, the
        reason the judge gave, and the LinkedIn headline it judged on. `verdict` filters to
        'target' or 'reject'. This is how you audit whether the judge is picking well."""
        cid = _cid(url)
        rows = svc.store.get_event_contacts(cid, limit=limit)
        out = []
        for c in rows:
            if verdict and c.verdict != verdict:
                continue
            out.append({"name": c.name, "headline": c.linkedin_headline,
                        "linkedin_url": c.linkedin_url, "title": c.title,
                        "company": c.company_name, "email": c.email,
                        "verdict": c.verdict, "score": c.score, "reason": c.reason,
                        "judged_by": c.judged_by, "status": c.status})
        return {"leads": out, "count": len(out)}

    @mcp.tool()
    def outreach_preview(url: str, limit: int = 10) -> dict:
        """Render the emails that WOULD go out, without touching Gmail. Safe to call any time —
        this is the way to check copy and targeting before switching on sending."""
        return svc.run_agent("outreach_send", {"url": url, "dry_run": True, "limit": limit})

    @mcp.tool()
    def outreach_get_template(url: str) -> dict:
        """The email template for this workspace: subject, body, and the fields that get
        substituted ({first_name}, {event_name}, {event_short}, {when})."""
        from warmgraph.agents.activities import outreach_send
        from warmgraph.outreach import template

        tmpl = outreach_send.load_template(svc.store, _cid(url))
        return {**tmpl.to_dict(), "fields": list(template.FIELDS)}

    @mcp.tool()
    def outreach_set_template(url: str, subject: str, body: str) -> dict:
        """Replace the outreach email. Write it exactly as you want it sent, calendar link and
        all; only {first_name}, {event_name}, {event_short} and {when} are substituted. Rejects
        unknown fields rather than letting a typo ship as literal text."""
        from warmgraph.agents.activities import outreach_send
        from warmgraph.outreach import template

        tmpl = template.MessageTemplate(subject=subject, body=body)
        unknown = template.unknown_fields(tmpl)
        if unknown:
            return {"error": f"unknown field(s): {unknown}. available: {list(template.FIELDS)}"}
        outreach_send.save_template(svc.store, _cid(url), tmpl)
        return tmpl.to_dict()

    @mcp.tool()
    def outreach_run(url: str, mode: str = "draft") -> dict:
        """Run one server pass: release dead browser leases, judge profiled attendees, find
        emails via Apollo, then deliver. mode='draft' puts messages in Gmail Drafts (safe);
        mode='send' actually sends. Does NOT do the browser work (registering, scanning,
        reading LinkedIn) — the Chrome extension owns that."""
        if mode not in ("draft", "send"):
            return {"error": "mode must be 'draft' or 'send'"}
        return svc.run_agent("outreach_daily", {"url": url, "mode": mode})

    @mcp.tool()
    def outreach_do_not_contact(url: str, values: list, reason: str = "") -> dict:
        """Exclude email addresses or whole domains from outreach, permanently. Use for
        investors, existing customers, friends, competitors."""
        from warmgraph.entities import DoNotContact, norm_email

        cid = _cid(url)
        rows = [DoNotContact(company_id=cid, value=norm_email(str(v)),
                             kind="email" if "@" in str(v) else "domain", reason=reason)
                for v in values if str(v).strip()]
        return {"added": svc.store.save_do_not_contact(rows)}

    return mcp
