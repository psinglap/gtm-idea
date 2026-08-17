from warmgraph.agents.scrapers.bluesky import BlueskyScraper
from warmgraph.agents.scrapers.discord import DiscordScraper
from warmgraph.agents.scrapers.github import GitHubScraper
from warmgraph.agents.scrapers.hackernews import HackerNewsScraper
from warmgraph.agents.scrapers.linkedin import LinkedInScraper
from warmgraph.agents.scrapers.news import NewsScraper
from warmgraph.agents.scrapers.producthunt import ProductHuntScraper
from warmgraph.agents.scrapers.reddit import RedditScraper
from warmgraph.agents.scrapers.twitter import TwitterScraper

# Order = display order in social listening.
SCRAPER_CLASSES = [
    HackerNewsScraper, RedditScraper, BlueskyScraper, GitHubScraper, ProductHuntScraper, NewsScraper,
    LinkedInScraper, TwitterScraper, DiscordScraper,
]

__all__ = ["SCRAPER_CLASSES", "HackerNewsScraper", "RedditScraper", "BlueskyScraper", "GitHubScraper",
           "ProductHuntScraper", "NewsScraper", "LinkedInScraper", "TwitterScraper",
           "DiscordScraper"]
