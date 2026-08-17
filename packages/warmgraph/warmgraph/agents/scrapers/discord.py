"""Discord scraper — intentionally a documented no-op.

Discord has NO global/public search API: messages are only readable by a bot that has been
invited into each specific server (with Read Message History), and scraping the client violates
ToS. So broad "listen across Discord" isn't feasible like Reddit/HN/GitHub. The right model is
per-community opt-in: the user adds our bot to communities they care about, then we read those.
Until that exists this returns [] (honest) rather than faking data."""
from __future__ import annotations

from typing import List

from warmgraph.agents.scrapers.base import ScraperAgent
from warmgraph.models import Post


class DiscordScraper(ScraperAgent):
    platform = "discord"

    def search(self, queries, since_days, limit, subject_domain="") -> List[Post]:
        # No global Discord search exists; requires a bot inside each target server.
        return []
