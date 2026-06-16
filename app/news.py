"""News fetching and formatting from RSS feeds."""
from __future__ import annotations

import os
import time
import uuid
import logging

from . import pg_store
import feedparser
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_FEEDS = [
    "https://techcrunch.com/feed/",
    "https://news.ycombinator.com/rss",
    "https://vnexpress.net/rss/khoa-hoc-cong-nghe.rss"
]

def get_configured_feeds() -> list[str]:
    env_feeds = os.getenv("NEWS_RSS_FEEDS", "")
    if env_feeds:
        return [f.strip() for f in env_feeds.split(",") if f.strip()]
    return DEFAULT_FEEDS


def get_user_feeds(user_aad_id: str) -> list[dict]:
    """Return user's custom RSS feeds stored in PostgreSQL."""
    if not user_aad_id:
        return []
    return pg_store.get_rss_feeds(user_aad_id)


def get_all_feeds(user_aad_id: str = "") -> list[str]:
    """Return configured default feeds + user custom feeds (deduplicated)."""
    base = get_configured_feeds()
    if not user_aad_id:
        return base
    user_urls = [f["url"] for f in get_user_feeds(user_aad_id)]
    seen = set(base)
    extra = [u for u in user_urls if u not in seen]
    return base + extra


def save_user_feed(user_aad_id: str, url: str, label: str) -> str:
    """Add a custom RSS feed for a user. Returns feed_id (idempotent on same URL)."""
    feeds = get_user_feeds(user_aad_id)
    for f in feeds:
        if f["url"] == url:
            return f["id"]
    feed_id = uuid.uuid4().hex[:8]
    feeds.append({"id": feed_id, "url": url, "label": label, "added_at": int(time.time())})
    pg_store.set_rss_feeds(user_aad_id, feeds)
    return feed_id


def remove_user_feed(user_aad_id: str, feed_id: str) -> bool:
    """Remove a custom RSS feed by id. Returns True if found and removed."""
    feeds = get_user_feeds(user_aad_id)
    new_feeds = [f for f in feeds if f["id"] != feed_id]
    if len(new_feeds) == len(feeds):
        return False
    pg_store.set_rss_feeds(user_aad_id, new_feeds)
    return True


def fetch_rss_feeds(feed_urls: list[str] | None = None, max_per_feed: int = 8) -> list[dict]:
    """Fetch articles from RSS feeds, return list of article dicts."""
    feeds = feed_urls or get_configured_feeds()
    articles = []
    for url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_feed]:
                summary_raw = entry.get("summary", entry.get("description", ""))
                summary_text = BeautifulSoup(summary_raw, "lxml").get_text(strip=True)[:400]
                articles.append({
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "summary": summary_text,
                    "published": entry.get("published", ""),
                    "source": feed.feed.get("title", url),
                })
        except Exception as e:
            logger.warning("Failed to fetch feed %s: %s", url, e)
    return articles


def fetch_article_text(url: str, max_chars: int = 3000) -> str:
    """Fetch full article text via HTTP and extract readable content."""
    try:
        resp = httpx.get(url, timeout=10, follow_redirects=True)
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)[:max_chars]
    except Exception as e:
        return f"Error fetching article: {e}"


def format_articles_for_llm(articles: list[dict], max_articles: int = 12) -> str:
    """Format article list into a string suitable for LLM input."""
    lines = []
    for i, a in enumerate(articles[:max_articles], 1):
        lines.append(f"{i}. [{a['source']}] {a['title']}")
        if a.get("summary"):
            lines.append(f"   {a['summary'][:300]}")
        lines.append(f"   URL: {a['url']}")
        lines.append("")
    return "\n".join(lines) if lines else "No articles available."
