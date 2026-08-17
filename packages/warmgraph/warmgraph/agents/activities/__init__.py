from warmgraph.agents.activities.company_icp import CompanyIcpAgent
from warmgraph.agents.activities.competitive_intelligence import CompetitiveIntelligenceAgent
from warmgraph.agents.activities.contacts import ContactsAgent
from warmgraph.agents.activities.customer_list import CustomerListAgent
from warmgraph.agents.activities.engagement import EngagementAgent
from warmgraph.agents.activities.event_icp_judge import EventIcpJudgeAgent
from warmgraph.agents.activities.events import EventsAgent
from warmgraph.agents.activities.fundraising_leads import FundraisingLeadsAgent
from warmgraph.agents.activities.hiring_leads import HiringLeadsAgent
from warmgraph.agents.activities.icp import IcpAgent
from warmgraph.agents.activities.lead_feedback import LeadFeedbackAgent
from warmgraph.agents.activities.outreach_daily import OutreachDailyAgent
from warmgraph.agents.activities.outreach_enrich import OutreachEnrichAgent
from warmgraph.agents.activities.outreach_send import OutreachSendAgent
from warmgraph.agents.activities.social_leads import SocialLeadsAgent
from warmgraph.agents.activities.social_listening import SocialListeningAgent
from warmgraph.agents.activities.team_signal import TeamSignalAgent

# Order = product flow: company_icp (foundation) -> CI/ICP -> signals -> customer list -> contacts
# -> engagement -> feedback, then the event-outreach pipeline (judge -> enrich -> send, with
# outreach_daily chaining all three for the cron).
ACTIVITY_AGENT_CLASSES = [
    CompanyIcpAgent, CompetitiveIntelligenceAgent, IcpAgent, SocialListeningAgent,
    HiringLeadsAgent, FundraisingLeadsAgent, TeamSignalAgent, SocialLeadsAgent,
    EventsAgent, CustomerListAgent, ContactsAgent, EngagementAgent, LeadFeedbackAgent,
    EventIcpJudgeAgent, OutreachEnrichAgent, OutreachSendAgent, OutreachDailyAgent,
]

__all__ = ["ACTIVITY_AGENT_CLASSES", "CompanyIcpAgent", "CompetitiveIntelligenceAgent", "IcpAgent",
           "SocialListeningAgent", "HiringLeadsAgent", "FundraisingLeadsAgent", "TeamSignalAgent",
           "SocialLeadsAgent", "EventsAgent", "CustomerListAgent", "ContactsAgent",
           "EngagementAgent", "LeadFeedbackAgent", "EventIcpJudgeAgent", "OutreachEnrichAgent",
           "OutreachSendAgent", "OutreachDailyAgent"]
